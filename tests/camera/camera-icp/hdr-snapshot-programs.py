# SPDX-License-Identifier: MIT
"""Execute the shipping HDR and downscaler CDM writers with bounded local buffers."""
from contextlib import contextmanager
import hashlib
import struct


@contextmanager
def command_buffers(h, emu, capacities):
    """Emulate only the command allocator and record the shipping nested patches."""
    buffers = {cmd: h.malloc(capacity) for cmd, capacity in capacities.items()}
    pending, patches = {}, []

    def command(u, pc, size, user):
        cmd, arg, src, offset = [u.reg_read(emu.UC_ARM64_REG_X0 + i) for i in range(4)]
        result = 0
        if pc == 0x11cac80:  # AddNestedCmdBufferInfo
            patches.append((cmd, arg, src, offset))
        elif pc == 0x11ca8f8:  # BeginCommands
            used = h.r32(cmd + 0x60) * 4
            assert cmd not in pending and 0 < arg * 4 <= capacities[cmd] - used
            pending[cmd] = arg
            result = buffers[cmd] + used
        else:  # CommitCommands
            h.w32(cmd + 0x60, h.r32(cmd + 0x60) + pending.pop(cmd))
        u.reg_write(emu.UC_ARM64_REG_X0, result)
        u.reg_write(emu.UC_ARM64_REG_PC, u.reg_read(emu.UC_ARM64_REG_LR))

    hooks = [h.u.hook_add(emu.UC_HOOK_CODE, command, begin=pc, end=pc)
             for pc in (0x11ca8f8, 0x11caa80, 0x11cac80)]
    try:
        yield buffers, patches
        assert not pending
    finally:
        for hook in hooks:
            h.u.hook_del(hook)


def hdr401_direct(h, emu, titan, lut, iq, blobs=None):
    isp, cmd_array, cmd, args = (h.malloc(size) for size in (8192, 320, 256, 16))
    h.w64(isp + 0x1018, cmd_array)
    h.w64(cmd_array + 5 * 8, cmd)
    header, source, offsets = h.malloc(256), h.malloc(256), h.malloc(8)
    h.w64(cmd_array + 7 * 8, header)
    h.w64(args, source)
    h.w64(args + 8, offsets)
    h.w64(source + 0x10, lut)
    h.w32(source + 0x60, 128)
    h.w32(source + 0x64, 1)
    h.w32(header + 0x74, 1)
    with command_buffers(h, emu, {cmd: 256, header: 24}) as (buffers, patches):
        assert h.call(0xcb4e48, titan, isp, args) == 0
        assert h.r32(cmd + 0x60) == 64 and h.r32(header + 0x60) == 0
        assert h.r64(titan + 0x40) == source and h.r64(titan + 0x48) == offsets and not patches
        direct = bytes(h.u.mem_read(buffers[cmd], 256))
        assert struct.unpack_from('<2I', direct) == (0x0300003e, 0x15964)
        assert direct[8:] == bytes(h.u.mem_read(h.r64(titan + 0x30), 248))
        node, array, frame = h.malloc(0x40000), h.malloc(320), h.malloc(39208)
        h.w32(node + 0xb2d8, 1)
        h.w32(node + 0xb2d8 + 0x2d4, 9)  # MFHDR fusion node profile.
        # HDR401::Create registers two LUTs. With the DMI buffer present the
        # node puts an additional descriptor in the array. Check the program tag.
        h.call(0x762620, node, 0x5b, 2, 0)
        assert h.r32(node + 0xb450) == 2
        mac_lut_calls = []
        def observe_mac_lut(u, pc, size, user):
            mac_lut_calls.append((u.reg_read(emu.UC_ARM64_REG_X3), u.reg_read(emu.UC_ARM64_REG_X4)))
        # The node passes a literal skip tag for the MAC tables. Observe the call
        # before the descriptor helper, independently of the final frame bytes.
        assert h.r32(0x71ad30) == 0x52801fe3  # mov w3, #255
        hook = h.u.hook_add(emu.UC_HOOK_CODE, observe_mac_lut, begin=0x71ad40, end=0x71ad40)
        try:
            assert h.call(0x7197d0, node, frame, iq, array, cmd_array, 1) == 0
        finally:
            h.u.hook_del(hook)
        assert mac_lut_calls == [(255, 4)]
        assert patches == [(0, 4, 0, 0), (0, 32, 0, 35112), (0, 35704, cmd, 0), (0, 35728, header, 0)]
        assert struct.unpack('<8I', h.u.mem_read(frame + 35696, 32)) == (
            0, 2, 0, (len(direct) - 1) << 12, 72, 0, 0, 0)
        assert struct.unpack('<4I', h.u.mem_read(frame + 35728, 16)) == (0, 23 << 12, 255, 0)
        if blobs is not None:
            blobs.update(registers=direct, tuning_lut=bytes(h.u.mem_read(lut, 512)))
    return dict(slot=5, program_type=72, dmi_slot=7, emitted_dmi_bytes=0,
                lut_program_type=255, mac_lut_index=4, frame_patch_offset=35704, cdm_bytes=len(direct),
                cdm_sha256=hashlib.sha256(direct).hexdigest(),
                lut_bytes=512, lut_sha256=hashlib.sha256(h.u.mem_read(lut, 512)).hexdigest())


