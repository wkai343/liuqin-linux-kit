#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Record continuous three-exposure IFE/HDR401 video with sensor-timed HLG output."""
import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import errno
import fcntl
import mmap
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import runpy
import subprocess
import sys
import tempfile
from threading import get_native_id
import time

HERE = Path(__file__).resolve().parent
photo = runpy.run_path(str(HERE / 'camera-hdr-photo.py'))
SIZE = (640, 480)


class StartupTrace:
    """Join recorder stages by middle-exposure timestamp on CLOCK_MONOTONIC."""
    def __init__(self, size, seconds=0):
        self.enabled = size == (3840, 2160) and os.environ.get('LIUQIN_HDR_TRACE_STARTUP') == '1'
        self.tail_delay = None
        if size == (3840, 2160) and seconds and os.environ.get('LIUQIN_HDR_TRACE_TAIL') == '1':
            self.tail_delay = max(0, seconds - 62) * 1_000_000_000
        self.tail_start = None
        self.stop = 0
        self.events = []

    def packet(self, ts, index, first_seq, slot):
        if self.enabled and not self.stop:
            self.stop = ts + 2_000_000_000
        if self.tail_delay is not None and self.tail_start is None:
            self.tail_start = ts + self.tail_delay
        self.mark(ts, 'packet', index, first_seq, slot)

    def mark(self, ts, event, *detail):
        if ts < self.stop or (self.tail_start is not None and ts >= self.tail_start):
            self.events.append((event, ts, time.monotonic_ns(), *detail))

    def report(self):
        for row in self.events:
            print('HDR stage_event', *row, file=sys.stderr)


class BracketSlots:
    """Keep three input brackets shared with capture until registration releases each one."""
    count = 3

    def __init__(self, stack, frame_bytes):
        self.group_bytes = 3 * frame_bytes
        self.reading_slot = None
        length = self.count * self.group_bytes
        fd = os.memfd_create('liuqin-hdr-brackets')
        stack.callback(os.close, fd)
        os.posix_fallocate(fd, 0, length)
        self.mapping = stack.enter_context(mmap.mmap(fd, length))
        read_fd, self.write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        stack.callback(os.close, read_fd)
        stack.callback(os.close, self.write_fd)
        os.write(self.write_fd, bytes(range(self.count)))
        self.fds = (fd, read_fd)

    def begin_read(self, slot):
        self.reading_slot = slot
        return ctypes.addressof(ctypes.c_ubyte.from_buffer(self.mapping, slot * self.group_bytes))

    def finish_read(self, ts, trace):
        if self.reading_slot is not None:
            slot = self.reading_slot
            os.write(self.write_fd, bytes([slot]))
            self.reading_slot = None
            if trace:
                trace.mark(ts, 'release', slot)


def packets(pipe, sensor, size=SIZE, slots=None, trace=None):
    uhd = size == (3840, 2160)
    frame_bytes = size[0] * size[1] * 3 // 2
    expected_delay = 3 if sensor == 'rear' and size != (4080, 2296) else 2
    period = 16667000 if uhd else 33334000
    window = 6000000 if uhd else 12000000
    times = photo['UHD_TIMES'] if uhd else photo['PROFILES'][sensor][1]
    last_seq = last_ts = 0
    group = 0
    while True:
        images = None if slots else bytearray(3 * frame_bytes)
        slot = None
        timestamps = []
        for role in range(3):
            line = pipe.readline(200)
            if not line and role == 0:
                return
            fields = line.split()
            if slots:
                if len(fields) != 12 or fields[0] != b'HDR3':
                    raise ValueError('incomplete shared exposure header')
                index_in_pool = int(fields.pop())
                if not 0 <= index_in_pool < slots.count or (role and slot != index_in_pool):
                    raise ValueError('invalid shared bracket slot')
                slot = index_in_pool
            elif len(fields) != 11 or fields[0] != b'HDR2':
                raise ValueError('incomplete live exposure header')
            index, leg, seq, ts, write_seq, write_ns, lines, us, delay, origin_ns = map(int, fields[1:])
            if (index != group or leg != role or seq <= last_seq or ts <= last_ts or
                    not (0 < origin_ns <= write_ns < ts) or lines < 1 or abs(us - times[role]) > 1 or
                    (delay == 0 and (seq - write_seq < 4 or ts - write_ns < 3 * period)) or
                    (delay != 0 and (delay != expected_delay or seq-write_seq != delay or
                                     write_ns-origin_ns > window or (role and seq != last_seq+1)))):
                raise ValueError(f'exposure/frame mismatch: {fields!r}')
            if not slots:
                frame = memoryview(images)[role * frame_bytes:(role + 1) * frame_bytes]
                if pipe.readinto(frame) != frame_bytes:
                    raise ValueError('incomplete IFE pixels')
            timestamps.append(ts)
            if role == 0:
                first_seq = seq
            last_seq, last_ts = seq, ts
        if slots:
            try:
                address = slots.begin_read(slot)
                if trace:
                    trace.packet(timestamps[1], group, first_seq, slot)
                yield group, timestamps[1], address
            finally:
                slots.finish_read(timestamps[1], trace)
        else:
            yield group, timestamps[1], (ctypes.c_ubyte * len(images)).from_buffer(images)
        group += 1


