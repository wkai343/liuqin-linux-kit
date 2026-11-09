#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build an inspection-only native liuqin Recovery from kernel/runtime sources.

No old installer.img, installed Android image, device access or flashing is
used. Embedded bootloader overlay compatibility still requires device tests.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_artifacts import boot_interface,bounded_initramfs
from liuqin_recovery_image import records,pack,add
from liuqin_native_recovery import MARKER,AGENT,BOARD_HELPER,UI_FILES,TOUCH_FILES


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def encoded(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()


def prepare_console_dtb(dtb,work,dtc='dtc',fdtoverlay='fdtoverlay'):
    work=Path(work);overlay=work/'native-display.dtbo'
    (work/'source.dtb').write_bytes(dtb)
    subprocess.run([str(dtc),'-@','-I','dts','-O','dtb','-o',str(overlay),
        str(PROJECT/'device/recovery/native-display-overlay.dts')],check=True)
    subprocess.run([str(fdtoverlay),'-i',str(work/'source.dtb'),
        '-o',str(work/'console.dtb'),str(overlay)],check=True)
    cmdline=subprocess.check_output(['fdtget','-t','s',str(work/'console.dtb'),'/chosen','bootargs'],text=True).strip().split()
    cmdline=[v for v in cmdline if v not in ('keep_bootcon','ignore_loglevel','initcall_debug',
        'initcall_blacklist=simplefb_driver_init','console=drm_log') and not v.startswith('msm.separate_gpu_kms=')]
    cmdline.append('consoleblank=0')
    subprocess.run(['fdtput','-t','s',str(work/'console.dtb'),'/chosen','bootargs',' '.join(cmdline)],check=True)
    return (work/'console.dtb').read_bytes()


def build(args):
    out=args.out.resolve()
    if out.exists():raise ValueError('output already exists: '+str(out))
    if not out.is_relative_to(PROJECT/'out'):raise ValueError('output must be below project/out')
    config=(args.kernel_out/'.config').read_text()
    for key in ('BLK_DEV_SD','SCSI_UFSHCD','SCSI_UFS_QCOM','USB_DWC3','USB_GADGET','USB_CONFIGFS_RNDIS','EXT4_FS','DEVTMPFS','FB_SIMPLE','FRAMEBUFFER_CONSOLE','BACKLIGHT_KTZ8866'):
        if 'CONFIG_'+key+'=y\n' not in config:raise ValueError('native Recovery needs builtin '+key)
    source_manifest=json.loads((args.runtime/'runtime-files.json').read_text())
    for name,digest in source_manifest.items():
        if sha(args.runtime/name)!=digest:raise ValueError('runtime input changed: '+name)
    out.mkdir(parents=True)
    work=out/'work';work.mkdir()
    runtime=work/'runtime';shutil.copytree(args.runtime,runtime,symlinks=True)
    (runtime/AGENT).parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(PROJECT/'device/recovery/native-status.py',runtime/AGENT)
    shutil.copyfile(PROJECT/'shared/liuqin_board_identity.py',runtime/BOARD_HELPER)
    ui=None
    if getattr(args,'ui_runtime',None):
        ui=json.loads((args.ui_runtime/'ui-manifest.json').read_text())
        if ui.get('architecture')!='aarch64':raise ValueError('UI runtime is not ARM64')
        for name,digest in ui['source_files'].items():
            if sha(PROJECT/name)!=digest:raise ValueError('UI binary was built from stale source: '+name)
        for name,target in [('liuqin-recovery-ui',UI_FILES[0]),('liuqin-ui-power',UI_FILES[2])]:
            source=args.ui_runtime/name
            if sha(source)!=ui['files'][name] or struct.unpack_from('<H',source.read_bytes(),18)[0]!=183:raise ValueError('invalid UI binary: '+name)
            (runtime/target).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,runtime/target);(runtime/target).chmod(0o755)
        shutil.copyfile(PROJECT/'device/recovery/native-ui-service.py',runtime/UI_FILES[1])
        shutil.copytree(args.ui_runtime/'licenses',runtime/'usr/share/licenses/liuqin-recovery-ui')
    touch_sources={}
    if ui:
        # This module follows the already lit panel; it never binds the real
        # panel's pinctrl or invokes its DSI/reset/power functions.
        commit=subprocess.check_output(['git','-C',str(args.kernel_source),'rev-parse','HEAD'],text=True).strip()
        if commit!='fe81794b5e1b8a3f11a165f3acbfdb8fa671dbb3':
            raise ValueError('touch handoff requires the qualified NVT no-engineering-reset kernel')
        module_build=work/'touch-build';module_build.mkdir()
        for source in sorted((PROJECT/'device/recovery/touch').glob('*')):
            if source.is_file():
                shutil.copy2(source,module_build/source.name);touch_sources[source.relative_to(PROJECT).as_posix()]=sha(source)
        subprocess.run(['make','-C',str(args.kernel_source.resolve()),'O='+str(args.kernel_out.resolve()),
            'ARCH=arm64','CROSS_COMPILE='+os.environ.get('CROSS_COMPILE','aarch64-unknown-linux-musl-'),
            'M='+str(module_build),'modules'],check=True)
        shutil.copy2(module_build/'liuqin_bootloader_panel.ko',runtime/TOUCH_FILES[0])
        shutil.copy2(PROJECT/'device/recovery/native-touch.py',runtime/TOUCH_FILES[1])
        touch_sources['device/recovery/native-touch.py']=sha(PROJECT/'device/recovery/native-touch.py')
    # The marker is added only after the final ABL-compatible DTB is hashed.
    # Its presence at boot starts the console without changing other images.
    env=dict(os.environ,RECOVERY_KERNEL_SOURCE=str(args.kernel_source.resolve()),
        RECOVERY_KERNEL_OUT=str(args.kernel_out.resolve()),RECOVERY_RUNTIME=str(runtime),OUT_DIR=str(work/'assembled'),
        FIRMWARE_POOL=str(args.board.resolve()/'firmware-pool'),WLAN_HSP2_TUPLE=str(args.board.resolve()/'wlan'),
        STOCK_BASE_DIR=str(args.board.resolve()/'dtbs'),STOCK_OVERLAY_DIR=str(args.board.resolve()/'dtbo'),
        AUDIO_TOPOLOGY=str(args.board.resolve()/'topology.bin'),
        SLOT_SUCCESS_GCC=os.environ.get('SLOT_SUCCESS_GCC','aarch64-unknown-linux-musl-gcc'))
    subprocess.run(['sh',str(PROJECT/'build/recovery/build-ram-base.sh')],env=env,check=True)
    image=work/'assembled/recovery-base.img';data=image.read_bytes()
    u=lambda offset:struct.unpack_from('<I',data,offset)[0]
    align=lambda size:(size+4095)//4096*4096
    if (u(36),u(40),u(24))!=(4096,2,0):raise ValueError('unexpected native image format')
    ks,rs,ds=u(8),u(16),u(1648);ramstart=4096+align(ks);dtstart=ramstart+align(rs)
    kernel=data[4096:4096+ks];dtb=data[dtstart:dtstart+ds]
    # Keep bootloader scanout for the maintenance console. This does not
    # change DTBO or any tablet partition.
    dtb=prepare_console_dtb(dtb,work,args.kernel_out/'scripts/dtc/dtc',args.kernel_out/'scripts/dtc/fdtoverlay')
    items=records(bounded_initramfs(data[ramstart:ramstart+rs],256*1024**2))
    kernel_sha=hashlib.sha256(gzip.decompress(kernel)).hexdigest()
    payload={name:hashlib.sha256(items[name][1]).hexdigest() for name in ('init',AGENT,BOARD_HELPER)+ (UI_FILES+TOUCH_FILES if ui else ())}
    sources={name:sha(PROJECT/name) for name in ('build/recovery/build-native.py','build/lib/build-initramfs.sh',
        'build/lib/build-bootimg.sh','device/firmware/firmware.manifest','device/boot/boot-target-v1.sh','device/recovery/native-display-overlay.dts')}
    for p in sorted((PROJECT/'device/installer').glob('*')):
        if p.is_file():sources[p.relative_to(PROJECT).as_posix()]=sha(p)
    for p in sorted((PROJECT/'shared').glob('liuqin*')):
        if p.suffix in ('.py','.json'):sources[p.relative_to(PROJECT).as_posix()]=sha(p)
    for name,digest in touch_sources.items():
        if sha(PROJECT/name)!=digest:raise ValueError('touch source changed during build: '+name)
    sources.update(touch_sources)
    identity=dict(kernel_sha256=kernel_sha,dtb_sha256=hashlib.sha256(dtb).hexdigest(),
        payload_sha256=payload,sources=sources,rootfs_archive_sha256=hashlib.sha256(data[ramstart:ramstart+rs]).hexdigest())
    if ui:identity['ui_build']=ui
    manifest=dict(schema=1,interface='liuqin-native-recovery-v1',device='liuqin',
        build_id=hashlib.sha256(encoded(identity)).hexdigest(),usage='temporary-boot-only',status='DEVICE_UNTESTED',inspection_only=True,
        board_identity_version=1,display_profile='bootloader-simplefb',
        kernel_sha256=kernel_sha,dtb_sha256=identity['dtb_sha256'],payload_sha256=payload,
        kernel_release=(args.kernel_out/'include/config/kernel.release').read_text().strip(),
        android_userspace_required=False,installed_vendor_kernel_modules_required=False,
        installed_dtbo_may_be_applied=True,usb_transport='rndis',
        limitations=['Only liuqin; not a universal image for other tablets.',
            'Bootloader, DTBO overlays and hardware firmware compatibility require physical tests.',
            'No install/format/default-boot writes or fixed Recovery installation. UI power actions require explicit confirmation.',
            'Trusted USB host only; the engineering control channel is not authenticated.'],identity=identity)
    if ui:manifest.update(ui_version=1,ui_engine='LVGL '+ui['lvgl']['version'],touch_version=1)
    add(items,MARKER,encoded(manifest))
    for name,value in [('kernel',kernel),('dtb',dtb),('ramdisk',gzip.compress(pack(items),compresslevel=9,mtime=0))]:
        (work/name).write_bytes(value)
    result=out/'recovery.img'
    subprocess.run([sys.executable,str(PROJECT/'inputs/aosp-mkbootimg/mkbootimg.py'),'--header_version','2',
        '--pagesize','4096','--base','0','--kernel_offset','0x8000','--ramdisk_offset','0x1000000','--tags_offset','0x100',
        '--dtb_offset','0x1f00000','--kernel',str(work/'kernel'),'--ramdisk',str(work/'ramdisk'),
        '--dtb',str(work/'dtb'),'--cmdline','','--output',str(result)],check=True)
    checked=boot_interface(result,installer=True)
    if checked['native_recovery_manifest']!=manifest:raise ValueError('native Recovery identity round trip failed')
    if checked['kernel_sha256']!=sha(args.kernel_out/'arch/arm64/boot/Image'):raise ValueError('kernel changed during packaging')
    manifest.update(image_sha256=sha(result),image_bytes=result.stat().st_size)
    (out/'native-recovery-manifest.json').write_bytes(encoded(manifest))
    (out/'SHA256SUMS').write_text(manifest['image_sha256']+'  recovery.img\n')
    shutil.copyfile(PROJECT/'build/recovery/NATIVE-NOTICE.md',out/'NOTICE.md')
    print(json.dumps(dict(image=str(result),bytes=manifest['image_bytes'],sha256=manifest['image_sha256'],
        status=manifest['status'],inspection_only=True),indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('kernel-source','kernel-out','runtime','board','out'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--ui-runtime',type=Path,help='Static ARM64 LVGL runtime produced by build-ui.py')
    build(p.parse_args())
