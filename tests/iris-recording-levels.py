#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the Iris H.264 bitstream with the caps used by desktop recorders."""
import json
from pathlib import Path
import re
import subprocess
import tempfile


def check(output, width, height, fps, *expected_levels, requested_level=None):
    raw = f'video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1,colorimetry=bt709'
    pipeline = ['gst-launch-1.0', '-qe', 'videotestsrc', 'num-buffers=8', 'pattern=ball',
                '!', raw, '!', 'v4l2h264enc']
    if requested_level:
        pipeline.extend(['!', f'video/x-h264,level=(string){requested_level}'])
    pipeline.extend(['!', 'h264parse', '!', 'video/x-h264,stream-format=byte-stream,alignment=au',
                     '!', 'filesink', f'location={output}'])
    subprocess.run(pipeline, check=True, timeout=30)

    # Check the actual sequence headers. Negotiated caps can predate S_FMT.
    nals = re.split(rb'\x00\x00(?:\x00)?\x01', output.read_bytes())
    levels = {nal[3] for nal in nals if len(nal) >= 4 and (nal[0] & 31) == 7}
    stream, = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'fatal', '-count_packets', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,nb_read_packets', '-of', 'json', output,
    ], timeout=30))['streams']
    if len(levels) != 1 or not levels.issubset(expected_levels):
        raise RuntimeError(f'{width}x{height}@{fps}: SPS levels {levels}, expected {expected_levels}')
    if (stream['width'], stream['height'], int(stream['nb_read_packets'])) != (width, height, 8):
        raise RuntimeError(f'{width}x{height}@{fps}: SPS {levels}, stream {stream}')
    print(f'{width}x{height}@{fps} level_idc={levels.pop()}, 8 frames')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / 'test.h264'
        check(output, 1280, 720, 30, 31)
        check(output, 1280, 720, 30, 52, requested_level='5.2')
        # When S_FMT precedes S_PARM, the initial 30 fps selects 5.1.
        # Both 5.0 and the retained 5.1 cover the final 15 fps stream.
        check(output, 2592, 1952, 15, 50, 51)
        check(output, 2592, 1952, 30, 51)
        check(output, 3840, 2160, 60, 52)
        check(output, 4080, 3060, 30, 60)
