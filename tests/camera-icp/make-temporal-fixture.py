#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build private stock TF21 single-pass NV12 programs for the fixed IPE bridge.

Executes the matching OEM parser, settings conversion, Titan680 packing, CDM
writer and striping library under Unicorn. Uses the first (low-gain) region of
the default video tuning tree; it is not a gain-interpolating CamX replacement.
No hardware access. Derived binary data must remain private under ignored out/.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import struct

HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


emu = module('oem_hal', HERE / 'oem-hal.py')
fixture = module('ipe_frame', HERE / 'make-frame-fixture.py')
SENSORS = {
    'front': ('com.qti.tuned.liuqin_aac_imx596_front.bin',
              'd7f013bef1c515e57f8c8d6d12488e0dc8018794792223d8ace2bf6e83b5bd08'),
    'rear': ('com.qti.tuned.liuqin_qtech_s5kjn1_wide.bin',
             'ab4c7b74b4f4d88c65b2c545994e4c90314be3fee20b2b05042ca7e9114147ab'),
}


def tuning_file(vendor, sensor, version):
    h = emu.Hal(vendor / 'lib64/hw/camera.qcom.so')
    name, sha = SENSORS[sensor]
    rows = runpy.run_path(str(HERE.parent / 'camera-oem.py'))['decode'](
        vendor / 'lib64/camera' / name, sha)
    file, entries = 0x4000000, 0x4010000
    h.w32(file + 0x18, max(rows))
    h.w64(file + 0x28, entries)
    # The rear symbol table is larger than the interpreter's usual heap base.
    # Place raw records after the entire table, and keep the stack separate.
    h.alloc = (entries + (max(rows) + 1) * 224 + 4095) & ~4095
    for ident, (name, raw) in rows.items():
        p = entries + ident * 224
        h.w64(p, file)
        h.w32(p + 8, ident)
        h.u.mem_write(p + 12, name.encode() + b'\0')
        h.w64(p + 0x34, version)
        h.w32(p + 0xc8, len(raw))
        a = h.alloc
        h.alloc += (max(len(raw), 4) + 15) & ~15
        if h.alloc >= 0x7a00000:
            raise ValueError('Chromatix raw data exceeds bounded interpreter heap')
        h.u.mem_write(a, raw or bytes(4))
        h.w64(p + 0xd0, a)
    return h, entries


def parse_tuning(h, entries, index, name, version, parser):
    entry = entries + index * 224
    if h.cstr(entry + 12) != name.encode():
        raise ValueError(f'unexpected tuning module at index {index}')
    cls = h.malloc(256)
    h.u.mem_write(cls + 16, name.encode() + b'\0')
    h.w64(cls + 60, version)
    h.w64(entry + 0x34, version)
    obj = h.call(parser, cls, entry, 4)
    if not obj or h.r64(entry + 0xd8) != h.r32(entry + 0xc8):
        raise ValueError(f'incomplete OEM parse of {name}')
    return obj


def tuning(vendor, sensor):
    version = 2 | (1 << 16) | (1 << 32)
    h, entries = tuning_file(vendor, sensor, version)
    obj = parse_tuning(h, entries, 80, 'tf21_ipe_v2', version, 0x5d4bf8)
    assert h.r32(obj + 0x2d8) == 1
    node = h.r64(obj + 0x2e0)
    for depth in range(8):
        index, trigger, low, high, count, ident, child, regions, rid, leaf = \
            struct.unpack('<IIffIIQIIQ', h.u.mem_read(node, 48))
        assert index == depth and trigger == (8, 9, 14, 13, 2, 5, 1, 6)[depth]
        if count:
            node = child
        else:
            assert depth == 7 and regions == 1 and leaf
            break
    else:
        raise ValueError('Unsupported default TF tuning tree')
    inp, nc = h.malloc(512), h.malloc(4992)
    h.w64(inp + 8, obj + 0x120)
    h.w32(inp + 0x30, 4)
    h.w32(inp + 0x90, 4)
    h.w64(inp + 0x80, nc)
    h.w64(inp + 0xa0, h.malloc(4096))
    h.w64(inp + 0xa8, h.malloc(8192))
    h.w64(inp + 0xb0, h.malloc(8192))
    h.u.mem_write(inp + 0x4e, b'\x02')  # OEM CONFIG_VIDEO, before calculation.
    assert h.call(0xd89aa8, inp, leaf, obj + 0x198, obj + 0x124, 0) == 1
    return h, inp