def prefetch(items, initializer=None):
    """Overlap one item with its consumer and keep backpressure at capture."""
    with ThreadPoolExecutor(max_workers=1, initializer=initializer) as reader:
        pending = reader.submit(next, items, None)
        while (item := pending.result()) is not None:
            pending = reader.submit(next, items, None)
            yield item


def high_capacity_cpus(allowed):
    capacity = {i: int(Path(f'/sys/devices/system/cpu/cpu{i}/cpu_capacity').read_text())
                for i in allowed}
    largest = max(capacity.values())
    return {i for i, value in capacity.items() if 4 * value >= 3 * largest}


def prefer_fast_cpus(stack=None):
    # Restrict only the calling thread, within its inherited CPU mask.
    try:
        allowed = os.sched_getaffinity(0)
        fast = high_capacity_cpus(allowed)
        if fast != allowed:
            os.sched_setaffinity(0, fast)
            if stack is not None:
                stack.callback(os.sched_setaffinity, 0, allowed)
    except (OSError, ValueError):
        pass


def schedule_vp9_main(gst, bus, source, encoder):
    limit = source.get_property('max-bytes')
    source_tid = None
    allowed = fast = None
    pinned = False

    def output_buffer(pad, info):
        nonlocal fast, pinned
        # libvpx has started its workers. Share all CPUs with registration
        # again as soon as appsrc has space for the producer.
        if fast and get_native_id() == source_tid:
            full = source.get_property('current-level-bytes') >= limit
            if pinned != full:
                try:
                    os.sched_setaffinity(0, fast if full else allowed)
                    pinned = full
                except OSError:
                    fast = None
        return gst.PadProbeReturn.OK

    def stream_status(bus, message):
        nonlocal source_tid, allowed, fast, pinned
        if message.type == gst.MessageType.STREAM_STATUS:
            status, owner = message.parse_stream_status()
            if owner == source and status == gst.StreamStatusType.ENTER:
                source_tid = get_native_id()
                try:
                    allowed = os.sched_getaffinity(0)
                    preferred = high_capacity_cpus(allowed)
                    fast = preferred if preferred != allowed else None
                except (OSError, ValueError):
                    fast = None
            elif owner == source and status == gst.StreamStatusType.LEAVE:
                if pinned:
                    try:
                        os.sched_setaffinity(0, allowed)
                    except OSError as exc:
                        print(f'could not restore VP9 thread affinity: {exc}', file=sys.stderr)
                source_tid = None
                fast = None
                pinned = False
        return gst.BusSyncReply.PASS

    bus.set_sync_handler(stream_status)
    pad = encoder.get_static_pad('src')
    return pad, pad.add_probe(gst.PadProbeType.BUFFER, output_buffer)


