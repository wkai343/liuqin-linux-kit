#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regress greeter activation and protected audio preparation before PCM probing."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
PLASMA = PROJECT / 'build/rootfs/plasma-overlay'


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
        path = PROJECT / 'device/board-overlay/usr/share/alsa/ucm2/Qualcomm/sm8450/Xiaomi-Pad-6-Pro/HiFi.conf'
        verb = path.read_text().split('SectionDevice."Speaker"', 1)[0]
        sequence = verb.split('True.EnableSequence [', 1)[1].split(']', 1)[0]
        route = sequence.index("TERTIARY_TDM_RX_0 Audio Mixer MultiMedia1")
        for position in ('TL', 'TR', 'BL', 'BR'):
            mute = sequence.index(position + " Digital PCM Volume' 0")
            preload = sequence.index(position + " DSP1 Preload Switch' 1")
            source = sequence.index(position + " PCM Source' DSP")
            self.assertLess(mute, preload)
            self.assertLess(preload, source)
            self.assertLess(source, route)
        self.assertNotIn("PCM Volume' 913", sequence)
        self.assertIn('False.EnableSequence', verb)  # older kernels retain mute protection


if __name__ == '__main__':
    unittest.main(verbosity=2)
