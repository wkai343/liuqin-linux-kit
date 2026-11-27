# SPDX-License-Identifier: MIT
"""Check the Titan680 MAC initialization in the actual booted ICP firmware."""
import hashlib
import struct

from elftools.elf.elffile import ELFFile

SHIPPING_SHA = '1dfc57b560935614742fe4dc90e03bc510b9b7bf67f9a01e13740bd71d4a4edc'
REFERENCE_SHA = '8b1f76cce3df570f443af0551c6901fa3cba975d91d9281b2e7c9a33eff065fc'


def data_at(elf, address, size):
    for seg in elf.iter_segments():
        offset = address - seg['p_vaddr']
        if seg['p_type'] == 'PT_LOAD' and 0 <= offset <= seg['p_filesz'] - size:
            return seg.data()[offset:offset + size]
    raise ValueError(f'no loadable data at {address:#x}')


def audit(directory):
    signed, symbols = (directory / ('CAMERA_ICP.' + ext) for ext in ('mbn', 'elf'))
    for path, expected in ((signed, SHIPPING_SHA), (symbols, REFERENCE_SHA)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'unrecognized firmware: {path.name}')
    with signed.open('rb') as mbn, symbols.open('rb') as ref:
        fw, debug = ELFFile(mbn), ELFFile(ref)
        table, = debug.get_section_by_name('.symtab').get_symbol_by_name('ipeInitMfhdrMacLut_48')
        addr, size = struct.unpack('<II', data_at(fw, 0xec0e8, 8))
        assert addr == 0xecb30 and size == table['st_size'] == 512
        data = data_at(fw, addr, size)
        assert data == data_at(debug, table['st_value'], size)
        # Titan680 chip-info selector, single-buffer flag, and register literals.
        assert data_at(fw, 0xe94ec, 4) == struct.pack('<I', 0x38518)
        assert data_at(fw, 0xe946c + 117, 1) == b'\1'
        assert struct.unpack('<2I', data_at(fw, 0xaec74, 8)) == (0x1590c, 0x15910)
        # Xtensa PostPCInitCore: gate on ipeInfo+216 and chipInfo+117, select
        # 1/2, read words from offsets 0/0x100 and write 64 words per bank.
        # Pin the decoded instructions as well as their table and MMIO operands.
        code = data_at(fw, 0xbe0f2, 0xbe298 - 0xbe0f2)
        assert hashlib.sha256(code).hexdigest() == '557796f809916972d6561b527e8553709419f7b3598524f8a5cf0eb9c6d18cff'
        assert data_at(fw, 0xbe984, 11) == bytes.fromhex('01 cf c0 ce 93 bf d1 f7 ff 90 c0')
        error, = struct.unpack('<I', data_at(fw, 0xae7d4, 4))
        label, = struct.unpack('<I', data_at(fw, 0xaecc0, 4))
        assert error == 0xa0007 and data_at(fw, label, 10) == b'timed out\0'
    return dict(firmware_sha256=SHIPPING_SHA, lut_symbol=table.name, lut_address=addr,
                lut_sha256=hashlib.sha256(data).hexdigest(), selector=0x1590c,
                write_data=0x15910, banks=[1, 2], words_per_bank=64, timeout_code=error)
