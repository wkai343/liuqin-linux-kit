#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check visible DRM PRIME playback and replay after EOS on a V4L2 device."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time


def sample(value):
    codec, separator, path = value.partition('=')
    if not separator or codec not in ('h264', 'hevc', 'vp9') or not Path(path).is_file():
        raise argparse.ArgumentTypeError('expected h264|hevc|vp9=/path/to/short-video')
    return codec, Path(path).resolve()


def check(codec, video, output, env):
    ipc = output/(codec+'.socket')
    log = output/(codec+'.log')
    command = ['mpv', '--no-config', '--no-audio', '--keep-open=yes',
        '--hwdec=v4l2m2m', '--hwdec-software-fallback=no', '--geometry=960x540',
        '--input-ipc-server='+str(ipc), '--log-file='+str(log),
        '--msg-level=vd=debug,ffmpeg=debug', str(video)]
    result = dict(codec=codec, video=str(video), command=command, replays=[])
    with (output/(codec+'-console.log')).open('wb') as console:
        process = subprocess.Popen(command, env=env, stdout=console,
            stderr=subprocess.STDOUT, start_new_session=True)
        connection = socket.socket(socket.AF_UNIX)
        connection.settimeout(3)
        try:
            deadline = time.monotonic()+10
            while not ipc.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('mpv did not open its IPC socket')
                time.sleep(.05)
            connection.connect(str(ipc))
            stream = connection.makefile('rwb', buffering=0)
            sequence = 0

            def request(command):
                nonlocal sequence
                sequence += 1
                stream.write((json.dumps(dict(command=command, request_id=sequence))+'\n').encode())
                while True:
                    line = stream.readline()
                    if not line:
                        raise RuntimeError('mpv IPC closed unexpectedly')
                    reply = json.loads(line)
                    if reply.get('request_id') == sequence:
                        # The socket is ready before the first file is loaded.
                        if (command[0] == 'get_property' and
                                reply.get('error') == 'property unavailable'):
                            return None
                        if reply.get('error') != 'success':
                            raise RuntimeError(str(reply))
                        return reply.get('data')

            def until(predicate, description, timeout=10):
                deadline = time.monotonic()+timeout
                while not predicate():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('timed out: '+description)
                    time.sleep(.05)

            until(lambda: request(['get_property', 'eof-reached']), 'initial EOF')
            result['initial_end'] = request(['get_property', 'time-pos'])
            for index in range(2):
                request(['seek', .2, 'absolute+exact'])
                request(['set_property', 'pause', False])

                def moving():
                    position = request(['get_property', 'time-pos'])
                    return (position is not None and .3 < position < 1.7 and
                            not request(['get_property', 'eof-reached']))

                until(moving, 'new frames after seeking away from EOS', timeout=5)
                position = request(['get_property', 'time-pos'])
                hardware = request(['get_property', 'hwdec-current'])
                if hardware != 'v4l2m2m':
                    raise RuntimeError('unexpected decoder: '+str(hardware))
                request(['screenshot-to-file', str(output/(codec+f'-replay-{index}.png')), 'video'])
                until(lambda: request(['get_property', 'eof-reached']), 'replay EOF')
                result['replays'].append(dict(position=position, hardware=hardware,
                    end=request(['get_property', 'time-pos']),
                    open_fds=len(list(Path(f'/proc/{process.pid}/fd').iterdir()))))
            if not any('Decoder format:' in line and 'drm_prime' in line
                       for line in log.read_text().splitlines()):
                raise RuntimeError('decoder did not output DRM PRIME frames')
            result['passed'] = True
        finally:
            connection.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True, action='append', type=sample)
    parser.add_argument('--output', required=True, type=Path, help='new directory under out/')
    parser.add_argument('--library-path', help='optional candidate FFmpeg library directory')
    args = parser.parse_args()
    if len({codec for codec, _ in args.sample}) != len(args.sample):
        parser.error('use at most one sample per codec')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    if args.library_path:
        env['LD_LIBRARY_PATH'] = args.library_path
    results = []
    for codec, video in args.sample:
        try:
            result = check(codec, video, output, env)
        except Exception as error:
            results.append(dict(codec=codec, passed=False, error=str(error)))
            (output/'results.json').write_text(json.dumps(results, indent=2)+'\n')
            raise
        results.append(result)
        (output/'results.json').write_text(json.dumps(results, indent=2)+'\n')
        print('PASS:', codec, 'visible DRM PRIME playback and two replays after EOS', flush=True)


if __name__ == '__main__':
    main()
