#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build an offline-qualified, firmware-bound native fixed-Recovery candidate.

No device connection, flash, slot change, reboot or vbmeta modification occurs.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_artifacts import boot_interface,bounded_initramfs
from liuqin_recovery_image import records,pack,add
from liuqin_native_recovery import MARKER
from liuqin_fixed_recovery import LIMIT,USAGE,encoded,sha,align,pack_v0,inspect


def build(args):
    out=args.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new output directory below project/out')
    for path in (args.base_image,args.key,args.avbtool,args.profile):
        if not path.is_file():raise ValueError('missing regular input file: '+str(path))
    lock=json.loads((PROJECT/'build/recovery/fixed-sources.json').read_text())
    if sha(args.avbtool.read_bytes())!=lock['avbtool']['sha256']:raise ValueError('avbtool is not the pinned reference tool')
    profile=json.loads(args.profile.read_text())
    if (profile.get('schema')!=1 or profile.get('partition_bytes')!=LIMIT or profile.get('device')!='liuqin' or
        profile.get('unlocked_required') is not True or profile.get('boot_header_version')!=0 or
        profile.get('qualification')!='OFFLINE_CANDIDATE_NOT_HARDWARE_BOOT_TESTED'):raise ValueError('unsupported fixed-Recovery profile')
    info=boot_interface(args.base_image,installer=True);m=info.get('native_recovery_manifest')
    if not m or m.get('ui_version')!=1 or m.get('touch_version')!=1:raise ValueError('base must be a verified native LVGL touch image')
    if m['kernel_sha256']!=profile['kernel_sha256'] or m['dtb_sha256']!=profile['dtb_sha256']:raise ValueError('kernel/DTB have not been qualified for this fixed profile')
    b=args.base_image.read_bytes();u=lambda n:struct.unpack_from('<I',b,n)[0]
    if u(1632)!=0:raise ValueError('base already has an embedded Recovery DTBO')
    ramstart=4096+align(u(8));dtstart=ramstart+align(u(16))
    kernel=b[4096:4096+u(8)];dtb=b[dtstart:dtstart+u(1648)]
    items=records(bounded_initramfs(b[ramstart:ramstart+u(16)],256*1024**2))
    out.mkdir(parents=True);work=out/'work';work.mkdir()
    tool=[sys.executable,str(args.avbtool.resolve())]
    public=out/'recovery.avbpubkey'
    subprocess.run([*tool,'extract_public_key','--key',str(args.key.resolve()),'--output',str(public)],check=True)
    if len(public.read_bytes())!=520:raise ValueError('fixed candidate requires an RSA2048 signing key')
    source_files={name:sha((PROJECT/name).read_bytes()) for name in ('build/recovery/build-native-fixed.py','build/recovery/fixed-sources.json','shared/liuqin_fixed_recovery.py')}
    identity=dict(source_native_image_sha256=sha(b),source_native_build_id=m['build_id'],fixed_profile=profile,
        avb_public_key_sha256=sha(public.read_bytes()),kernel_sha256=m['kernel_sha256'],dtb_sha256=m['dtb_sha256'],
        payload_sha256=m['payload_sha256'],sources=source_files)
    fixed=dict(m,usage=USAGE,fixed_version=1,fixed_boot_header=0,fixed_profile=profile,identity=identity,
        build_id=sha(encoded(identity)),status='DEVICE_UNTESTED',inspection_only=True,
        limitations=['Fixed partition format candidate; hardware fixed boot and rollback not tested.',
          'Requires an unlocked liuqin with the exact ABL, external DTBO and AVB metadata profile.',
          'No Android/Linux installation, formatting, slot change or GUI fixed-flash eligibility.',
          'No OEM signature or universal firmware compatibility is claimed.'])
    add(items,MARKER,encoded(fixed));ramdisk=gzip.compress(pack(items),compresslevel=9,mtime=0)
    raw=pack_v0(kernel,ramdisk,dtb,b[:4096])
    if len(raw)>LIMIT-128*1024:raise ValueError('fixed image leaves insufficient AVB space')
    image=out/'recovery.img';image.write_bytes(raw)
    subprocess.run([*tool,'add_hash_footer','--image',str(image),'--partition_name','recovery','--partition_size',str(LIMIT),
        '--algorithm','SHA256_RSA2048','--key',str(args.key.resolve()),'--salt',sha(raw),
        '--rollback_index',str(profile['recovery_rollback_index'])],check=True)
    verified=subprocess.run([*tool,'verify_image','--image',str(image),'--key',str(args.key.resolve())],check=True,capture_output=True,text=True)
    (out/'avb-verification.txt').write_text(verified.stdout)
    checked=inspect(image)
    if checked['manifest']!=fixed:raise ValueError('fixed image identity round trip failed')
    for name,digest in source_files.items():
        if sha((PROJECT/name).read_bytes())!=digest:raise ValueError('source changed during build')
    result=dict(checked,profile_file_sha256=sha(args.profile.read_bytes()),avbtool_sha256=lock['avbtool']['sha256'],
        native_runtime_unchanged_except_marker=True,flash_requested=False,hardware_fixed_boot_tested=False)
    (out/'fixed-recovery-manifest.json').write_bytes(encoded(result))
    (out/'SHA256SUMS').write_text(checked['image_sha256']+'  recovery.img\n')
    (out/'STATUS.txt').write_text('liuqin 原生固定 Recovery 候选版\n\n仅完成离线验证；没有实际刷写或固定启动验收。\n'
        '镜像自带 Linux 内核、设备树、LVGL 和触控维护环境；存储保持只读。\n'
        '适配固件：'+profile['firmware']+'。要求已解锁及匹配的 ABL、机内 DTBO、AVB 元数据。\n'
        '使用 v0 + Image.gz-dtb 封装，避开此 ABL 的 v1/v2 AVB 长度计算缺陷。\n'
        '不要把候选版当作已验收版本，GUI 固定安装仍关闭。本构建器不连接设备。\n'
        'SHA256：'+checked['image_sha256']+'\n')
    shutil.copy2(PROJECT/'docs/RECOVERY-FIRMWARE-PROFILES.md',out/'RESEARCH.md')
    print(json.dumps({k:result[k] for k in ('image_bytes','image_sha256','status','hardware_fixed_boot_tested')},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-image',type=Path,required=True);p.add_argument('--key',type=Path,required=True)
    p.add_argument('--profile',type=Path,default=PROJECT/'build/recovery/fixed-liuqin-miui14.json')
    p.add_argument('--avbtool',type=Path,default=PROJECT/'inputs/recovery-avb/avbtool.py')
    p.add_argument('--out',type=Path,required=True);build(p.parse_args())
