#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Create a scoped installer candidate with a verified native kernel bundle."""
import argparse,gzip,hashlib,json,re,shutil,struct,subprocess,sys,tempfile
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_artifacts import boot_interface,bounded_initramfs
from liuqin_recovery_image import records,pack,add
from liuqin_native_recovery import MARKER,AGENT,BOARD_HELPER,UI_FILES,TOUCH_FILES,INSTALL_FILES,ADB_FILES,FIXED_RESTORE_FILES,TABLET_SWITCH_FILES,STORAGE_SEAL,BCB_FILES,STORE_FILES,RETENTION_FILES
from liuqin_native_install import CAPABILITIES,FIXED_USAGE
BASE_SHA='dcc287f5a8273260613fdcf26447e35e28fd40cec36358615d32c0e9c7abe569'
def sha(b):return hashlib.sha256(b).hexdigest()
def encoded(v):return (json.dumps(v,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
def native_init():
    source=(PROJECT/'device/boot/initramfs/init').read_bytes()
    marker=b'gadget=/sys/kernel/config/usb_gadget/liuqin\n'
    if source.count(marker)!=1:raise ValueError('shared init boundary changed')
    prefix=source.split(marker)[0]
    if b'native-adb-start.py' not in prefix:raise ValueError('ADB startup missing')
    # Even a damaged marker must not return PID 1 or start another system.
    return prefix+b"log 'native ADB identity missing; retaining RAM Recovery'\nwhile :; do sleep 60; done\n"

def qualified_witness(image):
    from liuqin_universal_recovery import inspect

    q = json.loads((image.parent/'hardware-qualification.json').read_text())
    checked = inspect(image)
    manifest = checked['manifest']
    serial = q.get('serial', '')
    if (manifest['usage'] != FIXED_USAGE or q.get('schema') != 1 or q.get('device') != 'liuqin'
            or q.get('slot') != 'a' or q.get('partition') != 'recovery_a'
            or not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', serial)
            or q.get('image_sha256') != checked['image_sha256'] or q.get('build_id') != manifest['build_id']
            or any(q.get(key) is not True for key in ('hardware_fixed_boot_verified',
                    'hardware_cold_boot_verified', 'readback_verified', 'storage_sealed_verified'))):
        raise ValueError('fixed Recovery must have a matching device cold-boot qualification')
    return dict(serial=serial, partition='recovery_a', image_sha256=checked['image_sha256'])


def build_storage_seal(sources):
    compiler_name = 'aarch64-unknown-linux-musl-gcc'
    compiler = shutil.which(compiler_name)
    if not compiler:
        compiler = PROJECT/'.devbox/nix/profile/default/bin'/compiler_name
        if not compiler.is_file():
            raise ValueError('missing ARM64 compiler; use the project Devbox toolchain')
    inputs = ['device/boot/liuqin-storage-seal.c', 'device/boot/liuqin-storage-ready.c']
    sources.update({name: sha((PROJECT/name).read_bytes()) for name in inputs})
    with tempfile.TemporaryDirectory(prefix='liuqin-recovery-seal-') as work:
        binary = Path(work)/'liuqin-storage-seal'
        subprocess.run([str(compiler), '-static', '-Os', '-s', '-Wall', '-Wextra', '-Werror',
                        *(str(PROJECT/name) for name in inputs), '-o', str(binary)], check=True)
        data = binary.read_bytes()
    if data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 183:
        raise ValueError('storage seal helper must be an ARM64 ELF')
    return data


def build(a):
    if not a.adbd_runtime:raise ValueError('native Recovery requires the ADB runtime')
    out=a.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new output directory below project/out')
    b=a.base_image.read_bytes()
    if sha(b)!=BASE_SHA:raise ValueError('base must be the qualified native LVGL/touch image')
    info=boot_interface(a.base_image,installer=True);m=info['native_recovery_manifest']
    u=lambda n:struct.unpack_from('<I',b,n)[0];align=lambda n:(n+4095)//4096*4096
    r=4096+align(u(8));d=r+align(u(16));kernel=b[4096:4096+u(8)];dtb=b[d:d+u(1648)]
    entries=records(bounded_initramfs(b[r:r+u(16)],256*1024**2));sources={}
    components = None
    kernel_runtime = getattr(a, 'kernel_runtime', None)
    if kernel_runtime:
        kernel_runtime = kernel_runtime.resolve()
        components = json.loads((kernel_runtime/'kernel-manifest.json').read_text())
        if (components.get('schema') != 1 or components.get('device') != 'liuqin'
                or components.get('base_image_sha256') != BASE_SHA):
            raise ValueError('Recovery kernel components do not match the qualified base')
        for name,digest in components['files'].items():
            if Path(name).name != name or sha((kernel_runtime/name).read_bytes()) != digest:
                raise ValueError('Recovery kernel component changed: '+name)
        for name,digest in components['source_files'].items():
            if sha((PROJECT/name).read_bytes()) != digest:
                raise ValueError('Recovery kernel source changed: '+name)
        kernel = (kernel_runtime/'Image.gz').read_bytes()
        dtb = (kernel_runtime/'recovery.dtb').read_bytes()
        if (sha(gzip.decompress(kernel)) != components['kernel_sha256'] or
                sha(dtb) != components['dtb_sha256']):
            raise ValueError('Recovery kernel or DTB differs from its component manifest')
        add(entries,TOUCH_FILES[0],(kernel_runtime/'liuqin_bootloader_panel.ko').read_bytes())
        add(entries,'etc/liuqin-installer-kernel.release',(components['kernel_release']+'\n').encode())
        for name in ('kernel.config','kernel-log-fixes.patch'):
            add(entries,'usr/share/licenses/liuqin-recovery-kernel/'+name,(kernel_runtime/name).read_bytes())
        add(entries,'usr/share/licenses/liuqin-recovery-kernel/aligned-entry.S',
            (PROJECT/'device/recovery/aligned-entry.S').read_bytes())
        add(entries,'usr/share/licenses/liuqin-recovery-kernel/kernel-manifest.json',encoded(components))
        m = dict(m,kernel_sha256=components['kernel_sha256'],dtb_sha256=components['dtb_sha256'],
                 kernel_release=components['kernel_release'])
        sources.update(components['source_files'])
    def replace(name,path):
        body=path.read_bytes();sources[str(path.relative_to(PROJECT))]=sha(body);add(entries,name,body,0o100755 if name.endswith('.sh') or name in (UI_FILES[0],UI_FILES[2]) else 0o100644)
    qualified_fixed = getattr(a, 'qualified_fixed', None)
    if a.inspection_only and qualified_fixed:
        raise ValueError('a read-only image cannot declare a tablet boot switch')
    tablet_files = () if a.inspection_only else TABLET_SWITCH_FILES
    bcb_files = () if a.inspection_only else BCB_FILES
    store_files = () if a.inspection_only else STORE_FILES
    for name in store_files:
        replace(name,PROJECT/'device/recovery'/Path(name).name)
    retention_files = () if a.inspection_only else RETENTION_FILES
    for name in retention_files:
        replace(name,PROJECT/'shared'/Path(name).name)
    for name in (*INSTALL_FILES,*FIXED_RESTORE_FILES,*tablet_files):
        base=Path(name).name
        if base in ('native-install.py','native-root-guard.py'):path=PROJECT/'device/recovery'/base
        elif base in ('root-interface-v2.sh','boot-target-v1.sh'):path=PROJECT/'device/boot'/base
        elif base=='provision.sh':path=PROJECT/'device/installer/provision-liuqin-from-persist.sh'
        elif (PROJECT/'device/installer'/base).is_file():path=PROJECT/'device/installer'/base
        else:path=PROJECT/'shared'/base
        replace(name,path)
    for name,source in [(AGENT,'native-status.py'),(UI_FILES[1],'native-ui-service.py'),(TOUCH_FILES[1],'native-touch.py')]:replace(name,PROJECT/'device/recovery'/source)
    if bcb_files:
        replace(BCB_FILES[0], PROJECT/'device/recovery/native-bcb.py')
        path = PROJECT/'device/recovery/native-power.py'
        sources[str(path.relative_to(PROJECT))] = sha(path.read_bytes())
        add(entries, BCB_FILES[1], path.read_bytes(), 0o100755)
    adb=None
    if a.adbd_runtime:
        adb=json.loads((a.adbd_runtime/'adbd-manifest.json').read_text())
        body=(a.adbd_runtime/'adbd').read_bytes()
        if adb['architecture']!='aarch64' or sha(body)!=adb['sha256'] or struct.unpack_from('<H',body,18)[0]!=183:raise ValueError('invalid ARM64 adbd')
        if adb['builder_sha256']!=sha((PROJECT/'build/recovery/build-adbd.py').read_bytes()) or adb['lock_sha256']!=sha((PROJECT/'build/recovery/adbd-sources.json').read_bytes()) or adb['compat_sha256']!=sha((PROJECT/'build/recovery/adbd-compat.h').read_bytes()):raise ValueError('adbd builder/source lock changed')
        for name,digest in adb.get('patches',{}).items():
            if Path(name).name != name or sha((PROJECT/'build/recovery'/name).read_bytes()) != digest:
                raise ValueError('adbd patch changed: '+name)
        add(entries,ADB_FILES[0],body,0o100755)
        for name in ADB_FILES[1:]:replace(name,PROJECT/'device/recovery'/Path(name).name)
        sources['device/boot/initramfs/init']=sha((PROJECT/'device/boot/initramfs/init').read_bytes())
        add(entries,'init',native_init(),0o100755)
        add(entries,'etc/liuqin-native-adb',b'1\n')
        add(entries,STORAGE_SEAL,build_storage_seal(sources),0o100755)
        for name in ('etc/liuqin-installer-usb-network','bin/telnetd','bin/udhcpd'):entries.pop(name,None)
        for f in sorted((a.adbd_runtime/'licenses').glob('*')):add(entries,'usr/share/licenses/liuqin-adbd/'+f.name,f.read_bytes())
        for name in ('sources.json','NOTICE.txt'):add(entries,'usr/share/licenses/liuqin-adbd/'+name,(a.adbd_runtime/name).read_bytes())
    ui=json.loads((a.ui_runtime/'ui-manifest.json').read_text())
    if ui['architecture']!='aarch64':raise ValueError('ARM64 UI required')
    for name,digest in ui['source_files'].items():
        if sha((PROJECT/name).read_bytes())!=digest:raise ValueError('UI source changed: '+name)
    for name,path in [('liuqin-recovery-ui',UI_FILES[0]),('liuqin-ui-power',UI_FILES[2])]:
        body=(a.ui_runtime/name).read_bytes()
        if sha(body)!=ui['files'][name] or struct.unpack_from('<H',body,18)[0]!=183:raise ValueError('UI binary identity mismatch')
        add(entries,path,body,0o100755)
    for tool in ('usr/bin/python3','usr/bin/tar','usr/sbin/sgdisk','usr/sbin/mkfs.ext4','usr/sbin/mkfs.f2fs','usr/sbin/mkfs.btrfs'):
        if tool not in entries:raise ValueError('missing installation tool '+tool)
    names=set(('init',AGENT,BOARD_HELPER,*UI_FILES,*TOUCH_FILES,*INSTALL_FILES,*FIXED_RESTORE_FILES,*tablet_files,*bcb_files,*store_files,*retention_files,*(ADB_FILES+(STORAGE_SEAL,) if adb else ())))
    payload={n:sha(entries[n][1]) for n in sorted(names)}
    for path in ('build/recovery/build-native-installer.py','shared/liuqin_native_recovery.py','shared/liuqin_artifacts.py'):sources[path]=sha((PROJECT/path).read_bytes())
    if a.inspection_only and not adb:raise ValueError('this readonly variant is for native ADB only')
    identity=dict(base_image_sha256=BASE_SHA,kernel_sha256=m['kernel_sha256'],dtb_sha256=m['dtb_sha256'],payload_sha256=payload,sources=sources,ui=ui,
        generic_ota_version=0 if a.inspection_only else 2,combined_install_version=0 if a.inspection_only else 1,
        boot_switch_version=0 if a.inspection_only else 1,android_sparse_store_version=0 if a.inspection_only else 1,sparse_boot_store_version=0 if a.inspection_only else 1,
        fixed_recovery_restore_version=0 if a.inspection_only else 1,
        inspection_only=a.inspection_only,installer_capabilities=[] if a.inspection_only else CAPABILITIES,adbd=adb)
    if components:
        identity['recovery_kernel_components'] = components
    if adb:
        identity['recovery_fast_start_version'] = 1
    if not a.inspection_only:
        identity['tablet_boot_switch_version'] = 1
        identity['bcb_cleanup_version'] = 1
        identity['boot_store_version'] = 1
        identity['boot_store_retention_version'] = 1
        if qualified_fixed:
            identity['fixed_recovery_witness'] = qualified_witness(qualified_fixed)
    m=dict(m,installer_version=1,installer_capabilities=CAPABILITIES,inspection_only=a.inspection_only,build_id=sha(encoded(identity)),identity=identity,payload_sha256=payload,
        limitations=['Temporary boot only; not a fixed Recovery image.','Installation writes have offline tests only; physical installation remains untested.',
            'Storage starts sealed; a confirmed scoped session is mandatory for writes.','Full ROM v2 can update declared A-slot firmware and ROM Recovery after complete backups and confirmation; no automatic reboot, slot change or AVB flag patching.',
            'Trusted USB host only; not a security boundary against a malicious root host.'])
    if a.inspection_only:m.pop('installer_version');m.pop('installer_capabilities')
    else:
        m.update(generic_ota_version=2,combined_install_version=1,boot_switch_version=1,android_sparse_store_version=1,sparse_boot_store_version=1,fixed_recovery_restore_version=1,
                 tablet_boot_switch_version=1,bcb_cleanup_version=1,boot_store_version=1,boot_store_retention_version=1)
        if 'fixed_recovery_witness' in identity:
            m['fixed_recovery_witness'] = identity['fixed_recovery_witness']
    if adb:m.update(native_adb_version=1,usb_transport='adb',recovery_fast_start_version=1)
    add(entries,MARKER,encoded(m));ram=gzip.compress(pack(entries),compresslevel=9,mtime=0);h=bytearray(b[:4096])
    struct.pack_into('<I',h,8,len(kernel));struct.pack_into('<I',h,16,len(ram))
    struct.pack_into('<I',h,1648,len(dtb))
    legacy=hashlib.sha1()
    for part in (kernel,ram,b'',b'',dtb):legacy.update(part);legacy.update(struct.pack('<I',len(part)))
    h[576:608]=legacy.digest()+bytes(12)
    result=bytes(h)+kernel+bytes(-len(kernel)%4096)+ram+bytes(-len(ram)%4096)+dtb+bytes(-len(dtb)%4096)
    if len(result)>=100*1024**2:raise ValueError('candidate image exceeds size budget')
    out.mkdir(parents=True);image=out/'recovery.img';image.write_bytes(result)
    if boot_interface(image,installer=True)['native_recovery_manifest']!=m:raise ValueError('candidate verification failed')
    for path,digest in sources.items():
        if sha((PROJECT/path).read_bytes())!=digest:raise ValueError('source changed during build')
    record=dict(m,image_sha256=sha(result),image_bytes=len(result),hardware_boot_tested=False,hardware_install_tested=False)
    (out/'native-recovery-manifest.json').write_bytes(encoded(record));(out/'SHA256SUMS').write_text(sha(result)+'  recovery.img\n')
    (out/'STATUS.txt').write_text('liuqin 原生安装候选\n仅临时启动，禁止固定刷入。尚未完成此镜像的真机启动和安装写入验收。\n默认全部存储只读；备份、核对目标并确认完整计划后，才允许范围内写入。\n支持 GUI 分区编辑、已适配 Android ROM、Linux rootfs 和 boot 绑定；完成后留在 Rec。\n完整 ROM v2 不要求固件哈希相同或固定 AVB flags；机型、完整性、容量与写入范围检查仍保留。\n完整 ROM v2 按包更新 A 槽固件和 ROM Recovery；不修改 AVB flags，不自动重启/切槽，不解密 data。\nSHA256: '+sha(result)+'\n',encoding='utf-8')
    if adb:
        with (out/'STATUS.txt').open('a',encoding='utf-8') as f:f.write('连接方式：USB ADB；不创建 USB 网卡，不启动 DHCP 或 Telnet。\n')
    if not a.inspection_only:
        with (out/'STATUS.txt').open('a',encoding='utf-8') as f:
            f.write('平板端可查看已安装系统，在核验引导后确认切换默认系统。\n')
            f.write('引导存储维护 v1：可预览重复快照回收、恢复中断的引导切换，并续接已回读完成的 Android/Linux 系统登记；需要电脑 GUI 明确确认。\n')
    if a.inspection_only:(out/'STATUS.txt').write_text('liuqin 原生 ADB 只读候选\n仅临时启动，USB 枚举尚未真机验收。此镜像没有安装写入能力。\n用于设备/分区读取和 ROM 分析；安装请明确选择另附的 ADB 安装候选。\n不创建 USB 网卡，不启动 DHCP/Telnet。\nSHA256: '+sha(result)+'\n',encoding='utf-8')
    shutil.copy2(PROJECT/'build/recovery/NATIVE-NOTICE.md',out/'NOTICE.md')
    print(json.dumps(dict(path=str(image),sha256=sha(result),bytes=len(result),build_id=m['build_id']),indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-image',type=Path,default=PROJECT/'out/releases/native-recovery/recovery.img');p.add_argument('--ui-runtime',type=Path,required=True);p.add_argument('--adbd-runtime',type=Path,required=True);p.add_argument('--inspection-only',action='store_true');p.add_argument('--out',type=Path,required=True)
    p.add_argument('--qualified-fixed',type=Path,help='Bind one cold-boot-qualified fixed image for tablet switching on that device')
    p.add_argument('--kernel-runtime',type=Path,help='Use the dedicated kernel bundle from prepare-native-kernel.py')
    build(p.parse_args())
