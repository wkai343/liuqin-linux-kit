# SPDX-License-Identifier: MIT
"""Native Recovery selection from measured firmware, independent of OS labels."""
import hashlib
import json
import os
import re
from pathlib import Path
import struct
import uuid
from liuqin_firmware import load_catalog,match_slot,validate_snapshot
from .engine import InstallerError

PROJECT=Path(__file__).resolve().parents[2]

def catalog():
    try:return load_catalog()
    except (ValueError,OSError) as error:raise InstallerError('原生 Recovery 适配表无效：'+str(error)) from error

def image_paths(selected='',fixed=False):
    relative='fixed-recoveries' if fixed else 'native-recoveries'
    paths=[] if fixed else ([Path(selected)] if selected else [])
    for parent in (PROJECT/'installer/runtime',PROJECT/'runtime',PROJECT/'out/releases'):
        if not fixed:paths.append(parent/'native-recovery.img')
        if not fixed:paths.extend(sorted((parent/'fixed-installers').glob('*/recovery.img'))[:16])
        directory=parent/relative
        if directory.is_dir():paths.extend(sorted(directory.glob('*/recovery.img'))[:32])
        if not fixed:
            directory=parent/'native-installers'
            if directory.is_dir():paths.extend(sorted(directory.glob('*/recovery.img'))[:8])
    if fixed:paths.append(PROJECT/'out/releases/native-recovery-fixed-candidate/recovery.img')
    if not fixed:paths.append(PROJECT/'out/releases/native-recovery-installer-candidate/recovery.img')
    if not fixed:paths.append(PROJECT/'out/releases/native-recovery-adb/recovery.img')
    return list(dict.fromkeys(str(p.resolve()) for p in paths if p.is_file() and not p.is_symlink()))[:40]

def native_image(path):
    from liuqin_artifacts import boot_interface
    p=Path(path)
    with p.open('rb') as f:header=f.read(44)
    if len(header)!=44 or header[:8]!=b'ANDROID!' or struct.unpack_from('<I',header,40)[0]!=2:return None
    info=boot_interface(p,installer=True);m=info.get('native_recovery_manifest')
    if not m:return None
    return dict(path=str(p.resolve()),image_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),build_id=m['build_id'],manifest=m)

def available(selected=''):
    records=[]
    for path in image_paths(selected):
        try:r=native_image(path)
        except (ValueError,RuntimeError,OSError):continue
        if r and r['manifest'].get('usb_transport')=='adb':records.append(r)
    return records

def installer_status(snapshot,manifest):
    from liuqin_native_install import capable
    from liuqin_rom_analysis import profiles as roms
    from liuqin_universal_recovery import load_catalog as universal_catalog
    validate_snapshot(snapshot);slot=snapshot['slots']['a'];match=slot.get('match',{})
    result=dict(native_installer_candidate=True,install_allowed=False,inspection_only=True,automatic_boot_allowed=False,auto_selected=False,
        build_id=manifest.get('build_id'),reason='安装候选仅通过离线验证；当前固件或布局未匹配，保持只读。')
    if not capable(manifest) or snapshot['active_slot']!='a' or slot['status']!='complete':return result
    bases=universal_catalog()['policy']['baselines']
    known_abl=any(match.get('abl',{}).get('sha256')==b['abl_partition_sha256'] and match['abl']['partition_bytes']==b['abl_partition_bytes'] for b in bases)
    known_rom=any(slot.get('avb',{}).get('normalized_flags_sha256')==p['projected']['vbmeta']['avb']['normalized_flags_sha256'] and
        slot.get('avb_system',{}).get('normalized_flags_sha256')==p['projected']['vbmeta_system']['avb']['normalized_flags_sha256'] and
        match.get('dtbo')==p['projected']['dtbo']['measurement'] for p in roms() if p.get('current_firmware_admitted',True))
    known_dtbo=any(match.get('dtbo')==p['projected']['dtbo']['measurement'] for p in roms() if p.get('current_firmware_admitted',True))
    if known_abl and (known_rom or (manifest.get('generic_ota_version',0)>=1 and known_dtbo)) and match.get('layout')==dict(boot=201326592,vendor_boot=100663296,recovery=104857600):
        result.update(install_allowed=True,inspection_only=False,reason='安装候选：当前固件条件匹配。默认存储只读；备份并确认完整计划后才允许受控写入。安装写入尚未真机验收，不会自动重启。')
    elif manifest.get('generic_ota_version',0)>=2 and match.get('layout')==dict(boot=201326592,vendor_boot=100663296,recovery=104857600):
        result.update(install_allowed=True,inspection_only=False,reason='原生 Rec 已运行，按实际分区检查和完整 ROM 清单准备安装。固件版本不同会列入更新计划。')
    elif manifest.get('generic_ota_version',0)>=1 and match.get('layout')==dict(boot=201326592,vendor_boot=100663296,recovery=104857600):
        result.update(install_allowed=True,inspection_only=False,ota_only=True,reason='当前固件未收录。仅开放通用 OTA 的准备检查；必须逐项证明固件与 DTBO 无需改变，禁止 GPT 编辑和 Linux 写入。')
    if result['install_allowed'] and manifest.get('fixed_installer_version')==1:
        result['reason']='固定版安装环境已连接。备份并确认方案后可管理分区、安装 Android/Linux 和切换默认系统。'
    return result

