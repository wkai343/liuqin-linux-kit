# SPDX-License-Identifier: MIT
"""Inspect liuqin Recovery images; back up and verify both dedicated slots."""
import gzip
import hashlib
import io
from pathlib import Path,PurePosixPath
import re
import shutil
import struct
from .engine import InstallerError,digest_file

LIMIT=100*1024**2
RAMDISK_LIMIT=512*1024**2
SLOTS=('recovery_a','recovery_b')
FIXED_INSTALL_UNAVAILABLE='固定 Recovery 安装暂未开放：尚无通过固定启动及回退验收的配套镜像。请使用临时启动。'


def unpack_ramdisk(data):
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:result=stream.read(RAMDISK_LIMIT+1)
    elif data.startswith(b'\x02\x21\x4c\x18'):
        import lz4.block
        result=bytearray();offset=4
        while offset<len(data):
            if offset+4>len(data):raise ValueError('Recovery LZ4 块不完整。')
            size=struct.unpack_from('<I',data,offset)[0];offset+=4
            if not 0<size<=9*1024**2 or offset+size>len(data):raise ValueError('Recovery LZ4 块大小无效。')
            result.extend(lz4.block.decompress(data[offset:offset+size],uncompressed_size=8*1024**2));offset+=size
            if len(result)>RAMDISK_LIMIT:raise ValueError('Recovery ramdisk 过大。')
        result=bytes(result)
    elif data.startswith(b'\x04\x22\x4d\x18'):
        import lz4.frame
        with lz4.frame.open(io.BytesIO(data)) as stream:result=stream.read(RAMDISK_LIMIT+1)
    else:raise ValueError('Recovery ramdisk 需要 gzip 或 LZ4。')
    if len(result)>RAMDISK_LIMIT:raise ValueError('Recovery ramdisk 过大。')
    return result


def inspect_image(path):
    path=Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or not 4096<=path.stat().st_size<=LIMIT:
        raise InstallerError('请选择 liuqin 配套 Recovery 镜像（不超过 100 MiB）。')
    data=path.read_bytes()
    u=lambda offset:struct.unpack_from('<I',data,offset)[0]
    if data[:8]!=b'ANDROID!' or u(40) not in (3,4) or u(20)!=(1584 if u(40)==4 else 1580):
        raise InstallerError('Recovery 需要 Android v3/v4 引导格式，不能使用 Linux boot.img 或 vendor_boot.img。')
    kernel,ramdisk=u(8),u(12);align=lambda n:(n+4095)//4096*4096
    start=4096+align(kernel);signature=u(1580) if u(40)==4 else 0
    if not ramdisk or start+align(ramdisk)+signature>len(data):raise InstallerError('Recovery 内核、ramdisk 或签名范围不完整。')
    raw=unpack_ramdisk(data[start:start+ramdisk]);position=0;properties={};binary=False;ended=False;names=set()
    while position+110<=len(raw):
        header=raw[position:position+110]
        if header[:6] not in (b'070701',b'070702'):raise InstallerError('Recovery CPIO 损坏。')
        try:size=int(header[54:62],16);length=int(header[94:102],16);mode=int(header[14:22],16)
        except ValueError as error:raise InstallerError('Recovery CPIO 头无效。') from error
        if not 1<=length<=4096:raise InstallerError('Recovery 路径无效。')
        name=raw[position+110:position+110+length-1].decode('utf-8');begin=(position+110+length+3)&~3
        if begin+size>len(raw):raise InstallerError('Recovery CPIO 不完整。')
        if name=='TRAILER!!!':ended=True;break
        name=name.removeprefix('./')
        if name in names or name.startswith('/') or '..' in PurePosixPath(name).parts:raise InstallerError('Recovery CPIO 路径重复或越界。')
        names.add(name)
        if mode&0o170000==0o100000:
            if name in ('system/bin/recovery','sbin/recovery','bin/recovery') and size>0:binary=True
            if name in ('prop.default','default.prop','system/build.prop','vendor/build.prop','system/etc/prop.default'):
                if size>1024**2:raise InstallerError('Recovery 属性文件过大。')
                for line in raw[begin:begin+size].decode('utf-8').splitlines():
                    if '=' in line and not line.startswith('#'):
                        key,value=line.split('=',1);properties.setdefault(key,set()).add(value.strip())
        position=(begin+size+3)&~3
    identities=properties.get('ro.product.device',set())|properties.get('ro.product.vendor.device',set())
    if not ended or not binary or 'liuqin' not in identities or identities&{'pipa','yudi','sheng'}:
        raise InstallerError('镜像缺少 liuqin 机型身份或 Recovery 程序，拒绝使用。')
    return dict(path=str(path.resolve()),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),header_version=u(40),
        device='liuqin',kernel_bytes=kernel,temporary_boot=bool(kernel),version=next(iter(properties.get('ro.twrp.version',{'Recovery'}))))


