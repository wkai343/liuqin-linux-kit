#!/usr/bin/env python3
"""Generate private, bounded IPE video profiles from the user's shipping HAL.

Only identity NV12 at four fixed geometries. No TF/HDR, arbitrary addresses,
register uploads, or native execution of OEM binaries. Keep output under out/.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location('fixture',
        Path(__file__).with_name('make-frame-fixture.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    hal = args.vendor / 'lib64/hw/camera.qcom.so'
    lib = args.vendor / 'lib64/libipebpsstriping.so'
    m.checked(hal, m.HAL_SHA)
    m.checked(lib, m.STRIPING_SHA)
    template = m.hal_frame(hal)
    frame = bytearray(len(template))
    for start, end in ((36, 168), (1180, 1188), (35112, len(template))):
        frame[start:end] = template[start:end]
    for off, value in ((0, 0x820000), (4, 0x810000), (16, 0x880000),
                       (20, 524288), (24, 1), (28, 33333333),
                       (32, 0x804000 + 35112), (184, 1), (444, 0x13ef9000)):
        struct.pack_into('<I', frame, off, value)
    blobs = {'video_frame': bytes(frame)}
    profiles = []
    for width, height in ((640, 480), (2592, 1472), (2592, 1952), (4080, 3060)):
        a, iq = m.striping(lib, 0x2400000, width, height)
        b, iq2 = m.striping(lib, 0x2700000, width, height)
        assert iq == iq2
        pointers = {640: ((0x8ff8, 0x9000), (0x94e0, 0x94f8)),
                    2592: ((0xda80, 0xda88), (0xdf68, 0xdf80)),
                    4080: ((0x11cc0, 0x11cc8), (0x121a8, 0x121c0))}[width]
        assert 0x820000 + len(a) <= 0x880000
        assert [o for o in range(0, len(a), 4) if a[o:o+4] != b[o:o+4]] == [p[0] for p in pointers]
        stripe = bytearray(a)
        for off, target in pointers:
            assert struct.unpack_from('<Q', a, off)[0] == 0x2400000 + target
            assert struct.unpack_from('<Q', b, off)[0] == 0x2700000 + target
            assert 0 <= target < len(stripe)
            struct.pack_into('<Q', stripe, off, 0x820000 + target)
        name = f'{width}_{height}'
        blobs['video_iq_' + name] = iq
        blobs['video_stripes_' + name] = bytes(stripe)
        profiles.append((width, height, name))
    args.output.mkdir(parents=True, exist_ok=False)
    header = '/* Private OEM-derived data; do not redistribute. */\n'
    manifest = {'hal_sha256': m.HAL_SHA, 'striping_sha256': m.STRIPING_SHA, 'files': {}}
    for name, blob in blobs.items():
        filename = name + '.bin'
        (args.output / filename).write_bytes(blob)
        manifest['files'][filename] = [len(blob), hashlib.sha256(blob).hexdigest()]
        header += 'static const u8 ' + name + '[] = {\n'
        header += '\n'.join(','.join('0x%02x' % v for v in blob[i:i+24]) + ','
                            for i in range(0, len(blob), 24)) + '\n};\n'
    header += 'static const struct ipe_profile video_profiles[] = {\n'
    for width, height, name in profiles:
        header += f' {{ {width}, {height}, video_iq_{name}, video_stripes_{name}, sizeof(video_stripes_{name}) }},\n'
    header += '};\n'
    (args.output / 'video-fixture.h').write_text(header)
    manifest['header_sha256'] = hashlib.sha256(header.encode()).hexdigest()
    (args.output / 'video-fixture.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
