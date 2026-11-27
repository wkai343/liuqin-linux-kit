#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the real FFmpeg V4L2 copy helpers under ASan/UBSan, without a device."""
import argparse
from pathlib import Path
import shlex
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True, type=Path,
                    help='configured FFmpeg source with libavutil/libavutil.a and libavcodec/packet.o built')
parser.add_argument('--cc', default='clang')
parser.add_argument('--drm-include', type=Path,
                    help='libdrm header directory; otherwise use pkg-config libdrm')
args = parser.parse_args()
source = args.source.resolve()
drm_flags = (['-I', str(args.drm_include.resolve())] if args.drm_include else
             shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'libdrm'], text=True)))
with tempfile.TemporaryDirectory(prefix='ffmpeg-v4l2-') as directory:
    for name in ('ffmpeg-v4l2', 'ffmpeg-v4l2-events', 'ffmpeg-v4l2-drm'):
        binary = Path(directory) / name
        subprocess.run([args.cc, '-g', '-O1', '-fsanitize=address,undefined',
                        '-ffunction-sections', '-fdata-sections', '-I', str(source), *drm_flags,
                        str(Path(__file__).with_name(name + '.c')),
                        str(source / 'libavcodec/packet.o'),
                        str(source / 'libavutil/libavutil.a'), '-Wl,--gc-sections',
                        '-lm', '-pthread', '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
