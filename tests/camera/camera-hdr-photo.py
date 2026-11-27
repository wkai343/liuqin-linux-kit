#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Take a bracketed still using the optional liuqin three-input IPE module."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
validate = runpy.run_path(str(HERE / 'camera-hdr-validate.py'))['validate']
bundle = runpy.run_path(str(HERE / 'camera-icp/hdr-bundle.py'))
WIDTH, HEIGHT = 640, 480
PIXELS = WIDTH * HEIGHT
DEVICE = Path('/sys/bus/platform/devices/ac01000.icp')
PROFILES = {'rear': (21, (1997, 8001, 31994)), 'front': (23, (1999, 8001, 32002))}
WIDE_SIZES = {'4080x2296': (4080, 2296), '3840x2160': (3840, 2160)}
UHD_TIMES = (1002, 4000, 16003)
CST_GBR = (1., -0.3457, -0.7168, 1., 1.7793, 0., 1., 0., 1.40723)


def resize_input(raw, meta):
    width, height, stride = (meta[key] for key in ('width', 'height', 'stride'))
    vf = f'crop={width}:{height}:0:0,scale={WIDTH}:{HEIGHT}:in_range=full:out_range=full'
    return subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'rawvideo', '-pix_fmt',
        'nv12', '-s', f'{stride}x{height}', '-i', 'pipe:0', '-vf', vf, '-frames:v', '1',
        '-pix_fmt', 'nv12', '-f', 'rawvideo', 'pipe:1'], input=raw, stdout=subprocess.PIPE, check=True).stdout


def frame(stage):
    with (DEVICE / 'selftest').open('wb', buffering=0) as control:
        control.write(str(stage).encode())
    status = dict(item.split('=', 1) for item in (DEVICE / 'status').read_text().split())
    expected = dict(result='0', frame_done='1', frame_error='0', frame_rc='0',
                    unsafe='0', completed_frames='1', input_changed='0', guard_changed='0')
    if any(status.get(k) != v for k, v in expected.items()):
        raise RuntimeError(f'IPE stage {stage} failed: {status}')


def fuse_bracket(inputs, stage):
    selection = DEVICE / 'snapshot_geometry'
    if selection.is_file():
        selection.write_text('640x480')
    for pre_stage, pixels in zip((16, 17, 18), inputs, strict=True):
        if len(pixels) != PIXELS * 3 // 2:
            raise ValueError('incomplete VGA input')
        (DEVICE / 'input_nv12').write_bytes(pixels)
        frame(pre_stage)
    frame(stage)
    frame(22)
    packed = (DEVICE / 'snapshot_p010').read_bytes()
    if len(packed) != PIXELS * 3:
        raise RuntimeError('incomplete IPE output')
    return packed


def register(images, geometry, stride):
    w, h = geometry
    size = w * h * 3 // 2
    result = subprocess.run([HERE / 'hdr-motion', str(w), str(h), str(stride)], input=b''.join(images),
                            stdout=subprocess.PIPE, check=True).stdout
    if len(result) != 3 * size + w * h:
        raise ValueError('incomplete registered bracket')
    return [result[i * size:(i + 1) * size] for i in range(3)], result[3 * size:]


def still_output_gain(meta):
    if meta['sensor'] != 'front':
        return 1.
    # The middle frame sets MAC unity. Never darken an already exposed scene
    # or lift beyond the recorded long exposure / two stops.
    return max(1., min(4., meta['long_to_medium'], meta['ae_to_middle']))