class Movie:
    def __init__(self, path, size, seconds=0, *, eight_tiles=False):
        self.trace = StartupTrace(size, seconds)
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        self.gst = Gst
        vp9_settings = 'threads=3'
        if size == (3840, 2160):
            tiles = 'tile-columns=3 target-bitrate=288000' if eight_tiles else 'tile-columns=2'
            vp9_settings = f'row-mt=true threads=4 {tiles}'
        self.pipeline = Gst.parse_launch(
            f'appsrc name=src format=time block=true max-bytes={6 * size[0] * size[1]} ! '
            f'video/x-raw,format=I420_10LE,width={size[0]},height={size[1]},framerate=0/1,colorimetry=bt2100-hlg ! '
            f'vp9enc name=encoder deadline=1 cpu-used=8 {vp9_settings} lag-in-frames=0 keyframe-max-dist=12 ! '
            'video/x-vp9,profile=(string)2 ! matroskamux name=mux ! filesink name=dest')
        self.pipeline.get_by_name('dest').set_property('location', str(path))
        self.src = self.pipeline.get_by_name('src')
        self.bus = self.pipeline.get_bus()
        self.pool = Gst.BufferPool.new()
        config = self.pool.get_config()
        self.frame_bytes = 3 * size[0] * size[1]
        Gst.BufferPool.config_set_params(config, None, self.frame_bytes, 0, 0)
        if not self.pool.set_config(config) or not self.pool.set_active(True):
            raise RuntimeError('HLG frame pool startup')
        # gst-python supplies writable memoryviews. Plain GI copies map data
        # into immutable bytes, so probe a single byte before recording.
        probe = Gst.Buffer.new_allocate(None, 1, None)
        ok, mapping = probe.map(Gst.MapFlags.WRITE)
        self.writable_maps = ok and not memoryview(mapping.data).readonly
        if ok:
            probe.unmap(mapping)
        self.previous = None
        self.start = 0
        self.frames = 0
        self.interval = 400000000
        self.encoder_probes = []
        self.fast_vp9 = size == (3840, 2160) and os.environ.get('LIUQIN_HDR_UHD_VP9_FAST_CPU') == '1'
        if self.fast_vp9:
            encoder = self.pipeline.get_by_name('encoder')
            self.encoder_probes.append(schedule_vp9_main(Gst, self.bus, self.src, encoder))
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('video encoder startup')
        if ((self.trace.enabled or self.trace.tail_delay is not None) and
                os.environ.get('LIUQIN_HDR_TRACE_ENCODER') == '1'):
            self.trace_encoder()

    def trace_encoder(self):
        gst = self.gst
        def probe(pad, info, event):
            if info.type & gst.PadProbeType.BUFFER_LIST:
                items = info.get_buffer_list()
                buffers = (items.get(i) for i in range(items.length()))
            else:
                buffers = (info.get_buffer(),)
            for buf in buffers:
                if not self.start or buf.pts == gst.CLOCK_TIME_NONE:
                    continue
                ts = self.start + buf.pts
                if event == 'vp9_end':
                    self.trace.mark(ts, event, int(not buf.has_flags(gst.BufferFlags.DELTA_UNIT)),
                                    self.src.get_property('current-level-buffers'))
                else:
                    self.trace.mark(ts, event)
            return gst.PadProbeReturn.OK

        encoder = self.pipeline.get_by_name('encoder')
        mux = self.pipeline.get_by_name('mux')
        sink = self.pipeline.get_by_name('dest')
        for pad, event in ((self.src.get_static_pad('src'), 'dequeue'),
                           (encoder.get_static_pad('sink'), 'vp9_begin'),
                           (encoder.get_static_pad('src'), 'vp9_end'),
                           (mux.sinkpads[0], 'mux_input'),
                           (sink.get_static_pad('sink'), 'file_input')):
            probe_id = pad.add_probe(gst.PadProbeType.BUFFER | gst.PadProbeType.BUFFER_LIST, probe, event)
            self.encoder_probes.append((pad, probe_id))

    def hlg_frame(self, library, surface, middle, mask, size):
        flow, buffer = self.pool.acquire_buffer(None)
        if flow != self.gst.FlowReturn.OK:
            raise RuntimeError('HLG frame allocation')
        if self.writable_maps:
            ok, mapping = buffer.map(self.gst.MapFlags.WRITE)
            if not ok:
                raise RuntimeError('HLG frame mapping')
            try:
                address = ctypes.addressof(ctypes.c_ubyte.from_buffer(mapping.data))
                library.hdr_video_output_sized(surface, middle, mask, address, *size)
            finally:
                buffer.unmap(mapping)
        else:
            raw = ctypes.create_string_buffer(self.frame_bytes)
            library.hdr_video_output_sized(surface, middle, mask, raw, *size)
            if buffer.fill(0, raw.raw) != self.frame_bytes:
                raise RuntimeError('incomplete HLG frame')
        return buffer

    def push(self, ts, b, duration):
        g = self.gst
        b.pts = ts - self.start
        b.duration = duration
        self.trace.mark(ts, 'push_begin')
        if self.src.emit('push-buffer', b) != g.FlowReturn.OK:
            raise RuntimeError('video encoder rejected frame')
        self.trace.mark(ts, 'push_end')
        message = self.bus.pop_filtered(g.MessageType.ERROR)
        if message:
            raise RuntimeError(message.parse_error())
        self.frames += 1

    def frame(self, ts, raw):
        if self.previous:
            prev, pixels = self.previous
            self.interval = ts - prev
            self.push(prev, pixels, self.interval)
        else:
            self.start = ts
        self.previous = ts, raw

    def finish(self):
        if not self.previous:
            raise RuntimeError('no validated HDR frames')
        self.push(*self.previous, self.interval)
        self.src.emit('end-of-stream')
        msg = self.bus.timed_pop_filtered(20 * self.gst.SECOND,
                                          self.gst.MessageType.ERROR | self.gst.MessageType.EOS)
        if not msg or msg.type == self.gst.MessageType.ERROR:
            raise RuntimeError(msg.parse_error() if msg else 'encoder EOS timed out')

    def close(self):
        self.pipeline.set_state(self.gst.State.NULL)
        if self.fast_vp9:
            self.bus.set_sync_handler(None)
        for pad, probe_id in self.encoder_probes:
            pad.remove_probe(probe_id)
        self.encoder_probes.clear()
        self.previous = None
        self.pool.set_active(False)


