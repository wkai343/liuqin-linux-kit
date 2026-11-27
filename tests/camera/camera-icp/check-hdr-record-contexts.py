#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check retained 4K HDR contexts and compare alternating groups with fresh sessions."""
import errno
import fcntl
import hashlib
from pathlib import Path


NODE = Path('/dev/liuqin-rear-hdr')
STATUS = Path('/sys/bus/platform/devices/ac01000.icp/status')
PIXELS = 3840 * 2160
PREPARE_UHD = 0x4870


def status():
    fields = dict(item.split('=', 1) for item in STATUS.read_text().split())
    assert all(fields[key] == '0' for key in
               ('result', 'unsafe', 'frame_error', 'frame_rc', 'input_changed', 'guard_changed')), fields
    return fields


def fuse(fd, bracket):
    assert fd.write(bracket) == len(bracket)
    image = fd.read(3 * PIXELS)
    assert len(image) == 3 * PIXELS
    fields = status()
    assert fields['completed_frames'] == '5'
    return hashlib.sha256(image).digest(), int(fields['messages'])


def main():
    brackets = []
    for levels in ((32, 96, 192), (48, 144, 224)):
        brackets.append(b''.join(bytes([y]) * PIXELS + bytes([128]) * (PIXELS // 2) for y in levels))
    with open('/run/lock/liuqin-hdr-photo.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reference = []
        for bracket in brackets:
            with NODE.open('r+b', buffering=0) as fd:
                digest, _ = fuse(fd, bracket)
                reference.append(digest)
        assert reference[0] != reference[1]
        with NODE.open('r+b', buffering=0) as fd:
            fcntl.ioctl(fd, PREPARE_UHD)
            configured = status()
            assert configured['completed_frames'] == '0'
            try:
                fd.read(1)
            except OSError as exc:
                assert exc.errno == errno.ENODATA
            else:
                raise AssertionError('preparation published a frame')
        assert status()['handle'] == '00000000'
        with NODE.open('r+b', buffering=0) as fd:
            fcntl.ioctl(fd, PREPARE_UHD)
            previous = int(status()['messages'])
            fcntl.ioctl(fd, PREPARE_UHD)
            assert int(status()['messages']) == previous
            for i in (0, 1, 0, 1, 1, 0):
                digest, messages = fuse(fd, brackets[i])
                assert digest == reference[i]
                # Even the first real bracket only sends PRE x3, MAC and POST.
                assert (messages - previous) % 2**32 == 5
                previous = messages
        closed = status()
        assert closed['handle'] == '00000000'
        assert (int(closed['messages']) - previous) % 2**32 == 3
    print('PASS preparation without frames, five replies per real group and three handles released')


if __name__ == '__main__':
    main()