def read_bracket(capture, native=False, align=False):
    meta = validate(capture)
    frames = meta['selected_frames']
    stage, times = PROFILES[meta['sensor']]
    sensor_size = (meta['width'], meta['height'])
    if meta['sensor'] == 'rear' and sensor_size == WIDE_SIZES['3840x2160']:
        if not native:
            raise ValueError('the 60 Hz bracket requires its native HDR401 exposure profile')
        times = UHD_TIMES
    if (not meta['captured_digests_verified'] or tuple(f['exposure_us'] for f in frames) != times or
            len({f['analogue_gain'] for f in frames}) != 1):
        raise ValueError('exposure metadata or input hashes do not match the fixed MAC profile')
    expected = {'front': {(2592, 1952)}, 'rear': {(4080, 3060), *WIDE_SIZES.values()}}
    if native and sensor_size not in expected[meta['sensor']]:
        raise ValueError('captured mode differs from the native hardware profile')
    geometry = sensor_size if native else (WIDTH, HEIGHT)
    stride = meta['stride'] if native else WIDTH
    images = []
    for f in frames:
        raw = (capture / f['image']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != f['sha256']:
            raise ValueError('image bytes differ from the completed exposure request')
        images.append(raw if native else resize_input(raw, meta))
    mask = None
    if align:
        images, mask = register(images, geometry, stride)
        stride = geometry[0]
    # IFE Gamma151 has already applied the sRGB OETF. MAC exposure ratios
    # and the middle-frame fallback both operate on scene-linear components.
    return [convert('linear', im, geometry, stride) for im in images], stage, geometry, mask, still_output_gain(meta)


def convert(operation, pixels, geometry, stride, *params):
    return subprocess.run([HERE / 'hdr-pixels', operation, *map(str, geometry), str(stride), *map(str, params)],
                          input=pixels, stdout=subprocess.PIPE, check=True).stdout


def fuse_native(inputs, stage, geometry):
    w, h = geometry
    selection = DEVICE / 'snapshot_geometry'
    if not selection.is_file():
        raise RuntimeError('resident module does not support native stills')
    selection.write_text(f'{w}x{h}')
    try:
        for pre, pixels in zip((16, 17, 18), inputs, strict=True):
            if len(pixels) != w * h * 3 // 2:
                raise ValueError('incomplete native input')
            (DEVICE / 'snapshot_input').write_bytes(pixels)
            frame(pre)
        frame(stage)
        frame(22)
        packed = (DEVICE / 'snapshot_p010').read_bytes()
        if len(packed) != w * h * 3:
            raise RuntimeError('incomplete native P010 result')
        return packed
    finally:
        selection.write_text('640x480')


def encode_jpeg(packed, geometry, gain=1.):
    rgb = convert('render', packed, geometry, geometry[0], gain)
    return subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
        '-s', f'{geometry[0]}x{geometry[1]}', '-i', 'pipe:0', '-frames:v', '1', '-c:v', 'mjpeg', '-q:v', '2',
        '-pix_fmt', 'yuvj444p', '-f', 'image2pipe', 'pipe:1'], input=rgb,
        stdout=subprocess.PIPE, check=True).stdout


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--capture', type=Path, help='completed schema-2 bracket directory')
    group.add_argument('--sensor', choices=PROFILES, help='take a fresh bracket')
    local_tool = HERE / 'camera-hdr-capture'
    p.add_argument('--capture-tool', default=str(local_tool) if local_tool.is_file() else 'camera-hdr-capture',
                   help='libcamera bracket capture executable')
    p.add_argument('--no-align', action='store_true', help='disable motion registration for a static comparison')
    p.add_argument('--resolution', choices=('native', 'vga', *WIDE_SIZES), help='defaults to native on a capable resident module')
    p.add_argument('--output', type=Path, required=True, help='new JPEG file')
    args = p.parse_args()
    if args.resolution in WIDE_SIZES and args.sensor == 'front':
        p.error('wide profiles require the rear camera')
    if args.output.exists():
        p.error('output already exists')
    if not (DEVICE / 'snapshot_p010').is_file() and not bundle['HDR'].is_dir():
        p.error('install the local HDR still-image module before capturing')
    # Serialize the bracket and the stateful sysfs stages across CLI instances.
    with open('/run/lock/liuqin-hdr-photo.lock', 'a') as lock, tempfile.TemporaryDirectory() as tmp:
        fcntl.flock(lock, fcntl.LOCK_EX)
        capture = args.capture
        if args.sensor:
            capture = Path(tmp) / 'bracket'
            mode = [args.resolution] if args.resolution in WIDE_SIZES else []
            subprocess.run([args.capture_tool, args.sensor, str(capture), *mode], check=True)
        native = args.resolution in ('native', *WIDE_SIZES) or (args.resolution is None and (DEVICE / 'snapshot_geometry').exists())
        inputs, stage, geometry, mask, gain = read_bracket(capture, native,
                        not args.no_align and (HERE / 'hdr-motion').is_file())
        if args.resolution in WIDE_SIZES and geometry != WIDE_SIZES[args.resolution]:
            raise ValueError('supplied bracket has a different sensor mode')
        with bundle['snapshot_module']():
            packed = fuse_native(inputs, stage, geometry) if native else fuse_bracket(inputs, stage)
        if mask is not None:
            packed = convert('deghost', packed + inputs[1] + mask, geometry, geometry[0])
        jpeg = encode_jpeg(packed, geometry, gain)
        with tempfile.NamedTemporaryFile(dir=args.output.parent) as tmpout:
            tmpout.write(jpeg)
            tmpout.flush()
            if os.geteuid() == 0 and 'SUDO_UID' in os.environ and 'SUDO_GID' in os.environ:
                os.fchown(tmpout.fileno(), int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
            os.link(tmpout.name, args.output)


if __name__ == '__main__':
    main()
