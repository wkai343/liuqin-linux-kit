#!/usr/bin/env python3
"""Replay one packed 640x480 NV12 frame through the already loaded IPE test.

Run as root on the tablet. Pixel bytes only: never accepts command buffers,
addresses, dimensions, register values or IQ settings. No module/boot changes.
Store the input, output and result under ignored out/ directories.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

DEVICE = Path('/sys/bus/platform/devices/ac01000.icp')
IMAGE_BYTES = 640 * 480 * 3 // 2


def status():
    return dict(item.split('=', 1) for item in (DEVICE / 'status').read_text().split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    image = args.input.read_bytes()
    if len(image) != IMAGE_BYTES:
        parser.error('input must contain exactly 460800 packed NV12 bytes')
    if os.geteuid() != 0:
        parser.error('root is required for the fixed experimental pixel interface')
    before = status()
    if before['unsafe'] != '0':
        raise RuntimeError('Firmware shutdown previously failed; device must be recovered')
    args.output.mkdir(parents=True, exist_ok=False)
    # Short writes are normal for sysfs. Keep one fd and strictly increasing
    # offsets; the kernel invalidates incomplete or out-of-order uploads.
    fd = os.open(DEVICE / 'input_nv12', os.O_WRONLY)
    try:
        written = 0
        while written < len(image):
            count = os.write(fd, image[written:written + 4096])
            if count <= 0:
                raise OSError('zero-length pixel upload')
            written += count
    finally:
        os.close(fd)
    staged = status()
    if staged['input_ready'] != '1' or int(staged['input_written']) != IMAGE_BYTES:
        raise RuntimeError('Incomplete pixel upload')
    started = time.monotonic()
    error = None
    fd = os.open(DEVICE / 'selftest', os.O_WRONLY)
    try:
        if os.write(fd, b'8') != 1:
            raise OSError('short selftest write')
    except OSError as e:
        error = str(e)
    finally:
        os.close(fd)
    final = status()
    result = dict(input_sha256=hashlib.sha256(image).hexdigest(), status=final,
                  elapsed_seconds=time.monotonic() - started, error=error)
    if final['unsafe'] == '0':
        try:
            output = (DEVICE / 'output_nv12').read_bytes()
            (args.output / 'output.nv12').write_bytes(output)
            result['output_sha256'] = hashlib.sha256(output).hexdigest()
            result['output_bytes'] = len(output)
            result['luma_exact'] = output[:640*480] == image[:640*480]
        except OSError as e:
            result['read_error'] = str(e)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    expected = dict(result='0', unsafe='0', frame_done='1', completed_frames='1',
                    user_input='1', frame_error='0', frame_rc='0',
                    input_changed='0', guard_changed='0')
    valid = (error is None and all(final[k] == v for k, v in expected.items()) and
             result.get('output_bytes') == IMAGE_BYTES and result.get('luma_exact'))
    print(json.dumps(result))
    if not valid:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
