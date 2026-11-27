#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare the video conversion library with the scalar hdr-pixels executable."""
import argparse
from array import array
import ctypes
from pathlib import Path
import random
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tools', type=Path, help='directory containing the built HDR tools')
    tools = parser.parse_args().tools.resolve()
    library = ctypes.CDLL(str(tools / 'libliuqin-hdr.so'))
    ptr, integer = ctypes.c_void_p, ctypes.c_int
    library.hdr_linear.argtypes = [ptr, ptr, integer, integer]
    library.hdr_linear.restype = None
    library.hdr_video_output_sized.argtypes = [ptr, ptr, ptr, ptr, integer, integer]
    library.hdr_video_output_sized.restype = None

    def scalar(operation, data, w, h):
        return subprocess.check_output([tools / 'hdr-pixels', operation, str(w), str(h), str(w)], input=data)

    def check_linear(nv12, w, h):
        result = ctypes.create_string_buffer(len(nv12))
        library.hdr_linear(nv12, result, w, h)
        assert result.raw == scalar('linear', nv12, w, h), (w, h, 'linear')

    def check_output(raw, middle, mask, w, h):
        surface = ctypes.create_string_buffer(raw, len(raw))
        result = ctypes.create_string_buffer(len(raw))
        library.hdr_video_output_sized(surface, middle, mask, result, w, h)
        blended = scalar('deghost', raw + middle + mask, w, h)
        assert surface.raw == blended and result.raw == scalar('hlg', blended, w, h), (w, h, 'HLG')

    # Each 128x2 tile exercises every Y code for a single (U,V) pair.
    w = h = 1024
    rows = [bytes(y for y in range(256) if y % 4 // 2 == row) * (w // 128) for row in range(2)]
    luma = b''.join(rows) * (h // 2)
    pairs = w * h // 256
    for start in range(0, 65536, pairs):
        chroma = b''.join(bytes(divmod(pair, 256)) * 64 for pair in range(start, start + pairs))
        check_linear(luma + chroma, w, h)

    rng = random.Random(8127)
    for w, h in [(18, 22), (640, 32)]:
        n = w * h
        middle = rng.randbytes(n * 3 // 2)
        check_linear(middle, w, h)
        # Exercise repeated colours, direct conversion in noisy rows, and mixed motion masks.
        for raw, mask in [(b'\xc0\x23' * (n * 3 // 2), bytes(n)),
                          (rng.randbytes(3 * n), rng.randbytes(n))]:
            check_output(raw, middle, mask, w, h)
    # Each 8x8 tile covers all green codes for one pair of low chroma codes.
    w, h = 512, 8
    n = w * h
    g = array('H', (((x % 8) + 8 * y) << 6 for y in range(h) for x in range(w)))
    for red in range(64):
        row = array('H')
        for blue in range(64):
            row.extend([blue << 6, red << 6] * 4)
        check_output((g + row * (h // 2)).tobytes(), bytes(n * 3 // 2), bytes(n), w, h)
    # Independently vary the four luma samples. This exercises chroma sums
    # near half codes and blocks straddling the lookup cutoff.
    w, h = 514, 256
    n = w * h
    raw = array('H', (rng.randrange(65) << 6 for _ in range(n * 3 // 2)))
    check_output(raw.tobytes(), bytes(n * 3 // 2), bytes(n), w, h)
    # In these blocks rounding the fixed-point sum alone gives the wrong
    # chroma code. Exercise both directions for Cb and Cr.
    for red, blue, green in [(0, 1, [59, 14, 30, 20]), (0, 13, [25, 5, 45, 34]),
                             (1, 48, [49, 58, 16, 42]), (3, 51, [34, 53, 63, 53])]:
        raw = array('H', (value << 6 for value in [*green, blue, red]))
        check_output(raw.tobytes(), bytes(6), bytes(4), 2, 2)
    print('PASS 16,777,216 YUV codes, SIMD tails, motion masks and all 262,144 low-code HLG triplets')


if __name__ == '__main__':
    main()
