#!/usr/bin/env python3
"""Exercise the opt-in Liuqin IFE680 NV12 path on the device (requires root).

Close camera clients first. This rewires the media graph; it does not load
modules, change boot files, or run a software ISP. Logs belong under out/.
"""
import argparse
import glob
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fps', type=int, choices=(30, 60), default=30)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--save', action='store_true', help='save every NV12 frame (large)')
    parser.add_argument('--exposure', type=int, default=2000, help='sensor lines')
    parser.add_argument('--gain', type=int, default=960, help='IMX596 analogue gain code')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('requires root to inspect clients and configure the media graph')
    if not 2 <= args.frames <= 3600:
        parser.error('--frames must be 2..3600')
    if not 4 <= args.exposure <= (3127 if args.fps == 60 else 6278):
        parser.error('exposure exceeds the selected frame interval')
    if not 0 <= args.gain <= 1008:
        parser.error('gain must be 0..1008')
    flag = Path('/sys/module/qcom_camss/parameters/ife680_pix')
    if not flag.exists() or flag.read_text().strip() != 'Y':
        parser.error('load qcom_camss with ife680_pix=1 first')
    nodes = glob.glob('/dev/v4l-subdev*') + glob.glob('/dev/video*')
    busy = subprocess.run(['fuser', *nodes], capture_output=True, text=True)
    if busy.returncode == 0:
        parser.error('camera/video nodes are in use: ' + busy.stdout.strip())
    if busy.returncode != 1:
        parser.error('could not check camera/video node users: ' + busy.stderr.strip())

    args.output.mkdir(parents=True, exist_ok=False)
    log = (args.output / 'commands.log').open('w')

    def run(command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        log.write(json.dumps(command) + '\n' + result.stdout + result.stderr + '\n')
        log.flush()
        result.check_returncode()
        return result.stdout

    # Find the intended sensor/media device instead of relying on /dev/videoN.
    media = None
    for candidate in sorted(glob.glob('/dev/media*')):
        if not Path(candidate).is_char_device():
            continue
        graph = run(['media-ctl', '-d', candidate, '-p'])
        if 'imx596 6-0010' in graph and 'msm_vfe0_pix' in graph:
            media = candidate
            break
    if media is None:
        raise RuntimeError('IMX596 / full IFE0 media graph not found')

    def mc(*command):
        return run(['media-ctl', '-d', media, *command])

    sensor = mc('-e', 'imx596 6-0010').strip()
    video = mc('-e', 'msm_vfe0_video3').strip()
    controls = 'horizontal_flip,vertical_flip,vertical_blanking,exposure,analogue_gain'
    old = run(['v4l2-ctl', '-d', sensor, '--get-ctrl=' + controls])
    old_controls = dict(re.findall(r'^\s*(\w+):\s*(-?\d+)\s*$', old, re.M))
    old_format = mc('--get-v4l2', '"imx596 6-0010":0').strip()
    old_match = re.search(r'fmt:([^ ]+)', old_format)
    if set(old_controls) != set(controls.split(',')) or not old_match:
        raise RuntimeError('cannot save sensor state')
    width, height = 2592, (1952 if args.fps == 30 else 1472)
    frame_size = width * height * 3 // 2
    result = {'kernel': os.uname().release, 'width': width, 'height': height,
              'fps_requested': args.fps, 'start': time.time()}
    kernel_before = run(['dmesg'])
    try:
        mc('-r')
        run(['v4l2-ctl', '-d', sensor,
             '--set-ctrl=horizontal_flip=0,vertical_flip=0'])
        mc('-l', '"msm_csiphy2":1->"msm_csid0":0[1],'
           '"msm_csid0":4->"msm_vfe0_pix":0[1]')
        for entity, pad in [('imx596 6-0010', 0), ('msm_csiphy2', 0),
                            ('msm_csid0', 0), ('msm_csid0', 4), ('msm_vfe0_pix', 0)]:
            mc('-V', f'"{entity}":{pad}[fmt:SRGGB10_1X10/{width}x{height} field:none]')
        mc('-V', f'"msm_vfe0_pix":1[fmt:YUYV8_1_5X8/{width}x{height} field:none]')
        run(['v4l2-ctl', '-d', sensor,
             f'--set-ctrl=vertical_blanking={(6302 if args.fps == 30 else 3151) - height},'
             f'exposure={args.exposure},analogue_gain={args.gain}'])
        run(['v4l2-ctl', '-d', video,
             f'--set-fmt-video=width={width},height={height},pixelformat=NV12'])
        fmt = run(['v4l2-ctl', '-d', video, '--get-fmt-video'])
        (args.output / 'format.txt').write_text(fmt)
        if (not re.search(rf'Width/Height\s*:\s*{width}/{height}\b', fmt) or
                "'NV12'" not in fmt or f': {frame_size}' not in fmt):
            raise RuntimeError('unexpected negotiated format: ' + fmt)
        (args.output / 'media.txt').write_text(mc('-p'))
        dest = str(args.output / 'frames.nv12') if args.save else '/dev/null'
        with (args.output / 'capture.log').open('w') as capture:
            process = subprocess.Popen(
                ['v4l2-ctl', '-d', video, '--stream-mmap=4',
                 f'--stream-count={args.frames}', '--stream-poll',
                 '--stream-to=' + dest, '--verbose'], stdout=capture, stderr=subprocess.STDOUT)
            try:
                process.wait(timeout=args.frames / args.fps + 15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise RuntimeError('capture timed out')
        # v4l2-ctl can return zero after STREAMON failure: count actual DQBUFs.
        output = (args.output / 'capture.log').read_text()
        frames = [(int(a), int(b), float(c)) for a, b, c in re.findall(
            r'cap dqbuf:.*?seq:\s*(\d+) bytesused:\s*(\d+) ts:\s*([\d.]+)', output)]
        if process.returncode or len(frames) != args.frames:
            raise RuntimeError(f'expected {args.frames} frames, received {len(frames)}')
        if [r[0] for r in frames] != list(range(args.frames)):
            raise RuntimeError('non-contiguous sequence numbers')
        if any(r[1] != frame_size for r in frames):
            raise RuntimeError('short frame')
        intervals = [b[2] - a[2] for a, b in zip(frames, frames[1:])]
        if min(intervals) <= 0:
            raise RuntimeError('non-monotonic frame timestamps')
        actual_fps = (len(frames) - 1) / (frames[-1][2] - frames[0][2])
        if not args.fps * .95 < actual_fps < args.fps * 1.05:
            raise RuntimeError(f'unexpected frame rate {actual_fps}')
        if args.save and Path(dest).stat().st_size != frame_size * args.frames:
            raise RuntimeError('saved stream size mismatch')
        result.update(frames=len(frames), fps=actual_fps,
                      median_interval_ms=statistics.median(intervals) * 1000,
                      max_interval_ms=max(intervals) * 1000)
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        try:
            mc('-r')
            mc('-V', f'"imx596 6-0010":0[fmt:{old_match[1]} field:none]')
            run(['v4l2-ctl', '-d', sensor, '--set-ctrl=' + ','.join(
                f'{key}={old_controls[key]}' for key in controls.split(','))])
        finally:
            kernel_after = run(['dmesg'])
            (args.output / 'kernel.log').write_text(kernel_after)
            delta = kernel_after[len(kernel_before):] if kernel_after.startswith(kernel_before) else kernel_after
            faults = re.findall(r'^.*(?:image violation|SMMU|smmu.*fault|queue \d timeout|BUG:|Oops:).*$', delta, re.M)
            result['kernel_faults'] = faults
            (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            log.close()
    if result['kernel_faults']:
        raise RuntimeError('kernel faults: ' + repr(result['kernel_faults']))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
