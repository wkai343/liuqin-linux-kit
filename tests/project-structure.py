#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify build and installer boundaries with isolated source trees."""
import importlib.util
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'installer'))
from liuqin_gui.demo import DemoEngine


class StructureTests(unittest.TestCase):
    def test_installer_package_excludes_local_environment_and_unrelated_files(self):
        spec = importlib.util.spec_from_file_location('installer_package', PROJECT / 'installer/package.py')
        packager = importlib.util.module_from_spec(spec); spec.loader.exec_module(packager)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('installer', 'shared', 'device/installer','device/recovery','device/boot','docs'):
                shutil.copytree(PROJECT/name, root/name,
                    ignore=shutil.ignore_patterns('.venv', '__pycache__', '*.pyc'))
            for name in ('LICENSE', 'NOTICE', 'CREDITS.md'):
                target = root/name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PROJECT/name, target)
            hidden = root/'installer/.venv/private.txt'; hidden.parent.mkdir()
            hidden.write_text('fixture that must not be exported')
            (root/'installer/local-notes.txt').write_text('local fixture')
            output = root/'installer.zip'
            with patch.object(packager, 'PROJECT', root):
                packager.package(output)
            with zipfile.ZipFile(output) as files:
                names = files.namelist()
                self.assertFalse(any('/.venv/' in name or name.endswith('/local-notes.txt') for name in names))
                self.assertIn('liuqin-linux-kit-installer/installer/install-gui.py', names)
                self.assertIn('liuqin-linux-kit-installer/docs/assets/installer-multiboot-plan.png', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin_boot_target.py', names)
                self.assertIn('liuqin-linux-kit-installer/device/installer/android-device.py', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin-android-profile.json', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin-miku-profile.json', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin_payload.py', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin_super.py', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin_managed.py', names)
                self.assertIn('liuqin-linux-kit-installer/shared/liuqin_partitions.py', names)
                self.assertIn('liuqin-linux-kit-installer/installer/liuqin_gui/partitioning.py', names)
                self.assertIn('liuqin-linux-kit-installer/installer/liuqin_gui/managed_linux.py', names)
                self.assertIn('liuqin-linux-kit-installer/docs/FULL-ROM-FLASH.md', names)

    def test_build_root_entry_does_not_require_installer_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build/scripts').mkdir(parents=True)
            shutil.copyfile(PROJECT / 'build/scripts/build-liuqin-custom-root.py', root / 'build/scripts/build-liuqin-custom-root.py')
            shutil.copytree(PROJECT / 'shared', root / 'shared', ignore=shutil.ignore_patterns('__pycache__'))
            result = subprocess.run([sys.executable, root / 'build/scripts/build-liuqin-custom-root.py', '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'installer').exists())

    def test_standalone_installer_worker_does_not_require_build_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'installer.zip'
            subprocess.run([sys.executable, PROJECT / 'installer/package.py', '--out', archive], check=True, capture_output=True)
            with zipfile.ZipFile(archive) as files:
                self.assertFalse(any('/build/' in name or '/inputs/' in name or '/out/' in name for name in files.namelist()))
                files.extractall(root)
            standalone = root / 'liuqin-linux-kit-installer'
            result = subprocess.run([sys.executable, standalone / 'installer/install-gui.py', '--worker', '--demo',
                                     '--state-dir', root / 'state'], input='{"action":"devices"}\n',
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertTrue(any(event['type'] == 'devices' and event['simulation'] for event in events))
            self.assertFalse((standalone / 'build').exists())


    def test_recovery_ram_base_has_no_target_root_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = DemoEngine(lambda event: None, root / 'demo')
            scripts = root / 'build/scripts'; scripts.mkdir(parents=True)
            (root/'build/recovery').mkdir()
            shutil.copyfile(PROJECT / 'build/recovery/build-ram-base.sh', root/'build/recovery/build-ram-base.sh')
            shutil.copytree(PROJECT / 'shared', root / 'shared', ignore=shutil.ignore_patterns('__pycache__'))
            fake = scripts / 'build-liuqin-native-boot.sh'
            fake.write_text('set -eu\n[ "$LIUQIN_ROOT_PROFILE" = installer ]\n'
                            '[ -n "$INSTALLER_RUNTIME" ]\n'
                            'mkdir -p "$OUT_DIR"\ncp "$LIUQIN_FIXTURE/installer.img" "$OUT_DIR/boot-liuqin-native.img"\n')
            out = root / 'out/build/installer'
            env = dict(os.environ, RECOVERY_KERNEL_SOURCE=str(root / 'own-source'),
                       RECOVERY_KERNEL_OUT=str(root / 'own-kernel'), RECOVERY_RUNTIME=str(root / 'own-runtime'),
                       OUT_DIR=str(out), LIUQIN_FIXTURE=str(demo.demo_bundle), NATIVE_ROOT_HASHES='missing-target-manifest')
            result = subprocess.run(['sh', root/'build/recovery/build-ram-base.sh'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((out / 'recovery-base.img').read_bytes(), (demo.demo_bundle / 'installer.img').read_bytes())
            demo.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
