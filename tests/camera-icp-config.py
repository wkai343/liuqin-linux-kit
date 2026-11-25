#!/usr/bin/env python3
"""Compare the actual test-driver NV12 descriptors to the shipping HAL writer.

Requires a host C compiler, unicorn, pyelftools and a privately extracted vendor
image. Interprets descriptor construction only; no Android service or device.
The HFI response shim below is only for extracting the C writer's output.
Real firmware acceptance must be checked separately with selftest level 5.
"""
import argparse
import hashlib
from pathlib import Path
import struct
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor', type=Path, required=True)
    args = parser.parse_args()
    from elftools.elf.elffile import ELFFile
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_PC,
                                    UC_ARM64_REG_LR, UC_ARM64_REG_SP,
                                    UC_ARM64_REG_TPIDR_EL0)
    library = args.vendor / 'lib64/hw/camera.qcom.so'
    assert hashlib.sha256(library.read_bytes()).hexdigest() == (
        '91146496f299c62ed2e79b7d7920cb525e8862e716dfbee7a857fffa9f7125d5')
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(0, 0x3000000)
    with library.open('rb') as f:
        for seg in ELFFile(f).iter_segments():
            if seg['p_type'] == 'PT_LOAD':
                u.mem_write(seg['p_vaddr'], seg.data())
    u.mem_map(0x4000000, 0x1000000)
    obj, cfg, fmt, tree = 0x4000000, 0x4100000, 0x4200000, 0x4300000
    end = 0x4fff000

    def w32(a, v):
        u.mem_write(a, struct.pack('<I', v))

    def w64(a, v):
        u.mem_write(a, struct.pack('<Q', v))

    def hook(uc, pc, size, data):
        if pc == 0x726240:
            # Descriptor complete. Exclude subsequent host container bookkeeping.
            uc.reg_write(UC_ARM64_REG_PC, end)
        elif 0x13a5800 <= pc < 0x13aa000:
            # NV12 has two planes, is neither raw nor UBWC; logging is inert.
            uc.reg_write(UC_ARM64_REG_X0, 2 if pc == 0x13a6330 else 0)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    for a in (0x13dce08, 0x13dce10, 0x13dce18):
        w64(a, 0x4401000)
    w32(0x4401000, 1)
    expected = {}
    geometries = ((640, 480), (2592, 1472), (2592, 1952), (3840, 2160), (4080, 2296), (4080, 3060))
    for width, height in geometries:
        for port in (0, 4, 8, 10):
            u.mem_write(obj, bytes(0x20000))
            u.mem_write(cfg, bytes(0x10000))
            u.mem_write(fmt, bytes(0x200))
            u.mem_write(tree, bytes(0x100))
            w64(obj + 0x1dd40, tree)
            w32(tree + 28, port)
            for off, value in ((0, width), (4, height), (12, 3), (32, width),
                               (36, height), (72, width), (76, height // 2)):
                w32(fmt + off, value)
            u.reg_write(UC_ARM64_REG_SP, 0x4f00000)
            u.reg_write(UC_ARM64_REG_LR, end)
            u.reg_write(UC_ARM64_REG_TPIDR_EL0, 0x4400000)
            for i, value in enumerate((obj, cfg, fmt, port, 0x4402000, 0)):
                u.reg_write(UC_ARM64_REG_X0 + i, value)
            u.emu_start(0x725798, end, count=100000)
            assert u.reg_read(UC_ARM64_REG_PC) == end
            expected[width, height, port] = bytes(u.mem_read(cfg + port * 96, 96))

    code = (Path(__file__).parent / 'camera-icp/qcom-icp-test.c').read_text()
    writer = 'static int config_test(' + code.split('static int config_test(', 1)[1]
    writer = writer.split('static int frame_test(', 1)[0]
    wrapper = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <assert.h>
typedef uint32_t u32;
#define SZ_1M (1024*1024)
#define SZ_64K (64*1024)
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define dma_wmb() ((void)0)
#define dev_info(...) ((void)0)
struct icp { void *heap; u32 handle, scratch_size, cdm_size, width, height, temporal; };
static int send(struct icp *c, const u32 *data, unsigned int n) {
 const u32 expected[] = {40,0x101000e,5,0x596,0,0,0,1,c->handle,0x800000};
 assert(n==10 && memcmp(data,expected,sizeof(expected))==0); return 0;
}
static int receive(struct icp *c, u32 id, u32 *p, unsigned int capacity) {
 (void)c; assert(id==0x102000e && capacity>=46);
 memset(p,0,capacity*4);p[2]=5;p[3]=0x596;return 46;
}
''' + writer + r'''
int main(int argc, char **argv) {
 if (argc != 4) return 3;
 struct icp c = {calloc(7,SZ_1M),0x12345678,0,0,atoi(argv[1]),atoi(argv[2]),atoi(argv[3])}; u32 reply[128];
 if(!c.heap || config_test(&c,reply,128)) return 1;
 if(fwrite(c.heap+6*SZ_1M,1,8344,stdout)!=8344) return 2;
 free(c.heap);return 0;
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        src, exe = Path(tmp) / 'config.c', Path(tmp) / 'config'
        src.write_text(wrapper)
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-O2', str(src), '-o', str(exe)], check=True)
        for width, height in geometries:
            for temporal in (0, 1):
                actual = subprocess.check_output([str(exe), str(width), str(height), str(temporal)])
                assert len(actual) == 8344
                for port in ((0, 4, 8, 10) if temporal else (0, 8)):
                    assert actual[port*96:(port+1)*96] == expected[width, height, port]
                for port in set(range(62)) - ({0, 4, 8, 10} if temporal else {0, 8}):
                    assert actual[port*96:(port+1)*96] == bytes(96)
                assert struct.unpack_from('<5I', actual, 5960) == (0, 65536, 1, 0, 0)
    print('PASS: actual C CONFIG_IO and current/reference NV12 descriptors match shipping HAL at all supported geometries; unused ports remain disabled')


if __name__ == '__main__':
    main()
