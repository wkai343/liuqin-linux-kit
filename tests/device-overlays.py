#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise board overlay staging and target path protection."""
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from device_overlay import stage_overlay

BOARD = PROJECT / 'device/board-overlay'
SLEEP = 'usr/lib/systemd/sleep.conf.d/30-liuqin-suspend.conf'
SCHEMA = 'usr/share/liuqin/power/io.github.liuqin.power.gschema.xml'


class DeviceOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_generic_board_does_not_require_gnome_or_ship_desktop_actions(self):
        target = self.root / 'payload'
        stage_overlay(target, board_overlay=BOARD)
        for name in (SLEEP, 'usr/libexec/liuqin-rtc', 'usr/local/sbin/liuqin-wlan-mac',
                     'usr/local/sbin/liuqin-gnome-storage-guard'):
            self.assertEqual((target / name).read_bytes(), (BOARD / name).read_bytes())
        self.assertTrue((target / 'usr/libexec/liuqin-rtc').stat().st_mode & 0o111)
        for name in ('usr/local/bin/liuqin-shell','usr/local/sbin/liuqin-rescue',
                     'usr/local/sbin/liuqin-gnome-usb-rescue',
                     'etc/systemd/system/liuqin-gnome-usb-rescue.service'):
            self.assertFalse((target/name).exists(),name)
        for name in (SCHEMA, 'etc/dconf', 'usr/local/libexec/liuqin-power-key-action',
                     'usr/local/bin/liuqin-camera', 'usr/local/sbin/liuqin-switch',
                     'usr/local/sbin/liuqin-snap-root-admission', 'etc/flash-kernel'):
            self.assertFalse((target / name).exists(), name)

    def test_missing_board_fails_before_copying(self):
        with self.assertRaisesRegex(ValueError, 'overlay directory is unavailable'):
            stage_overlay(self.root / 'payload', board_overlay=self.root / 'missing')
        self.assertFalse((self.root / 'payload').exists())

    def test_existing_parent_file_or_link_fails_before_copying(self):
        board = self.root / 'board'; (board / 'etc').mkdir(parents=True)
        (board / 'first.conf').write_text('must not be copied')
        (board / 'etc/policy.conf').write_text('new')
        outside = self.root / 'outside'; outside.mkdir()
        for kind in ('file', 'symlink'):
            with self.subTest(kind=kind):
                target = self.root / kind; target.mkdir()
                if kind == 'file':
                    (target / 'etc').write_text('existing')
                else:
                    (target / 'etc').symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'parent is not a directory'):
                    stage_overlay(target, board_overlay=board)
                self.assertFalse((target / 'first.conf').exists())
                self.assertFalse((outside / 'policy.conf').exists())
                if kind == 'file':
                    self.assertEqual((target / 'etc').read_text(), 'existing')

    def test_existing_payload_is_not_overwritten(self):
        board = self.root / 'board'; board.mkdir()
        (board / 'config').write_text('new')
        target = self.root / 'payload'; target.mkdir()
        (target / 'config').write_text('existing')
        with self.assertRaisesRegex(ValueError, 'target already exists'):
            stage_overlay(target, board_overlay=board)
        self.assertEqual((target / 'config').read_text(), 'existing')



if __name__ == '__main__':
    unittest.main(verbosity=2)