def pack(vendor, sensor, width, height):
    h, inp = tuning(vendor, sensor)
    obj, regs = h.malloc(256), h.malloc(1616)
    refine, tf, lmc = h.malloc(108), h.malloc(1632), h.malloc(4096)
    h.w64(obj + 0x30, regs)
    for off, ptr in ((0x58, refine), (0x60, tf), (0x68, lmc)):
        h.w64(inp + off, ptr)
    for off, value in ((0x1c, 0x3f800000), (0x20, width), (0x24, height),
                       (0x30, 1), (0x3c, 1), (0x40, 256)):
        h.w32(inp + off, value)
    isp, iq, tags = h.malloc(0x30000), h.malloc(6180), h.malloc(4096)
    internal, offsets = h.malloc(64), h.malloc(16)
    h.w64(isp + 0x1000, iq)
    h.w64(isp + 0xf10, tags)
    h.u.mem_write(tags + 0x70, b'\x02')
    h.w64(internal, isp)
    h.w64(internal + 8, inp)
    h.w64(internal + 24, offsets)
    command_input, array, command = h.malloc(8192), h.malloc(320), h.malloc(256)
    data = h.malloc(2048)
    h.w64(command_input + 4120, array)
    h.w64(array + 9 * 8, command)
    used, pending, spans = 0, 0, []

    def hook(uc, pc, size, context):
        nonlocal used, pending
        if pc == 0x11ca8f8:  # CmdBuffer::BeginCommands: bounded local allocation.
            assert uc.reg_read(emu.UC_ARM64_REG_X0) == command
            pending = uc.reg_read(emu.UC_ARM64_REG_X1) * 4
            assert used + pending <= 2048
            ret = data + used
            spans.append(pending)
        elif pc == 0x11caa80:  # CmdBuffer::CommitCommands.
            assert uc.reg_read(emu.UC_ARM64_REG_X0) == command
            used += pending
            pending, ret = 0, 0
        else:
            return
        uc.reg_write(emu.UC_ARM64_REG_X0, ret)
        uc.reg_write(emu.UC_ARM64_REG_PC, uc.reg_read(emu.UC_ARM64_REG_LR))

    h.u.hook_add(emu.UC_HOOK_CODE, hook)
    result = []
    for reference in (0, 1):
        h.u.mem_write(inp + 0x4c, bytes([reference]))
        assert h.call(0xcc26b0, obj, inp, 0) == 0
        h.u.mem_write(iq, bytes(6180))
        h.u.mem_write(iq + 0xe20, bytes(h.u.mem_read(refine, 108)) + bytes(4))
        assert h.call(0xcc2f50, obj, internal) == 0
        firmware = bytes(h.u.mem_read(iq, 6180))
        assert [h.r32(iq + 0x578 + i * 408) for i in range(4)] == [1, 0, 0, 0]
        assert h.r32(iq + 0x57c) == 1 - reference
        used, pending = 0, 0
        spans.clear()
        assert h.call(0xcc2428, obj, command_input, 0) == 0
        assert used == 1680 and spans == [384, 36] * 4
        cdm = bytes(h.u.mem_read(data, used))
        for i in range(4):
            assert struct.unpack_from('<2I', cdm, i * 420) == (0x0300005e, 0x5868)
            assert struct.unpack_from('<2I', cdm, i * 420 + 384) == (0x03000007, 0x59fc)
        result.append((firmware, cdm))
    return result


