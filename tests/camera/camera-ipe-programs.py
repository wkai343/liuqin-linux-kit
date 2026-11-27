#!/usr/bin/env python3
"""Check shipping Titan680 TF211/HDR401 command spans without programming hardware.

This verifies command routing and register-buffer boundaries, not tuning values,
CDM bytecode, reference-frame correctness or image quality. Never enable TF/HDR
from the reset defaults used here.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import struct


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location('fixture',
        Path(__file__).parent / 'camera-icp/make-frame-fixture.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    hal = args.vendor / 'lib64/hw/camera.qcom.so'
    m.checked(hal, m.HAL_SHA)
    u = m.Uc(m.UC_ARCH_ARM64, m.UC_MODE_ARM)
    u.mem_map(0, 0x3000000)
    u.mem_map(0x4000000, 0x1000000)
    m.load(u, hal)
    obj, regs, inp, array = 0x4000000, 0x4010000, 0x4100000, 0x4300000
    writes = []

    def hook(uc, pc, size, data):
        if pc == 0x12a4688:  # PacketBuilder::WriteRegRange, intercepted before CDM emission.
            cmd, address, count, source = [uc.reg_read(m.UC_ARM64_REG_X0 + i) for i in range(4)]
            if not (0x4400000 <= cmd < 0x4428000 and (cmd - 0x4400000) % 4096 == 0):
                raise ValueError('Unexpected command-buffer route')
            if not (regs <= source and source + count * 4 <= regs + 4096):
                raise ValueError('Register-buffer overrun')
            writes.append({'slot': (cmd - 0x4400000) // 4096, 'register': address,
                'count': count, 'source_offset': source - regs,
                'words': list(struct.unpack('<' + 'I' * count, uc.mem_read(source, count * 4)))})
        elif pc in (0x13a5920, 0x13a5930, 0x13a5940, 0x13a5960, 0x13a5970):
            pass
        elif 0x13a5800 <= pc < 0x13aa000:
            raise ValueError(f'Unexpected PLT call {pc:#x}')
        else:
            return
        uc.reg_write(m.UC_ARM64_REG_X0, 0)
        uc.reg_write(m.UC_ARM64_REG_PC, uc.reg_read(m.UC_ARM64_REG_LR))

    u.hook_add(m.UC_HOOK_CODE, hook)
    u.mem_write(obj + 48, struct.pack('<Q', regs))
    u.mem_write(inp + 4120, struct.pack('<Q', array))
    for i in range(40):
        u.mem_write(array + i * 8, struct.pack('<Q', 0x4400000 + i * 4096))
    for a in (0x13dce08, 0x13dce10, 0x13dce18):
        u.mem_write(a, struct.pack('<Q', 0x4500000))
    invoke = lambda pc, *a: m.call(u, pc, 0x4fff000, 0x4f00000, 0x4600000, *a)
    assert invoke(0xcc2d48, obj) == 404  # actual GetRegSize, not older TF20's 396.
    invoke(0xcc2208, obj)
    assert invoke(0xcc2428, obj, inp, 0) == 0
    expected = [(9, register, count, offset + pass_index * 404)
                for pass_index in range(4)
                for register, count, offset in ((0x5868, 94, 0), (0x59fc, 7, 376))]
    assert [(r['slot'], r['register'], r['count'], r['source_offset']) for r in writes] == expected
    tf = writes.copy()
    # Use distinct input words to prove the HDR command's source span. These are
    # deliberately not image parameters and must never reach a hardware queue.
    u.mem_write(regs, struct.pack('<1024I', *range(1024)))
    writes.clear()
    assert invoke(0xcb4e48, obj, inp, 0) == 0
    assert len(writes) == 1
    hdr = writes[0]
    assert (hdr['slot'], hdr['register'], hdr['count'], hdr['source_offset']) == (5, 0x15964, 62, 0)
    assert hdr['words'] == list(range(62))
    result = {'scope': 'offline command-span oracle only; no hardware submission',
              'hal_sha256': m.HAL_SHA, 'tf211': tf, 'hdr401': writes}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: TF211 four 404-byte pass buffers (94+7 registers), HDR401 62-register span; no hardware submission')


if __name__ == '__main__':
    main()
