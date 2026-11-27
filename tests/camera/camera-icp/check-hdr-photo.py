#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the tablet's three-input MAC and measure differences in its 10-bit output."""
import argparse
from array import array
import fcntl
import json
from pathlib import Path
import runpy
import sys

photo = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'camera-hdr-photo.py'))
VGA = photo['WIDTH'], photo['HEIGHT']


def ramp(scale, darken, geometry):
    w, h = geometry
    row = bytes(max(0, min(255, round(15.8 * x / (w - 1) * scale)) - darken) for x in range(w))
    # Each pair spans two columns. Place native detail at the bottom edge and
    # on every horizontal stripe, using a spatial frequency VGA cannot retain.
    bars = bytes(max(0, min(255, (1 if x % 4 < 2 else 3) * scale) - darken) for x in range(w))
    y = row * h if geometry == VGA else row * (h - 16) + bars * 16
    shades = [(128, 128), (110, 148), (150, 105), (128, 128)]
    uv = b''.join(bytes(shades[min(3, 8 * cy // h)]) * (w // 2) for cy in range(h // 2))
    return y + uv


def planes(packed, geometry):
    w, h = geometry
    samples = array('H')
    samples.frombytes(packed)
    if sys.byteorder != 'little':
        samples.byteswap()
    assert not any(word & 63 for word in samples)
    return samples[:w * h], samples[w * h::2], samples[w * h + 1::2]


def check(sensor, native, size=None, recorder=None):
    geometry = {'front': (2592, 1952), 'rear': (4080, 3060)}[sensor] if native else VGA
    if size is not None:
        if sensor != 'rear' or size not in photo['WIDE_SIZES']:
            raise ValueError('unsupported HDR test geometry')
        geometry, native = photo['WIDE_SIZES'][size], True
    w, h = geometry
    scales = (16, 64, 256)
    inputs = [ramp(scale, 0, geometry) for scale in scales]
    profile = photo['PROFILES'][sensor][0]
    def fuse(images):
        # The ramp synthesizes linear exposure values; bypass the camera OETF inverse.
        packed = [photo['convert']('pack', data, geometry, w) for data in images]
        if recorder is not None:
            payload = b''.join(packed)
            if recorder.write(payload) != len(payload):
                raise RuntimeError('short HDR recording write')
            result = recorder.read(w * h * 3)
            if len(result) != w * h * 3:
                raise RuntimeError('short HDR recording read')
            return planes(result, geometry)
        if native:
            return planes(photo['fuse_native'](packed, profile, geometry), geometry)
        return planes(photo['fuse_bracket'](packed, profile), geometry)
    base = fuse(inputs)
    if native:
        for y in (h - 9, h - 3):
            for x in range(4, w - 4, 4):
                if (base[0][y*w+x+2] - base[0][y*w+x]) // 64 < 30:
                    raise ValueError(f'lost two-pixel detail at {x}, {y}')
    # On the neutral row the middle exposure has clipped while the short
    # exposure still distinguishes highlights. Test both gradient and neutrality.
    start = inputs[1][:w].index(255)
    green = base[0][30 * w:(30 + 1) * w]
    grays = {s >> 6 for s in green[start:]}
    assert len(grays) >= 100 and min(grays) >= 245 and max(grays) > 900
    for x in range(start + 1, w, 2):
        cp = 15 * (w // 2) + x // 2
        assert max(abs((green[x] >> 6) - (plane[cp] >> 6)) for plane in base[1:]) <= 12
    changes = {}
    for i, role in enumerate(('short', 'middle', 'long')):
        trial = inputs.copy()
        trial[i] = ramp(scales[i], 35, geometry)
        output = fuse(trial)
        stats = {}
        for ch, orig, changed in zip('GBR', base, output):
            step = 97 if native else 1
            differences = [abs(a - b) >> 6 for a, b in zip(orig[::step], changed[::step])]
            stats[ch] = dict(count=sum(d != 0 for d in differences), peak=max(differences))
        assert stats['G']['count'] > 0, f'{role} exposure has no effect'
        changes[role] = stats
    return dict(sensor=sensor, geometry=geometry, sample_stride=step, middle_clips_from_x=start, green_highlight_levels=len(grays),
                green_highlight_range=[min(grays), max(grays)], changes=changes)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sensor', required=True, choices=['front', 'rear'])
    p.add_argument('--native', action='store_true', help='also check two-pixel detail at native resolution')
    p.add_argument('--size', choices=photo['WIDE_SIZES'], help='test a rear wide HDR geometry')
    p.add_argument('--recording', action='store_true', help='reuse the rear recording fd for all four groups')
    args = p.parse_args()
    if args.recording and (args.sensor != 'rear' or not args.size):
        p.error('--recording requires a rear wide --size')
    with open('/run/lock/liuqin-hdr-photo.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with photo['bundle']['snapshot_module']():
            if args.recording:
                with open('/dev/liuqin-rear-hdr', 'r+b', buffering=0) as recorder:
                    print(json.dumps(check(args.sensor, True, args.size, recorder)))
            else:
                print(json.dumps(check(args.sensor, args.native, args.size)))
