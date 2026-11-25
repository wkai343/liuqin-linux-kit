#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build fixed-profile IPE conversion frames for the still-photo intermediates."""
import argparse
import hashlib
from pathlib import Path
import runpy
import struct

m = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'camera-ipe-hdr-snapshot.py'))
fixture = m['fixture']


def frame_and_stripes(slib, cfg, iq, original):
    desc = m['stage'](slib, cfg, iq)
    stripes = bytearray(fixture.calculate_stripes(slib, 0x2400000, cfg, iq)[0])
    assert len(stripes) <= 0x40000 and desc['command_buffer_bytes'] <= 524288
    for off, offset in desc['relocations']:
        struct.pack_into('<Q', stripes, off, 0x820000 + offset)
    frame = bytearray(39208)
    frame[36:168] = original[36:168]
    frame[1180:1188] = original[1180:1188]
    frame[35112:] = original[35112:]
    # All addresses refer to the existing one-MiB shared mapping. Image planes
    # and firmware-sized scratch are populated independently by snapshot_buffers.
    for off, addr in ((0, 0x820000), (4, 0x810000), (16, 0x880000), (20, 524288),
                      (24, 1), (28, 33333333), (32, 0x804000 + 35112), (168, 1), (184, 1)):
        struct.pack_into('<I', frame, off, addr)
    return frame, stripes


def generate(vendor, width=640, height=480, sensor='front'):
    h, entries = m['tuning']['tuning_file'](vendor, sensor, 4 | 1 << 32)
    h.u.mem_unmap(0, 4096)
    ilib = vendor / 'lib64/libcamximageformatutils.so'
    slib = vendor / 'lib64/libipebpsstriping.so'
    fixture.checked(ilib, m['planes'].IMAGE_FORMAT_SHA)
    fixture.checked(slib, fixture.STRIPING_SHA)
    formats = m['geometry'](width, height, width, ilib)
    graph = m['graph'].snapshot_graphs(vendor)[3]
    cfg = m['config'](h, {0: 3, 10: 12, 11: 18}, formats, graph['profile_processing'][5])
    blobs = {}
    dsx, program = m['dsx_settings'](h, entries, width, height, cfg, blobs)
    iq = fixture.base_iq(width, height)
    iq[0xf94:0x1094] = dsx
    frame, stripes = frame_and_stripes(slib, cfg, iq, blobs['frame'])
    locations = {'registers': 0x860000, 'dmi': 0x861000}
    for off, name, start in blobs['frame_patches']:
        struct.pack_into('<I', frame, off, locations[name] + start)
    dmi = bytearray(blobs['dmi'])
    for patch in program['nested_patches']:
        struct.pack_into('<I', dmi, patch['header_offset'], 0x862000 + patch['lut_offset'])
    post_cfg = m['config'](h, {0: 12, 1: 18, 8: 3}, formats, graph['profile_processing'][4])
    post_iq = fixture.base_iq(width, height)
    post_frame, post_stripes = frame_and_stripes(
        slib, post_cfg, post_iq, fixture.hal_frame(vendor / 'lib64/hw/camera.qcom.so'))
    result = dict(post_frame=post_frame, post_iq=post_iq, post_stripes=post_stripes,
                  pre_frame=frame, pre_iq=iq, pre_stripes=stripes,
                  pre_registers=blobs['registers'], pre_dmi=dmi, pre_lut=blobs['lut'])
    if (width, height) != (640, 480):
        result['pre_cfg'] = cfg
        return result
    # Use the OEM exposure map and both actual CHI port sets. With MAC off
    # only the input order and route matter below the 16x sensitivity threshold.
    for count, plan in m['graph'].snapshot_graphs(vendor).items():
        h, entries = m['tuning']['tuning_file'](vendor, sensor, 4 | 1 << 32)
        h.u.mem_unmap(0, 4096)
        ports = {p: f for pair in plan['inputs'].values() for p, f in zip(pair, (12, 18))}
        ports.update(zip(plan['fusion_output'], (12, 18)))
        fusion_cfg = m['config'](h, ports, formats, plan['profile_processing'][count])
        disabled = m['topology'].fill(h, count, 1, 4, 16)
        fusion_iq = iq.copy()
        fusion_iq[0x1094:0x1110] = disabled[0x1094:0x1110]
        assert struct.unpack_from('<3I', fusion_iq, 0x10fc) == (1, 3 if count == 3 else 0, 2)
        fusion_blobs = {}
        fusion_dsx, _ = m['dsx_settings'](h, entries, width, height, fusion_cfg, fusion_blobs)
        assert fusion_dsx == dsx and all(fusion_blobs[key] == blobs[key] for key in (
            'dmi', 'lut', 'registers', 'frame_patches'))
        fusion_frame, fusion_stripes = frame_and_stripes(slib, fusion_cfg, fusion_iq, fusion_blobs['frame'])
        for off, name, start in fusion_blobs['frame_patches']:
            struct.pack_into('<I', fusion_frame, off, locations[name] + start)
        prefix = 'fusion_two_off_' if count == 2 else 'fusion_off_'
        result.update({prefix + 'frame': fusion_frame, prefix + 'iq': fusion_iq,
                       prefix + 'stripes': fusion_stripes})
    # In MAC bypass the OEM emits identical commands for both counts. The
    # count and pixel bindings remain distinct in the IQ and CONFIG_IO blocks.
    for part in ('frame', 'stripes'):
        assert result.pop('fusion_two_off_' + part) == result['fusion_off_' + part]
    return result


