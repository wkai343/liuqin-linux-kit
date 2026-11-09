# SPDX-License-Identifier: MIT
"""Preserve/reinstall one Android while retaining managed Linux partitions."""
from __future__ import annotations
import base64
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import uuid

from .engine import InstallerError, canonical, digest_file, cli
from liuqin_ota import generic
from liuqin_rom_admission import assessment
from liuqin_android import load_profile, normalize, verify_rom, image_plan, managed_layout, next_registry


def select_profile(engine,path):
    from liuqin_android import identify_profile
    source=Path(path).expanduser() if path else None
    from liuqin_ota import generic
    from . import native_ota
    if engine.simulation:profile=engine.android_profile_for_run()
    elif source and source.is_dir() and (source/native_ota.PROFILE).is_file():
        profile=native_ota.read_profile(source,engine.check_cancel)
    elif source and source.suffix.lower()=='.zip':
        if engine.manifest.get('native_recovery_manifest',{}).get('generic_ota_version',0)<2:raise InstallerError('请临时启动新版完整 ROM 安装候选；旧 Rec 可继续只读检查。')
        engine.phase('android_plan',detail='导入完整 ROM')
        profile,directory=native_ota.import_profile(source,engine.state_dir/'rom-cache',engine.check_cancel)
    else:profile=load_profile(identify_profile(path))
    if generic(profile) and engine.manifest.get('native_recovery_manifest',{}).get('generic_ota_version',0)<profile['generic_ota_version']:raise InstallerError('当前 Rec 执行器版本不足。')
    from liuqin_rom_admission import require_install
    require_install(profile)
    engine.android_profile=profile;engine.android_profile_sha=hashlib.sha256(canonical(profile)).hexdigest()
    return profile


def import_rom(path, profile, cache, check):
    source=Path(path).expanduser()
    from liuqin_ota import generic
    if generic(profile) and not source.is_dir():
        from .native_ota import import_profile
        actual,directory=import_profile(source,cache,check)
        if canonical(actual)!=canonical(profile):raise InstallerError('OTA 导入描述发生变化。')
        return verify_rom(directory,profile,check)
    if source.is_dir():
        if generic(profile):
            from .native_ota import read_profile,verify_directory
            if canonical(read_profile(source,check))!=canonical(profile):raise InstallerError('OTA 材料描述发生变化。')
            return verify_directory(source,profile,check)
        return verify_rom(source,profile,check)
    archive=profile['archive']
    if source.is_symlink() or not source.is_file() or source.stat().st_size!=archive['bytes'] or digest_file(source,check=check)!=archive['sha256']:
        raise InstallerError('ROM 压缩包不属于固定版本 '+profile['rom'])
    if archive.get('format')=='payload-zip-v2':return import_payload_rom(source,profile,cache,check)
    cache.mkdir(parents=True,exist_ok=True)
    destination=cache/profile['rom']
    if destination.exists():return verify_rom(destination,profile,check)
    required={archive['archive_root']+'/images/'+name:name for name in profile['images']}
    if shutil.disk_usage(cache).free<sum(p['bytes'] for p in profile['images'].values())+512*1024**2:
        raise InstallerError('电脑空间不足以导入 ROM 镜像。')
    with tempfile.TemporaryDirectory(prefix='import-',dir=cache) as temporary:
        found=set();root=Path(temporary)
        with tarfile.open(source,'r|gz') as stream:
            for member in stream:
                check();name=required.get(member.name)
                if name is None:continue
                expected=profile['images'][name]
                if not member.isfile() or member.issparse() or name in found or member.size!=expected['bytes']:
                    raise InstallerError('ROM 必要镜像条目无效：'+name)
                with stream.extractfile(member) as src,(root/name).open('xb') as out:
                    while chunk:=src.read(4*1024**2):check();out.write(chunk)
                found.add(name)
        if found!=set(profile['images']):raise InstallerError('ROM 缺少必需镜像。')
        verify_rom(root,profile,check)
        # Only nine explicitly admitted images are retained; no flash scripts.
        root.rename(destination)
    return verify_rom(destination,profile,check)


