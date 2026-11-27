#!/usr/bin/env python3
"""Check current/reference/HDR image address slots against the shipping HAL.

This interprets only the constructor's offset-table block and reference-port
mapping functions. It does not submit frames, validate TF noise parameters or
enable HDR. Private oracle output belongs in out/.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('fixture', here / 'camera-icp/make-frame-fixture.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    hal = a.vendor / 'lib64/hw/camera.qcom.so'
    m.checked(hal, m.HAL_SHA)
    u = m.Uc(m.UC_ARCH_ARM64, m.UC_MODE_ARM)
    u.mem_map(0, 0x3000000)
    u.mem_map(0x4000000, 0x1000000)
    m.load(u, hal)
    u.reg_write(m.UC_ARM64_REG_SP, 0x4f00000)
    # Self-contained offset initialisation, stopping before other constructor work.
    u.emu_start(0x7006f0, 0x7008fc, count=100000)
    assert u.reg_read(m.UC_ARM64_REG_PC) == 0x7008fc
    table = bytes(u.mem_read(0x1451970, 16 * 62 * 16))
    header = here / 'camera-icp/frame-layout.h'
    code = '#include ' + json.dumps(str(header)) + r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
int main(void) {
 for (unsigned b=0;b<16;b++) for (unsigned p=0;p<62;p++)
  for (unsigned m=0;m<2;m++) for (unsigned plane=0;plane<2;plane++) {
   int offset=ipe_buffer_offset(b,p,plane,m);assert(offset>=0 && offset+4<=35112);
   uint32_t word=offset;assert(fwrite(&word,4,1,stdout)==1);
  }
 assert(ipe_buffer_offset(16,0,0,0)==-1);
 assert(ipe_buffer_offset(0,62,0,0)==-1);
 assert(ipe_buffer_offset(0,0,2,0)==-1);
 assert(ipe_buffer_offset(0,0,0,2)==-1);
 assert(ipe_reference_input(4)==-1);
 for(unsigned p=0;p<4;p++)assert(ipe_reference_input(p)==(int)p+4);
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'offsets.c', Path(tmp) / 'offsets'
        c.write_text(code)
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
        assert subprocess.check_output([str(exe)]) == table
    for port in range(4):
        reference = m.call(u, 0x76c938, 0x4fff000, 0x4f00000, 0x4400000, 0x4000000, port)
        assert reference == port + 4
        current = m.call(u, 0x76c8d8, 0x4fff000, 0x4f00000, 0x4400000, 0x4000000, reference)
        assert current == port
    result = dict(scope='address layout oracle only; no hardware submission', hal_sha256=m.HAL_SHA,
                  table_sha256=hashlib.sha256(table).hexdigest(), frame_set_stride=1608,
                  ports={str(p):struct.unpack_from('<4I', table, p * 16) for p in range(17)})
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: 3968 shipping frame address offsets, four reference mappings, bounded header; no TF/HDR submission')


if __name__ == '__main__':
    main()
