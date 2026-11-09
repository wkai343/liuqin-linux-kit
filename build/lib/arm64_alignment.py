# SPDX-License-Identifier: MIT
"""Publish a self-relocating ARM64 Image while retaining the original binary.

The published Image, including its entry code, is the identity used by boot
contracts and module packages. Consumers never bypass or normalize its hash.
"""
from pathlib import Path
import hashlib
import struct
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
PROTOCOL = 'liuqin-arm64-align2m-v1'
MAGIC = b'LIUQIN-ALIGN2M-1'
ALIGNMENT = 2 * 1024**2


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inputs():
    return dict(protocol=PROTOCOL,
                helper_sha256=digest(Path(__file__).read_bytes()),
                entry_sha256=digest((PROJECT/'device/boot/arm64-aligned-entry.S').read_bytes()))


def wrap(data, compiler, objcopy):
    if len(data) < 64 or data[56:60] != b'ARM\x64':
        raise ValueError('alignment requires a raw ARM64 Image')
    offset, size, flags = struct.unpack_from('<QQQ', data, 8)
    if offset or flags & 1 or size < len(data) or size+ALIGNMENT > 64*1024**2:
        raise ValueError('ARM64 Image exceeds the validated liuqin load window')
    with tempfile.TemporaryDirectory(prefix='liuqin-align2m-') as directory:
        root=Path(directory);raw=root/'Image';raw.write_bytes(data)
        obj=root/'entry.o';elf=root/'entry.elf';binary=root/'entry.bin'
        subprocess.run([str(compiler),'-c','-DKERNEL_FILE="'+str(raw)+'"',
                        '-DKERNEL_IMAGE_SIZE='+str(size),'-DKERNEL_FLAGS='+str(flags),
                        str(PROJECT/'device/boot/arm64-aligned-entry.S'),'-o',str(obj)],check=True)
        subprocess.run([str(compiler),'-nostdlib','-static','-no-pie',
                        '-Wl,--build-id=none,-Ttext=0,--entry=_start',str(obj),'-o',str(elf)],check=True)
        subprocess.run([str(objcopy),'-O','binary',str(elf),str(binary)],check=True)
        result=binary.read_bytes()
    if (result[256:272] != MAGIC or result[4096:4096+len(data)] != data or
            len(result) != ALIGNMENT+(len(data)+15)//16*16 or
            struct.unpack_from('<Q',result,16)[0] != size+ALIGNMENT):
        raise ValueError('ARM64 relocation entry failed its payload checks')
    return result


def publish(image, compiler, objcopy):
    image=Path(image);original=image.with_name('Image.raw')
    before=inputs();data=image.read_bytes()
    already_wrapped=data[256:272] == MAGIC
    if already_wrapped:
        if not original.is_file():
            raise ValueError('aligned Image lacks its original build artifact')
        raw=original.read_bytes()
    else:
        raw=data
    result=wrap(raw,compiler,objcopy)
    if already_wrapped and result != data:
        raise ValueError('existing aligned Image differs from the exact relocation entry')
    if inputs() != before:
        raise ValueError('ARM64 alignment sources changed during publication')
    original.write_bytes(raw)
    temporary=image.with_name('Image.aligned.tmp');temporary.write_bytes(result);temporary.replace(image)
    return dict(**before,alignment_bytes=ALIGNMENT,raw_image_sha256=digest(raw),
                image_sha256=digest(result),raw_image_bytes=len(raw),image_bytes=len(result))