def import_payload_rom(source,profile,cache,check):
    from liuqin_payload import metadata,extract
    from liuqin_super import build
    info=metadata(source);expected=profile['ota'];layout=profile['super']['layout']
    if (info['ota'].get('post-build')!=expected['fingerprint'] or info['ota'].get('post-sdk-level')!=str(expected['sdk']) or
        info['groups']!=[dict(name=layout['group'],bytes=layout['group_bytes'],partitions=[p['name'] for p in layout['partitions']])] or
        info['snapshot']!=1 or sorted(n for n,p in info['partitions'].items() if p['optional_postinstall'])!=sorted(expected['optional_postinstall'])):
        raise InstallerError('OTA 元数据或动态分区布局与适配描述不符。')
    cache.mkdir(parents=True,exist_ok=True);destination=cache/profile['rom']
    if destination.exists():return verify_rom(destination,profile,check)
    needed=sum(p['bytes'] for p in expected['partitions'].values())+layout['bytes']+512*1024**2
    if shutil.disk_usage(cache).free<needed:raise InstallerError('电脑空间不足以解包 OTA 并组装 super 镜像。')
    with tempfile.TemporaryDirectory(prefix='payload-',dir=cache) as temporary:
        root=Path(temporary)
        names=[n for n in expected['partitions'] if n not in expected['ignored_partitions']]
        extract(source,root,info,expected['partitions'],names,check)
        build(root,root/'super.img',layout,check)
        verify_rom(root,profile,check)
        for p in root.iterdir():
            if p.name not in profile['material_images']:p.unlink()
        root.rename(destination)
    return verify_rom(destination,profile,check)


