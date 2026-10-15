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
            for name in ('installer', 'shared', 'device/installer'):
                shutil.copytree(PROJECT/name, root/name,
                    ignore=shutil.ignore_patterns('.venv', '__pycache__', '*.pyc'))
            for name in ('LICENSE', 'NOTICE', 'CREDITS.md', 'docs/INSTALLER-GUI.md'):
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

    def test_package_output_and_work_directories_are_separate(self):
        spec = importlib.util.spec_from_file_location('devbox_build', PROJECT / 'build/devbox/build.py')
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            previous = builder.PROJECT; builder.PROJECT = Path(directory)
            try: data = builder.config(PROJECT / 'build/devbox/build.example.json')
            finally: builder.PROJECT = previous
        self.assertTrue(data['image_work'].startswith('out/build/'))
        self.assertTrue(data['bundle_out'].startswith('out/releases/'))
        self.assertNotEqual(data['image_work'], data['bundle_out'])

    def exercise_image_export(self, corrupt=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = DemoEngine(lambda event: None, root / 'demo')
            scripts = root / 'build/scripts'; scripts.mkdir(parents=True)
            shutil.copyfile(PROJECT / 'build/scripts/build-liuqin-custom-bundle.sh', scripts / 'build-liuqin-custom-bundle.sh')
            shutil.copytree(PROJECT / 'shared', root / 'shared', ignore=shutil.ignore_patterns('__pycache__'))
            fake = scripts / 'build-liuqin-native-boot.sh'
            fake.write_text('set -eu\nmkdir -p "$OUT_DIR"\n'
                            '[ -z "$INSTALLER_RUNTIME" ]\n'
                            'cp "$LIUQIN_FIXTURE/boot.img" "$OUT_DIR/boot-liuqin-native.img"\n'
                            'printf "BUILD REPORT\\n" >"$OUT_DIR/report.txt"\n')
            assembled = root / 'out/build/root'; assembled.mkdir(parents=True)
            shutil.copyfile(demo.demo_bundle / 'rootfs.tar.gz', assembled / 'rootfs.tar.gz')
            from liuqin_gui.artifacts import boot_interface
            (assembled / 'native-root.contract').write_bytes(boot_interface(demo.demo_bundle/'boot.img')['contract'])
            if corrupt: (assembled / 'rootfs.tar.gz').write_bytes(b'corrupt archive')
            final = root / 'out/releases/custom'; work = root / 'out/build/images'
            installer = demo.demo_bundle / 'installer.img'
            original = installer.read_bytes()
            env = dict(os.environ, CUSTOM_ROOT_OUT=str(assembled), INSTALLER_IMAGE=str(installer),
                       OUT_DIR=str(final), BUILD_WORK_DIR=str(work), LIUQIN_FIXTURE=str(demo.demo_bundle))
            result = subprocess.run(['sh', scripts / 'build-liuqin-custom-bundle.sh'], env=env, capture_output=True, text=True)
            if corrupt:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(final.exists(), 'an invalid package must not be published')
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual({path.name for path in final.iterdir()}, {'boot.img', 'installer.img', 'rootfs.tar.gz'})
                self.assertTrue((work / 'boot/report.txt').is_file())
                self.assertFalse((work / 'installer').exists())
                self.assertEqual(installer.read_bytes(), original)
                self.assertEqual((final / 'installer.img').read_bytes(), original)
            demo.close()

    def test_export_keeps_build_reports_outside_three_file_package(self):
        self.exercise_image_export()

    def test_invalid_package_is_not_published(self):
        self.exercise_image_export(corrupt=True)

    def test_independent_installer_entry_has_no_target_root_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = DemoEngine(lambda event: None, root / 'demo')
            scripts = root / 'build/scripts'; scripts.mkdir(parents=True)
            shutil.copyfile(PROJECT / 'build/scripts/build-liuqin-installer.sh', scripts / 'build-liuqin-installer.sh')
            shutil.copytree(PROJECT / 'shared', root / 'shared', ignore=shutil.ignore_patterns('__pycache__'))
            fake = scripts / 'build-liuqin-native-boot.sh'
            fake.write_text('set -eu\n[ "$LIUQIN_ROOT_PROFILE" = installer ]\n'
                            '[ -n "$INSTALLER_RUNTIME" ]\n'
                            'mkdir -p "$OUT_DIR"\ncp "$LIUQIN_FIXTURE/installer.img" "$OUT_DIR/boot-liuqin-native.img"\n')
            out = root / 'out/build/installer'
            env = dict(os.environ, INSTALLER_KERNEL_SOURCE=str(root / 'own-source'),
                       INSTALLER_KERNEL_OUT=str(root / 'own-kernel'), INSTALLER_RUNTIME=str(root / 'own-runtime'),
                       OUT_DIR=str(out), LIUQIN_FIXTURE=str(demo.demo_bundle), NATIVE_ROOT_HASHES='missing-target-manifest')
            result = subprocess.run(['sh', scripts / 'build-liuqin-installer.sh'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((out / 'installer.img').read_bytes(), (demo.demo_bundle / 'installer.img').read_bytes())
            demo.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
