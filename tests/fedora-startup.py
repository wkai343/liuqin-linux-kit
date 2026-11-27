#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regress greeter activation and protected audio preparation before PCM probing."""
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
PLASMA = PROJECT / 'build/rootfs/plasma-overlay'
HIFI = PROJECT / 'device/board-overlay/usr/share/alsa/ucm2/Qualcomm/sm8450/Xiaomi-Pad-6-Pro/HiFi.conf'


class StartupTests(unittest.TestCase):
    def test_greeter_requires_a_live_compositor_socket_but_regular_desktops_do_not(self):
        helper = PLASMA / 'usr/libexec/liuqin-plasma-power-ready'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'id').write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_USER"\n')
            (root / 'systemctl').write_text('#!/bin/sh\n[ "$*" = "--user is-active --quiet plasma-login-kwin_wayland.service" ] && [ "$TEST_KWIN" = active ]\n')
            for name in ('id', 'systemctl'):
                (root / name).chmod(0o755)
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                       XDG_RUNTIME_DIR=directory, WAYLAND_DISPLAY='wayland-0')
            def check(user, compositor):
                return subprocess.run([helper], env=dict(env, TEST_USER=user, TEST_KWIN=compositor)).returncode
            self.assertEqual(check('wk', 'inactive'), 0)
            self.assertEqual(check('plasmalogin', 'inactive'), 1)
            self.assertEqual(check('plasmalogin', 'active'), 1)
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(root / 'wayland-0'))
                self.assertEqual(check('plasmalogin', 'active'), 0)
                self.assertEqual(check('plasmalogin', 'inactive'), 1)
            (root / 'wayland-0').unlink()
            self.assertEqual(check('plasmalogin', 'active'), 1)

    def test_pcm_probe_prepares_protected_dsp_while_muted_before_routing_backend(self):
        verb = HIFI.read_text().split('SectionDevice."Speaker"', 1)[0]
        sequence = verb.split('True.EnableSequence [', 1)[1].split(']', 1)[0]
        route = sequence.index("TERTIARY_TDM_RX_0 Audio Mixer MultiMedia1")
        for position in ('TL', 'TR', 'BL', 'BR'):
            mute = sequence.index(position + " Digital PCM Volume' 0")
            preload = sequence.index(position + " DSP1 Preload Switch' 1")
            dre = sequence.index(position + " DRE Switch' 1")
            source = sequence.index(position + " PCM Source' DSP")
            self.assertLess(mute, preload)
            self.assertLess(mute, dre)
            self.assertLess(dre, route)
            self.assertLess(preload, source)
            self.assertLess(source, route)
        self.assertNotRegex(sequence, r"PCM Volume' [1-9]")
        self.assertIn('False.EnableSequence', verb)  # older kernels retain mute protection

    def test_speaker_restores_stock_gains_only_in_protected_enable(self):
        speaker = HIFI.read_text().split('SectionDevice."Speaker" {', 1)[1].split('If.mic_pcm', 1)[0]
        setup, guarded = speaker.split('If.protection_owner {', 1)
        enable, disable = setup.split('DisableSequence [', 1)
        condition, rest = guarded.split('True.EnableSequence [', 1)
        playback, fallback = rest.split(']', 1)
        self.assertIn("name='BL Speaker Calibration Loaded'", condition)
        self.assertNotIn(" PCM Volume'", fallback)
        for position in ('TL', 'TR', 'BL', 'BR'):
            for kind, stock in [('Analog', '18'), ('Digital', '817')]:
                control = rf"name='{position} {kind} PCM Volume' (\d+)"
                with self.subTest(position=position, kind=kind):
                    self.assertEqual(re.findall(control, enable), ['0'])
                    self.assertEqual(re.findall(control, disable), ['0'])
                    self.assertEqual(re.findall(control, playback), [stock])

    def test_speaker_pcm_restores_all_stock_noise_gate_fields_on_hw_free(self):
        verb, rest = HIFI.read_text().split('LibraryConfig.speaker.SubstiConfig.pcm.liuqin_speaker', 1)
        pcm, speaker = rest.split('SectionDevice."Speaker"', 1)
        prepare = verb.split('True.EnableSequence [', 1)[1].split(']', 1)[0]
        self.assertIn('type ctl_elems', pcm)
        self.assertIn('True.Value.PlaybackPCM "liuqin_speaker"', speaker)
        self.assertIn('False.Value.PlaybackPCM "hw:${CardId},0"', speaker)
        for position in ('TL', 'TR', 'BL', 'BR'):
            for field, idle, playing in [('Enable', 0, 63), ('Delay', 3, 7), ('Threshold', 3, 5)]:
                control = f'{position} HW Noise Gate {field}'
                with self.subTest(control=control):
                    self.assertIn(f"'{control}' {idle}", prepare)
                    self.assertIn(f'name "{control}" value {playing} preserve true', pcm)


if __name__ == '__main__':
    unittest.main(verbosity=2)
