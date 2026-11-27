#!/usr/bin/env python3
"""Generate a private, fixed NV12 IPE fixture from the matching shipping binaries.

Requires unicorn and pyelftools. No Android service, device access or native
execution of vendor code. Output belongs in ignored out/, never in a release.
The two-base comparison identifies pointer relocation; packed register values
are not scanned or guessed as addresses. This is not a general striping API.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_PC,
    UC_ARM64_REG_LR, UC_ARM64_REG_SP, UC_ARM64_REG_TPIDR_EL0, UC_ARM64_REG_D0)

HAL_SHA = '91146496f299c62ed2e79b7d7920cb525e8862e716dfbee7a857fffa9f7125d5'
STRIPING_SHA = 'e9abc8bf1cebdb686e562f165e0373d08b636c81f9febd6135a955d87660a97f'


def checked(path, sha):
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValueError('Unsupported shipping binary: ' + str(path))


def load(u, path):
    with path.open('rb') as f:
        for s in ELFFile(f).iter_segments():
            if s['p_type'] == 'PT_LOAD':
                u.mem_write(s['p_vaddr'], s.data())


def call(u, pc, end, stack, tls, *args):
    u.reg_write(UC_ARM64_REG_SP, stack)
    u.reg_write(UC_ARM64_REG_LR, end)
    u.reg_write(UC_ARM64_REG_TPIDR_EL0, tls)
    for i, a in enumerate(args):
        u.reg_write(UC_ARM64_REG_X0 + i, a)
    u.emu_start(pc, end, count=10000000)
    if u.reg_read(UC_ARM64_REG_PC) != end:
        raise ValueError('Interpreter instruction limit reached')
    return u.reg_read(UC_ARM64_REG_X0)


def hal_frame(hal):
    """Run actual HAL CDM-array construction with all optional IQ buffers absent.

    Only the nested-buffer bookkeeping method and log/trace PLT are intercepted;
    offsets, empty array headers and skip program tags come from the HAL itself.
    """
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(0, 0x3000000)
    u.mem_map(0x4000000, 0x1000000)
    load(u, hal)
    patches = []

    def hook(uc, pc, size, data):
        if pc == 0x11cac80:
            patches.append([uc.reg_read(UC_ARM64_REG_X0 + i) for i in range(4)])
        elif pc in (0x13a5920, 0x13a5930, 0x13a5940, 0x13a5960, 0x13a5970):
            pass
        elif 0x13a5800 <= pc < 0x13aa000:
            raise ValueError(f'Unexpected HAL PLT call {pc:#x}')
        else:
            return
        uc.reg_write(UC_ARM64_REG_X0, 0)
        uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    for a in (0x13dce08, 0x13dce10, 0x13dce18):
        u.mem_write(a, struct.pack('<Q', 0x4401000))
    result = call(u, 0x7197d0, 0x4fff000, 0x4f00000, 0x4400000,
        0x4000000, 0x4100000, 0x4200000, 0x4300000, 0x4301000, 1)
    if result != 0 or patches != [[0, 4, 0, 0], [0, 32, 0, 35112]]:
        raise ValueError(('Unexpected HAL CDM construction', result, patches))
    b = bytes(u.mem_read(0x4100000, 35112 + 4096))
    expected = (256, 408, 0, 32, 64, 96, 128, 160, 192, 224)
    assert struct.unpack_from('<10I', b, 36) == expected
    assert struct.unpack_from('<2I', b, 1180) == (816, 848)
    return b


def calculate_stripes(lib, output_base, cfg, iq):
    """Offline Titan680 entry points shared by the fixed profiles and still-photo audit."""
    if output_base not in (0x2400000, 0x2700000) or len(cfg) != 8336 or len(iq) != 6180:
        raise ValueError('unexpected striping buffer layout')
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(0, 0x200000)
    u.mem_map(0x2000000, 0x4000000)
    load(u, lib)
    hooks, alloc = {}, 0x3000000
    with lib.open('rb') as f:
        e = ELFFile(f)
        rel = e.get_section_by_name('.rela.plt')
        sym = e.get_section(rel['sh_link'])
        for i, r in enumerate(rel.iter_relocations()):
            a = 0x180000 + i * 4
            hooks[a] = sym.get_symbol(r['r_info_sym']).name
            u.mem_write(r['r_offset'], struct.pack('<Q', a))

    def hook(uc, pc, size, data):
        nonlocal alloc
        if pc not in hooks:
            return
        name = hooks[pc]
        a = [uc.reg_read(UC_ARM64_REG_X0 + i) for i in range(4)]
        result = 0
        if name in ('malloc', 'calloc'):
            n = a[0] if name == 'malloc' else a[0] * a[1]
            assert n < 0x1000000 and alloc + n < 0x5d00000
            result = alloc
            alloc += (n + 255) & ~255
            uc.mem_write(result, bytes(n))
        elif name == 'memcpy':
            uc.mem_write(a[0], bytes(uc.mem_read(a[1], a[2])))
            result = a[0]
        elif name == 'memset':
            uc.mem_write(a[0], bytes([a[1] & 255]) * a[2])
            result = a[0]
        elif name == 'memcmp':
            x, y = bytes(uc.mem_read(a[0], a[2])), bytes(uc.mem_read(a[1], a[2]))
            result = (x > y) - (x < y)
        elif name == 'ldexp':
            value = struct.unpack('<d', struct.pack('<Q', uc.reg_read(UC_ARM64_REG_D0)))[0]
            exp = a[0] & 0xffffffff
            if exp >= 0x80000000:
                exp -= 0x100000000
            uc.reg_write(UC_ARM64_REG_D0, struct.unpack('<Q', struct.pack('<d', math.ldexp(value, exp)))[0])
        elif name not in ('free', '__android_log_print', 'fopen', 'fclose', 'fprintf',
                          'fwrite', 'printf', 'vsnprintf', '__cxa_finalize'):
            raise ValueError('Unexpected striping PLT call: ' + name)
        uc.reg_write(UC_ARM64_REG_X0, result & ((1 << 64) - 1))
        uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    for endpoint in hooks:
        u.hook_add(UC_HOOK_CODE, hook, begin=endpoint, end=endpoint)
    u.mem_write(0x2000000, bytes(cfg))
    invoke = lambda pc, *a: call(u, pc, 0x1ff000, 0x5f00000, 0x5e00000, *a)
    assert invoke(0x2bca0, 0x2000000, 0, 0x60800, 0x100, 0x2100000, 0x2100008, 0x210000c) == 0
    handle, stripe_size, bl_size = struct.unpack('<QII', u.mem_read(0x2100000, 16))
    if not 0 < stripe_size <= 0x100000 or not 0 < bl_size <= 0x100000:
        raise ValueError('striping output exceeds interpreter workspace')
    u.mem_write(0x2200000, bytes(iq))
    u.mem_write(0x2300000, struct.pack('<5QI', 0x2200000, 0x2200000,
        0x2200000 + 588, 0x2200000, 0x2200000 + 588, 1))
    assert invoke(0x2cac8, handle, 0x2300000, output_base, 0x2500000, 0x2600000) == 0
    pixels = struct.unpack('<I', u.mem_read(0x2500000 + 1360, 4))[0]
    blob = bytes(u.mem_read(output_base, stripe_size))
    assert invoke(0x2de28, 0x2100000) == 0
    return blob, bl_size, pixels


def base_iq(width, height):
    iq = bytearray(6180)
    # IFE crop windows at +24/+612 must remain zero for this default YUV topology.
    for off in (0, 588):
        struct.pack_into('<6I', iq, off, width, height, 0, 0, width, height)
    for off in (52, 68, 640, 656):
        struct.pack_into('<I', iq, off, 1)
    for off in (3588, 3596, 3604, 3612):
        struct.pack_into('<I', iq, off, 255)
    struct.pack_into('<f', iq, 4772, 1.0)
    return iq


def striping(lib, output_base, width=640, height=480, temporal_iq=None):
    if (width, height) not in ((640, 480), (2592, 1472), (2592, 1952), (3840, 2160), (4080, 2296), (4080, 3060)):
        raise ValueError('Unreviewed IPE geometry')
    cfg = bytearray(8336)
    for port in ((0, 4, 8, 10) if temporal_iq is not None else (0, 8)):
        struct.pack_into('<3I', cfg, port * 96, 20, width, height)
        struct.pack_into('<4I', cfg, port * 96 + 64, width, height, width, height // 2)
    struct.pack_into('<2I', cfg, 5964, 65536, 1)
    iq = base_iq(width, height)
    if temporal_iq is not None:
        if len(temporal_iq) != len(iq):
            raise ValueError('Unexpected TF firmware structure size')
        if [struct.unpack_from('<I', temporal_iq, 0x578 + i * 408)[0]
                for i in range(4)] != [1, 0, 0, 0]:
            raise ValueError('Only the reviewed single-pass TF topology is supported')
        iq[0x578:0xbd8] = temporal_iq[0x578:0xbd8]
        iq[0xe20:0xe90] = temporal_iq[0xe20:0xe90]
    blob, bl_size, pixels = calculate_stripes(lib, output_base, cfg, iq)
    assert (len(blob), bl_size) == ({640: (64872, 156544), 2592: (104004, 264576),
                                       3840: (134440, 348544), 4080: (138788, 360576)}[width])
    expected_pixels = {(640, 480): 311040, (2592, 1472): 3897856,
                       (2592, 1952): 5168896, (3840, 2160): 8484480, (4080, 2296): 9588096, (4080, 3060): 12778560}
    if temporal_iq is not None:
        expected_pixels = {(640, 480): 326400, (2592, 1472): 4050944,
                           (2592, 1952): 5371904, (3840, 2160): 8812800, (4080, 2296): 9918720, (4080, 3060): 13219200}
    assert pixels == expected_pixels[width, height]
    return blob, bytes(iq)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    hal = args.vendor / 'lib64/hw/camera.qcom.so'
    lib = args.vendor / 'lib64/libipebpsstriping.so'
    checked(hal, HAL_SHA)
    checked(lib, STRIPING_SHA)
    original = hal_frame(hal)
    a, iq = striping(lib, 0x2400000)
    b, iq2 = striping(lib, 0x2700000)
    assert iq == iq2
    assert [o for o in range(0, len(a), 4) if a[o:o+4] != b[o:o+4]] == [0x8ff8, 0x94e0]
    stripes = bytearray(a)
    for off, target in ((0x8ff8, 0x9000), (0x94e0, 0x94f8)):
        assert struct.unpack_from('<Q', a, off)[0] == 0x2400000 + target
        assert struct.unpack_from('<Q', b, off)[0] == 0x2700000 + target
        struct.pack_into('<Q', stripes, off, 0x820000 + target)
    frame = bytearray(len(original))
    frame[36:168] = original[36:168]
    frame[1180:1188] = original[1180:1188]
    frame[35112:] = original[35112:]
    for off, value in ((0, 0x820000), (4, 0x810000), (16, 0x840000),
        (20, 524288), (24, 1), (28, 33333333), (32, 0x804000 + 35112),
        (168, 1), (184, 1), (188, 0x13e10000), (192, 0x13e5b000),
        (316, 0x13e88000), (320, 0x13ed3000), (444, 0x13ef9000)):
        struct.pack_into('<I', frame, off, value)
    blobs = {'ipe_frame': bytes(frame), 'ipe_iq': iq, 'ipe_stripes': bytes(stripes)}
    args.output.mkdir(parents=True, exist_ok=False)
    header = '/* Generated private fixture; do not redistribute vendor artifacts. */\n'
    manifest = {'schema': 1, 'hal_sha256': HAL_SHA, 'striping_sha256': STRIPING_SHA,
                'width': 640, 'height': 480, 'format': 'NV12', 'files': {}}
    for name, blob in blobs.items():
        filename = name + '.bin'
        (args.output / filename).write_bytes(blob)
        manifest['files'][filename] = {'size': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
        header += 'static const unsigned char ' + name + '[] = {\n'
        header += '\n'.join(','.join('0x%02x' % v for v in blob[i:i+24]) + ','
                            for i in range(0, len(blob), 24)) + '\n};\n'
    (args.output / 'frame-fixture.h').write_text(header)
    manifest['files']['frame-fixture.h'] = {'size': len(header.encode()),
                                          'sha256': hashlib.sha256(header.encode()).hexdigest()}
    (args.output / 'fixture.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('PASS: shipping HAL CDM layout, two-base striping relocation, fixed bounded frame fixture')


if __name__ == '__main__':
    main()
