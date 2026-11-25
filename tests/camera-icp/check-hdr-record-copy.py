#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise partial 4K input writes during IPE prefetch without opening a camera."""
import ctypes
import errno
import fcntl
import hashlib
import mmap
from pathlib import Path


def main():
    frame_bytes = 3840 * 2160 * 3 // 2
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.write.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    libc.write.restype = ctypes.c_ssize_t
    status = Path('/sys/bus/platform/devices/ac01000.icp/status')

    with open('/run/lock/liuqin-hdr-photo.lock', 'a') as lock, mmap.mmap(-1, 3 * frame_bytes) as pixels:
        fcntl.flock(lock, fcntl.LOCK_EX)
        address = ctypes.addressof(ctypes.c_ubyte.from_buffer(pixels))
        for leg in (1, 2):
            with open('/dev/liuqin-rear-hdr', 'r+b', buffering=0) as fd:
                def submit():
                    assert fd.write(pixels) == 3 * frame_bytes
                    raw = fd.read(2 * frame_bytes)
                    assert len(raw) == 2 * frame_bytes
                    fields = dict(s.split('=') for s in status.read_text().split())
                    assert fields['completed_frames'] == '5'
                    assert all(fields[n] == '0' for n in ('result', 'unsafe', 'input_changed', 'guard_changed'))
                    return hashlib.sha256(raw).digest()

                # The second bracket uses the persistent stage mappings and
                # takes the same prefetch path as the ensuing partial write.
                expected = submit()
                assert submit() == expected
                page = address + (leg * frame_bytes + mmap.PAGESIZE - 1) // mmap.PAGESIZE * mmap.PAGESIZE
                assert libc.mprotect(page, mmap.PAGESIZE, 0) == 0
                try:
                    assert libc.write(fd.fileno(), address, len(pixels)) == -1
                    assert ctypes.get_errno() == errno.EFAULT
                    try:
                        fd.read(2 * frame_bytes)
                    except OSError as exc:
                        assert exc.errno == errno.ENODATA
                    else:
                        raise AssertionError('partial HDR group was readable')
                finally:
                    assert libc.mprotect(page, mmap.PAGESIZE, mmap.PROT_READ | mmap.PROT_WRITE) == 0
                assert submit() == submit() == expected
    print('PASS partial middle and long exposures, discarded output, clean same-fd retries')


if __name__ == '__main__':
    main()
