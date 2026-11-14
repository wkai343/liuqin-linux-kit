#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Short on-device recording through the real libobs standard FFmpeg muxer.

Uses an isolated generated image, never captures the desktop or microphone.
Independently software-decode the resulting files on another machine.
"""
import argparse
import ctypes as C
import json
from pathlib import Path
import time


class VideoInfo(C.Structure):
    _fields_ = [('graphics_module', C.c_char_p), ('fps_num', C.c_uint32),
        ('fps_den', C.c_uint32), ('base_width', C.c_uint32), ('base_height', C.c_uint32),
        ('output_width', C.c_uint32), ('output_height', C.c_uint32), ('format', C.c_int),
        ('adapter', C.c_uint32), ('gpu_conversion', C.c_bool), ('colorspace', C.c_int),
        ('range', C.c_int), ('scale_type', C.c_int)]


class AudioInfo(C.Structure):
    _fields_ = [('samples_per_sec', C.c_uint32), ('speakers', C.c_int)]


class Packet(C.Structure):
    _fields_ = [('data', C.c_void_p), ('size', C.c_size_t), ('pts', C.c_int64),
        ('dts', C.c_int64), ('timebase_num', C.c_int32), ('timebase_den', C.c_int32),
        ('type', C.c_int), ('keyframe', C.c_bool), ('dts_usec', C.c_int64),
        ('sys_dts_usec', C.c_int64), ('priority', C.c_int), ('drop_priority', C.c_int),
        ('track_idx', C.c_size_t), ('encoder', C.c_void_p)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin', required=True, type=Path)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='new directory under out/')
    parser.add_argument('--codec', choices=('h264', 'hevc'), required=True)
    parser.add_argument('--container', choices=('mkv', 'mp4', 'ts'), default='mkv')
    parser.add_argument('--frames', type=int, default=60)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps-num', type=int, default=30)
    parser.add_argument('--fps-den', type=int, default=1)
    parser.add_argument('--divisor', type=int, default=1)
    args = parser.parse_args()
    if args.frames < 15 or args.repeat < 1 or min(args.fps_num, args.fps_den, args.divisor) < 1:
        parser.error('at least 15 frames and one repeat required')
    step = args.fps_den*args.divisor
    frame_ns = 1e9*step/args.fps_num
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    lib = C.CDLL('libobs.so.30', mode=C.RTLD_GLOBAL)

    def fn(name, restype, *args):
        f = getattr(lib, name)
        f.restype = restype
        f.argtypes = list(args)
        return f

    ptr, text = C.c_void_p, C.c_char_p
    wayland = C.CDLL('libwayland-client.so.0')
    wayland.wl_display_connect.restype = ptr
    wayland.wl_display_connect.argtypes = [text]
    display = wayland.wl_display_connect(None)
    assert display, 'a running Wayland session is required'
    fn('obs_set_nix_platform', None, C.c_int)(2)
    fn('obs_set_nix_platform_display', None, ptr)(display)
    assert fn('obs_startup', C.c_bool, text, text, ptr)(b'en-US',
        str(output_dir/'config').encode(), None)
    fn('obs_add_data_path', None, text)(b'/usr/share/obs/libobs')
    assert fn('obs_reset_audio', C.c_bool, C.POINTER(AudioInfo))(C.byref(AudioInfo(48000, 2)))
    video = VideoInfo(b'/usr/lib64/libobs-opengl.so', args.fps_num, args.fps_den, args.width, args.height,
        args.width, args.height, 2, 0, True, 2, 1, 2)
    assert fn('obs_reset_video', C.c_int, C.POINTER(VideoInfo))(C.byref(video)) == 0
    modules = [(f'/usr/lib64/obs-plugins/{name}.so', f'/usr/share/obs/obs-plugins/{name}')
               for name in ('image-source', 'obs-ffmpeg')]
    modules.append((str(args.plugin.resolve()), str(args.data.resolve())))
    for binary, data in modules:
        module = ptr()
        assert fn('obs_open_module', C.c_int, C.POINTER(ptr), text, text)(
            C.byref(module), binary.encode(), data.encode()) == 0
        assert fn('obs_init_module', C.c_bool, ptr)(module)
    new_settings = fn('obs_data_create_from_json', ptr, text)
    release_settings = fn('obs_data_release', None, ptr)

    def settings(values):
        return new_settings(json.dumps(values).encode())

    s = settings(dict(file=str(args.image.resolve()), unload=False))
    source = fn('obs_source_create', ptr, text, text, ptr, ptr)(b'image_source', b'generated-image', s, None)
    assert source
    release_settings(s)
    set_source = fn('obs_set_output_source', None, C.c_uint32, ptr)
    set_source(0, source)
    s = settings(dict(bitrate=4000, keyint_sec=1, rate_control='VBR'))
    vencoder = fn('obs_video_encoder_create', ptr, text, text, ptr, ptr)(
        ('liuqin_v4l2_'+args.codec).encode(), b'v4l2-test', s, None)
    release_settings(s)
    assert vencoder
    fn('obs_encoder_set_video', None, ptr, ptr)(vencoder, fn('obs_get_video', ptr)())
    assert fn('obs_encoder_set_frame_rate_divisor', C.c_bool, ptr, C.c_uint32)(vencoder, args.divisor)
    actual_size = [fn('obs_encoder_get_width', C.c_uint32, ptr)(vencoder),
                   fn('obs_encoder_get_height', C.c_uint32, ptr)(vencoder)]
    s = settings(dict(bitrate=128))
    aencoder = fn('obs_audio_encoder_create', ptr, text, text, ptr, C.c_size_t, ptr)(
        b'ffmpeg_aac', b'silent-test-audio', s, 0, None)
    release_settings(s)
    assert aencoder
    fn('obs_encoder_set_audio', None, ptr, ptr)(aencoder, fn('obs_get_audio', ptr)())
    callback_type = C.CFUNCTYPE(None, ptr, C.POINTER(Packet), ptr, ptr)
    now = fn('os_gettime_ns', C.c_uint64)
    results = []
    for index in range(args.repeat):
        path = output_dir/f'recording-{index}.{args.container}'
        s = settings(dict(path=str(path), allow_overwrite=False))
        output = fn('obs_output_create', ptr, text, text, ptr, ptr)(b'ffmpeg_muxer', b'isolated-recording', s, None)
        release_settings(s)
        assert output
        fn('obs_output_set_video_encoder', None, ptr, ptr)(output, vencoder)
        fn('obs_output_set_audio_encoder', None, ptr, ptr, C.c_size_t)(output, aencoder, 0)
        packets = []

        @callback_type
        def packet_callback(unused_output, packet_ptr, unused_time, unused_param):
            p = packet_ptr.contents
            if p.type == 1:
                packets.append(dict(pts=p.pts, dts=p.dts, keyframe=bool(p.keyframe),
                    sys_dts_usec=p.sys_dts_usec, num=p.timebase_num, den=p.timebase_den))

        fn('obs_output_add_packet_callback', None, ptr, callback_type, ptr)(output, packet_callback, None)
        assert fn('obs_output_start', C.c_bool, ptr)(output), 'encoder/output initialization failed'
        active = fn('obs_output_active', C.c_bool, ptr)
        deadline = time.monotonic()+8
        while not packets and time.monotonic() < deadline and active(output):
            time.sleep(.005)
        assert packets, 'no encoded packet within eight seconds'
        # Stop halfway between frames N-1 and N, relative to the OBS encoder
        # timeline. Standard outputs must preserve all N earlier video frames.
        start_us = packets[0]['sys_dts_usec']
        target_ns = start_us*1000 + round((args.frames-.5)*frame_ns)
        while now() < target_ns:
            time.sleep(.001)
        before = now()
        fn('obs_output_stop', None, ptr)(output)
        after = now()
        deadline = time.monotonic()+8
        while active(output) and time.monotonic() < deadline:
            time.sleep(.01)
        error = fn('obs_output_get_last_error', text, ptr)(output)
        result = dict(codec=args.codec, container=args.container, path=str(path),
            size=actual_size, requested_size=[args.width, args.height],
            fps_num=args.fps_num, fps_den=step, expected_frames=args.frames,
            stop_before_ns=before, stop_after_ns=after, start_us=start_us,
            active_after_stop=bool(active(output)), error=error.decode() if error else None,
            output_frames=fn('obs_output_get_total_frames', C.c_int, ptr)(output),
            packets=packets.copy(), bytes=path.stat().st_size if path.exists() else 0)
        results.append(result)
        (output_dir/'results.json').write_text(json.dumps(results, indent=2)+'\n')
        print('RECORDING', json.dumps({k:v for k,v in result.items() if k != 'packets'}), flush=True)
        assert not result['active_after_stop'] and not result['error'] and result['bytes'] > 1024
        # This bounds scheduling jitter independently of the requested N.
        assert (args.frames-1)*frame_ns < before-start_us*1000
        assert after-start_us*1000 < args.frames*frame_ns
        earlier = [p for p in packets if p['sys_dts_usec']*1000 < before]
        assert [p['pts'] for p in earlier] == [i*step for i in range(args.frames)], earlier
        fn('obs_output_remove_packet_callback', None, ptr, callback_type, ptr)(output, packet_callback, None)
        fn('obs_output_release', None, ptr)(output)
    fn('obs_encoder_release', None, ptr)(vencoder)
    fn('obs_encoder_release', None, ptr)(aencoder)
    set_source(0, None)
    fn('obs_source_release', None, ptr)(source)
    fn('obs_shutdown', None)()


if __name__ == '__main__':
    main()
