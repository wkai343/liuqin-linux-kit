#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure physical speaker output before, during and after a local RTP/Opus call."""
import argparse
from array import array
from collections import deque
from contextlib import contextmanager, ExitStack
import cmath
import json
import math
from pathlib import Path
import socket
from statistics import median
import subprocess
import threading
import time

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

SINK = 'alsa_output.platform-sound.HiFi__Speaker__sink'
MIC = 'alsa_input.platform-sound.HiFi__Mic__source'
TONES = (1090, 1730)
CHANNELS = 4
FRAMES = 4800  # 100 ms at 48 kHz
DECIMATE = 4
REFS = [[cmath.exp(2j * math.pi * f * k / 12000) for k in range(FRAMES // DECIMATE)]
        for f in TONES]


def run(*args):
    return subprocess.check_output(args, text=True, timeout=8).strip()


def wait_for(check):
    deadline = time.monotonic() + 7
    while not check():
        if time.monotonic() >= deadline:
            raise RuntimeError('Timed out waiting for audio buffers')
        time.sleep(.05)


@contextmanager
def microphone():
    # Keep only the two tone amplitudes for each channel; discard the PCM window.
    windows = deque(maxlen=20)
    args = ['parec', '--device='+MIC, '--raw', '--format=s16le', '--rate=48000', '--channels=4',
            '--channel-map=front-left,front-right,rear-left,rear-right', '--no-remix', '--latency-msec=50']
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)

    def read():
        size = FRAMES * CHANNELS * 2
        while len(block := proc.stdout.read(size)) == size:
            pcm = array('h', block)
            levels = []
            for ch in range(CHANNELS):
                samples = pcm[ch::CHANNELS * DECIMATE]
                levels.append([2 * abs(sum(a*b for a, b in zip(samples, ref))) / len(samples)
                               for ref in REFS])
            windows.append((time.monotonic(), levels))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        wait_for(lambda: len(windows) >= 2)
        yield windows
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        reader.join(timeout=3)
        proc.stdout.close()


@contextmanager
def playing(description):
    pipeline = Gst.parse_launch(description)
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('GStreamer could not start the test stream')
        yield pipeline
        error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if error:
            raise RuntimeError(error.parse_error())
    finally:
        pipeline.set_state(Gst.State.NULL)


@contextmanager
def call():
    with socket.socket(type=socket.SOCK_DGRAM) as a, socket.socket(type=socket.SOCK_DGRAM) as b:
        a.bind(('127.0.0.1', 0))
        b.bind(('127.0.0.1', 0))
        down, up = a.getsockname()[1], b.getsockname()[1]

    def receive(port):
        return (f'udpsrc address=127.0.0.1 port={port} '
                'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=96" '
                '! rtpopusdepay ! opusdec ! audioconvert ! audioresample ! ')

    def send(port):
        return ('audioconvert ! audioresample ! audio/x-raw,channels=1,rate=48000 ! opusenc '
                f'! rtpopuspay pt=96 ! udpsink host=127.0.0.1 port={port} sync=false async=false')

    with ExitStack() as stack:
        peer = stack.enter_context(playing(
            receive(up) + 'fakesink name=uplink sync=false async=false signal-handoffs=true '
            'audiotestsrc is-live=true name=tone volume=0 ! ' + send(down)))
        uplink_buffers = 0

        def received(*_):
            nonlocal uplink_buffers
            uplink_buffers += 1

        peer.get_by_name('uplink').connect('handoff', received)
        stack.enter_context(playing(
            receive(down) + 'pulsesink stream-properties="props,media.role=(string)phone" '
            'pulsesrc stream-properties="props,media.role=(string)phone" ! ' + send(up)))
        wait_for(lambda: uplink_buffers >= 5)
        yield peer.get_by_name('tone')


@contextmanager
def music_player():
    # Feed raw PCM to a desktop player so media keeps its place when the call
    # temporarily unlinks its sink. Only the player supplies the playback clock.
    args = ['paplay', '--raw', '--format=s16le', '--rate=48000', '--channels=2',
            '--latency-msec=50', '--property=media.role=music']
    with subprocess.Popen(args, stdin=subprocess.PIPE, bufsize=0) as player:
        try:
            with playing('audiotestsrc name=tone volume=0 ! '
                         'audio/x-raw,format=S16LE,rate=48000,channels=2 ! '
                         f'fdsink fd={player.stdin.fileno()} sync=false') as source:
                yield source.get_by_name('tone')
        finally:
            player.terminate()


def measure(windows, tone, mode, check_dsp):
    observed = {}
    try:
        for hz in (0, *TONES):
            tone.set_property('freq', hz or TONES[0])
            tone.set_property('volume', .35 if hz else 0)
            start = time.monotonic() + .65
            wait_for(lambda: sum(t > start for t, _ in list(windows)) >= 5)
            rows = [levels for t, levels in list(windows) if t > start]
            observed[hz] = [[median(row[ch][i] for row in rows) for i in range(len(TONES))]
                            for ch in range(CHANNELS)]
    finally:
        tone.set_property('volume', 0)
    contrast = []
    for ch in range(CHANNELS):
        row = []
        for i, hz in enumerate(TONES):
            floor = max(.4, observed[0][ch][i], observed[TONES[1-i]][ch][i])
            row.append(round(20 * math.log10(max(.01, observed[hz][ch][i]) / floor), 1))
        contrast.append(row)
    if min(min(ch) for ch in contrast) < 12:
        raise RuntimeError(f'{mode}: the built-in microphones did not confirm both tones: {contrast}')
    result = {'mode': mode, 'tone_contrast_db': contrast}
    if check_dsp:
        check = Path(__file__).with_name('speaker-tuning-device.py')
        result['dsp'] = run('sudo', '-n', 'python3', str(check), mode).splitlines()
    return result


def main(check_dsp):
    if (run('pactl', 'get-default-sink') != SINK or run('pactl', 'get-default-source') != MIC):
        raise RuntimeError('Select the tablet speakers and built-in microphone before measuring')
    Gst.init(None)
    with microphone() as windows, music_player() as tone:
        report = {'before_call': measure(windows, tone, 'music', check_dsp)}
        with call() as peer_tone:
            report['in_call'] = measure(windows, peer_tone, 'voice', check_dsp)
        report['after_call'] = measure(windows, tone, 'music', check_dsp)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-dsp', action='store_true', help='also read amplifier state using sudo -n')
    main(parser.parse_args().check_dsp)