def find_running(probe,selected):
    matches=[r for r in available(selected) if r['build_id']==probe.get('build_id') and r['manifest']['kernel_release']==probe.get('kernel')]
    if not matches:
        # The bounded candidate list may omit an older image still in RAM.
        # Use its sidecar as an index, then validate the actual image again.
        archive = PROJECT/'out/releases/native-installers'
        for path in archive.glob('*/native-recovery-manifest.json'):
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
                    continue
                manifest = json.loads(path.read_text())
                if not isinstance(manifest, dict) or manifest.get('build_id') != probe.get('build_id'):
                    continue
                image = path.with_name('recovery.img')
                if image.is_symlink() or not image.is_file():
                    continue
                record = native_image(image)
                if (record and record['build_id'] == probe.get('build_id') and
                        record['image_sha256'] == manifest.get('image_sha256') and
                        record['manifest'].get('usb_transport') == 'adb' and
                        record['manifest']['kernel_release'] == probe.get('kernel')):
                    matches.append(record)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                continue
    if not matches:raise InstallerError('没有与当前原生 Recovery 对应的可验证本地镜像；保持当前环境。')
    if len({r['image_sha256'] for r in matches})!=1:raise InstallerError('当前 Recovery 构建标识对应多个不同镜像，停止选择。')
    return next((r['path'] for r in matches if r['path']==str(Path(selected).resolve())),matches[0]['path'])

def universal_status(snapshot):
    from liuqin_universal_recovery import CATALOG,load_catalog,assess
    if not CATALOG.is_file():return None
    try:return assess(snapshot,load_catalog())
    except (OSError,ValueError,TypeError,KeyError) as error:
        return dict(status='invalid-policy',requirements_satisfied=False,automatic_boot_allowed=False,fixed_install_allowed=False,
            reason='v2 通用候选适配资料无效；保持现有只读维护流程。',error=str(error)[:300])

def selection(snapshot,selected,records=None,profiles=None):
    validate_snapshot(snapshot)
    profiles=catalog() if profiles is None else profiles
    results={s:match_slot(snapshot,s,profiles) for s in ('a','b')}
    active=snapshot['active_slot'];matched=results[active];profile=matched['profile']
    slots={s:dict(status=r['status'],fingerprint_id=snapshot['slots'][s].get('fingerprint_id',''),profile_id=r['profile']['id'] if r['profile'] else '',
        label=r['profile']['label'] if r['profile'] else '',mismatches=r['mismatches'],temporary_boot_verified=bool(r['profile'] and s in r['profile']['artifacts']['temporary']['slots'])) for s,r in results.items()}
    status=dict(ready=False,native=True,inspection_only=True,profile_id='',automatic_boot_allowed=False,auto_selected=False,
        fixed_install_allowed=False,firmware=dict(status=matched['status'],active_slot=active,slots=slots),fixed_candidate=None,universal_candidate=universal_status(snapshot))
    records=available(selected) if records is None else records
    from liuqin_native_install import capable
    candidate=next((r for r in records if r['path']==str(Path(selected).resolve()) and capable(r.get('manifest',{}))),None)
    if candidate:
        status.update(installer_status(snapshot,candidate['manifest']),image_sha256=candidate['image_sha256'],ready=True)
        if candidate['manifest'].get('fixed_installer_version')==1:
            status.update(fixed_installer=True,universal_candidate=None)
        if candidate['build_id']!=snapshot['runtime_build_id']:
            status.update(ready=False,install_allowed=False,reason='所选安装候选尚未运行。请手动回到 Bootloader Fastboot，再点击读取分区临时启动；当前 Rec 保持不变。')
            if candidate['manifest'].get('fixed_installer_version')==1:
                status['reason']='请在平板进入与所选镜像一致的固定 Recovery，再读取分区。'
        return candidate['path'],status
    adb=next((r for r in records if r['path']==str(Path(selected).resolve()) and r.get('manifest',{}).get('native_adb_version')==1),None)
    if adb:
        ready=adb['build_id']==snapshot['runtime_build_id']
        status.update(native_adb_candidate=True,ready=ready,auto_selected=False,automatic_boot_allowed=False,install_allowed=False,inspection_only=True,
            build_id=adb['build_id'],image_sha256=adb['image_sha256'],reason='当前是 ADB 只读候选，可读取分区和分析 ROM；没有安装写入权限。' if ready else '所选 ADB Rec 尚未运行，请手动回到 Fastboot 后明确临时启动。')
        return adb['path'],status
    if profile is None:
        reasons={'unknown':'固件指纹尚未收录','mismatch':'已识别 ABL，但其他固件或启动分区布局不匹配','incomplete':'固件指纹不完整'}
        status['reason']=reasons[matched['status']]+'；不会自动选择其他镜像或开放固定安装。'
        return selected,status
    status['profile_id']=profile['id'];target=profile['artifacts']['temporary']
    fixed=profile['artifacts'].get('fixed_candidate')
    if fixed:status['fixed_candidate']=dict(fixed,status='offline-only',install_allowed=False,reason='v0 固定候选仅通过离线验证，尚未开放安装。')
    if active not in target['slots']:
        status['reason']='固件已匹配，但当前槽位尚无临时启动验收记录；保留当前环境。';return selected,status
    matches=[r for r in records if r['image_sha256']==target['image_sha256'] and r['build_id']==target['build_id']]
    if not matches:
        status['reason']='固件已匹配，但缺少对应的已验收临时 Recovery 镜像，或文件校验不符。';return selected,status
    path=next((r['path'] for r in matches if r['path']==str(Path(selected).resolve())),matches[0]['path'])
    status.update(ready=True,automatic_boot_allowed=True,auto_selected=True,
        image_sha256=target['image_sha256'],build_id=target['build_id'],
        reason='固件指纹匹配 '+profile['label']+'，已自动选择通过临时启动验收的 Recovery；仍只开放读取。')
    return path,status

