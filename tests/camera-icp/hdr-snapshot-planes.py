# SPDX-License-Identifier: MIT
"""Decode internal snapshot image planes with the shipping ImageFormatUtils."""
import struct

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
from unicorn.arm64_const import UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_TPIDR_EL0, UC_ARM64_REG_X0

IMAGE_FORMAT_SHA = 'bf46e27569015822a236a62a118528501813f7224a8c245559f0c48993911d31'
TP10, PD10 = 12, 18


def internal_planes(library, fmt, width, height):
    """Return byte geometry and offsets within one contiguous image allocation."""
    if fmt not in (TP10, PD10) or min(width, height) <= 0 or width % 2 or height % 2:
        raise ValueError('unsupported internal format or dimensions')
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    u.mem_map(0x1000, 0x29000)
    u.mem_map(0x100000, 0x20000)
    with library.open('rb') as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            if segment['p_type'] == 'PT_LOAD':
                addr = segment['p_vaddr']
                skip = max(0, 0x1000 - addr)
                u.mem_write(addr + skip, segment.data()[skip:])
        rel = elf.get_section_by_name('.rela.plt')
        symbols = elf.get_section(rel['sh_link'])
        for entry in rel.iter_relocations():
            symbol = symbols.get_symbol(entry['r_info_sym'])
            if symbol.name == '_ZN4CamX16ImageFormatUtils12GetPlaneSizeEPKNS_11ImageFormatEj':
                u.mem_write(entry['r_offset'], struct.pack('<Q', symbol['st_value']))
    # Only log globals and the embedded GetPlaneSize routine are used here.
    u.mem_write(0x17688, struct.pack('<QQ', 0x105000, 0x106000))
    u.mem_write(0x106000, struct.pack('<I', 1))

    def invoke(pc, *args):
        for i, value in enumerate(args):
            u.reg_write(UC_ARM64_REG_X0 + i, value)
        u.reg_write(UC_ARM64_REG_SP, 0x11e000)
        u.reg_write(UC_ARM64_REG_TPIDR_EL0, 0x110000)
        u.reg_write(UC_ARM64_REG_LR, 0x11f000)
        u.emu_start(pc, 0x11f000, count=100000)
        if u.reg_read(UC_ARM64_REG_PC) != 0x11f000:
            raise ValueError('ImageFormatUtils did not return')
        return u.reg_read(UC_ARM64_REG_X0)

    image = 0x101000
    u.mem_write(image, struct.pack('<4I', width, height, 0, fmt))
    u.mem_write(image + 0xb8, struct.pack('<I', 4096))
    if fmt == TP10:
        for offset, lines in [(0x18, height), (0x40, height // 2)]:
            u.mem_write(image + offset, struct.pack('<II', width, lines))
        invoke(0x11cc0, image)  # SetupUBWCPlanes
    else:
        invoke(0x12628, 0, image, 0x102000)  # GetPlaneAlignment (PD10)
    result, offset = [], 0
    for plane in range(2 if fmt == TP10 else 1):
        stride, rows, ms, mr, metadata = struct.unpack('<5I', u.mem_read(image + 0x20 + 40 * plane, 20))
        length = invoke(0x10fd8, image, plane)
        if not stride or not rows or metadata < ms * mr or length < metadata + stride * rows:
            raise ValueError(('inconsistent internal plane geometry', fmt, width, height, plane,
                              stride, rows, ms, mr, metadata, length))
        result.append(dict(stride=stride, rows=rows, meta_stride=ms, meta_rows=mr,
                           metadata_bytes=metadata, plane_bytes=length, offset=offset,
                           pixels_offset=offset + metadata))
        offset += length
    return result


def descriptor(h, fmt, fw, width, height, layout, port):
    """Run the shipping SetConfigIOData writer up to the end of the descriptor."""
    from unicorn import UC_HOOK_CODE

    obj, cfg, image, tree, label = (h.malloc(n) for n in (0x20000, 8336, 256, 64, 16))
    h.w64(obj + 0x1dd40, tree)
    h.w32(tree + 28, port)
    h.u.mem_write(image, struct.pack('<4I', width, height, 0, fmt))
    h.u.mem_write(label, b'Image\0')
    # Internally allocated lossless UBWC 4 surfaces. Encoded UBWC_VERSION_4 is 2.
    if fmt == TP10:
        h.w32(image + 0xe0, 2)
    for i, p in enumerate(layout):
        h.u.mem_write(image + 0x20 + 40 * i, struct.pack('<4I', p['stride'], p['rows'],
                                                         p['meta_stride'], p['meta_rows']))
    responses = {0x13a6330: len(layout), 0x13a6450: int(fmt == TP10), 0x13a6460: int(fmt == TP10)}
    def finish(u, pc, size, data):
        if pc == 0x726240:  # Skip only the subsequent host container bookkeeping.
            u.reg_write(UC_ARM64_REG_PC, 0x7fff000)
        else:
            u.reg_write(UC_ARM64_REG_X0, responses[pc])
            u.reg_write(UC_ARM64_REG_PC, u.reg_read(UC_ARM64_REG_LR))
    hooks = [h.u.hook_add(UC_HOOK_CODE, finish, begin=pc, end=pc) for pc in (*responses, 0x726240)]
    try:
        h.call(0x725798, obj, cfg, image, port, label, 0)
    finally:
        for hook in hooks:
            h.u.hook_del(hook)
    packed = bytes(h.u.mem_read(cfg + 96 * port, 96))
    assert struct.unpack_from('<3I', packed) == (fw, width, height)
    for i, p in enumerate(layout):
        assert struct.unpack_from('<2I', packed, 64 + 8*i) == (p['stride'], p['rows'])
        assert struct.unpack_from('<2I', packed, 80 + 8*i) == (p['meta_stride'], p['meta_rows'])
    return packed
