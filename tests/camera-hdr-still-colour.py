#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline regressions for the still pipeline. Does not emulate or open the IPE."""
import argparse
from array import array
from pathlib import Path
import runpy
import subprocess
import sys


def p010(values):
    words = array('H', (x << 6 for x in values))
    if sys.byteorder != 'little':
        words.byteswap()
    return words.tobytes()


def inverse(code):
    v = max(0, min(255, round(code))) / 255
    return round(255 * (v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4))


def block(raw, x, y, stride, h):
    k = stride * h + y // 2 * stride + x
    u, v = raw[k] - 128, raw[k + 1] - 128
    ys = [raw[dy * stride + dx] for dy in (y, y + 1) for dx in (x, x + 1)]
    return ([inverse(z - .3457 * u - .7168 * v) for z in ys],
            [(sum(inverse(z + off) for z in ys) + 2) // 4 for off in (1.7793 * u, 1.40723 * v)])


def fixture(photo):
    conv = photo['convert']
    for code, expected in [(34, 4), (71, 16), (137, 64)]:
        # Padding on both planes is deliberately conspicuous.
        nv12 = bytes([code, code, 0, 255] * 2 + [128, 128, 255, 0])
        assert conv('linear', nv12, (2, 2), 4) == bytes([expected] * 6)
    nv12 = bytes([32, 224, 32, 224, 152, 109])
    green, br = block(nv12, 0, 0, 2, 2)
    assert conv('linear', nv12, (2, 2), 2) == bytes(green + br)
    # The moving-region fallback must be in the same light units as the MAC result.
    middle = bytes([16] * 6)
    assert conv('deghost', p010([640] * 6) + middle + b'\xff' * 4, (2, 2), 2) == p010(middle)
    # Real production SDR JPEG path, driven by a known linear MAC output.
    levels = [0, 4, 16, 64, 176, 255, 512, 1023]
    w, h = 16 * len(levels), 16
    packed = p010([v for v in levels for _ in range(16)] * (h + h // 2))
    jpeg = photo['encode_jpeg'](packed, (w, h))
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-frames:v', '1',
                              '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], input=jpeg,
                             stdout=subprocess.PIPE, check=True).stdout
    assert len(decoded) == w * h * 3
    previous = -1
    for i, level in enumerate(levels):
        value = level if level <= 176 else 176 + 79 * (level - 176) / (79 + level - 176)
        light = value / 255
        code = round(255 * (12.92 * light if light <= .0031308 else 1.055 * light ** (1/2.4) - .055))
        pos = 3 * (8 * w + 16 * i + 8)
        rgb = decoded[pos:pos + 3]
        assert max(abs(c - code) for c in rgb) <= 2 and previous < min(rgb), (level, rgb, code)
        previous = max(rgb)
    print('PASS: 34/71/137 -> 4/16/64; stride, colour subsampling, motion fallback, SDR JPEG and highlights')


def ae_fixture(photo):
    select = photo['still_output_gain']
    for sensor, reference, long, want in [('rear', 3.75, 4, 1), ('front', .5, 4, 1),
                                           ('front', 3.75, 4, 3.75), ('front', 9, 3, 3),
                                           ('front', 9, 8, 4)]:
        assert select(dict(sensor=sensor, ae_to_middle=reference, long_to_medium=long)) == want
    # All 1024 linear levels; adjacent pixels in a chroma block have equal RGB.
    w, h = 2048, 2
    ramp = [i // 2 for i in range(w)]
    surface = p010(ramp * 3)
    def render(*gain):
        rgb = photo['convert']('render', surface, (w,h), w, *gain)
        assert rgb[::3] == rgb[1::3] == rgb[2::3]
        return rgb[:3*w:6]
    unity = render()
    assert unity == render(1)
    for gain in (1.5, 3.75, 4.):
        lifted = render(gain)
        assert lifted[0] == 0 and lifted[6] > unity[6] and lifted[100:] == unity[100:]
        assert all(a <= b for a,b in zip(lifted, lifted[1:]))
    print('PASS: AE metadata bounds, monotone neutral ramp, black and bright levels preserved')


def capture(photo, path, align):
    meta = photo['validate'](path)
    modes = (False, True)
    if (meta['width'], meta['height']) == photo['WIDE_SIZES']['3840x2160']:
        try:
            photo['read_bracket'](path, False, False)
        except ValueError as error:
            assert 'native HDR401 exposure profile' in str(error)
        else:
            raise AssertionError('60 Hz bracket accepted by the VGA exposure profile')
        modes = (True,)
    for native in modes:
        prepared, stage, (w, h), mask, gain = photo['read_bracket'](path, native, False)
        assert stage == photo['PROFILES'][meta['sensor']][0] and mask is None
        for frame, pixels in zip(meta['selected_frames'], prepared, strict=True):
            raw = (path / frame['image']).read_bytes()
            stride = meta['stride'] if native else w
            if not native:
                raw = photo['resize_input'](raw, meta)
            assert len(pixels) == w * h * 3 // 2
            for j in range(8):
                for i in range(16):
                    x, y = (w - 2) * i // 15 & ~1, (h - 2) * j // 7 & ~1
                    g, br = block(raw, x, y, stride, h)
                    assert g == [pixels[dy*w+dx] for dy in (y,y+1) for dx in (x,x+1)]
                    k = w * h + y // 2 * w + x
                    assert list(pixels[k:k+2]) == br
        if align:
            registered, same_stage, size, mask, aligned_gain = photo['read_bracket'](path, native, True)
            assert aligned_gain == gain and same_stage == stage and size == (w, h) and len(mask) == w*h
            assert registered[1] == prepared[1] and all(len(p) == len(prepared[1]) for p in registered)
        print(f'PASS: {meta["sensor"]} {w}x{h}, three verified exposures, 128 colour blocks per image'
              + (', registered middle identical' if align else ''))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tools', type=Path, required=True, help='directory containing the built still tools')
    p.add_argument('--capture', action='append', type=Path, default=[], help='existing schema-2 capture; repeatable')
    p.add_argument('--align', action='store_true', help='also process each capture through hdr-motion')
    args = p.parse_args()
    photo = runpy.run_path(str(args.tools / 'camera-hdr-photo.py'))
    fixture(photo)
    ae_fixture(photo)
    for path in args.capture:
        capture(photo, path, args.align)
