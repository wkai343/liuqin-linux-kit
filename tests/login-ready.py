#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the display/input admission gate without touching host devices."""
import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / 'build/rootfs/plasma-overlay/usr/libexec/liuqin-login-ready'
gate = types.ModuleType('login_ready')
exec(compile(HELPER.read_text(), str(HELPER), 'exec'), gate.__dict__)


class LoginReadyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sys = self.root / 'sys'; self.dev = self.root / 'dev'
        for name, value in [('SYS', self.sys), ('DEV', self.dev)]:
            p = patch.object(gate, name, value); p.start(); self.addCleanup(p.stop)
        self.put('firmware/devicetree/base/compatible', 'xiaomi,liuqin\0')
        self.put('class/drm/card2/device/of_node/compatible', 'qcom,sm8450-dpu\0')
        self.put('class/drm/card2/dev', '226:2')
        self.put('class/drm/card2-DSI-1/status', 'connected')
        self.put('class/drm/card2-DSI-1/modes', '1800x2880')
        self.put('class/drm/renderD130/dev', '226:130')
        (self.sys / 'class/drm/renderD130/device').symlink_to(self.sys / 'class/drm/card2/device')
        self.put('class/input/event6/device/name', 'NVTCapacitiveTouchScreen')
        self.put('class/input/event6/device/phys', 'input/ts')
        self.put('class/input/event6/dev', '13:70')
        self.put('class/input/event1/device/name', 'pmic_pwrkey')
        self.put('class/input/event1/device/device/of_node/compatible', 'qcom,pmk8350-pwrkey\0')
        self.put('class/input/event1/dev', '13:65')
        self.put('class/backlight/ktz8866-backlight/device/of_node/compatible', 'kinetic,ktz8866\0')
        self.put('class/backlight/ktz8866-backlight/max_brightness', '4095')
        self.ids = {'dri/card2': (226, 2), 'dri/renderD130': (226, 130),
                    'input/event6': (13, 70), 'input/event1': (13, 65)}
        for name in self.ids:
            p = self.dev / name; p.parent.mkdir(parents=True, exist_ok=True); p.touch()
        original = Path.lstat
        def fake_lstat(path):
            rel = str(path.relative_to(self.dev)) if path.is_relative_to(self.dev) else ''
            if rel in self.ids:
                return types.SimpleNamespace(st_mode=stat.S_IFCHR | 0o600,
                                             st_rdev=os.makedev(*self.ids[rel]))
            return original(path)
        p = patch.object(Path, 'lstat', fake_lstat); p.start(); self.addCleanup(p.stop)

    def put(self, name, value):
        p = self.sys / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)

    def test_waits_only_for_initialized_local_devices_with_dynamic_numbers(self):
        with patch.object(gate.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            gate.wait_ready()
        args = run.call_args.args[0]
        self.assertIn('--initialized=yes', args)
        self.assertNotIn('--settle', args)
        self.assertEqual(args[4:], list(map(str, gate.required_devices())))
        self.assertEqual(len(args[4:]), 6)
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertLessEqual(run.call_args.kwargs['timeout'], gate.TIMEOUT)

    def test_missing_touch_does_not_fall_back_to_keyboard_or_pen(self):
        self.put('class/input/event6/device/name', 'NVTCapacitivePen')
        with self.assertRaisesRegex(gate.NotReady, 'touchscreen'):
            gate.required_devices()

    def test_rejects_disconnected_display_and_mismatched_character_device(self):
        self.put('class/drm/card2-DSI-1/status', 'disconnected')
        with self.assertRaisesRegex(gate.NotReady, 'DSI'):
            gate.required_devices()
        self.put('class/drm/card2-DSI-1/status', 'connected')
        self.ids['dri/card2'] = (226, 9)
        with self.assertRaisesRegex(gate.NotReady, 'mismatched'):
            gate.required_devices()

    def test_wrong_board_and_abl_board_identity(self):
        self.put('firmware/devicetree/base/compatible', 'qcom,cape-mtp\0')
        self.put('firmware/devicetree/base/model', 'Liuqin based on Qualcomm Technologies, Inc SM8475\0')
        p = self.sys / 'firmware/devicetree/base/xiaomi,miboard-id'
        p.write_bytes(bytes.fromhex('0000001000000000'))
        gate.board_identity()
        p.write_bytes(bytes(8))
        with self.assertRaises(gate.NotReady):
            gate.board_identity()

    def test_symlink_device_node_is_not_admitted(self):
        node = self.dev / 'dri/card2'
        node.unlink(); node.symlink_to(self.dev / 'dri/renderD130')
        with self.assertRaisesRegex(gate.NotReady, 'noncanonical'):
            gate.required_devices()

    def test_timeout_and_udev_failure_cannot_admit_login(self):
        with patch.object(gate.time, 'monotonic', side_effect=[0, 9]):
            with self.assertRaisesRegex(gate.NotReady, 'timed out'):
                gate.wait_ready()
        with patch.object(gate.subprocess, 'run', side_effect=gate.subprocess.CalledProcessError(1, 'udevadm')):
            with self.assertRaises(gate.subprocess.CalledProcessError):
                gate.wait_ready()

    def test_device_disappearance_after_udev_is_rejected(self):
        def remove(*a, **kw):
            self.put('class/drm/card2-DSI-1/status', 'disconnected')
        with patch.object(gate.subprocess, 'run', side_effect=remove):
            with self.assertRaisesRegex(gate.NotReady, 'DSI'):
                gate.wait_ready()


if __name__ == '__main__':
    unittest.main(verbosity=2)
