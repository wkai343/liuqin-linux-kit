# SPDX-License-Identifier: MIT
"""Strict Android v4/newc utilities for the paired Recovery builder and client."""
import gzip,hashlib,io,json,re,struct
from pathlib import Path,PurePosixPath
LIMIT=100*1024**2
MARKER='system/etc/liuqin-recovery.json'
BUSYBOX_PATH='system/bin/liuqin/busybox'

def ramdisk_decode(data):
    if data[:2]==b'\x1f\x8b':
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:raw=f.read(512*1024**2+1)
    elif data[:4]==b'\x02\x21\x4c\x18':
        import lz4.block
        raw=bytearray();offset=4
        while offset<len(data):
            if offset+4>len(data):raise ValueError('truncated LZ4 size')
            size=struct.unpack_from('<I',data,offset)[0];offset+=4
            if not 0<size<=9*1024**2 or offset+size>len(data):raise ValueError('invalid LZ4 block')
            raw.extend(lz4.block.decompress(data[offset:offset+size],uncompressed_size=8*1024**2));offset+=size
            if len(raw)>512*1024**2:raise ValueError('ramdisk too large')
        raw=bytes(raw)
    elif data[:4]==b'\x04\x22\x4d\x18':
        import lz4.frame
        with lz4.frame.open(io.BytesIO(data)) as f:raw=f.read(512*1024**2+1)
    else:raise ValueError('unsupported Recovery ramdisk compression')
    if len(raw)>512*1024**2:raise ValueError('ramdisk too large')
    return raw

def unpack(image):
    b=Path(image).read_bytes();u=lambda n:struct.unpack_from('<I',b,n)[0]
    if not 4096<=len(b)<=192*1024**2 or b[:8]!=b'ANDROID!' or u(40) not in (3,4):raise ValueError('unsupported Recovery header')
    if u(20)!=(1584 if u(40)==4 else 1580):raise ValueError('invalid Recovery header size')
    ks,rs=u(8),u(12);start=4096+(ks+4095)//4096*4096
    if not rs or start+rs>len(b):raise ValueError('truncated Recovery ramdisk')
    return b[:4096],b[4096:4096+ks],ramdisk_decode(b[start:start+rs])

def records(raw):
    result={};pos=0
    while pos+110<=len(raw):
        h=raw[pos:pos+110]
        if h[:6] not in (b'070701',b'070702') or not re.fullmatch(b'[0-9A-Fa-f]{104}',h[6:]):raise ValueError('invalid CPIO')
        fields=[int(h[6+i*8:14+i*8],16) for i in range(13)];size,n=fields[6],fields[11]
        if not 1<=n<=4096 or pos+110+n>len(raw) or raw[pos+109+n]!=0:raise ValueError('invalid CPIO name')
        name=raw[pos+110:pos+109+n].decode('utf-8');start=(pos+110+n+3)&~3
        if start+size>len(raw):raise ValueError('truncated CPIO')
        if name=='TRAILER!!!':return result
        name=name.removeprefix('./')
        if name in result or name.startswith('/') or '..' in PurePosixPath(name).parts:raise ValueError('unsafe CPIO path')
        result[name]=(fields,raw[start:start+size]);pos=(start+size+3)&~3
    raise ValueError('CPIO trailer missing')

def encode_record(name,fields,data):
    fields=list(fields);fields[6]=len(data);fields[11]=len(name.encode())+1;fields[12]=0
    b=('070701'+''.join(f'{v:08x}' for v in fields)).encode()+name.encode()+b'\0';b+=bytes(-len(b)%4)
    b+=data;return b+bytes(-len(b)%4)

def add(entries,name,data,mode=0o100644):
    for parent in reversed(PurePosixPath(name).parents):
        if str(parent)=='.':continue
        n=str(parent)
        if n in entries and entries[n][0][1]&0o170000!=0o040000:raise ValueError('payload parent is not a directory: '+n)
        entries.setdefault(n,([0,0o040755,0,0,2,0,0,0,0,0,0,0,0],b''))
    entries[name]=([0,mode,0,0,1,0,0,0,0,0,0,0,0],data)

def pack(entries):
    out=bytearray()
    for name,(fields,data) in entries.items():out.extend(encode_record(name,fields,data))
    out.extend(encode_record('TRAILER!!!',[0]*13,b''));out.extend(bytes(-len(out)%512));return bytes(out)

def inspect_paired(path):
    header,kernel,raw=unpack(path);items=records(raw)
    if MARKER not in items:return None
    if items[MARKER][0][1]&0o170000!=0o100000 or len(items[MARKER][1])>1024**2:raise ValueError('invalid Recovery manifest file')
    manifest=json.loads(items[MARKER][1])
    if manifest.get('schema')!=1 or manifest.get('device')!='liuqin' or manifest.get('backend')!='recovery-adb-v1':raise ValueError('invalid paired Recovery manifest')
    if 'system/bin/liuqin-busybox' in manifest.get('payload_files',{}) and BUSYBOX_PATH not in manifest.get('payload_files',{}):
        raise ValueError('旧配套 Recovery 的 BusyBox 路径不兼容，请更新 recovery.img。')
    required={'system/bin/liuqin-recovery',BUSYBOX_PATH,'system/etc/liuqin-runtime.tar.xz',
        'system/etc/liuqin-runtime.sha256','system/etc/liuqin-build-id'}
    if not isinstance(manifest.get('payload_files'),dict) or not required<=set(manifest['payload_files']):raise ValueError('incomplete Recovery payload')
    if not re.fullmatch(r'[a-f0-9]{64}',manifest.get('build_id','')) or not re.fullmatch(r'[A-Za-z0-9._+-]{1,255}',manifest.get('kernel_release','')):raise ValueError('invalid Recovery identity')
    if not manifest.get('filesystems') or not set(manifest['filesystems'])<={'ext4','btrfs'}:raise ValueError('invalid Recovery filesystem capabilities')
    if not kernel or hashlib.sha256(kernel).hexdigest()!=manifest['kernel_sha256']:raise ValueError('Recovery kernel mismatch')
    for name,digest in manifest['payload_files'].items():
        if name not in items or items[name][0][1]&0o170000!=0o100000 or hashlib.sha256(items[name][1]).hexdigest()!=digest:raise ValueError('Recovery payload mismatch: '+name)
    if items['system/etc/liuqin-build-id'][1].decode().strip()!=manifest['build_id']:raise ValueError('Recovery build ID mismatch')
    return manifest

def runtime_interface(path):
    m=inspect_paired(path)
    if m is None:raise ValueError('Recovery lacks Liuqin installation runtime')
    return dict(interface='liuqin-installer-v2',kernel_release=m['kernel_release'],kernel_sha256=m['kernel_sha256'],
        android_version=1,multiboot_version=1,filesystems=m['filesystems'],usb_network='adb',recovery_manifest=m)
