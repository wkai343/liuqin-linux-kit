#!/usr/bin/env python3
"""Check the real libcamera/GStreamer NV12 path (run on the tablet).

The selected camera must expose hardware NV12. Close other camera clients;
keep the output in an ignored out/ directory. No boot or module changes.
"""
import argparse
import json
from pathlib import Path
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fps', type=int, choices=(30, 60), default=30)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--size', choices=('2592x1952', '2592x1472', '4080x3060', '4080x2296', '3840x2160'), help='explicit hardware geometry')
    args = parser.parse_args()
    if not 30 <= args.frames <= 3600:
        parser.error('--frames must be 30..3600')
    args.output.mkdir(parents=True, exist_ok=False)
    Gst.init(None)
    width, height = map(int, (args.size or ('2592x1952' if args.fps == 30 else '2592x1472')).split('x'))
    pipeline = Gst.parse_launch(
        'libcamerasrc name=camera ! '
        f'video/x-raw,format=NV12,width={width},height={height},framerate={args.fps}/1 ! '
        f'identity eos-after={args.frames + 1} ! '
        'appsink name=sink sync=false emit-signals=true max-buffers=4 drop=false')
    pipeline.get_by_name('camera').set_property('camera-name', args.camera)
    points, sizes, errors = [], [], []
    loop = GLib.MainLoop()

    def sample(sink):
        value = sink.emit('pull-sample')
        buffer = value.get_buffer()
        index = len(points)
        points.append(int(buffer.pts))
        sizes.append(buffer.get_size())
        if index in (0, args.frames - 1):
            try:
                (args.output / f'{index}.nv12').write_bytes(buffer.extract_dup(0, buffer.get_size()))
                (args.output / 'caps.txt').write_text(value.get_caps().to_string())
            except OSError as error:
                errors.append(str(error))
                loop.quit()
                return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def message(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            error, detail = msg.parse_error()
            errors.append(str(error) + ' ' + str(detail))
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    def timeout():
        errors.append('capture timeout')
        loop.quit()
        return False

    pipeline.get_by_name('sink').connect('new-sample', sample)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect('message', message)
    GLib.timeout_add_seconds(args.frames // args.fps + 20, timeout)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
    if len(points) != args.frames:
        errors.append(f'expected {args.frames} frames, got {len(points)}')
    if any(size != width * height * 3 // 2 for size in sizes):
        errors.append('short or padded frame')
    if any(b <= a for a, b in zip(points, points[1:])) or Gst.CLOCK_TIME_NONE in points:
        errors.append('invalid timestamps')
    fps = (len(points) - 1) * 1e9 / (points[-1] - points[0]) if len(points) > 1 and points[-1] > points[0] else 0
    if not args.fps * .95 < fps < args.fps * 1.05:
        errors.append(f'unexpected frame rate {fps}')
    result = dict(camera=args.camera, frames=len(points), fps=fps, pts=points,
                  bytes=sizes, errors=errors)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('frames', 'fps', 'errors')}))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
