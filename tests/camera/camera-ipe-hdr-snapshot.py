#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the stock IPE snapshot graph, internal plane geometry and striping against a captured bracket."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ICP = HERE / 'camera-icp'
tuning = runpy.run_path(str(ICP / 'make-temporal-fixture.py'))
fixture = tuning['fixture']
graph = tuning['module']('snapshot_graph', ICP / 'hdr-snapshot-graph.py')
programs = tuning['module']('snapshot_programs', ICP / 'hdr-snapshot-programs.py')
planes = tuning['module']('snapshot_planes', ICP / 'hdr-snapshot-planes.py')
topology = tuning['module']('hdr_topology', ICP / 'hdr-topology.py')
validate = runpy.run_path(str(HERE / 'camera-hdr-validate.py'))['validate']
firmware_mac = runpy.run_path(str(ICP / 'hdr-firmware.py'))['audit']


def ds4_size(width, height):
    return (width + 7) // 8 * 2, (height + 7) // 8 * 2


def dsx_settings(h, entries, width, height, cfg, blobs=None, mac=None):
    parse = tuning['parse_tuning']
    dsx = parse(h, entries, 65, 'dsx10_ipe_tf_dc4_v2', 1 | 1 << 32, 0x4f25b8)
    ds4 = parse(h, entries, 64, 'ds4to1v11_ipe', 1 | 1 << 16, 0x4efb80)
    dw, dh = ds4_size(width, height)
    inp, unpacked = h.malloc(512), h.malloc(4 * 2904)
    h.w64(inp + 8, dsx + 0x120)
    h.w64(inp + 0x40, h.malloc(4096))
    h.u.mem_write(inp + 0x1c, struct.pack('<4I4fI', width, height, dw, dh, 4., 4., 0., 0., 1))
    h.w32(inp + 0x60, 1)
    region = h.r64(h.r64(dsx + 0x178) + 40)
    assert h.call(0xd50260, inp, region, dsx + 0x16c, ds4 + 0x154, unpacked) == 1
    obj, tables, iq = h.malloc(128), h.malloc(4 * 8), h.malloc(6180)
    h.w64(obj + 0x30, h.malloc(0x150 * 4))
    h.w64(obj + 0x40, h.malloc(0x10000))
    # Titan680 Pack writes four independent tables through this pointer array.
    for index, size in enumerate(programs.DSX_TABLE_BYTES):
        h.w64(tables + index * 8, h.malloc(size))
    assert h.call(0xcad5b0, obj, unpacked, tables) == 0
    assert h.call(0xcb0d60, obj, iq) == 0
    packed = bytes(h.u.mem_read(iq + 0xf94, 256))
    assert struct.unpack_from('<2I', packed, 24) == (dw, dh)
    assert struct.unpack_from('<2I', packed, 56) == (dw // 2, dh // 2)
    return packed, programs.dsx_commands(h, topology.emu, obj, tables, cfg, iq, blobs, mac)


def hdr_settings(h, obj, ratios, exposures, count, enable_hdr, blobs=None):
    node = h.r64(obj + 0x1b8)
    for depth in range(3):
        index, trigger, low, high, children, ident, child, regions, rid, leaf = struct.unpack(
            '<IIffIIQIIQ', h.u.mem_read(node, 48))
        assert index == depth and trigger == (5, 5, 0)[depth]
        if depth < 2:
            assert children == 1
            node = child
        else:
            assert not children and regions == 1 and leaf
    inp, unpacked = h.malloc(256), h.malloc(648)
    h.w64(inp + 8, obj + 0x120)
    h.u.mem_write(inp + 0x20, ratios)
    h.w32(inp + 0x60, count)
    # Enable only a private copy of the OEM enable section for the offline A/B.
    enable = h.malloc(16)
    h.u.mem_write(enable, bytes(h.u.mem_read(obj + 0x124, 16)))
    assert h.r32(enable + 4) == 0
    h.w32(enable + 4, int(enable_hdr))
    assert h.call(0xd59998, inp, leaf, obj + 0x174, enable, unpacked) == 1
    assert h.u.mem_read(unpacked, 1)[0] == int(enable_hdr)
    titan, regs, module_cfg, lut, iq = (h.malloc(n) for n in (128, 248, 4, 512, 6180))
    h.w64(titan + 0x30, regs)
    h.w64(titan + 0x38, module_cfg)
    assert h.call(0xcb5118, titan, unpacked, lut) == 0
    h.u.mem_write(iq, topology.fill(h, count, *(t * g for t, g in exposures)))
    h.w32(iq + 0x10c8, int(enable_hdr))
    assert h.call(0xcb63b0, titan, iq) == 0
    direct = programs.hdr401_direct(h, topology.emu, titan, lut, iq, blobs) if enable_hdr else None
    return bytes(h.u.mem_read(iq + 0x1094, 0x1110 - 0x1094)), direct


def geometry(width, height, stride, library):
    dw, dh = ds4_size(width, height)
    linear = [dict(stride=stride, rows=n, meta_stride=0, meta_rows=0) for n in (height, height // 2)]
    return {3: (20, width, height, linear),
            12: (14, width, height, planes.internal_planes(library, 12, width, height)),
            18: (10, dw, dh, planes.internal_planes(library, 18, dw, dh)),
            22: (19, width, height, [dict(p, stride=stride * 2) for p in linear])}


def config(h, port_formats, formats, properties):
    cfg = bytearray(8336)
    for port, fmt in port_formats.items():
        cfg[port*96:(port+1)*96] = planes.descriptor(h, fmt, *formats[fmt], port)
    struct.pack_into('<2I', cfg, 5964, 65536, 1)
    struct.pack_into('<I', cfg, 0x1758, topology.config_topology(h, *properties))
    return cfg


def check_probe(configurations, formats):
    # Compare every byte written by the fixed kernel probe, including unused ports.
    source = """
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
#include "hdr-config.h"
int main(void) {
    const int stages[] = { SNAP_PRE, SNAP_TWO, SNAP_THREE, SNAP_POST_THREE, SNAP_POST_TWO, SNAP_POST_P010 };
    for (unsigned i = 0; i < sizeof(stages) / sizeof(stages[0]); i++) {
        u32 cfg[8336 / 4] = {0};
        snapshot_config(cfg, stages[i]);
        cfg[5964 / 4] = 65536;
        cfg[5968 / 4] = 1;
        if (fwrite(cfg, 1, sizeof(cfg), stdout) != sizeof(cfg)) return 1;
        for (unsigned port = 0; port < 16; port++) {
            u32 layout[5] = {0};
            layout[0] = snapshot_layout(cfg + 24 * port, layout + 1, layout + 3);
            if (fwrite(layout, 1, sizeof(layout), stdout) != sizeof(layout)) return 1;
        }
    }
    return 0;
}
"""
    with tempfile.TemporaryDirectory() as tmp:
        exe = str(Path(tmp) / 'probe')
        subprocess.run(['cc', '-I', str(ICP), '-x', 'c', '-', '-Wall', '-Werror', '-o', exe],
                       input=source.encode(), check=True)
        actual = subprocess.check_output([exe])
    expected = bytearray()
    by_firmware = {fw: (fmt, layout) for fmt, (fw, _, _, layout) in formats.items()}
    for cfg in configurations:
        expected.extend(cfg)
        for port in range(16):
            fw, = struct.unpack_from('<I', cfg, 96 * port)
            sizes, meta = [0, 0], [0, 0]
            layout = by_firmware[fw][1] if fw else []
            for i, p in enumerate(layout):
                sizes[i] = p.get('plane_bytes', p['stride'] * p['rows'])
                meta[i] = p.get('metadata_bytes', 0)
            expected.extend(struct.pack('<5I', len(layout), *sizes, *meta))
    if actual != expected:
        raise ValueError('fixed probe differs from the OEM descriptor writer')


def stage(lib, cfg, iq):
    bases = (0x2400000, 0x2700000)
    a, b = (fixture.calculate_stripes(lib, base, cfg, iq) for base in bases)
    left, right = a[0], b[0]
    assert len(left) == len(right) and a[1:] == b[1:]
    offsets = []
    for i in range(0, len(left), 4):
        if left[i:i+4] == right[i:i+4]:
            continue
        first, second = (struct.unpack_from('<Q', data, i)[0] - base for data, base in zip((left, right), bases))
        if first != second or not 0 <= first < len(left):
            raise ValueError('non-pointer difference in OEM striping')
        offsets.append((i, first))
    assert offsets and a[2] > 0
    return dict(striping_bytes=len(left), command_buffer_bytes=a[1], pixels=a[2],
                relocations=offsets, striping_sha256=hashlib.sha256(left).hexdigest())


def audit(vendor, capture, native):
    plans = graph.snapshot_graphs(vendor)
    data = validate(capture)
    if not data['captured_digests_verified']:
        raise ValueError('snapshot inputs require schema 2 hashes recorded by the completed capture requests')
    frames = data['selected_frames']
    exposures = [(f['exposure_us'], f['analogue_gain']) for f in frames]
    h, entries = tuning['tuning_file'](vendor, data['sensor'], 4 | 1 << 32)
    # Do not let absent HAL subobjects silently alias the ELF header at address 0.
    h.u.mem_unmap(0, 4096)
    entry = entries + 62 * 224
    cst = bytes(h.u.mem_read(h.r64(entry + 0xd0), 108))
    assert h.cstr(entry + 12) == b'cst12_ipe' and h.r32(entry + 0xc8) == len(cst)
    matrix = runpy.run_path(str(HERE / 'camera-hdr-photo.py'))['CST_GBR']
    assert cst[48:84] == struct.pack('<9f', *matrix)
    assert struct.unpack_from('<6I', cst, 84) == (0, 0, 0, 0, 8192, 8192)
    obj = tuning['parse_tuning'](h, entries, 70, 'hdr40_ipe_v2', 4 | 1 << 32, 0x51bf48)
    ratios = topology.hdr401_ratios(h, exposures)
    library = vendor / 'lib64/libipebpsstriping.so'
    image_lib = vendor / 'lib64/libcamximageformatutils.so'
    fixture.checked(library, fixture.STRIPING_SHA)
    fixture.checked(image_lib, planes.IMAGE_FORMAT_SHA)
    width, height, stride = (data['width'], data['height'], data['stride']) if native else (640, 480, 640)
    formats = geometry(width, height, stride, image_lib)
    pre = config(h, {0: 3, 10: 12, 11: 18}, formats, plans[3]['profile_processing'][5])
    dsx, dsx_cdm = dsx_settings(h, entries, width, height, pre)
    base = fixture.base_iq(width, height)
    scaled = base.copy()
    scaled[0xf94:0x1094] = dsx
    stages = {'preprocess': stage(library, pre, scaled)}
    stages['preprocess']['dsx_commands'] = dsx_cdm
    fusion_configs, post_configs = {}, {}
    bindings = []
    for frame, role in zip(frames, ('short', 'middle', 'long')):
        target = f"TARGET_BUFFER_YUV{frame['generation'] - 1}"
        bindings.append(dict(role=role, target=target, ports=plans[3]['inputs'][target], **frame))
    for count, plan in plans.items():
        port_formats = {p: fmt for pair in plan['inputs'].values() for p, fmt in zip(pair, (12, 18))}
        port_formats.update(zip(plan['fusion_output'], (12, 18)))
        cfg = config(h, port_formats, formats, plan['profile_processing'][count])
        fusion_configs[count] = cfg
        post_ports = {0: 12, 1: 18, **dict.fromkeys(plan['post_outputs'], 3)}
        post_configs[count] = config(h, post_ports, formats, plan['profile_processing'][count + 1])
        stages[f'postprocess_{count}'] = stage(library, post_configs[count], base)
        variants = {}
        for enabled, label in [(False, 'disabled'), (True, 'enabled')]:
            iq = scaled.copy()
            settings, direct = hdr_settings(h, obj, ratios, exposures, count, enabled)
            iq[0x1094:0x1110] = settings
            assert struct.unpack_from('<I', iq, 0x10c8)[0] == int(enabled)
            variants[label] = stage(library, cfg, iq)
            if direct:
                variants[label]['pre_ltm_direct'] = direct
        stages[f'fusion_{count}'] = variants
    assert stages['fusion_3']['enabled']['pixels'] > stages['fusion_2']['enabled']['pixels']
    fw = firmware_mac(vendor / 'firmware')
    assert fw['lut_sha256'] != stages['fusion_3']['enabled']['pre_ltm_direct']['lut_sha256']
    if (width, height) == (640, 480):
        p010 = config(h, {0: 12, 1: 18, 8: 22}, formats, plans[3]['profile_processing'][4])
        check_probe([pre, fusion_configs[2], fusion_configs[3], post_configs[3], post_configs[2], p010], formats)
    return dict(scope='offline geometry only; no DMA or hardware photo', sensor=data['sensor'],
                boot_id=data['boot_id'], captured_digests_verified=data['captured_digests_verified'],
                input_bindings_3=bindings, hdr401_ratios=struct.unpack('<9f', ratios),
                two_exposure_reference='short and long ports; three-channel AEC metadata from the full bracket',
                width=width, height=height, formats=formats, plans=plans, stages=stages, firmware_mac=fw)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', required=True, type=Path)
    p.add_argument('--capture', required=True, type=Path)
    p.add_argument('--native', action='store_true', help='check geometry at the captured sensor resolution')
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    result = audit(args.vendor, args.capture, args.native)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: validated captured frame bindings and OEM snapshot stage geometry', result['sensor'],
          result['width'], result['height'])
