#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded offline interpreter for the SHA-pinned shipping CamX tuning functions.

No native vendor execution, device access or arbitrary PLT-call passthrough.
"""
from pathlib import Path
import hashlib
import struct
from elftools.elf.elffile import ELFFile
from unicorn import *
from unicorn.arm64_const import *
SHA = '91146496f299c62ed2e79b7d7920cb525e8862e716dfbee7a857fffa9f7125d5'

class Hal:

    def __init__(self, binary):
        HAL = Path(binary)
        assert hashlib.sha256(HAL.read_bytes()).hexdigest() == SHA
        self.u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.u.mem_map(0, 0x3100000)
        self.u.mem_map(0x4000000, 0x4000000)
        self.alloc = 0x5000000
        self.blocks = []
        self.hooks = {}
        self.logs = []
        with HAL.open('rb') as f:
            e = ELFFile(f)
            for s in e.iter_segments():
                if s['p_type'] == 'PT_LOAD':
                    self.u.mem_write(s['p_vaddr'], s.data())
            sec = e.get_section_by_name('.rela.dyn')
            for r in sec.iter_relocations() if hasattr(sec, 'iter_relocations') else []:
                if r['r_info_type'] == 1027:
                    self.w64(r['r_offset'], r['r_addend'])
            sec = e.get_section_by_name('.rela.plt')
            sym = e.get_section(sec['sh_link'])
            for i, r in enumerate(sec.iter_relocations()):
                a = 0x3000000 + i * 4
                self.hooks[a] = sym.get_symbol(r['r_info_sym']).name
                self.w64(r['r_offset'], a)
        for a in (0x13dce08, 0x13dce10, 0x13dce18):
            self.w64(a, 0x4f00000)
        self.u.hook_add(UC_HOOK_CODE, self.hook)

    def w32(self, a, v):
        self.u.mem_write(a, struct.pack('<I', v))

    def w64(self, a, v):
        self.u.mem_write(a, struct.pack('<Q', v))

    def r32(self, a):
        return struct.unpack('<I', self.u.mem_read(a, 4))[0]

    def r64(self, a):
        return struct.unpack('<Q', self.u.mem_read(a, 8))[0]

    def cstr(self, a):
        b = bytearray()
        for i in range(65536):
            v = self.u.mem_read(a + i, 1)
            if v == b'\x00':
                return bytes(b)
            b.extend(v)
        raise ValueError('unbounded cstr')

    def malloc(self, n):
        assert 0 <= n <= 0x1000000 and self.alloc + n < 0x7c00000, n
        a = self.alloc
        self.alloc += (n + 255) & ~255
        self.blocks.append((a, n))
        self.u.mem_write(a, bytes(n))
        return a

    def hook(self, u, pc, size, data):
        if pc not in self.hooks:
            return
        n = self.hooks[pc]
        a = [u.reg_read(UC_ARM64_REG_X0 + i) for i in range(8)]
        v = 0
        if n in ('malloc', 'calloc', '_ZnwmiiPKcj', '_ZnamiiPKcj', '_Znwm', '_Znam', '_Znamii', '_Znwmii'):
            v = self.malloc(a[0] * (a[1] if n == 'calloc' else 1))
        elif n in ('memcpy', 'memmove', '__memcpy_chk'):
            u.mem_write(a[0], bytes(u.mem_read(a[1], a[2])))
            v = a[0]
        elif n in ('memset', '__memset_chk'):
            u.mem_write(a[0], bytes([a[1] & 255]) * a[2])
            v = a[0]
        elif n == 'strlen':
            v = len(self.cstr(a[0]))
        elif n in ('strcmp', 'strncmp', '_ZN4CamX7OsUtils8StrNICmpEPKcS2_m'):
            x, y = (self.cstr(a[0]), self.cstr(a[1]))
            if n != 'strcmp':
                x, y = (x[:a[2]], y[:a[2]])
            if 'StrNICmp' in n:
                x, y = (x.lower(), y.lower())
            v = (x > y) - (x < y)
        elif n == 'memcmp':
            x, y = (bytes(u.mem_read(a[0], a[2])), bytes(u.mem_read(a[1], a[2])))
            v = (x > y) - (x < y)
        elif n in ('strlcpy', '_ZN4CamX7OsUtils7StrLCpyEPcPKcm'):
            s = self.cstr(a[1])
            v = len(s)
            if a[2]:
                u.mem_write(a[0], s[:a[2] - 1] + b'\x00')
        elif n in ('free', '_ZdlPv', '_ZdaPv', '_ZdlPvii', '_ZdaPvii', 'atrace_get_enabled_tags', 'atrace_begin_body', 'atrace_end_body', '__cxa_finalize') or n.startswith('_ZN4CamX3Log'):
            self.logs.append(n)
        else:
            raise ValueError(('unhandled PLT', n, hex(u.reg_read(UC_ARM64_REG_LR)), a))
        u.reg_write(UC_ARM64_REG_X0, v & ((1 << 64) - 1))
        u.reg_write(UC_ARM64_REG_PC, u.reg_read(UC_ARM64_REG_LR))

    def call(self, pc, *args):
        u = self.u
        u.reg_write(UC_ARM64_REG_SP, 0x7f00000)
        u.reg_write(UC_ARM64_REG_LR, 0x7fff000)
        u.reg_write(UC_ARM64_REG_TPIDR_EL0, 0x7e00000)
        for i, a in enumerate(args):
            u.reg_write(UC_ARM64_REG_X0 + i, a)
        u.emu_start(pc, 0x7fff000, count=20000000)
        assert u.reg_read(UC_ARM64_REG_PC) == 0x7fff000, hex(u.reg_read(UC_ARM64_REG_PC))
        return u.reg_read(UC_ARM64_REG_X0)
