#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check shipping MFHDR count, exposure map and sensitivity-ratio routing offline."""
import argparse
import importlib.util
import json
from pathlib import Path
import struct


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--capture', type=Path, help='also validate a completed bracket and calculate its OEM routing')
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location('topology', Path(__file__).parent / 'camera-icp/hdr-topology.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    h = m.emu.Hal(args.vendor / 'lib64/hw/camera.qcom.so')
    results = []
    for count in (2, 3):
        for medium, long in ((4., 16.), (16., 256.), (17., 289.), (4., 80.)):
            iq = m.fill(h, count, 1., medium, long)
            get = lambda off: struct.unpack_from('<I', iq, off)[0]
            mapping = tuple(get(o) for o in (0x10fc, 0x1100, 0x1104))
            assert get(0x1094) == count
            assert mapping == ((1, 0, 2) if count == 2 else (1, 3, 2))
            assert (get(0x1108), get(0x110c)) == (int(medium > 16), int(long / medium > 16))
            allowed = {0x1094, 0x10fc, 0x1100, 0x1104, 0x1108, 0x110c}
            assert all(not get(off) for off in range(0, len(iq), 4) if off not in allowed)
            results.append(dict(exposures=count, medium_to_short=medium,
                long_to_medium=long / medium, exposure_map=mapping,
                ratio_gt_16=[get(0x1108), get(0x110c)], hdr_enable=get(0x10c8)))
    obj = h.malloc(0x12000)
    h.u.mem_write(obj + 0x111d8, b'\xa5' * 12)
    assert h.call(0x760b50, obj, 4) == 1297
    assert bytes(h.u.mem_read(obj + 0x111d8, 12)) == b'\xa5' * 12
    result = dict(scope='offline OEM topology oracle only; no HDR enable or DMA submission',
                  hal_sha256=m.emu.SHA, cases=results)
    if args.capture:
        source = Path(__file__).with_name('camera-hdr-validate.py')
        vs = importlib.util.spec_from_file_location('capture_validation', source)
        validation = importlib.util.module_from_spec(vs)
        vs.loader.exec_module(validation)
        capture = validation.validate(args.capture)
        frames = capture['selected_frames']
        sensitivity = [frame['exposure_us'] * frame['analogue_gain'] for frame in frames]
        iq = m.fill(h, 3, *sensitivity)
        get = lambda off: struct.unpack_from('<I', iq, off)[0]
        result['capture'] = capture
        result['capture_topology'] = dict(exposures=get(0x1094),
            exposure_map=[get(off) for off in (0x10fc, 0x1100, 0x1104)],
            ratio_gt_16=[get(0x1108), get(0x110c)], hdr_enable=get(0x10c8))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: 8 OEM MFHDR map/ratio cases; invalid count rejected; HDR enable untouched')


if __name__ == '__main__':
    main()
