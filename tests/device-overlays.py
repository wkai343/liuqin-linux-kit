#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise overlay boundaries and the GNOME DEB with synthetic build inputs."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from device_overlay import stage_overlay

BOARD = PROJECT / 'device/board-overlay'
GNOME = PROJECT / 'device/gnome-overlay'
SLEEP = 'usr/lib/systemd/sleep.conf.d/30-liuqin-suspend.conf'
SCHEMA = 'usr/share/liuqin/power/io.github.liuqin.power.gschema.xml'


class DeviceOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_generic_board_does_not_require_gnome_or_ship_desktop_actions(self):
        target = self.root / 'payload'
        stage_overlay(target, board_overlay=BOARD, gnome_overlay=self.root / 'missing')
        for name in (SLEEP, 'usr/libexec/liuqin-rtc', 'usr/local/sbin/liuqin-wlan-mac',
                     'usr/local/sbin/liuqin-gnome-storage-guard'):
            self.assertEqual((target / name).read_bytes(), (BOARD / name).read_bytes())
        self.assertTrue((target / 'usr/libexec/liuqin-rtc').stat().st_mode & 0o111)
        for name in (SCHEMA, 'etc/dconf', 'usr/local/libexec/liuqin-power-key-action',
                     'usr/local/bin/liuqin-camera', 'usr/local/sbin/liuqin-switch',
                     'usr/local/sbin/liuqin-snap-root-admission', 'etc/flash-kernel'):
            self.assertFalse((target / name).exists(), name)

    def test_gnome_adds_integration_to_the_same_board_policy(self):
        target = self.root / 'payload'
        stage_overlay(target, board_overlay=BOARD, gnome_overlay=GNOME, desktop='gnome')
        self.assertEqual((target / SLEEP).read_bytes(), (BOARD / SLEEP).read_bytes())
        self.assertEqual((target / SCHEMA).read_bytes(), (GNOME / SCHEMA).read_bytes())
        self.assertTrue((target / 'usr/local/bin/liuqin-camera').is_file())
        self.assertTrue((target / 'usr/local/sbin/liuqin-switch').is_file())
        self.assertTrue((target / 'usr/local/sbin/liuqin-snap-root-admission').is_file())
        self.assertFalse((target / 'etc/systemd/system/apt-daily.timer').is_symlink())

    def test_conflicting_layers_fail_before_copying(self):
        board = self.root / 'board'; gnome = self.root / 'gnome'
        for source in (board, gnome):
            (source / 'etc').mkdir(parents=True)
            (source / 'etc/policy.conf').write_text(str(source))
        (board / 'etc/first.conf').write_text('must not be copied on failure')
        with self.assertRaisesRegex(ValueError, 'overlapping overlay path'):
            stage_overlay(self.root / 'payload', board_overlay=board,
                          gnome_overlay=gnome, desktop='gnome')
        self.assertFalse((self.root / 'payload').exists())

    def test_missing_gnome_layer_does_not_leave_a_partial_payload(self):
        with self.assertRaisesRegex(ValueError, 'overlay directory is unavailable'):
            stage_overlay(self.root / 'payload', board_overlay=BOARD,
                          gnome_overlay=self.root / 'missing', desktop='gnome')
        self.assertFalse((self.root / 'payload').exists())

    def test_file_directory_conflict_fails_before_copying_in_either_layer_order(self):
        leaf = self.root / 'leaf'; nested = self.root / 'nested'
        leaf.mkdir(); (nested / 'etc/policy').mkdir(parents=True)
        (leaf / 'etc').write_text('file where the other layer needs a directory')
        (nested / 'etc/policy/example.conf').write_text('nested policy')
        for board, gnome in ((leaf, nested), (nested, leaf)):
            with self.subTest(board=board):
                with self.assertRaisesRegex(ValueError, 'overlapping overlay path'):
                    stage_overlay(self.root / 'payload', board_overlay=board,
                                  gnome_overlay=gnome, desktop='gnome')
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

    def test_gnome_deb_composes_both_overrides_and_keeps_package_contract(self):
        # The changed code assembles payloads, not C helpers. Use a compiler
        # stub with an AArch64 ELF header; never execute target binaries.
        board = self.root / 'board'; gnome = self.root / 'gnome'
        shutil.copytree(BOARD, board, symlinks=True)
        shutil.copytree(GNOME, gnome, symlinks=True)
        (board / 'etc/board-fixture.conf').write_text('board override\n')
        (gnome / 'etc/gnome-fixture.conf').write_text('GNOME override\n')
        elf = self.root / 'arm64-elf'
        elf.write_bytes(b'\x7fELF' + bytes([2, 1, 1, 0]) + bytes(8) +
                        struct.pack('<HHIQQQIHHHHHH', 2, 183, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0))
        compiler = self.root / 'cc'
        compiler.write_text('#!/usr/bin/env python3\nimport os,shutil,sys\n'
                            'shutil.copyfile(os.environ["OVERLAY_TEST_ELF"],'
                            'sys.argv[sys.argv.index("-o")+1])\n')
        compiler.chmod(0o755)
        alsa = self.root / 'ubuntu/usr/lib/aarch64-linux-gnu/libasound.so.2.0.0'
        alsa.parent.mkdir(parents=True); alsa.write_bytes(b'synthetic link input')
        settings = self.root / 'settings'; settings.write_bytes(b'synthetic settings')
        busybox = self.root / 'busybox'; busybox.write_bytes(b'synthetic busybox'); busybox.chmod(0o755)
        output = self.root / 'debs'
        env = dict(os.environ, DEVICE_DESKTOP='gnome', BOARD_OVERLAY=str(board),
                   GNOME_OVERLAY=str(gnome), OUT_DIR=str(output), BUSYBOX=str(busybox),
                   BUSYBOX_SHA256=hashlib.sha256(busybox.read_bytes()).hexdigest(),
                   POWER_KEY_CC=str(compiler), OVERLAY_TEST_ELF=str(elf),
                   UBUNTU_DESKTOP_ROOT=str(self.root / 'ubuntu'), POWER_SETTINGS_BINARY=str(settings),
                   POWER_SETTINGS_SHA256=hashlib.sha256(settings.read_bytes()).hexdigest())
        subprocess.run(['sh', PROJECT / 'build/scripts/build-liuqin-debs.sh', 'device-support'],
                       env=env, check=True, capture_output=True, text=True)
        package = next(output.glob('*.deb')); payload = self.root / 'extracted'
        subprocess.run(['dpkg-deb', '-x', package, payload], check=True)
        for name, text in (('board', 'board override\n'), ('gnome', 'GNOME override\n')):
            self.assertEqual((payload / f'etc/{name}-fixture.conf').read_text(), text)
        self.assertEqual((payload / SLEEP).read_bytes(), (BOARD / SLEEP).read_bytes())
        self.assertEqual((payload / SCHEMA).read_bytes(), (GNOME / SCHEMA).read_bytes())
        self.assertTrue((payload / 'usr/share/liuqin/power/gschemas.compiled').is_file())
        self.assertEqual((payload / 'usr/bin/gnome-control-center').read_bytes(), settings.read_bytes())
        self.assertIn('marker=/etc/liuqin-native-root',
                      (payload / 'usr/local/sbin/liuqin-gnome-storage-guard').read_text())
        depends = subprocess.check_output(['dpkg-deb', '-f', package, 'Depends'], text=True)
        self.assertIn('gnome-control-center', depends)
        control = self.root / 'control'
        subprocess.run(['dpkg-deb', '-e', package, control], check=True)
        self.assertIn('/usr/bin/gnome-control-center', (control / 'preinst').read_text())
        self.assertIn('/etc/board-fixture.conf', (control / 'conffiles').read_text())
        self.assertFalse((payload / 'etc/systemd/system/apt-daily.timer').is_symlink())


if __name__ == '__main__':
    unittest.main(verbosity=2)