DSX_TABLE_BYTES = (768, 768, 384, 384)


def dsx_commands(h, emu, titan, tables, cfg, iq, blobs=None, mac=None):
    # FillCmdBufferManagerParams writes ResourceParams including its CmdParams
    # subobject at 0x24. CmdBuffer::Initialize consumes that subobject.
    isp, resource = h.malloc(0x25000), h.malloc(0x38)
    h.u.reg_write(emu.UC_ARM64_REG_X21, isp)
    h.u.reg_write(emu.UC_ARM64_REG_X22, resource)
    h.u.emu_start(0x8a1f4c, 0x8a1f9c, count=64)
    assert h.u.reg_read(emu.UC_ARM64_REG_PC) == 0x8a1f9c
    capacity = h.r32(resource)
    assert capacity == 4 * sum(DSX_TABLE_BYTES)
    backing = h.malloc(capacity)
    info, source, args, offsets = (h.malloc(n) for n in (64, 256, 16, 16))
    h.w32(info, 1)  # Nonzero handle in the offline CSLBufferInfo.
    h.w64(info + 8, backing)
    assert h.call(0x11ca400, source, resource + 0x24, info, 0, capacity) == 0
    assert h.r32(source + 0x64) == 1
    h.w64(args, source)
    h.w64(args + 8, offsets)
    used = 0
    for i, length in enumerate(DSX_TABLE_BYTES):
        h.w32(offsets + i * 4, used)
        h.u.mem_write(backing + used, bytes(h.u.mem_read(h.r64(tables + 8 * i), length)))
        used += length
    h.w32(source + 0x60, used // 4)

    direct, dmi, cmds = h.malloc(256), h.malloc(256), h.malloc(320)
    h.w64(isp + 0x1018, cmds)
    h.w64(cmds + 10 * 8, direct)
    h.w64(cmds + 7 * 8, dmi)
    h.w32(dmi + 0x74, 1)  # Emit a nested pointer per DMI write.
    ranges = [(0x8880, 0x28, 24), (0x8900, 0x88, 24),
              (0x8a80, 0x110, 7), (0x8ac0, 0x12c, 9)]
    per_pass = 4 * sum(words + 2 for _, _, words in ranges)
    with command_buffers(h, emu, {direct: per_pass * 4, dmi: 48}) as (buf, patches):
        assert h.call(0xcacc80, titan, isp, args) == 0
        assert h.r32(direct + 0x60) == per_pass and h.r32(dmi + 0x60) == 12
        payload = h.u.mem_read(buf[direct], per_pass * 4)
        pos = 0
        for p in range(4):
            registers = h.r64(titan + 0x30) + 0x150 * p
            for reg, off, words in ranges:
                assert struct.unpack_from('<2I', payload, pos) == (0x03000000 | words, reg)
                end = pos + (words + 2) * 4
                assert payload[pos+8:end] == h.u.mem_read(registers + off, words * 4)
                pos = end
        headers = h.u.mem_read(buf[dmi], 48)
        for i, length in enumerate(DSX_TABLE_BYTES):
            off = h.r32(offsets + 4 * i)
            assert patches[i] == (dmi, 12 * i + 4, source, off)
            assert struct.unpack_from('<3I', headers, i * 12) == (
                0x01000000 | (length - 1), 0, ((1 + i % 2) << 24) | (0x8808 if i < 2 else 0x8a08))
        assert len(patches) == 4
        # IPEDSX101::Execute records each luma/chroma header offset before
        # CreateCmdList. GetModuleData returns the six offsets and register span.
        module, module_data = h.malloc(1024), h.malloc(64)
        h.w32(dmi + 0x60, 0)
        h.w64(module + 0x1c8, source)
        h.u.reg_write(emu.UC_ARM64_REG_X10, module + 0x1d0)
        h.u.reg_write(emu.UC_ARM64_REG_X8, dmi)
        h.u.reg_write(emu.UC_ARM64_REG_X19, module)
        h.u.emu_start(0x8a2d3c, 0x8a2d98, count=64)
        assert h.u.reg_read(emu.UC_ARM64_REG_PC) == 0x8a2d98
        h.w32(dmi + 0x60, 12)
        h.w32(module + 0x1e4, h.call(0xcae9a8, titan) + 32)
        h.call(0x8a4b78, module, module_data)
        span, *offsets = struct.unpack('<7I', h.u.mem_read(module_data + 0x14, 28))
        assert span == per_pass and offsets == [0, 24] * 3
        node, array, frame, cfg_addr = (h.malloc(n) for n in (0x40000, 320, 39208, 8336))
        h.u.mem_write(cfg_addr, bytes(cfg))
        if struct.unpack_from('<I', cfg, 0x1758)[0] == 4:
            h.w32(node + 0xb2d8, 1)
            h.w32(node + 0xb2d8 + 0x2d4, 9)
        h.w64(node + 0xb2d8 + 0x3550, cfg_addr)
        h.w32(node + 0xb2d8 + 8, 1)  # Titan680 DSX hardware enabled on the node.
        h.w32(node + 0xb2d8 + 0x3478, span)
        h.u.mem_write(node + 0xb49c, struct.pack('<6I', *offsets))
        h.call(0x762620, node, 0x5a, 4, 0)
        if mac is not None:
            mac_cmd = h.malloc(256)
            assert len(mac['registers']) == 256 and len(mac['settings']) == 0x7c
            h.w32(mac_cmd + 0x60, len(mac['registers']) // 4)
            h.w64(cmds + 5 * 8, mac_cmd)
            h.u.mem_write(iq + 0x1094, mac['settings'])
            h.call(0x762620, node, 0x5b, 2, 0)
        assert h.call(0x7197d0, node, frame, iq, array, cmds, 1) == 0
        assert patches[4:6] == [(0, 4, 0, 0), (0, 32, 0, 35112)]
        frame_patches = []
        remaining = patches[6:]
        if mac is not None:
            assert remaining[:2] == [(0, 35704, mac_cmd, 0), (0, 35728, dmi, 0)]
            assert (h.r32(frame + 35712), h.r32(frame + 35736)) == (72, 255)
            frame_patches.extend([(35704, 'mac_registers', 0), (35728, 'dmi', 0)])
            remaining = remaining[2:]
        assert len(remaining) == 9
        for level in range(3):
            group = 35992 + level * 80
            assert h.r32(frame + group + 4) == 3
            for i, (source_cmd, length, typ) in enumerate(
                    [(direct, per_pass, 68), (dmi, 24, 23), (dmi, 24, 24)]):
                off = group + 8 + 24 * i
                _, dest, src, start = remaining[level * 3 + i]
                assert (dest, src) == (off, source_cmd)
                assert 0 <= start <= h.r32(src + 0x60) * 4 - length
                assert h.r32(frame + off + 4) == (length - 1) << 12
                assert h.r32(frame + off + 8) == (typ if level == 0 else 255)
                frame_patches.append((off, 'registers' if src == direct else 'dmi', start))
        if blobs is not None:
            blobs.update(frame=bytes(h.u.mem_read(frame, 39208)), registers=bytes(payload),
                         dmi=bytes(headers), lut=bytes(h.u.mem_read(backing, used)),
                         frame_patches=frame_patches)
    return dict(register_slot=10, register_bytes=len(payload), bytes_per_pass=per_pass,
                frame_patches=frame_patches, register_sha256=hashlib.sha256(payload).hexdigest(), dmi_slot=7,
                dmi_header_bytes=len(headers), lut_bytes=used, lut_sha256=hashlib.sha256(
                    h.u.mem_read(backing, used)).hexdigest(),
                nested_patches=[dict(header_offset=p[1], lut_offset=p[3], length=length)
                                for p, length in zip(patches[:4], DSX_TABLE_BYTES)])