def prepare(engine,payload):
    engine.guard_operation();config=normalize(payload.get('android_config'))
    if not payload.get('backup_dir'):raise InstallerError('请先选择当前多系统布局的设备备份。')
    engine.begin_run(payload,'android')
    if engine.manifest.get('installer_android_version')!=1:
        raise InstallerError('installer.img 缺少 Android 安装环境，请重新构建；未访问设备。')
    profile=select_profile(engine,engine.android_rom_path(config['rom_path']))
    engine.android_config=config;engine.android_image_paths={}
    engine.receipt.update(layout='android-manage',android_mode=config['mode'],boot_action='android-default' if config['set_default'] else 'keep-default')
    record=engine.load_backup(payload['backup_dir'],engine.receipt['serial'])
    engine.receipt['backup_dir']=record['path'];engine.save_receipt()
    sources=import_rom(engine.android_rom_path(config['rom_path']),profile,engine.state_dir/'rom-cache',engine.check_cancel) if config['rom_path'] else {}
    disk=engine.connect_ram(require_archive=False)
    if config['mode']=='reinstall' and hasattr(engine.transport,'validate_android_install'):engine.transport.validate_android_install()
    if not config['rom_path'] and not engine.simulation:
        current=engine.transport.android_rpc('partition-inspect',{})
        registered=[s for s in (current.get('registry') or {}).get('systems',[]) if s.get('type')=='android']
        if registered:
            from liuqin_rom_admission import catalog,require_install
            matches=[load_profile(r['id']) for r in catalog() if load_profile(r['id'])['rom']==registered[0].get('rom')]
            if len(matches)!=1:raise InstallerError('已登记 Android 的 ROM 适配缺失，不能按原厂 ROM 继续操作。')
            profile=matches[0];require_install(profile);engine.android_profile=profile
            engine.android_profile_sha=hashlib.sha256(canonical(profile)).hexdigest()
    if not generic(profile) and engine.device.get('anti_version',profile['anti_version']+1)>profile['anti_version']:
        raise InstallerError('设备防回滚等级高于当前 ROM，拒绝降级。')
    engine.phase('android_scan')
    scan=engine.transport.android_rpc('inspect',dict(mode=config['mode']))
    layout=managed_layout(disk,scan['inventory'],scan,engine.device['serial'])
    if config['mode']=='preserve' and (scan['userdata_blank'] or scan['pending_transactions']):
        raise InstallerError('Android 数据尚未初始化，或上次 Android 写入未完成；保留模式不会修复它，请选择重装。')
    compatibility=engine.verify_current_backup(record)
    directory=Path(engine.receipt['run_dir'])/'android';directory.mkdir(mode=0o700)
    if shutil.disk_usage(directory).free < 2*engine.device['boot_bytes']+128*1024**2:
        raise InstallerError('电脑空间不足以保存 Android 引导和本次 boot_a 快照。')
    previous_dir=directory/'previous';previous_dir.mkdir(mode=0o700)
    previous_hash=engine.transport.snapshot_boot(previous_dir)
    engine.android_previous_boot=previous_dir/'boot_a.img'
    boot=directory/'android-boot.img'
    if sources:
        shutil.copyfile(sources['boot.img'],boot)
    elif previous_hash==profile['images']['boot.img']['sha256']:
        shutil.copyfile(engine.android_previous_boot,boot)
    elif layout['android']:
        result=engine.transport.android_rpc('export-boot',dict(scan=scan))
        data=base64.b64decode(result['boot_gzip_base64'],validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            content=stream.read(profile['images']['boot.img']['bytes']+1)
        boot.write_bytes(content)
    else:
        raise InstallerError('尚未保存 Android boot.img；请选择固定 ROM 目录或完整压缩包以登记引导。')
    if boot.stat().st_size!=profile['images']['boot.img']['bytes'] or digest_file(boot)!=profile['images']['boot.img']['sha256']:
        raise InstallerError('Android 引导不属于选定 ROM。')
    engine.android_boot=boot
    existing = layout['android']
    registration_required = not (config['mode']=='preserve' and not config['set_default'] and existing and
        existing.get('rom')==profile['rom'] and existing.get('boot_sha256')==profile['images']['boot.img']['sha256'] and
        existing.get('userdata')==layout['userdata'] and existing.get('metadata')==layout['metadata'])
    store_boot_bytes = 2 * engine.device['boot_bytes']
    if registration_required and engine.manifest.get('native_recovery_manifest', {}).get('android_sparse_store_version') == 1:
        from liuqin_android import sparse_boot_file_bytes
        store_boot_bytes = sparse_boot_file_bytes(boot) + sparse_boot_file_bytes(engine.android_previous_boot)
    if registration_required and scan['free_bytes'] < store_boot_bytes+16*1024**2:
        raise InstallerError('引导存储空间不足以追加 Android 镜像和回退快照；不会删除 Linux 文件。')
    generation=str(uuid.uuid4());android_id=layout['android']['id'] if layout['android'] else str(uuid.uuid5(uuid.UUID(scan['registry']['layout_generation']),'android'))
    images=[]
    if config['mode']=='reinstall':
        for name in profile['install_images']:
            engine.phase('android_plan',detail=name)
            record_image=profile['images'][name];part=scan['inventory'][record_image['partition']]
            images.append(image_plan(sources[name],part,record_image,engine.check_cancel))
            engine.android_image_paths[part['name']]=sources[name]
    android=dict(id=android_id,name='Android '+profile['rom'],type='android',rom=profile['rom'],slot='a',
                 generation=generation,status='first-boot-pending' if config['mode']=='reinstall' else 'preserved-unverified',
                 userdata=layout['userdata'],metadata=layout['metadata'],boot_sha256=profile['images']['boot.img']['sha256'],
                 image='systems/'+android_id+'/images/'+generation+'.img')
    registry=next_registry(scan['registry'],android,generation,config['mode'],config['set_default'])
    registry['resolved_android_transactions']=sorted(set(registry['resolved_android_transactions'])|set(scan['pending_transactions']))
    if not registration_required: registry = copy.deepcopy(scan['registry'])
    plan=dict(schema_version=1,mode=config['mode'],set_default=config['set_default'],registration_required=registration_required,transaction=generation,
              android_id=android_id,inventory=scan['inventory'],protected=layout['protected'],
              registry=scan['registry'],registry_sha256=scan['registry_sha256'],store_files=scan['files'],
              previous_boot_sha256=previous_hash,images=images,candidate_registry=copy.deepcopy(registry),candidate_registry_sha256=hashlib.sha256(canonical(registry)).hexdigest())
    snapshot=dict(plan=copy.deepcopy(plan),sources=dict(sources),configuration=config,
                  registry=registry,manifest=engine.manifest_digest,report=engine.report_text,
                  boot_id=engine.transport.boot_id,backup_id=record['backup_id'],
                  profile_sha256=engine.android_profile_sha,current_persist_sha256=compatibility['current_persist_sha256'])
    engine.android_plan=plan;engine.android_registry=registry;engine.android_sources=sources
    engine.plan_snapshot=snapshot;engine.plan_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(plan_id=engine.plan_id,backup_id=record['backup_id'],backup_verified=True,
                          android_plan=plan,protected_partitions=layout['protected'],rom=profile['rom'])
    engine.save_receipt();engine.check_cancel();engine.set_state('prepared')
    writes=([p['partition'] for p in images]+[layout['userdata'],layout['metadata']]) if config['mode']=='reinstall' else []
    if config['set_default']: writes = writes + [dict(scan['inventory']['boot_a'], action='boot')]
    engine.emit('plan_ready',plan=dict(plan_id=engine.plan_id,serial=engine.device['serial'],layout='android-manage',
        android_mode=config['mode'],rom=profile['rom'],set_default=config['set_default'],registration_required=registration_required,protected=layout['protected'],
        writes=writes,protected_names={**{s['partition']['name']:s['name'] for s in scan['registry']['systems'] if s['type']=='linux'}, 'linux_boot':'引导存储'},
        changes_partition_boundaries=False,erases_personal_data=config['mode']=='reinstall',
        backup_verified=True,backup_dir=record['path'],run_dir=engine.receipt['run_dir'],experimental=True,
        warnings=compatibility['warnings']+['Linux rootfs 保持只读；引导存储仅追加 Android 版本与事务、原子更新系统清单。' if registration_required else 'Android 已登记，本次只读核验，不新增引导文件或改动系统清单。',
            '重装只清空 Android userdata 和 metadata；设备备份不包含这些个人数据。' if config['mode']=='reinstall'
            else '保留模式不改写 Android ROM、userdata 或 metadata；不解密现有数据。',
            '首次 Android 启动会初始化加密，实际启动仍需真机验收。']))


def install(engine,payload):
    consent=payload.get('confirmation',{});plan=engine.android_plan
    required=('allow_experimental','preserve_linux')
    if plan['mode']=='reinstall':required+=('personal_data_backed_up','erase_android_data')
    else:required+=('keep_android',)
    if (payload.get('session_id')!=engine.session_id or payload.get('plan_id')!=engine.plan_id or
        consent.get('serial')!=engine.device['serial'] or any(consent.get(key)is not True for key in required)):
        raise InstallerError('请确认当前设备、Android 操作范围，以及保留全部 Linux rootfs 和引导镜像。')
    snapshot=engine.plan_snapshot
    if (hashlib.sha256(canonical(snapshot)).hexdigest()!=engine.plan_id or plan!=snapshot['plan'] or
        engine.android_config!=snapshot['configuration'] or engine.android_registry!=snapshot['registry'] or
        engine.android_sources!=snapshot['sources'] or engine.android_profile_sha!=snapshot['profile_sha256'] or
        hashlib.sha256(canonical(engine.android_profile)).hexdigest()!=snapshot['profile_sha256']):
        raise InstallerError('确认后 Android 方案发生变化。')
    engine.committing=True;engine.set_state('installing');engine.phase('revalidate')
    engine.validate_installer(str(engine.bundle))
    if engine.manifest_digest!=snapshot['manifest'] or engine.transport.boot_id!=snapshot['boot_id'] or engine.transport.report()!=snapshot['report']:
        raise InstallerError('确认后安装环境或 GPT 发生变化。')
    record=engine.load_backup(engine.receipt['backup_dir'],engine.device['serial'])
    compatibility=engine.verify_current_backup(record)
    if record['backup_id']!=snapshot['backup_id'] or compatibility['current_persist_sha256']!=snapshot['current_persist_sha256']:
        raise InstallerError('设备备份或校准发生变化。')
    for name,path in engine.android_sources.items():
        if digest_file(Path(path))!=engine.android_profile['images'][name]['sha256']:
            raise InstallerError('确认后 ROM 文件发生变化：'+name)
    if digest_file(engine.android_previous_boot)!=plan['previous_boot_sha256'] or digest_file(engine.android_boot)!=engine.android_profile['images']['boot.img']['sha256']:
        raise InstallerError('确认后的引导材料发生变化。')
    engine.transport.preflight(require_archive=False)
    engine.transport.android_rpc('guard',dict(plan=plan))
    if not plan['registration_required']:
        engine.transport.cancel_cleanup()
        engine.receipt.update(android_registered=True,linux_preserved=True,installation_commands_completed=True,read_only_preservation=True)
        engine.save_receipt();engine.set_state('android_complete')
        engine.emit('installation_written',receipt=engine.receipt,first_boot_verified=False)
        engine.committing=False;engine.clean_scratch();engine.release_device()
        return
    engine.receipt.update(write_dispatched=True,confirmation=dict(consent));engine.save_receipt()
    engine.phase('store_boots');engine.transport.android_rpc('stage',dict(plan=plan))
    engine.receipt['android_staged']=True;engine.save_receipt()
    if plan['mode']=='reinstall':
        engine.phase('android_write')
        result=engine.transport.android_rpc('install',dict(plan=plan,erase_android_data=True))
        if result.get('rom_written')is not True or result.get('android_data_initialized')is not True:
            raise InstallerError('设备未确认 Android ROM 和数据初始化完成。')
        engine.receipt.update(android_rom_written=True,android_data_initialized=True,
            **{k:v for k,v in result.items() if k.startswith('recovery_')});engine.save_receipt()
    if plan['set_default']:
        engine.transport.android_default(engine.android_registry)
    else:
        result=engine.transport.android_rpc('commit',dict(plan=plan,registry=engine.android_registry,expected_boot=plan['previous_boot_sha256']))
        if result.get('linux_preserved')is not True:raise InstallerError('Linux 保护检查未完成。')
        engine.receipt['android_registered']=True
        engine.transport.cancel_cleanup()
    engine.receipt.update(linux_preserved=True,installation_commands_completed=True);engine.save_receipt()
    engine.set_state('awaiting_first_boot' if plan['set_default'] else 'android_complete')
    engine.emit('installation_written',receipt=engine.receipt,first_boot_verified=False)
    engine.committing=False;engine.clean_scratch();engine.release_device()
