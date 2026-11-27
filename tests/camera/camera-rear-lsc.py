#!/usr/bin/env python3
"""Compare actual rear LSC MMIO geometry with the pinned shipping grid helper.

No device access. Also execute the geometry guard when switching sensors.
Requires a C compiler, Unicorn and pyelftools; no per-unit EEPROM is needed.
"""
import argparse
import hashlib
import math
from pathlib import Path
import struct
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path, required=True)
    p.add_argument('--vendor', type=Path, required=True)
    a = p.parse_args()
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_SP,
                                    UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_S0)
    from elftools.elf.elffile import ELFFile
    library = a.vendor / 'lib64/hw/camera.qcom.so'
    assert hashlib.sha256(library.read_bytes()).hexdigest() == (
        '91146496f299c62ed2e79b7d7920cb525e8862e716dfbee7a857fffa9f7125d5')
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(0, 0x3000000)
    u.mem_map(0x4000000, 0x1000000)
    with library.open('rb') as stream:
        for segment in ELFFile(stream).iter_segments():
            if segment['p_type'] == 'PT_LOAD':
                u.mem_write(segment['p_vaddr'], segment.data())

    def hook(uc, pc, size, data):
        if pc == 0x13a6b00:  # logf PLT, used only to select interpolation level
            value, = struct.unpack('<f', struct.pack('<I', uc.reg_read(UC_ARM64_REG_S0)))
            word, = struct.unpack('<I', struct.pack('<f', math.log(value)))
            uc.reg_write(UC_ARM64_REG_S0, word)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    u.hook_add(UC_HOOK_CODE, hook)
    inp, state, enable, reserve, ptr = 0x4000000, 0x4010000, 0x4020000, 0x4030000, 0x4040000
    u.mem_write(inp + 0x24, struct.pack('<2I', 4080, 3060))
    u.mem_write(enable + 4, struct.pack('<I', 1))
    u.reg_write(UC_ARM64_REG_SP, 0x4f00000)
    u.reg_write(UC_ARM64_REG_LR, 0x4fff000)
    for i, value in enumerate([inp, state, enable, reserve, ptr, ptr + 4, ptr + 8, ptr + 12]):
        u.reg_write(UC_ARM64_REG_X0 + i, value)
    u.emu_start(0xd78e00, 0x4fff000, count=100000)
    assert u.reg_read(UC_ARM64_REG_PC) == 0x4fff000
    read = lambda offset: struct.unpack('<I', u.mem_read(state + offset, 4))[0]
    # Values below are derived from the shipping helper's output, not copied
    # from the Linux grid expressions. Front 1952 uses a different valid grid.
    expected = [read(0xdec), read(0xdf8), read(0xdfc), read(0xdf0), read(0xe00),
                read(0xdf4), read(0xe04), read(0xe18), read(0xe1c), read(0xe20)]
    source = (a.kernel / 'drivers/media/platform/qcom/camss/camss-vfe-680-iq.h').read_text()
    writer = 'static void vfe680_lsc_start(' + source.split('static void vfe680_lsc_start(', 1)[1]
    writer = writer.split('static void vfe680_iq_start(', 1)[0]
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
struct vfe680_iq { bool lsc; u32 mesh_width, mesh_height; uint16_t mesh[884]; };
struct vfe_line { struct vfe680_iq *iq; struct {u32 width, height;} fmt[1]; };
struct vfe_device { unsigned char *base; };
static u32 mem[0x6000/4], writes;
static void writel(u32 value, void *address) { *(u32 *)address=value; writes++; }
''' + writer + r'''
int main(void) {
 struct vfe680_iq iq={.lsc=true,.mesh_width=4080,.mesh_height=3060};
 struct vfe_line line={.iq=&iq,.fmt={{4080,3060}}};
 struct vfe_device vfe={(unsigned char *)mem};
 vfe680_lsc_start(&vfe,&line);
 assert(mem[0x4360/4]&1);
 printf("%u %u %u %u %u %u %u %u %u %u\n",
  (mem[0x4360/4]>>12)&3,mem[0x436c/4]&0xffff,mem[0x436c/4]>>16,
  mem[0x4370/4]&0xfff,mem[0x4370/4]>>12,
  mem[0x4374/4]&0xfff,mem[0x4374/4]>>12,
  mem[0x4378/4]&0xffff,mem[0x4378/4]>>16,mem[0x4380/4]);
 for(unsigned mode=0;mode<4;mode++) {
  memset(mem,0x5a,sizeof(mem));writes=0;
  iq.lsc=mode!=0; iq.mesh_width=mode==1?2592:4080;
  iq.mesh_height=mode==2?1952:3060;
  if(mode==3){line.fmt[0].width=iq.mesh_width=2592;line.fmt[0].height=iq.mesh_height=1400;}
  vfe680_lsc_start(&vfe,&line);
  assert(writes==1 && mem[0x4360/4]==0);
 }
}
'''
    with tempfile.TemporaryDirectory() as directory:
        c, exe = Path(directory) / 'lsc.c', Path(directory) / 'lsc'
        c.write_text(code)
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
        actual = list(map(int, subprocess.check_output([str(exe)], text=True).split()))
        assert actual == expected, (actual, expected)
    print('PASS: real rear LSC writer matches shipping grid geometry; disabled/stale/unsupported meshes rejected')


if __name__ == '__main__':
    main()
