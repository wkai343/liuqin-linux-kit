#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise queue-driven VP9 thread placement and GStreamer task-pool reuse."""
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst


video = runpy.run_path(str(Path(__file__).with_name('camera-hdr-video.py')))


def check_recording_modes():
    uhd = (3840, 2160)
    switch = 'LIUQIN_HDR_UHD_VP9_FAST_CPU'
    # Each case specifies the switch, size, eight-column tiles and expected scheduling.
    cases = [(None, uhd, False, False), (None, uhd, True, False), ('0', uhd, True, False),
             ('1', uhd, False, True), ('1', uhd, True, True),
             ('1', (4080, 2296), False, False), ('1', (640, 480), False, False)]
    with TemporaryDirectory() as tmp, patch.dict(os.environ, {'LIUQIN_HDR_TRACE_ENCODER': '0'}):
        for index, (value, size, eight_tiles, expected) in enumerate(cases):
            if value is None:
                os.environ.pop(switch, None)
            else:
                os.environ[switch] = value
            path = Path(tmp) / f'{index}.mkv'
            # Only the opt-in streaming task reads its inherited mask at startup.
            with patch.object(os, 'sched_getaffinity', wraps=os.sched_getaffinity) as read_mask:
                movie = video['Movie'](path, size, eight_tiles=eight_tiles)
                try:
                    frame = Gst.Buffer.new_allocate(None, movie.frame_bytes, None)
                    frame.memset(0, 0, movie.frame_bytes)
                    movie.frame(Gst.SECOND, frame)
                    movie.finish()
                finally:
                    movie.close()
                assert read_mask.called == expected, (value, size, eight_tiles)
            assert path.stat().st_size > 0


def check(stop, allowed):
    cpus = sorted(allowed)
    fast = set(cpus[-2:]) if len(cpus) > 2 else {cpus[-1]}
    if stop == 'uniform':
        fast = allowed
    frame_bytes = 640 * 480 * 3 // 2
    boundary = 'queue ! ' if stop == 'separate-task' else ''
    pipeline = Gst.parse_launch(
        f'appsrc name=src format=time max-bytes={2 * frame_bytes} ! '
        'video/x-raw,format=I420,width=640,height=480,framerate=25/1 ! '
        f'{boundary}vp9enc name=enc deadline=1 cpu-used=8 threads=4 lag-in-frames=0 ! '
        f'identity error-after={1 if stop == "error" else -1} ! fakesink sync=false')
    bus = pipeline.get_bus()
    src = pipeline.get_by_name('src')
    enc = pipeline.get_by_name('enc')
    entered, resume, first_output = (threading.Event() for _ in range(3))
    outputs, changes = [], []
    read_text = Path.read_text
    set_mask = os.sched_setaffinity

    def capacity(path, *args, **kwargs):
        if path.name != 'cpu_capacity':
            return read_text(path, *args, **kwargs)
        if stop == 'missing-capacity':
            raise FileNotFoundError(path)
        cpu = int(path.parent.name[3:])
        if cpu == cpus[-1] or stop == 'uniform':
            return '1024'
        return '832' if cpu in fast else '277'

    def change(tid, mask):
        if stop == 'denied':
            raise PermissionError('sched_setaffinity')
        set_mask(tid, mask)
        changes.append((threading.get_native_id(), os.sched_getaffinity(tid)))

    def hold_first_input(pad, info):
        entered.set()
        resume.wait(5)
        return Gst.PadProbeReturn.REMOVE

    def observe_output(pad, info):
        outputs.append((threading.get_native_id(), os.sched_getaffinity(0),
                        src.get_property('current-level-buffers')))
        if stop == 'cancel':
            src.get_static_pad('src').pause_task()
        first_output.set()
        return Gst.PadProbeReturn.OK

    def push(index):
        buf = Gst.Buffer.new_allocate(None, frame_bytes, None)
        buf.pts = index * 40_000_000
        buf.duration = 40_000_000
        assert src.emit('push-buffer', buf) == Gst.FlowReturn.OK

    enc.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, hold_first_input)
    pad, probe_id = video['schedule_vp9_main'](Gst, bus, src, enc)
    pad.add_probe(Gst.PadProbeType.BUFFER, observe_output)
    with patch.object(Path, 'read_text', capacity), patch.object(os, 'sched_setaffinity', change):
        try:
            assert pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
            if stop != 'empty':
                push(0)
                assert entered.wait(5)
                # The source has popped frame zero. Fill its two waiting slots.
                push(1)
                push(2)
                resume.set()
                if stop == 'cancel':
                    assert first_output.wait(5)
                else:
                    assert src.emit('end-of-stream') == Gst.FlowReturn.OK
                    msg = bus.timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                    assert msg and msg.type == (Gst.MessageType.ERROR if stop == 'error' else Gst.MessageType.EOS)
        finally:
            resume.set()
            pipeline.set_state(Gst.State.NULL)
            bus.set_sync_handler(None)
            pad.remove_probe(probe_id)

    assert os.sched_getaffinity(0) == allowed
    if stop in ('empty', 'separate-task', 'missing-capacity', 'denied') or fast == allowed:
        assert not changes and all(mask == allowed for _, mask, _ in outputs)
    else:
        tid = outputs[0][0]
        assert outputs[0] == (tid, fast, 2) and tid != threading.get_native_id()
        assert changes == [(tid, fast), (tid, allowed)]
        assert all(thread == tid and mask == (fast if depth == 2 else allowed)
                   for thread, mask, depth in outputs)


if __name__ == '__main__':
    Gst.init(None)
    check_recording_modes()
    for reason in ('eos', 'cancel', 'error', 'empty', 'separate-task', 'uniform',
                   'missing-capacity', 'denied', 'eos'):
        check(reason, os.sched_getaffinity(0))
    print('PASS VP9 opt-in by recording mode, backlog, affinity fallback, shutdown and task reuse')
