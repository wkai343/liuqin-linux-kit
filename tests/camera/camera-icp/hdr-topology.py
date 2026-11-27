# SPDX-License-Identifier: MIT
"""Offline oracle for the shipping MFHDR exposure topology, not an enable switch."""
import importlib.util
import math
from pathlib import Path
import struct
from unicorn.arm64_const import (UC_ARM64_REG_X8, UC_ARM64_REG_X10,
    UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X23, UC_ARM64_REG_X24,
    UC_ARM64_REG_SP, UC_ARM64_REG_PC)

spec = importlib.util.spec_from_file_location('oem_hal', Path(__file__).with_name('oem-hal.py'))
emu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emu)


def fill(hal, count, short, medium, long):
    """Run SetupMFHDRExposureMap and the exact FillMFHDRIQSettings basic blocks.

    Sensitivities are exposure time times total gain, with the same units for
    every leg. No guessed frame latency or nominal exposure ratios are accepted.
    The routine does not set HDR401 enable, configure DMA or emit commands.
    """
    if count not in (2, 3):
        raise ValueError('only two/three-exposure inputs are reviewed')
    if not all(math.isfinite(x) and x > 0 for x in (short, medium, long)):
        raise ValueError('invalid sensitivity')
    if not short < medium < long:
        raise ValueError('exposures must be strictly ordered')
    h = hal
    node, inp, aec, cycle, iq = (h.malloc(n) for n in (0x20000, 0x3000, 0x100, 4, 6180))
    h.w32(cycle, count)
    h.w64(node + 0x111d0, cycle)
    h.w32(node + 0x11200, 1)
    if h.call(0x760b50, node, count):
        raise ValueError('shipping exposure-map setup rejected inputs')
    h.w64(inp, aec)
    for offset, value in ((0x0c, short), (0x24, long), (0x3c, medium)):
        h.w32(aec + offset, struct.unpack('<I', struct.pack('<f', value))[0])
    registers = ((UC_ARM64_REG_X19, iq), (UC_ARM64_REG_X20, node + 0x9bc8),
                 (UC_ARM64_REG_X23, node), (UC_ARM64_REG_X24, inp),
                 (UC_ARM64_REG_X8, 1), (UC_ARM64_REG_X10, 0),
                 (UC_ARM64_REG_SP, 0x7f00000))
    for reg, value in registers:
        h.u.reg_write(reg, value)
    h.w64(0x7f00000 + 0x58, iq + 0xbf4)
    # Enter after the MFHDR usecase gate. Stop before the enclosing function's
    # epilogue, whose stack was never entered. All calls inside are OEM code
    # or the Hal interpreter's bounded, non-executing log hooks.
    h.u.emu_start(0x7072bc, 0x70750c, count=100000)
    if h.u.reg_read(UC_ARM64_REG_PC) != 0x70750c:
        raise ValueError('bounded MFHDR oracle did not complete')
    return bytes(h.u.mem_read(iq, 6180))


def hdr401_ratios(h, exposures):
    """Run the ratio block of IQSetupTriggerData for three AEC channels in S/M/L order."""
    if len(exposures) != 3 or any(t <= 0 or not math.isfinite(g) or g <= 0 for t, g in exposures):
        raise ValueError('invalid exposure channels')
    isp, aec = h.malloc(0x3000), h.malloc(0x48)
    h.w64(isp, aec)
    for (t, gain), offset in zip(exposures, (0, 0x30, 0x18)):
        h.u.mem_write(aec + offset, struct.pack('<Qff', t * 1000, gain, t * gain))
    h.u.reg_write(emu.UC_ARM64_REG_X19, isp)
    h.u.reg_write(emu.UC_ARM64_REG_X21, isp)
    h.u.reg_write(emu.UC_ARM64_REG_SP, 0x7f00000)
    h.u.emu_start(0x91b99c, 0x91bbbc, count=10000)
    if h.u.reg_read(UC_ARM64_REG_PC) != 0x91bbbc:
        raise ValueError('OEM HDR trigger block did not finish')
    packed = bytes(h.u.mem_read(isp + 0x29c0, 36))
    expected = []
    for channels in ([g for _, g in exposures], [t for t, _ in exposures], [t*g for t, g in exposures]):
        s, m, l = channels
        expected.extend((l/m, m/s, l/s))
    if not all(math.isclose(a, b, rel_tol=3e-6) for a, b in zip(struct.unpack('<9f', packed), expected)):
        raise ValueError('HDR401 ratio order mismatch')
    return packed


def config_topology(h, profile, processing):
    """Execute the ConfigureIPE selection for this CHI node, before any image DMA."""
    stack, node, settings, cfg = (h.malloc(n) for n in (256, 0x20000, 0x2000, 8336))
    h.w64(stack + 0x48, settings)
    h.w64(stack + 0x50, cfg)
    h.w32(settings + 0x19e4, profile)
    h.w32(settings + 0x19ec, processing)
    h.u.reg_write(emu.UC_ARM64_REG_X19, stack)
    h.u.reg_write(emu.UC_ARM64_REG_X21, node)
    h.u.emu_start(0x7179e8, 0x717a34, count=1000)
    if h.u.reg_read(UC_ARM64_REG_PC) != 0x717a34:
        raise ValueError('ConfigureIPE topology selection did not finish')
    return h.r32(cfg + 0x1758)
