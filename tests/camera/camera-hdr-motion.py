#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify registration, disocclusion handling and HLG endpoints using known input pixels."""
import argparse
from pathlib import Path
import subprocess
import numpy as np


def check(motion, converter):
    w, h = 640, 480
    rng = np.random.default_rng(596)
    scene = rng.integers(20, 160, (h // 4, w // 4), dtype=np.uint8).repeat(4, 0).repeat(4, 1)
    images = []
    for dx, dy, scale, obj in [(12, -6, .5, 120), (0, 0, 1., 250), (-8, 4, 1.5, 370)]:
        view = scene.copy()
        view[150:218, obj:obj+60] = 239
        view = np.roll(view, (dy, dx), axis=(0, 1))
        y = np.clip(np.rint(view * scale), 0, 255).astype(np.uint8)
        images.append(y.tobytes() + bytes([128]) * (w*h//2))
    result = subprocess.run([motion, str(w), str(h), str(w)], input=b''.join(images),
                            stdout=subprocess.PIPE, check=True).stdout
    size = w*h*3//2
    assert len(result) == 3*size+w*h
    mask = np.frombuffer(result[3*size:], np.uint8).reshape(h, w)
    for n, scale in [(0, .5), (2, 1.5)]:
        plane = np.frombuffer(result[n*size:n*size+w*h], np.uint8).reshape(h,w)
        error = np.abs(plane.astype(float) - np.rint(scene*scale))
        # RANSAC estimates subpixel transforms. Interpolation on a four-pixel
        # checker must still reduce alignment error by at least 90 percent.
        unaligned = np.frombuffer(images[n], np.uint8)[:w*h].reshape(h,w)
        before = np.abs(unaligned.astype(float) - np.rint(scene*scale))
        region = np.s_[260:430, 60:580]
        assert error[region].mean() < .1 * before[region].mean()
    assert np.mean(mask[270:420, 60:580])/255 < .05
    assert np.mean(mask[160:208, 260:300])/255 > .95
    assert np.mean(mask[160:208, 130:170])/255 > .9
    assert np.mean(mask[:, :6])/255 > .95
    # Middle exposure really wins in moving regions of the 10-bit output.
    middle = np.frombuffer(images[1], np.uint8)
    mac = np.full(middle.size, 500 << 6, dtype='<u2')
    args = [str(w), str(h), str(w)]
    raw = subprocess.run([converter, 'deghost', *args], input=mac.tobytes()+images[1]+mask.tobytes(),
                         stdout=subprocess.PIPE, check=True).stdout
    y = np.frombuffer(raw, '<u2')[:w*h].reshape(h,w) >> 6
    assert np.all(y[170:192, 270:290] == 239) and np.median(y[300:330,80:140]) == 500
    for code, expected_y in [(0,64),(1023,940)]:
        mac[:] = code << 6
        encoded = subprocess.run([converter, 'hlg', *args], input=mac.tobytes(),
                                 stdout=subprocess.PIPE, check=True).stdout
        plane = np.frombuffer(encoded, '<u2')
        assert np.all(plane[:w*h]==expected_y) and np.all(plane[w*h:]==512)
    print('PASS: two known translations, moving object, revealed background, borders and HLG range')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--motion', required=True, type=Path)
    p.add_argument('--pixels', required=True, type=Path)
    args = p.parse_args()
    check(args.motion, args.pixels)