def validate_usage(record,mode):
    if mode=='boot' and not record['temporary_boot']:
        raise InstallerError('此 Recovery 不含内核，不能用于独立临时启动。')
    if mode=='install' and record['kernel_bytes']:
        raise InstallerError('此镜像包含临时启动用内核，不能刷入 liuqin 的 recovery_a/recovery_b。当前配套镜像仅可临时启动；固定安装需要独立适配的无内核 Recovery。')
    if mode=='install':
        raise InstallerError(FIXED_INSTALL_UNAVAILABLE)


def validate_temporary_target(engine,record):
    if engine.simulation:return
    if record['sha256']!=engine.manifest['files'].get('recovery.img') or not hasattr(engine.transport,'require_boot_compatibility'):
        raise InstallerError('临时 Recovery 目标必须是当前已核验的配套安装镜像；其他镜像需先完成独立固件适配，不能在安装末尾直接尝试。')


def targets(state):
    found={name:state['inventory'].get(name) for name in SLOTS}
    if any(p is None for p in found.values()):raise InstallerError('设备未报告独立 recovery_a / recovery_b，不能固定安装。')
    for part in found.values():
        if part['parent']=='sda' or part['bytes']!=LIMIT:raise InstallerError('Recovery 分区位置或容量不符合 liuqin 适配。')
    return found


def install(engine,record,state,directory):
    actual=inspect_image(record['path']);validate_usage(actual,'install')
    if actual['sha256']!=record['sha256']:raise InstallerError('Recovery 镜像在确认后变化。')
    transport=engine.transport;parts=targets(state);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if digest_file(Path(record['path']))!=record['sha256']:raise InstallerError('Recovery 镜像在确认后变化。')
    previous_boot=transport.current_boot_digest();previous={};attempted=[]
    for name,part in parts.items():
        previous[name]=transport.snapshot_recovery(directory,name)
        if (directory/(name+'.img')).stat().st_size!=part['bytes']:raise InstallerError('Recovery 完整分区备份大小不符。')
    padded=directory/'recovery-new.img'
    with Path(record['path']).open('rb') as source,padded.open('xb') as output:
        shutil.copyfileobj(source,output);output.write(bytes(LIMIT-record['bytes']))
    expected=digest_file(padded)
    engine.receipt['recovery_backups']=dict(previous);engine.save_receipt()
    try:
        transport.return_fastboot();transport.check_device()
        for name in SLOTS:
            if not engine.simulation and int(transport.getvar('partition-size:'+name),0)!=parts[name]['bytes']:
                raise InstallerError('Recovery 分区大小在 Fastboot 中不一致。')
        for name in SLOTS:
            attempted.append(name);engine.receipt['recovery_write_started']=list(attempted);engine.save_receipt()
            transport.fastboot('flash',name,str(padded))
        transport.boot_ram()
        after=transport.android_rpc('partition-inspect',{})
        if after['inventory']!=state['inventory']:raise InstallerError('Recovery 写入后分区身份发生变化。')
        for name in SLOTS:
            if partition_digest(engine,name)!=expected:raise InstallerError('Recovery 回读失败：'+name)
        if transport.current_boot_digest()!=previous_boot:raise InstallerError('默认 boot_a 在 Recovery 安装时改变。')
        engine.receipt['recovery_verified']=True;engine.save_receipt()
    except Exception:
        if attempted:
            engine.receipt['recovery_rollback_attempted']=True
            try:
                if transport.ram_active:transport.return_fastboot()
                for name in attempted:transport.fastboot('flash',name,str(directory/(name+'.img')))
                transport.boot_ram()
                if any(partition_digest(engine,name)!=previous[name] for name in SLOTS):raise InstallerError('Recovery 恢复回读不符。')
                engine.receipt['recovery_rollback_verified']=True
            except Exception as error:engine.receipt['recovery_rollback_error']=str(error)
            engine.save_receipt()
        raise


def partition_digest(engine,name):
    if name not in SLOTS:raise ValueError('无效 Recovery 目标。')
    if engine.simulation:return digest_file(engine.demo_recovery_partitions[name])
    return engine.transport.remote('/bin/busybox sha256sum /dev/disk/by-partlabel/'+name,180).decode().split()[0]
