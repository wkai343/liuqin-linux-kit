#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare full-resolution rear JPEGs of a stationary, diffusely lit target."""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def profile(path):
    with Image.open(path) as image:
        if image.format != 'JPEG' or image.size != (4080, 3060):
            raise ValueError(f'{path}: expected a 4080 x 3060 JPEG')
        luma = np.asarray(image.convert('L'))
    # Include the very first and last scanlines. Also test a grid to catch
    # incomplete coverage of a scene whose reflectance varies horizontally.
    rows = luma.mean(axis=1)
    tiles = np.array([[tile.mean() for tile in np.array_split(band, 6, axis=1)]
                      for band in np.array_split(luma, 12, axis=0)]).ravel()
    return np.concatenate((rows, tiles))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--off', type=Path, nargs='+', required=True, help='at least two unlit reference shots')
    parser.add_argument('--flash', type=Path, nargs='+', required=True)
    args = parser.parse_args()
    if len(args.off) < 2:
        parser.error('take at least two reference shots')
    off = np.stack([profile(path) for path in args.off])
    ambient = np.median(off, axis=0)
    threshold = np.maximum(4, np.max(np.abs(off - ambient), axis=0) + 2)
    failed = False
    for path in args.flash:
        gains = profile(path) - ambient
        rows = gains[:3060]
        passed = bool(np.all(gains > threshold))
        print(json.dumps(dict(file=path.name, passed=passed, min_row=round(rows.min(), 2),
                              min_tile=round(gains[3060:].min(), 2),
                              top_middle_bottom=[round(float(x), 2) for x in rows[[0, 1530, -1]]])))
        failed |= not passed
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
