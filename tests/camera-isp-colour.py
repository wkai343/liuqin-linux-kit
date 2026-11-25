#!/usr/bin/env python3
"""Check the real CC131 MMIO writer against the pinned shipping Titan680 packer.

Requires a host C compiler, unicorn and pyelftools. No device or MMIO access;
the proprietary library is only interpreted in an isolated emulated address space.
"""
import argparse
import hashlib
from pathlib import Path
import random
import runpy
import struct
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path, required=True)
    p.add_argument('--libcamera', type=Path, required=True)
    p.add_argument('--vendor', type=Path, required=True)
    a = p.parse_args()
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
                                    UC_ARM64_REG_X2, UC_ARM64_REG_X3,
                                    UC_ARM64_REG_SP, UC_ARM64_REG_LR,
                                    UC_ARM64_REG_PC, UC_ARM64_REG_TPIDR_EL0)
    from elftools.elf.elffile import ELFFile

    library = a.vendor / 'lib64/hw/camera.qcom.so'
    assert hashlib.sha256(library.read_bytes()).hexdigest() == (
        '91146496f299c62ed2e79b7d7920cb525e8862e716dfbee7a857fffa9f7125d5')
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(0, 0x3000000)
    with library.open('rb') as f:
        for segment in ELFFile(f).iter_segments():
            if segment['p_type'] == 'PT_LOAD':
                u.mem_write(segment['p_vaddr'], segment.data())
    u.mem_map(0x4000000, 0x1000000)
    obj, regs, inp, state, aux = 0x4000000, 0x4010000, 0x4100000, 0x4200000, 0x4300000
    end = 0x4fff000
    writes = {}

    def hook(uc, pc, size, data):
        if pc != 0x12a4688:  # PacketBuilder::WriteRegRange
            return
        address = uc.reg_read(UC_ARM64_REG_X1)
        count = uc.reg_read(UC_ARM64_REG_X2)
        pointer = uc.reg_read(UC_ARM64_REG_X3)
        writes[address] = list(struct.unpack('<' + 'I' * count, uc.mem_read(pointer, 4 * count)))
        uc.reg_write(UC_ARM64_REG_X0, 0)
        uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)

    def call(pc, *args):
        u.reg_write(UC_ARM64_REG_SP, 0x4f00000)
        u.reg_write(UC_ARM64_REG_TPIDR_EL0, 0x4400000)
        u.reg_write(UC_ARM64_REG_LR, end)
        for i, value in enumerate(args):
            u.reg_write(UC_ARM64_REG_X0 + i, value)
        u.emu_start(pc, end, count=10000)
        assert u.reg_read(UC_ARM64_REG_PC) == end
        assert u.reg_read(UC_ARM64_REG_X0) == 0

    # Compile the actual kernel writer, replacing only writel and its context.
    source = (a.kernel / 'drivers/media/platform/qcom/camss/camss-vfe-680-iq.h').read_text()
    writer = 'static void vfe680_cc_queue(' + source.split('static void vfe680_cc_queue(', 1)[1]
    writer = writer.split('static void vfe680_iq_queue(', 1)[0]
    wrapper = '''#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32;
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
struct vfe_device { unsigned char *base; };
struct vfe680_iq { u16 cc[9]; bool cc_dirty, cc_wide; };
static void writel(u32 v, void *p) { *(u32 *)p = v; }
''' + writer + '''
int main(void) {
    u32 mem[0x6000 / 4] = {0};
    struct vfe_device v = {(unsigned char *)mem};
    struct vfe680_iq iq = {.cc_dirty = true};
    int x, wide;
    if (scanf("%d", &wide) != 1) return 2;
    iq.cc_wide = wide;
    for (int i = 0; i < 9; ++i) {
        if (scanf("%d", &x) != 1) return 2;
        iq.cc[i] = x + (wide ? 4096 : 2048);
    }
    vfe680_cc_queue(&v, &iq);
    printf("%u ", mem[0x5860 / 4]);
    for (int i = 0; i < 10; ++i) printf("%u ", mem[0x5868 / 4 + i]);
    return iq.cc_dirty ? 3 : 0;
}
'''
    rng = random.Random(596)
    matrices = [[1024, 0, 0, 0, 1024, 0, 0, 0, 1024],
                [-2048, 2047, 0, 2047, -2048, 0, 0, 0, 1024]]
    matrices += [[rng.randrange(-2048, 2048) for _ in range(9)] for _ in range(128)]
    vectors = [(m, False) for m in matrices]
    if 'cc_wide' in writer:
        vectors += [(m, True) for m in matrices]
        vectors += [([-4096, 4095, 0, 4095, -4096, 0, 0, 0, 1024], True)]
        vectors += [([rng.randrange(-4096, 4096) for _ in range(9)], True) for _ in range(128)]
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'cc.c' , Path(tmp) / 'cc'
        c.write_text(wrapper)
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-O2', str(c), '-o', str(exe)], check=True)
        for matrix, wide in vectors:
            u.mem_write(regs, bytes(44))
            u.mem_write(obj + 0x30, struct.pack('<Q', regs))
            u.mem_write(inp + 0x50, struct.pack('<Q', aux))
            # The common IQ struct uses GBR order; the public matrix is RGB.
            shift = int(wide and any(x < -2048 or x > 2047 for x in matrix))
            fields = [int(matrix[i] / (1 << shift)) for i in (4, 5, 3, 7, 8, 6, 1, 2, 0)]
            u.mem_write(state, struct.pack('<14h', 1, *fields, 0, 0, 0, 3 - shift))
            writes.clear()
            call(0xc6c5a8, obj, state, aux)
            call(0xc6c240, obj, inp, aux)
            actual = subprocess.check_output([str(exe)], input=' '.join(map(str, [int(wide), *matrix])), text=True)
            assert list(map(int, actual.split())) == writes[0x5860] + writes[0x5868]

    decode = runpy.run_path(str(Path(__file__).with_name('camera-oem.py')))['decode']
    tuned = decode(a.vendor / 'lib64/camera/com.qti.tuned.liuqin_aac_imx596_front.bin',
                   'd7f013bef1c515e57f8c8d6d12488e0dc8018794792223d8ace2bf6e83b5bd08')
    # IFE stock CCM is identity. The useful baseline is the IPE D50 branch.
    assert struct.unpack('<9f3i', tuned[1127][1]) == (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)
    lo, hi, _, _, _, region = struct.unpack_from('<ff4I', tuned[1574][1], 3 * 24)
    assert (lo, hi) == (4500, 5500)
    import json
    header = a.libcamera / 'src/libcamera/pipeline/simple/liuqin_colour.h'
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'matrix.cpp', Path(tmp) / 'matrix'
        c.write_text('#include ' + json.dumps(str(header.resolve())) +
                     '\n#include <iostream>\n#include <iomanip>\nint main() {'
                     'std::cout << std::setprecision(10);'
                     'for (float v : libcamera::liuqinColour::matrix(5000)) std::cout << v << " ";}\n')
        subprocess.run(['c++', '-std=c++17', '-O2', str(c), '-o', str(exe)], check=True)
        values = list(map(float, subprocess.check_output([str(exe)], text=True).split()))
    expected = struct.unpack('<9f3i', tuned[region][1])
    assert expected[9:] == (0, 0, 0)
    assert len(values) == 9 and all(abs(x-y) < 1e-6 for x, y in zip(values, expected))
    print(f'PASS: {len(vectors)} CC131 MMIO vectors against OEM; IMX596 IPE D50 parameters match')


if __name__ == '__main__':
    main()