def video_buffers(pixels):
    return tuple(ctypes.create_string_buffer(n) for n in (9 * pixels // 2, pixels, 3 * pixels))


def prepare_video(library, items, size, recycled, trace, slots=None):
    pixels = size[0] * size[1]
    for index, timestamp, images in items:
        trace.mark(timestamp, 'prepare_begin')
        try:
            buffers = recycled.get_nowait()
        except Empty:
            buffers = video_buffers(pixels)
        packed, mask, _ = buffers
        trace.mark(timestamp, 'register_begin')
        if library.hdr_prepare_video_sized(images, packed, mask, *size):
            raise RuntimeError('motion registration failed')
        trace.mark(timestamp, 'register_end')
        # Registration is the last reader of the capture slot. The prepared
        # planes have independent storage and may wait for IPE or the encoder.
        if slots:
            slots.finish_read(timestamp, trace)
        yield index, timestamp, buffers


def record(sensor, seconds, path, resolution='vga'):
    size = photo['WIDE_SIZES'].get(resolution, SIZE)
    pixels = size[0] * size[1]
    frame_bytes = pixels * 3 // 2
    env = {**os.environ, 'LIBCAMERA_LIUQIN_IFE_ONLY': '1'}
    if sensor == 'front' and resolution == 'vga' and (HERE / 'libcamera.so.0.7').exists():
        env['LD_LIBRARY_PATH'] = str(HERE)
    device = photo['DEVICE']
    if not all((device / name).is_file() for name in ('snapshot_geometry', 'video_inputs', 'video_fuse')):
        raise RuntimeError('the resident IPE module lacks the live three-exposure interface')
    library = ctypes.CDLL(str(HERE / 'libliuqin-hdr.so'))
    library.hdr_prepare_video_sized.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_int] * 2
    library.hdr_video_output_sized.argtypes = [ctypes.c_void_p] * 4 + [ctypes.c_int] * 2
    library.hdr_video_output_sized.restype = None
    eight_tiles = size == (3840, 2160) and os.environ.get('LIUQIN_HDR_UHD_EIGHT_TILES') == '1'
    movie = Movie(path, size, seconds, eight_tiles=eight_tiles)
    fusion_seconds = 0.
    recorder = None
    geometry_set = False
    # Each set belongs to one bracket through registration, IPE and HLG.
    # The encoder keeps its own output and never sees recycled input storage.
    recycled = SimpleQueue()
    trace = movie.trace
    def encode(ts, buffers):
        prepared, mask, surface = buffers
        trace.mark(ts, 'hlg_begin')
        frame = movie.hlg_frame(library, surface, ctypes.byref(prepared, frame_bytes), mask, size)
        trace.mark(ts, 'hlg_end')
        movie.frame(ts, frame)
        recycled.put(buffers)

    try:
        node = Path('/dev/liuqin-rear-hdr')
        if sensor == 'rear' and resolution != 'vga' and node.exists():
            recorder = node.open('r+b', buffering=0)
        else:
            (device / 'snapshot_geometry').write_text('%dx%d' % size)
            geometry_set = True
        if size == (3840, 2160):
            if recorder:
                # _IO('H', 0x70) prepares the fixed 4K IPE contexts without
                # submitting pixels. Older modules configure on the first write.
                try:
                    fcntl.ioctl(recorder, 0x4870)
                except OSError as exc:
                    if exc.errno != errno.ENOTTY:
                        raise
            # Fault in one working set for each overlapping stage before the
            # first exposure. Allocation during capture can exhaust slot credits.
            for _ in range(3):
                recycled.put(video_buffers(pixels))
        mode = [] if resolution == 'vga' else [resolution]
        with ExitStack() as stack:
            slots = BracketSlots(stack, frame_bytes) if mode else None
            fds = slots.fds if slots else ()
            source = stack.enter_context(subprocess.Popen(
                [HERE / 'camera-hdr-live', sensor, str(seconds), *mode, *map(str, fds)],
                env=env, stdout=subprocess.PIPE, pass_fds=fds))
            items = packets(source.stdout, sensor, size, slots, trace)
            inputs = items if slots else prefetch(items)
            register = prepare_video(library, inputs, size, recycled, trace, slots)
            feed = prefetch(register, prefer_fast_cpus if eight_tiles else None)
            try:
                with ThreadPoolExecutor(max_workers=1) as encoder:
                    previous = None
                    for index, ts, buffers in feed:
                        started = time.monotonic()
                        prepared, _, surface = buffers
                        if recorder:
                            trace.mark(ts, 'ipe_begin')
                            written = recorder.write(prepared)
                            trace.mark(ts, 'ipe_written')
                            if written != len(prepared) or recorder.readinto(surface) != len(surface):
                                raise RuntimeError('incomplete recording I/O')
                            trace.mark(ts, 'ipe_end')
                        else:
                            (device / 'video_inputs').write_bytes(memoryview(prepared).cast('B'))
                            (device / 'video_fuse').write_text(sensor)
                            status = dict(v.split('=', 1) for v in (device / 'status').read_text().split())
                            if any(status.get(k) != v for k, v in dict(result='0', completed_frames='5',
                                    frame_rc='0', frame_error='0', unsafe='0', input_changed='0', guard_changed='0').items()):
                                raise RuntimeError(f'incomplete HDR401 group: {status}')
                            with (device / 'snapshot_p010').open('rb', buffering=0) as output:
                                view = memoryview(surface).cast('B')
                                while view:
                                    n = output.readinto(view)
                                    if not n:
                                        raise RuntimeError('short P010 output')
                                    view = view[n:]
                        fusion_seconds += time.monotonic() - started
                        if previous:
                            trace.mark(ts, 'encoder_wait_begin')
                            previous.result()
                            trace.mark(ts, 'encoder_wait_end')
                        previous = encoder.submit(encode, ts, buffers)
                        if index == 0 and recorder and size == (3840, 2160):
                            prefer_fast_cpus(stack)
                    if previous:
                        previous.result()
                if source.wait():
                    raise RuntimeError('continuous IFE capture failed')
                movie.finish()
                duration = (movie.previous[0] - movie.start + movie.interval) / 1e9
                print(f'{movie.frames} HDR frames, {duration:.3f} s, {movie.frames/duration:.2f} fps, '
                      f'{movie.frames / fusion_seconds:.2f} IPE groups/s including transfers', file=sys.stderr)
            finally:
                if source.poll() is None:
                    source.terminate()
                feed.close()
                inputs.close()
                items.close()
    finally:
        movie.close()
        trace.report()
        # Release the host staging planes once the recorder no longer owns a group.
        if recorder:
            recorder.close()
            status = (device / 'status').read_text().split()
            if 'unsafe=0' not in status or (sys.exc_info()[0] is None and 'result=0' not in status):
                raise RuntimeError(f'IPE recording shutdown failed: {" ".join(status)}')
        if geometry_set and 'unsafe=0 ' in (device / 'status').read_text():
            (device / 'snapshot_geometry').write_text('640x480')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sensor', choices=('front', 'rear'), required=True)
    p.add_argument('--resolution', choices=('vga', *photo['WIDE_SIZES']), default='vga')
    p.add_argument('--seconds', type=int, required=True)
    p.add_argument('--output', type=Path, required=True, help='new .mkv file')
    a = p.parse_args()
    if a.resolution != 'vga' and a.sensor != 'rear':
        p.error('wide profiles require the rear sensor')
    if not 1 <= a.seconds <= 3600 or a.output.suffix.lower() != '.mkv' or a.output.exists():
        p.error('use --seconds 1..3600 and a new .mkv output')
    with open('/run/lock/liuqin-hdr-photo.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.NamedTemporaryFile(dir=a.output.parent) as tmp:
            record(a.sensor, a.seconds, Path(tmp.name), a.resolution)
            if os.geteuid() == 0 and 'SUDO_UID' in os.environ and 'SUDO_GID' in os.environ:
                os.fchown(tmp.fileno(), int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
            os.link(tmp.name, a.output)


if __name__ == '__main__':
    main()