def save_snapshot(engine,snapshot):
    directory=engine.state_dir/'firmware-fingerprints'
    if directory.is_symlink() or (hasattr(directory,'is_junction') and directory.is_junction()):raise InstallerError('固件记录目录不能是链接。')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    safe_serial=re.sub(r'[^A-Za-z0-9_.-]','_',snapshot['serial'])[:64]
    path=directory/(safe_serial+'-'+uuid.uuid4().hex+'.json')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(snapshot,f,ensure_ascii=False,indent=2);f.write('\n')
    return str(path.resolve())

def inspect_running(engine,environment,selected):
    from .firmware import verified_reader,collect
    probe=environment['properties'];running=find_running(probe,selected)
    reader=verified_reader(engine,running,probe)
    snapshot=collect(reader,probe,reader.probe_current)
    from liuqin_native_install import capable
    chosen=native_image(selected) if selected else None
    running_manifest=native_image(running)['manifest']
    from liuqin_native_install import FIXED_USAGE
    if capable(running_manifest) and (running_manifest.get('usage')==FIXED_USAGE or not (chosen and capable(chosen['manifest']))):selected=running
    path,status=selection(snapshot,selected)
    status['firmware_record_path']=save_snapshot(engine,snapshot)
    environment=dict(environment,firmware=snapshot)
    if status.get('native_installer_candidate') or status.get('native_adb_candidate'):
        return environment,path,status
    if status['auto_selected']:
        if status['build_id']!=probe['build_id']:
            status.update(ready=False,reason='已按固件选择匹配镜像，但与当前运行版本不同。保持当前 Recovery；需要另行临时启动所选版本。')
    else:
        # Unknown firmware does not make an already verified RAM session
        # unsafe to inspect. It never qualifies a boot or flash candidate.
        path=running;status.update(ready=True,profile_id=status['profile_id'] or 'running-native-unqualified')
        status['reason']+=' 当前已验证的 Recovery 可继续只读检查。'
    return environment,path,status

def assess_connected_transport(transport,probe):
    from .firmware import collect
    snapshot=collect(transport,probe,transport.probe_current)
    engine=transport.engine;path=str(engine.runtime_path or engine.installer_snapshot)
    _,status=selection(snapshot,path)
    status['firmware_record_path']=save_snapshot(engine,snapshot)
    if not status['auto_selected'] and not status.get('native_installer_candidate') and not status.get('native_adb_candidate'):
        status['ready']=True;status['reason']+=' 当前会话保持只读检查。'
    engine.receipt['firmware_fingerprint']=snapshot
    engine.receipt['native_recovery_selection']=status;engine.save_receipt()
    label='固定 Recovery（安装环境）' if probe.get('fixed_installer_version')==1 else '原生 Recovery（安装候选）' if probe.get('installer_version')==1 else '原生 Recovery（只读维护）'
    engine.emit('firmware',firmware=snapshot,compatibility=status,runtime_path=path,environment=dict(serial=probe['serial'],kind='native-recovery',label=label,transport='adb-native',properties=probe,firmware=snapshot))
    return snapshot
