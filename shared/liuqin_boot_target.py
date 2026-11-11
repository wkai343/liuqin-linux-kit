# SPDX-License-Identifier: MIT
"""Versioned root binding and lossless Android-v2 boot image specialization."""
from __future__ import annotations
import gzip
import hashlib
import re
import struct
import uuid
from pathlib import Path

CAPABILITY = 'etc/liuqin-multiboot'
TARGET_PATH = 'etc/liuqin-boot-target'
HELPER_PATH = 'usr/lib/liuqin/boot-target-v1.sh'
LEGACY_HELPER_SHA256 = '6f0805e8b7ee49b4b9f24667422ae5b9c4477578bfc08649e3784cf4b3887ffa'
KEYS = ('os_id', 'root_partuuid', 'root_fsuuid', 'root_fstype', 'root_path', 'boot_generation')


def current_helper():
    installed = Path(__file__).resolve().with_name('boot-target-v1.sh')
    if installed.exists() or installed.is_symlink():
        if installed.is_symlink() or not installed.is_file():
            raise ValueError('已安装的引导绑定解析器不是普通文件。')
        return installed.read_bytes()
    return (Path(__file__).resolve().parents[1]/'device/boot/boot-target-v1.sh').read_bytes()


def validate_helper(interface):
    expected = hashlib.sha256(current_helper()).hexdigest()
    if interface.get('boot_target_helper_sha256') not in (expected, LEGACY_HELPER_SHA256):
        raise ValueError('boot.img 的分区绑定解析器未知或缺失，请重新构建；尚未修改分区。')


def encode(target):
    if set(target) != {'schema_version', *KEYS} or target['schema_version'] != 1:
        raise ValueError('不支持的 rootfs 引导绑定版本。')
    for key in ('os_id', 'root_partuuid', 'root_fsuuid', 'boot_generation'):
        if not isinstance(target[key], str) or str(uuid.UUID(target[key])) != target[key]:
            raise ValueError('引导绑定包含无效 UUID：' + key)
    if target['root_fstype'] not in ('ext4', 'btrfs') or target['root_path'] != 'native-root':
        raise ValueError('引导绑定包含不支持的根文件系统或路径。')
    return ('LIUQIN_BOOT_TARGET_V1\n' + ''.join(key+'='+target[key]+'\n' for key in KEYS)).encode('ascii')


def parse(content):
    if len(content) > 512:
        raise ValueError('引导绑定过大。')
    lines = content.decode('ascii').splitlines()
    if len(lines) != 7 or lines[0] != 'LIUQIN_BOOT_TARGET_V1':
        raise ValueError('引导绑定格式错误。')
    result = dict(schema_version=1)
    for key, line in zip(KEYS, lines[1:]):
        if not line.startswith(key+'='):
            raise ValueError('引导绑定字段错误。')
        result[key] = line[len(key)+1:]
    if encode(result) != content:
        raise ValueError('引导绑定不是规范格式。')
    return result


def _record(name, content):
    fields = [0, 0o100644, 0, 0, 1, 0, len(content), 0, 0, 0, 0, len(name)+1, 0]
    data = ('070701'+''.join(f'{v:08x}' for v in fields)).encode()+name.encode()+b'\0'
    data += bytes(-len(data)%4)
    data += content
    return data + bytes(-len(data)%4)