def frame_programs(vendor, iq):
    h = emu.Hal(vendor / 'lib64/hw/camera.qcom.so')
    obj, frame, firmware = h.malloc(0x40000), h.malloc(39208), h.malloc(6180)
    iqarray, cmdarray, command = h.malloc(320), h.malloc(320), h.malloc(256)
    h.w32(obj + 0xb2d8 + 0x3474, 420)
    for i in range(4):
        h.w32(obj + 0xb2d8 + 0x3460 + i * 4, i * 420)
    h.w64(cmdarray + 9 * 8, command)
    h.u.mem_write(firmware, iq)
    patches = []

    def hook(uc, pc, size, context):
        if pc != 0x11cac80:
            return
        patches.append([uc.reg_read(emu.UC_ARM64_REG_X0 + i) for i in range(4)])
        uc.reg_write(emu.UC_ARM64_REG_X0, 0)
        uc.reg_write(emu.UC_ARM64_REG_PC, uc.reg_read(emu.UC_ARM64_REG_LR))

    h.u.hook_add(emu.UC_HOOK_CODE, hook)
    assert h.call(0x7197d0, obj, frame, firmware, iqarray, cmdarray, 1) == 0
    assert patches[:2] == [[0, 4, 0, 0], [0, 32, 0, 35112]]
    assert patches[2:] == [[0, 35120 + i * 32, command, max(0, i - 4) * 420]
                           for i in range(8)]
    original = bytes(h.u.mem_read(frame, 39208))
    b = bytearray(39208)
    b[36:168], b[1180:1188], b[35112:] = original[36:168], original[1180:1188], original[35112:]
    for off, value in ((0, 0x820000), (4, 0x810000), (16, 0x880000), (20, 524288),
                       (24, 1), (28, 33333333), (32, 0x804000 + 35112),
                       (168, 1), (184, 1), (444, 0x13ef9000)):
        struct.pack_into('<I', b, off, value)
    for _, off, _, offset in patches[2:]:
        struct.pack_into('<I', b, off, 0x860000 + offset)
    return bytes(b)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    fixture.checked(args.vendor / 'lib64/libipebpsstriping.so', fixture.STRIPING_SHA)
    args.output.mkdir(parents=True, exist_ok=False)
    header = '/* Private stock TF21 single-pass front profiles; not a release fixture. */\n'
    manifest, profiles = {}, []

    def add(name, blob):
        nonlocal header
        (args.output / (name + '.bin')).write_bytes(blob)
        manifest[name] = dict(size=len(blob), sha256=hashlib.sha256(blob).hexdigest())
        header += 'static const unsigned char ' + name + '[] = {\n'
        header += '\n'.join(','.join(str(x) for x in blob[i:i+32]) + ','
                            for i in range(0, len(blob), 32)) + '\n};\n'

    first_iq = []
    for width, height in ((640, 480), (2592, 1472), (2592, 1952), (3840, 2160), (4080, 2296), (4080, 3060)):
        name = f'p{width}_{height}'
        for reference, (iq, cdm) in enumerate(pack(args.vendor, 'rear' if width >= 3840 else 'front', width, height)):
            a, firmware = fixture.striping(args.vendor / 'lib64/libipebpsstriping.so', 0x2400000, width, height, iq)
            b, firmware2 = fixture.striping(args.vendor / 'lib64/libipebpsstriping.so', 0x2700000, width, height, iq)
            assert firmware == firmware2
            locations = {640: (0x8ff8, 0x94e0), 2592: (0xda80, 0xdf68), 3840: (0x11478, 0x11960), 4080: (0x11cc0, 0x121a8)}[width]
            assert tuple(i for i in range(0, len(a), 4) if a[i:i+4] != b[i:i+4]) == locations
            stripes = bytearray(a)
            for off in locations:
                target = struct.unpack_from('<Q', a, off)[0] - 0x2400000
                assert 0 <= target < len(a)
                assert struct.unpack_from('<Q', b, off)[0] == 0x2700000 + target
                struct.pack_into('<Q', stripes, off, 0x820000 + target)
            assert 0x820000 + len(stripes) < 0x860000
            for kind, blob in (('iq', firmware), ('stripes', stripes), ('cdm', cdm)):
                add(f'{name}_{kind}_{reference}', blob)
            if width == 640:
                first_iq.append(firmware)
        profiles.append((width, height, name))
    for reference, iq in enumerate(first_iq):
        add(f'tf_frame_{reference}', frame_programs(args.vendor, iq))
    header += 'static const struct ipe_profile video_profiles[] = {\n'
    for width, height, n in profiles:
        header += f'{{{width},{height},{n}_iq_0,{n}_stripes_0,sizeof({n}_stripes_0),{n}_iq_1,{n}_stripes_1,{n}_cdm_0,{n}_cdm_1}},\n'
    header += '};\n'
    (args.output / 'video-fixture.h').write_text(header)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('PASS: sensor-specific stock TF21 calculation, first/reference frames, CDM, two-base striping; no hardware submission')


if __name__ == '__main__':
    main()
