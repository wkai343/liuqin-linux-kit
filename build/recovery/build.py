#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the paired liuqin Recovery from pinned TWRP, stock kernel and tool runtime.

This is a release-based repack, not a TWRP source build. No root, mounts or
installer.img are needed. The resulting image still requires device testing.
"""
import argparse,gzip,hashlib,io,json,lzma,os,re,shutil,struct,subprocess,sys,tarfile,tempfile,urllib.request
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_recovery_image import LIMIT,MARKER,BUSYBOX_PATH,add,inspect_paired,pack,records,unpack,ramdisk_decode

def digest(data):return hashlib.sha256(data).hexdigest()
def json_bytes(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
def checked(path,record):
    data=path.read_bytes()
    if len(data)!=record['bytes'] or digest(data)!=record['sha256']:raise ValueError('pinned input mismatch: '+str(path))
    return data

def runtime_tree(source,busybox,dest,kernel_release):
    for name in ('usr/bin/python3','usr/bin/tar','usr/bin/install','usr/sbin/mkfs.ext4',
                 'usr/sbin/mkfs.f2fs','usr/sbin/sgdisk','usr/sbin/e2fsck','lib/ld-musl-aarch64.so.1'):
        data=(source/name).read_bytes()
        if data[:6]!=b'\x7fELF\x02\x01' or struct.unpack_from('<H',data,18)[0]!=183:
            raise ValueError('required runtime tool is not ARM64: '+name)
    manifest=json.loads((source/'runtime-files.json').read_text())
    for name,expected in manifest.items():
        if digest((source/name).read_bytes())!=expected:raise ValueError('runtime input changed: '+name)
    shutil.copytree(source,dest,symlinks=True)
    # The fixed stock kernel is 5.10. Do not create a filesystem requiring the
    # newer mainline installer's orphan_file feature and then fail to mount it.
    config=dest/'etc/mke2fs.conf'
    config.write_text(config.read_text().replace(',orphan_file','').replace(',metadata_csum_seed',''))
    for name in ('bin','sbin','etc','dev','proc','sys','tmp','run','mnt/install','usr/lib/liuqin'):(dest/name).mkdir(parents=True,exist_ok=True)
    shutil.copyfile(busybox,dest/'bin/busybox');(dest/'bin/busybox').chmod(0o755)
    # Link the complete, explicit tool surface used by the verified device scripts.
    for applet in 'awk basename blockdev cat chmod chown chroot cp cut date dd dirname dmesg du echo find findfs grep gzip head id ln losetup ls mkdir mknod mount mv readlink reboot rm rmdir sed sh sha256sum sleep sort stat sync tail test touch tr truncate umount uname uniq wc wget xargs'.split():
        p=dest/'bin'/applet
        if not p.exists() and not p.is_symlink():p.symlink_to('busybox')
    target=dest/'usr/lib/liuqin'
    for p in (PROJECT/'device/installer').glob('*.sh'):shutil.copyfile(p,target/p.name)
    shutil.copyfile(PROJECT/'device/installer/provision-liuqin-from-persist.sh',target/'provision.sh')
    shutil.copyfile(PROJECT/'device/installer/android-device.py',target/'android-device.py')
    for p in (PROJECT/'shared').glob('liuqin*'):
        if p.suffix in ('.py','.json'):shutil.copyfile(p,target/p.name)
    for name in ('root-interface-v2.sh','boot-target-v1.sh'):shutil.copyfile(PROJECT/'device/boot'/name,target/name)
    shutil.copyfile(PROJECT/'device/recovery/recovery-session.py',target/'recovery-session.py')
    markers={'liuqin-installer':'liuqin','liuqin-storage-mode':'readonly','liuqin-android-installer':'1',
        'liuqin-multiboot':'1','liuqin-installer-interface':'liuqin-installer-v2','liuqin-filesystems':'ext4',
        'liuqin-installer-usb-network':'adb','liuqin-installer-kernel.release':kernel_release}
    for name,text in markers.items():(dest/'etc'/name).write_text(text+'\n')
    # Preserve original interpreters only inside the private root, never TWRP's /lib.
    for p in dest.rglob('*'):
        if p.is_symlink():
            link=os.readlink(p);resolved=dest/link.lstrip('/') if link.startswith('/') else p.parent/link
            if not resolved.exists():raise ValueError('runtime contains dangling link: '+str(p))
    hashes={p.relative_to(dest).as_posix():digest(p.read_bytes()) for p in sorted(dest.rglob('*')) if p.is_file() and not p.is_symlink()}
    (dest/'etc/liuqin-runtime-files.json').write_bytes(json_bytes(hashes))
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w',format=tarfile.PAX_FORMAT) as tar:
        for p in sorted(dest.rglob('*')):
            item=tar.gettarinfo(str(p),arcname=p.relative_to(dest).as_posix())
            item.uid=item.gid=item.mtime=0;item.uname=item.gname='';item.pax_headers={}
            if item.isfile():
                with p.open('rb') as f:tar.addfile(item,f)
            else:tar.addfile(item)
    return lzma.compress(out.getvalue(),preset=9),hashes

def build(base,boot,runtime,busybox,output):
    if output.exists():raise ValueError('output already exists: '+str(output))
    pin=json.loads((PROJECT/'build/recovery/source.json').read_text());checked(base,pin)
    runtime_files=json.loads((runtime/'runtime-files.json').read_text())
    if digest(busybox.read_bytes())!=runtime_files.get('bin/busybox'):
        raise ValueError('BusyBox differs from the source-built runtime manifest')
    header=busybox.read_bytes()[:20]
    if header[:6]!=b'\x7fELF\x02\x01' or header[18:20]!=b'\xb7\x00':
        raise ValueError('BusyBox is not ARM64')
    if 'INTERP' in subprocess.check_output(['readelf','-l',str(busybox)],text=True):
        raise ValueError('BusyBox must be static')
    boot_pin=json.loads((PROJECT/'shared/liuqin-rom-images.json').read_text())['images']['boot.img']
    stock=checked(boot,boot_pin)
    if stock[:8]!=b'ANDROID!' or struct.unpack_from('<I',stock,40)[0] not in (3,4):raise ValueError('invalid stock boot header')
    size=struct.unpack_from('<I',stock,8)[0];kernel=stock[4096:4096+size]
    if not size or len(kernel)!=size:raise ValueError('stock boot lacks kernel')
    expanded=gzip.decompress(kernel) if kernel[:2]==b'\x1f\x8b' else ramdisk_decode(kernel) if kernel[:4] in (b'\x02\x21\x4c\x18',b'\x04\x22\x4d\x18') else kernel
    match=re.search(rb'Linux version ([0-9][^ \x00\r\n]+)',expanded)
    if not match:raise ValueError('cannot identify stock kernel release')
    # Stock boot stores an uncompressed Image. Gzip leaves the decompressed
    # kernel byte-identical and makes the full Recovery fit its 100 MiB slot.
    compressed_kernel=gzip.compress(expanded,compresslevel=9,mtime=0)
    if len(compressed_kernel)<len(kernel):kernel=compressed_kernel
    release=match[1].decode('ascii');header,_,raw=unpack(base);items=records(raw)
    removed={name:digest(data) for name,(_,data) in items.items() if any(name.startswith(prefix) for prefix in pin['remove_prefixes'])}
    for name in removed:del items[name]
    modified={}
    for name,(fields,data) in list(items.items()):
        if name.endswith('.xml') and b'/customzip/' in data:
            updated=re.sub(rb'[ \t]*<listitem\b[^>]*>.*?</listitem>',
                lambda match:b'' if b'/customzip/' in match[0] else match[0],data,flags=re.S)
            if b'/customzip/' in updated:raise ValueError('unhandled removed add-on UI action: '+name)
            items[name]=(fields,updated);modified[name]=dict(before=digest(data),after=digest(updated))
    # Upstream init disables/loads modules itself. Preserve those files and all
    # device firmware; only remove the unrelated root-add-on installers.
    with tempfile.TemporaryDirectory(prefix='liuqin-recovery-') as temp:
        payload,files=runtime_tree(runtime,busybox,Path(temp)/'root',release)
    added={'system/etc/liuqin-runtime.tar.xz':payload,
        'system/etc/liuqin-runtime.sha256':(digest(payload)+'\n').encode(),
        'system/bin/liuqin-recovery':(PROJECT/'device/recovery/gateway.sh').read_bytes(),
        'system/bin/liuqin/reboot':(runtime/'usr/sbin/liuqin-reboot').read_bytes(),
        BUSYBOX_PATH:busybox.read_bytes()}
    identity=dict(source=pin['sha256'],boot=boot_pin['sha256'],payload={k:digest(v) for k,v in added.items()})
    build_id=digest(json_bytes(identity));added['system/etc/liuqin-build-id']=(build_id+'\n').encode()
    manifest=dict(schema=1,device='liuqin',backend='recovery-adb-v1',build_id=build_id,
        kernel_release=release,kernel_sha256=digest(kernel),kernel_image_sha256=digest(expanded),
        kernel_encoding='gzip' if kernel[:2]==b'\x1f\x8b' else 'original',filesystems=['ext4'],
        payload_files={k:digest(v) for k,v in added.items()},runtime_files=files,
        source=pin,stock_boot=boot_pin,removed_files=removed,modified_files=modified,status='DEVICE_UNTESTED',usage='temporary-boot-only',
        notes=['Release-based TWRP repack; not a source build.', 'Root ADB inherits upstream trust model; trusted USB host required.',
               'Only ext4 Linux roots are qualified by this build. Android userdata formatting also supports F2FS.',
               'Temporary boot only. Do not flash this kernel-containing image to recovery_a/recovery_b.',
               'Stock vendor_boot/DTBO compatibility and this updated runtime need physical device validation.'])
    for name,data in added.items():add(items,name,data,0o100755 if name.startswith('system/bin/') else 0o100644)
    add(items,MARKER,json_bytes(manifest))
    import lz4.block
    archive=pack(items);compressed=bytearray(b'\x02\x21\x4c\x18')
    for start in range(0,len(archive),8*1024**2):
        block=lz4.block.compress(archive[start:start+8*1024**2],mode='high_compression',compression=12,store_size=False)
        compressed.extend(struct.pack('<I',len(block)));compressed.extend(block)
    h=bytearray(header);struct.pack_into('<II',h,8,len(kernel),len(compressed))
    # Repacking invalidates the upstream boot signature and AVB footer. This
    # image is for an unlocked bootloader; never retain a stale signature.
    if struct.unpack_from('<I',h,40)[0]==4:struct.pack_into('<I',h,1580,0)
    result=bytes(h)+kernel+bytes(-len(kernel)%4096)+compressed+bytes(-len(compressed)%4096)
    if len(result)>LIMIT:raise ValueError(f'Recovery exceeds partition: {len(result)} > {LIMIT}')
    used=len(result);result+=bytes(LIMIT-len(result))
    output.mkdir(parents=True)
    try:
        image=output/'recovery.img';image.write_bytes(result)
        if inspect_paired(image)['build_id']!=build_id:raise ValueError('built Recovery verification failed')
        manifest.update(image_sha256=digest(result),image_bytes=len(result),used_bytes=used)
        (output/'recovery-manifest.json').write_bytes(json_bytes(manifest))
        (output/'SHA256SUMS').write_text(digest(result)+'  recovery.img\n')
        shutil.copyfile(PROJECT/'build/recovery/NOTICE.md',output/'NOTICE.md')
        print(json.dumps(dict(image=str(image.resolve()),build_id=build_id,used_bytes=used,kernel=release,status=manifest['status']),indent=2))
    except BaseException:
        shutil.rmtree(output);raise
    return image

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=PROJECT/'inputs/recovery/liuqin/twrp-20250209.img')
    parser.add_argument('--boot',type=Path,default=PROJECT/'inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images/boot.img')
    parser.add_argument('--runtime',type=Path,required=True,help='ARM64 tools directory produced by build/recovery/build-runtime.py')
    parser.add_argument('--busybox',type=Path,help='Defaults to the supplied runtime/bin/busybox')
    parser.add_argument('--out',type=Path,default=PROJECT/'out/releases/recovery')
    args=parser.parse_args()
    if not args.boot.exists():
        subprocess.run([sys.executable,str(PROJECT/'build/recovery/fetch-kernel.py'),'--out',str(args.boot)],check=True)
    if not args.base.exists():
        pin=json.loads((PROJECT/'build/recovery/source.json').read_text());args.base.parent.mkdir(parents=True,exist_ok=True)
        with urllib.request.urlopen(pin['url'],timeout=60) as response:data=response.read(LIMIT+1)
        if len(data)!=pin['bytes'] or digest(data)!=pin['sha256']:raise ValueError('downloaded TWRP does not match pin')
        args.base.write_bytes(data)
    build(args.base,args.boot,args.runtime,args.busybox or args.runtime/'bin/busybox',args.out)

if __name__=='__main__':main()