def _inject(raw, binding):
    # Validate complete records, retaining original headers, hardlinks, modes,
    # ownership, device nodes and payloads byte-for-byte.
    position = 0
    names = set(); chunks = []; helper_found = False
    while position + 110 <= len(raw):
        header = raw[position:position+110]
        if header[:6] not in (b'070701', b'070702') or not re.fullmatch(rb'[0-9A-Fa-f]{104}', header[6:]):
            raise ValueError('CPIO 头损坏。')
        fields = [int(header[6+i*8:14+i*8],16) for i in range(13)]
        size, namesize = fields[6], fields[11]
        if not 1 <= namesize <= 4096 or position+110+namesize > len(raw):
            raise ValueError('CPIO 路径不完整。')
        namebytes=raw[position+110:position+110+namesize]
        if namebytes[-1:] != b'\0' or b'\0' in namebytes[:-1]: raise ValueError('CPIO 路径缺少结束符。')
        name=namebytes[:-1].decode().removeprefix('./')
        start=(position+110+namesize+3)&~3
        finish=(start+size+3)&~3
        if finish > len(raw): raise ValueError('CPIO 载荷不完整。')
        if name in names: raise ValueError('CPIO 路径重复。')
        names.add(name)
        if name == TARGET_PATH: raise ValueError('此 boot.img 已绑定系统；请使用未绑定的发布镜像。')
        if name == HELPER_PATH:
            helper = current_helper()
            if (fields[1] & 0o170000 != 0o100000 or fields[4] > 1 or
                hashlib.sha256(raw[start:start+size]).hexdigest() not in
                    (hashlib.sha256(helper).hexdigest(), LEGACY_HELPER_SHA256)):
                raise ValueError('未知的引导绑定解析器，拒绝修改。')
            updated = bytearray(header)
            updated[54:62] = f'{len(helper):08x}'.encode()
            updated[102:110] = f'{sum(helper) & 0xffffffff if header[:6]==b"070702" else 0:08x}'.encode()
            chunks.append(bytes(updated)+raw[position+110:start]+helper+bytes(-len(helper)%4))
            helper_found = True
        elif name != 'TRAILER!!!':
            chunks.append(raw[position:finish])
        if name == 'TRAILER!!!':
            if size or any(raw[finish:]): raise ValueError('CPIO 含未知的后续归档。')
            if not helper_found: raise ValueError('boot.img 缺少引导绑定解析器。')
            return b''.join(chunks)+_record(TARGET_PATH,binding)+raw[position:finish]
        position=finish
    raise ValueError('CPIO 缺少结束记录。')


def bind(source, destination, target, maximum=192*1024*1024):
    from liuqin_artifacts import boot_interface, bounded_initramfs
    source, destination = Path(source), Path(destination)
    before = boot_interface(source)
    if before.get('multiboot_version') != 1:
        raise ValueError('boot.img 尚不支持分区绑定，请重新构建目标引导镜像。')
    validate_helper(before)
    binding = encode(target)
    data = source.read_bytes()
    u32=lambda offset: struct.unpack_from('<I',data,offset)[0]
    if u32(24) or u32(1632) or struct.unpack_from('<Q',data,1636)[0]:
        raise ValueError('不支持包含 second 或 recovery_dtbo 的引导镜像。')
    align=lambda n:(n+4095)&~4095
    kernel=data[4096:4096+u32(8)]
    ramstart=4096+align(len(kernel))
    ram=data[ramstart:ramstart+u32(16)]
    dtbstart=ramstart+align(len(ram))
    dtb=data[dtbstart:dtbstart+u32(1648)]
    if any(data[dtbstart+len(dtb):]):
        raise ValueError('镜像包含未知的签名或尾部数据，不能重打包。')
    raw=_inject(bounded_initramfs(ram,256*1024*1024),binding)
    if ram.startswith(b'\x1f\x8b'):
        ram=gzip.compress(raw,mtime=0)
    else:
        import zstandard
        ram=zstandard.ZstdCompressor(level=10).compress(raw)
    header=bytearray(data[:4096])
    struct.pack_into('<I',header,16,len(ram))
    digest=hashlib.sha1()
    for blob in (kernel,ram,b'',b'',dtb):
        digest.update(blob);digest.update(struct.pack('<I',len(blob)))
    header[576:608]=digest.digest()+bytes(12)
    result=bytes(header)+kernel+bytes(-len(kernel)%4096)+ram+bytes(-len(ram)%4096)+dtb
    if len(result)>maximum: raise ValueError('绑定后的 boot.img 超过 boot 分区容量。')
    with destination.open('xb') as stream: stream.write(result)
    try:
        after=boot_interface(destination)
        if (after['contract']!=before['contract'] or after['kernel_sha256']!=before['kernel_sha256'] or after.get('boot_target')!=target or
            after.get('boot_target_helper_sha256')!=hashlib.sha256(current_helper()).hexdigest()):
            raise ValueError('重打包后的引导接口不一致。')
    except Exception:
        destination.unlink()
        raise
    return hashlib.sha256(result).hexdigest()
