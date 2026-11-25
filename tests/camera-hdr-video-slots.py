#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify that registration releases a capture slot exactly once, before IPE."""
from contextlib import ExitStack, closing
import ctypes
import io
import os
from pathlib import Path
from queue import SimpleQueue
import runpy
from unittest.mock import patch


video = runpy.run_path(str(Path(__file__).with_name('camera-hdr-video.py')))
size = (4, 2)
frame_bytes = 12


def headers():
    rows = []
    for group in range(2):
        for leg, us in enumerate(video['photo']['PROFILES']['rear'][1]):
            seq = 10 + 3 * group + leg
            ts = seq * 33_334_000
            origin = ts - 3 * 33_334_000
            rows.append(f'HDR3 {group} {leg} {seq} {ts} {seq-3} {origin+1000} 10 {us} 3 {origin} 0\n')
    return io.BytesIO(''.join(rows).encode())


def expect_no_credit(fd):
    try:
        os.read(fd, 1)
    except BlockingIOError:
        return
    raise AssertionError('capture slot returned early or twice')


def check(fail):
    with ExitStack() as stack:
        slots = video['BracketSlots'](stack, frame_bytes)
        fd = slots.fds[1]
        assert os.read(fd, 3) == bytes(range(3))
        trace = video['StartupTrace'](size)
        items = stack.enter_context(closing(video['packets'](headers(), 'rear', size, slots, trace)))

        class Registration:
            def hdr_prepare_video_sized(self, images, packed, mask, width, height):
                expect_no_credit(fd)
                ctypes.memmove(packed, images, slots.group_bytes)
                return int(fail)

        prepared = stack.enter_context(closing(video['prepare_video'](
            Registration(), items, size, SimpleQueue(), trace, slots)))
        if fail:
            try:
                next(prepared)
            except RuntimeError as exc:
                assert str(exc) == 'motion registration failed'
            else:
                raise AssertionError('registration failure ignored')
            items.close()
            assert os.read(fd, 1) == b'\x00'
        else:
            for number in (1, 2):
                pixels = bytes([number]) * slots.group_bytes
                slots.mapping[:slots.group_bytes] = pixels
                _, _, (packed, _, _) = next(prepared)
                assert os.read(fd, 1) == b'\x00'
                slots.mapping[:slots.group_bytes] = bytes(slots.group_bytes)
                assert packed.raw == pixels
            prepared.close()
            items.close()
        expect_no_credit(fd)


def check_trace_windows():
    second = 1_000_000_000
    times = [second + offset for offset in (0, second, 2*second, 118*second, 179*second)]
    for startup, tail, wanted in [(False, False, []), (True, False, times[:2]),
                                  (False, True, times[-2:]), (True, True, times[:2] + times[-2:])]:
        with patch.dict(os.environ, {'LIUQIN_HDR_TRACE_STARTUP': str(int(startup)),
                                     'LIUQIN_HDR_TRACE_TAIL': str(int(tail))}):
            for size in [(3840, 2160), (640, 480)]:
                trace = video['StartupTrace'](size, 180)
                for i, ts in enumerate(times):
                    trace.packet(ts, i, i*3, i % 3)
                    trace.mark(ts, 'release', i % 3)
                assert [r[1] for r in trace.events if r[0] == 'release'] == (wanted if size[0] == 3840 else [])


if __name__ == '__main__':
    check(False)
    check(True)
    check_trace_windows()
    print('PASS capture credit, independent prepared planes, failure cleanup and diagnostic windows')
