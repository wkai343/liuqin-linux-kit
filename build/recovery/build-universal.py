#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build one v2 native Recovery candidate with an embedded DTBO policy.

Only host files are written. No device access or root-vbmeta changes occur.
"""
import argparse,gzip,json,shutil,struct,subprocess,sys
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_artifacts import boot_interface,bounded_initramfs
from liuqin_recovery_image import records,pack,add
from liuqin_native_recovery import MARKER
from liuqin_native_install import FIXED_USAGE, capable
from liuqin_fixed_recovery import LIMIT,encoded,sha,align
from liuqin_universal_recovery import USAGE,validate_policy,validate_dtbo,pack_v2,inspect

def build(args):
    out=args.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new output directory below project/out')
    for path in (args.base_image,args.dtbo,args.policy,args.key,args.avbtool):
        if not path.is_file():raise ValueError('missing regular input: '+str(path))
    lock=json.loads((PROJECT/'build/recovery/fixed-sources.json').read_text())
    if sha(args.avbtool.read_bytes())!=lock['avbtool']['sha256']:raise ValueError('unqualified avbtool source')
    policy=validate_policy(json.loads(args.policy.read_text(encoding='utf-8')))
    source=boot_interface(args.base_image,installer=True);base=source.get('native_recovery_manifest')
    if not base or base.get('ui_version')!=1 or base.get('touch_version')!=1:raise ValueError('native LVGL touch base required')
    installer=getattr(args,'installer',False)
    if installer:
        if not capable(base) or base.get('native_adb_version')!=1:raise ValueError('fixed installer requires an ADB installer base')
    elif base.get('inspection_only') is not True:raise ValueError('universal candidate requires a read-only base')
    if policy['inspection_only'] is installer:raise ValueError('policy does not match the requested installer role')
    if any(base[k]!=policy[k] for k in ('kernel_sha256','dtb_sha256')):raise ValueError('base kernel/DTB is not supported by this policy')
    b=args.base_image.read_bytes();u=lambda n:struct.unpack_from('<I',b,n)[0]
    r=4096+align(u(8));d=r+align(u(16));kernel=b[4096:4096+u(8)];dtb=b[d:d+u(1648)]
    table=args.dtbo.read_bytes()
    if len(table)<32 or len(table)>64*1024**2:raise ValueError('DTBO input size exceeds bounds')
    size=struct.unpack_from('>I',table,4)[0];table=table[:size];validate_dtbo(table,policy)
    items=records(bounded_initramfs(b[r:r+u(16)],256*1024**2))
    source_names=['build/recovery/build-universal.py','build/recovery/fixed-sources.json','shared/liuqin_universal_recovery.py','shared/liuqin_fixed_recovery.py']
    sources={name:sha((PROJECT/name).read_bytes()) for name in source_names};policy_digest=sha(args.policy.read_bytes())
    out.mkdir(parents=True);tool=[sys.executable,str(args.avbtool.resolve())];public=out/'recovery.avbpubkey'
    subprocess.run([*tool,'extract_public_key','--key',str(args.key.resolve()),'--output',str(public)],check=True)
    if len(public.read_bytes())!=520:raise ValueError('an RSA2048 signing key is required')
    identity=dict(source_native_image_sha256=sha(b),source_native_build_id=base['build_id'],universal_policy=policy,
        avb_public_key_sha256=sha(public.read_bytes()),kernel_sha256=base['kernel_sha256'],dtb_sha256=base['dtb_sha256'],
        embedded_dtbo_sha256=sha(table),payload_sha256=base['payload_sha256'],sources=sources)
    if base.get('native_adb_version')==1:
        identity.update(inspection_only=not installer,installer_capabilities=base.get('installer_capabilities',[]))
        if 'recovery_fast_start_version' in base:
            identity['recovery_fast_start_version'] = base['recovery_fast_start_version']
    if installer:
        identity.update({name:base[name] for name in ('generic_ota_version','combined_install_version','boot_switch_version','android_sparse_store_version','sparse_boot_store_version','fixed_recovery_restore_version','tablet_boot_switch_version','bcb_cleanup_version','boot_store_version','boot_store_retention_version') if name in base})
        identity['fixed_installer_version']=1
    m=dict(base,usage=FIXED_USAGE if installer else USAGE,universal_version=1,fixed_boot_header=2,universal_policy=policy,identity=identity,
        build_id=sha(encoded(identity)),status='DEVICE_UNTESTED',inspection_only=not installer,fixed_path_external_dtbo_required=False,
        limitations=['Only liuqin; known ABL baselines and ROM metadata policies are required.',
          'Old MIUI ABL requires root VERIFICATION_DISABLED; the builder never changes root vbmeta.',
          'MikuUI does not supply ABL; actual installed baseline must be identified.',
          'No automatic boot/flash eligibility; physical fixed boot and rollback remain untested.',
          'Storage starts sealed; installer writes require an explicit scoped session.'])
    if installer:m['fixed_installer_version']=1
    m.pop('fixed_recovery_witness', None)
    add(items,MARKER,encoded(m));ram=gzip.compress(pack(items),compresslevel=9,mtime=0);raw=pack_v2(kernel,ram,dtb,table,b[:4096])
    if len(raw)>LIMIT-128*1024:raise ValueError('insufficient partition space for AVB metadata')
    image=out/'recovery.img';image.write_bytes(raw)
    subprocess.run([*tool,'add_hash_footer','--image',str(image),'--partition_name','recovery','--partition_size',str(LIMIT),
        '--algorithm','SHA256_RSA2048','--key',str(args.key.resolve()),'--salt',sha(raw),'--rollback_index',str(policy['recovery_rollback_index'])],check=True)
    log=subprocess.run([*tool,'verify_image','--image',str(image),'--key',str(args.key.resolve())],capture_output=True,text=True,check=True)
    (out/'avb-verification.txt').write_text(log.stdout,encoding='utf-8')
    checked=inspect(image)
    if checked['manifest']!=m:raise ValueError('universal identity round trip failed')
    if any(sha((PROJECT/name).read_bytes())!=digest for name,digest in sources.items()) or sha(args.policy.read_bytes())!=policy_digest:raise ValueError('build inputs changed during packaging')
    manifest=dict(checked,source_policy_sha256=policy_digest,root_vbmeta_modified=False,device_access=False,hardware_boot_tested=False)
    (out/'universal-recovery-manifest.json').write_bytes(encoded(manifest));(out/'SHA256SUMS').write_text(checked['image_sha256']+'  recovery.img\n')
    if installer:
        (out/'native-recovery-manifest.json').write_bytes(encoded(dict(m,image_sha256=checked['image_sha256'],image_bytes=checked['image_bytes'],hardware_boot_tested=False)))
    (out/'policy.json').write_bytes(encoded(policy))
    (out/'STATUS.txt').write_text('liuqin 原生通用 Recovery v2 候选\n\n一份镜像，内置 Linux 内核、DTB、DTBO、LVGL 及触控维护工具。\n'
      'MIUI 旧 ABL：要求根 vbmeta 的镜像验证关闭位已设置；不会自动更改。\n'
      'HyperOS 2.0.6 ABL：可保留验证，但仍需匹配的校验元数据。\n'
      'MikuUI：依据实际 ABL 底座应用规则，不能按 ROM 名称推定 Bootloader。\n'
      '本构建没有连接设备或更改 AVB。硬件固定启动、回退均未验收；禁止自动启动或刷写。\n'
      'SHA256：'+checked['image_sha256']+'\n',encoding='utf-8')
    if installer:
        (out/'STATUS.txt').write_text('liuqin 固定安装版 Recovery\nADB 连接，启动后存储只读。\n支持电脑 GUI 的分区、Android/Linux 安装和默认引导切换，写入需备份和方案确认。\n从 Recovery 分区启动；固定刷入和冷启动需要逐镜像验收。\nSHA256 '+checked['image_sha256']+'\n',encoding='utf-8')
        shutil.copy2(PROJECT/'build/recovery/NATIVE-NOTICE.md',out/'NOTICE.md')
    print(json.dumps(dict(image=str(image),sha256=checked['image_sha256'],bytes=checked['image_bytes'],payload_bytes=checked['avb']['original_bytes'],build_id=m['build_id'],root_vbmeta_modified=False),indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-image',type=Path,required=True);p.add_argument('--dtbo',type=Path,required=True);p.add_argument('--key',type=Path,required=True)
    p.add_argument('--policy',type=Path,default=PROJECT/'build/recovery/universal/liuqin-v2-policy.json');p.add_argument('--avbtool',type=Path,default=PROJECT/'inputs/recovery-avb/avbtool.py');p.add_argument('--out',type=Path,required=True)
    p.add_argument('--installer',action='store_true',help='Use a scoped ADB installer runtime in the fixed image')
    build(p.parse_args())