def mac_frame(vendor, capture, baseline, width=640, height=480):
    meta = m['validate'](capture)
    if not meta['captured_digests_verified']:
        raise ValueError('MAC requires exposure and hashes from completed requests')
    exposures = [(f['exposure_us'], f['analogue_gain']) for f in meta['selected_frames']]
    h, entries = m['tuning']['tuning_file'](vendor, meta['sensor'], 4 | 1 << 32)
    h.u.mem_unmap(0, 4096)
    hdr = m['tuning']['parse_tuning'](h, entries, 70, 'hdr40_ipe_v2', 4 | 1 << 32, 0x51bf48)
    mac, blobs = {}, {}
    ratios = m['topology'].hdr401_ratios(h, exposures)
    mac['settings'], _ = m['hdr_settings'](h, hdr, ratios, exposures, 3, True, mac)
    formats = m['geometry'](width, height, width, vendor / 'lib64/libcamximageformatutils.so')
    plan = m['graph'].snapshot_graphs(vendor)[3]
    ports = {p: f for pair in plan['inputs'].values() for p, f in zip(pair, (12, 18))}
    ports.update(zip(plan['fusion_output'], (12, 18)))
    cfg = m['config'](h, ports, formats, plan['profile_processing'][3])
    dsx, _ = m['dsx_settings'](h, entries, width, height, cfg, blobs, mac)
    iq = bytearray(baseline['pre_iq'])
    assert dsx == iq[0xf94:0x1094] and blobs['registers'] == baseline['pre_registers']
    assert blobs['lut'] == baseline['pre_lut']
    iq[0x1094:0x1110] = mac['settings']
    assert struct.unpack_from('<I', iq, 0x10c8)[0] == 1
    frame, stripes = frame_and_stripes(vendor / 'lib64/libipebpsstriping.so', cfg, iq, blobs['frame'])
    addresses = dict(registers=0x860000, dmi=0x861000, mac_registers=0x863000)
    for off, name, start in blobs['frame_patches']:
        struct.pack_into('<I', frame, off, addresses[name] + start)
    # Linear P010 (CamX 22, firmware 19) retains sample precision after MAC.
    post = m['config'](h, {0: 12, 1: 18, 8: 22}, formats, plan['profile_processing'][4])
    # FillIQSettings branches on Is10BitFormat. Port 8 uses the display
    # luma/chroma clamp words at 0xe14/0xe1c, distinct from video port 9.
    p010_iq = bytearray(baseline['post_iq'])
    for offset in (0xe14, 0xe1c):
        assert struct.unpack_from('<I', p010_iq, offset)[0] == 255
        struct.pack_into('<I', p010_iq, offset, 1023)
    p010_frame, p010_stripes = frame_and_stripes(vendor / 'lib64/libipebpsstriping.so', post,
        p010_iq, fixture.hal_frame(vendor / 'lib64/hw/camera.qcom.so'))
    assert p010_frame == baseline['post_frame']
    result = dict(mac_frame=frame, mac_iq=iq, mac_stripes=stripes, mac_registers=mac['registers'],
                p010_stripes=p010_stripes, p010_iq=p010_iq)

    if (width, height) != (640, 480):
        result.update(mac_cfg=cfg, p010_cfg=post)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--capture', type=Path, help='also build the MAC frame for this fixed three-exposure bracket')
    p.add_argument('--native', action='store_true', help='use the full active sensor mode for all IPE stages')
    args = p.parse_args()
    width, height, sensor = 640, 480, 'front'
    if args.native:
        if not args.capture:
            p.error('--native requires --capture')
        meta = m['validate'](args.capture)
        width, height, sensor = meta['width'], meta['height'], meta['sensor']
        if (width, height) not in {'front': {(2592, 1952)}, 'rear': {(4080, 3060), (4080, 2296), (3840, 2160)}}[sensor]:
            p.error('unsupported native capture geometry')
    blobs = generate(args.vendor, width, height, sensor)
    if args.capture:
        blobs.update(mac_frame(args.vendor, args.capture, blobs, width, height))
    args.output.mkdir(parents=True, exist_ok=False)
    for name, data in blobs.items():
        (args.output / (name + '.bin')).write_bytes(data)
        print(name, len(data), hashlib.sha256(data).hexdigest())
