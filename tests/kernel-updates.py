#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise real transaction code on regular temporary files, never block devices."""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import tempfile
import unittest
from unittest.mock import patch
from kernel_fixtures import PROJECT, kernel_fixture, install_fixture, archive_root, installer_fixture, put
from liuqin_artifacts import inspect_package, ArtifactError
from liuqin_kernel import digest, build_contract, extract_modules
from liuqin_gui.demo import demo_image

spec = importlib.util.spec_from_file_location('boot_update', PROJECT / 'device/boot/boot-update.py')
updater = importlib.util.module_from_spec(spec); spec.loader.exec_module(updater)
spec = importlib.util.spec_from_file_location('kernel_debs', PROJECT / 'build/scripts/build-liuqin-kernel-debs.py')
packager = importlib.util.module_from_spec(spec); spec.loader.exec_module(packager)


class FileDevice:
    def __init__(self, path, release):
        self.path, self.release = path, release
        self.ro, self.writes, self.failure = True, 0, None
        self.details = dict(serial='SIMULATED', slot='a', boot={'size': 65536}, root={'uuid': 'fixture'})
    def identity(self): return copy.deepcopy(self.details)
    def running_release(self): return self.release
    def read(self, output, expected):
        assert expected == self.identity()
        put(Path(output), self.path.read_bytes())
    def write(self, source, expected):
        assert expected == self.identity() and self.ro
        self.ro = False; self.writes += 1
        failure, self.failure = self.failure, None
        try:
            data = Path(source).read_bytes().ljust(65536, b'\0')
            if failure == 'interrupt':
                self.path.write_bytes(data[:100]); raise InterruptedError('simulated interruption')
            if failure == 'corrupt': data = b'wrong' + data[5:]
            self.path.write_bytes(data)
        finally: self.ro = True


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)
        self.root = self.base / 'root'; self.root.mkdir()
        self.old, self.new = '6.17.0-liuqin1', '6.18.0-liuqin1'
        self.first = kernel_fixture(self.base / 'old', self.old)
        self.second = kernel_fixture(self.base / 'new', self.new)
        for release, data in ((self.old, self.first), (self.new, self.second)): install_fixture(self.root, release, data)
        self.block = self.base / 'fake-boot-a'; self.block.write_bytes(self.first[2].read_bytes().ljust(65536, b'\0'))
        self.original = self.block.read_bytes()
        self.device = FileDevice(self.block, self.old)
        self.update = updater.Updater(self.root, self.device)
        self.update.enable()
    def tearDown(self): self.temp.cleanup()

    def test_upgrade_confirmation_and_rollback_preserve_old_modules(self):
        record = self.update.apply(self.new)
        self.assertEqual(record['state'], 'verified'); self.assertTrue(self.device.ro)
        self.assertTrue((self.root / f'usr/lib/modules/{self.old}/kernel/demo.ko').exists())
        with self.assertRaisesRegex(RuntimeError, '尚未启动'): self.update.confirm()
        self.device.release = self.new
        self.assertEqual(self.update.confirm()['state'], 'confirmed')
        self.assertEqual(self.update.rollback()['state'], 'rolled-back')
        self.assertEqual(self.block.read_bytes(), self.original)

    def test_btrfs_root_rejects_an_ext4_only_candidate_before_boot_write(self):
        put(self.root/'etc/liuqin-root-filesystem',b'btrfs\n')
        with self.assertRaisesRegex(RuntimeError,'不支持当前根文件系统'):
            self.update.plan(self.new)
        self.assertEqual(self.device.writes,0)
        self.assertEqual(self.block.read_bytes(), self.original)

    def test_repeating_same_update_never_replaces_original_backup(self):
        record = self.update.apply(self.new); count = self.device.writes
        self.update.apply(self.new)
        self.assertEqual(self.device.writes, count)
        self.assertEqual((self.update.state / record['transaction'] / 'previous.img').read_bytes(), self.original)

    def test_corruption_and_interruption_restore_and_reseal(self):
        for failure in ('corrupt', 'interrupt'):
            with self.subTest(failure=failure):
                self.device.failure = failure
                with self.assertRaises((RuntimeError, InterruptedError)): self.update.apply(self.new)
                self.assertTrue(self.device.ro); self.assertEqual(self.block.read_bytes(), self.original)
                self.assertEqual(self.update.current()['state'], 'rolled-back')

    def test_pending_write_requires_recovery_not_a_new_backup(self):
        record = self.update.apply(self.new)
        record['state'] = 'writing'; self.update.save(self.update.state / 'current.json', record)
        self.block.write_bytes(b'partial boot'); before = self.device.writes
        with self.assertRaisesRegex(RuntimeError, '中断'): self.update.apply(self.new)
        self.assertEqual(self.device.writes, before)
        self.update.rollback(); self.assertEqual(self.block.read_bytes(), self.original)

    def test_wrong_device_or_changed_modules_stop_before_write(self):
        self.device.details['serial'] = 'OTHER'
        with self.assertRaisesRegex(RuntimeError, '不匹配'): self.update.apply(self.new)
        self.device.details['serial'] = 'SIMULATED'
        (self.root / f'usr/lib/modules/{self.new}/kernel/demo.ko').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, '模块损坏'): self.update.apply(self.new)
        self.assertEqual(self.device.writes, 0)

    def test_corrupted_backup_and_required_kernel_removal_are_refused(self):
        record = self.update.apply(self.new)
        (self.update.state / record['transaction'] / 'previous.img').write_bytes(b'corrupt')
        before = self.device.writes
        with self.assertRaisesRegex(RuntimeError, '备份损坏'): self.update.rollback()
        self.assertEqual(self.device.writes, before)
        for release in (self.old, self.new):
            with self.assertRaisesRegex(RuntimeError, '不能移除'): self.update.can_remove(release)

    def test_same_release_cannot_be_overwritten_by_another_kernel_build(self):
        with self.assertRaisesRegex(RuntimeError, '另一组'):
            self.update.check_version(self.old, '0' * 64, '1' * 64)

    def test_unversioned_legacy_module_directory_is_never_overwritten(self):
        from liuqin_kernel import parse_contract
        identity = parse_contract(self.first[3])
        directory = self.root / 'usr/share/liuqin/kernels' / self.old
        shutil.rmtree(directory)
        with self.assertRaisesRegex(RuntimeError, '旧模块目录'):
            self.update.check_version(self.old, identity['image_sha256'], identity['modules_sha256'])
        # dpkg configures the modules package before unpacking the image package.
        put(directory / 'modules.contract', self.first[3])
        self.update.check_version(self.old, identity['image_sha256'], identity['modules_sha256'])

    def test_disable_does_not_touch_boot_and_recovery_still_works(self):
        self.update.apply(self.new)
        count = self.device.writes
        self.update.disable()
        self.assertFalse(self.update.enabled.exists()); self.assertEqual(self.device.writes, count)
        self.update.rollback(); self.assertEqual(self.block.read_bytes(), self.original)

    def test_disabled_package_hook_never_accesses_device(self):
        self.update.disable()
        with patch.object(updater, 'Updater', return_value=self.update), \
             patch.object(updater.os, 'geteuid', return_value=0), \
             patch.object(updater.os, 'umask'), patch.object(updater.signal, 'signal'), \
             patch.object(sys, 'argv', ['boot-update', 'hook', self.new]), \
             patch.object(self.device, 'identity', side_effect=AssertionError('unexpected device access')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            updater.main()
        self.assertEqual(json.loads(output.getvalue())['state'], 'disabled-or-unrelated')
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.block.read_bytes(), self.original)

    def test_module_extraction_rechecks_contents_after_initial_admission(self):
        import tarfile
        modules = self.second[1]
        with tarfile.open(modules / 'modules.tar', 'w') as archive:
            for name, payload in ((f'usr/lib/modules/{self.new}/modules.dep', b''),
                                  (f'usr/lib/modules/{self.new}/kernel/demo.ko', b'changed after validation')):
                item = tarfile.TarInfo(name); item.size = len(payload); item.mode = 0o644
                archive.addfile(item, io.BytesIO(payload))
        with self.assertRaisesRegex(ValueError, '构建期间发生变化'):
            extract_modules(modules, self.base / 'staging', self.second[3])
        self.assertEqual(self.device.writes, 0)

    def test_plan_reads_only_and_device_number_change_after_reboot_is_accepted(self):
        plan = self.update.plan(self.new)
        self.assertEqual(plan['target_release'], self.new); self.assertEqual(self.device.writes, 0)
        self.update.apply(self.new); self.device.release = self.new
        self.device.details['boot']['node'] = 'changed-linux-node'
        self.assertEqual(self.update.confirm()['state'], 'confirmed')

    def test_unsupported_boot_interface_is_rejected_before_any_write(self):
        old = demo_image({'etc/liuqin-storage-mode': b'persistent\n',
                          'etc/liuqin-root-profile': b'native-custom\n',
                          'etc/liuqin-native-root.contract': b'LIUQIN_NATIVE_ROOT_CONTRACT_V1\n'})
        self.block.write_bytes(old.ljust(65536, b'\0'))
        before = self.block.read_bytes(); count = self.device.writes
        for operation in (self.update.enable, lambda: self.update.plan(self.new), lambda: self.update.apply(self.new)):
            with self.assertRaises(ValueError): operation()
            self.assertEqual(self.device.writes, count)
            self.assertEqual(self.block.read_bytes(), before)

    def test_real_write_path_restores_readonly_flags_even_when_io_fails(self):
        device = updater.Device()
        context = dict(boot={'parent': 'sde', 'node': 'sde1', 'dev': '8:65', 'size': 65536}, parent_dev='8:64')
        flags = {}; parent_file = self.base / 'parent'; parent_file.touch()
        @contextlib.contextmanager
        def node(name, _number, writable=False):
            fd = os.open(parent_file if name == 'sde' else self.block, os.O_RDWR)
            flags.setdefault(fd, 1)
            try: yield fd
            finally: os.close(fd)
        calls = []
        def set_ro(fd, value): flags[fd] = value; calls.append(value)
        def ioctl(_fd, request, _value=0):
            if request == updater.BLKGETSIZE64: return struct.pack('Q', 65536)
            self.assertEqual(request, updater.BLKFLSBUF)
            return 0
        with patch.object(device, 'identity', return_value=context), patch.object(device, 'node', side_effect=node), \
             patch.object(Path, 'glob', return_value=[]), patch.object(updater, 'ro', side_effect=lambda fd: flags[fd]), \
             patch.object(updater, 'set_ro', side_effect=set_ro), patch.object(updater.fcntl, 'ioctl', side_effect=ioctl):
            device.write(self.second[2], context)
            self.assertEqual(self.block.read_bytes(), self.second[2].read_bytes().ljust(65536, b'\0'))
            self.assertEqual(calls, [0, 0, 1, 1]); calls.clear()
            with patch.object(updater.os, 'write', side_effect=OSError('simulated write error')):
                with self.assertRaises(OSError): device.write(self.first[2], context)
            self.assertEqual(calls, [0, 0, 1, 1])

    def test_custom_root_builder_keeps_old_modules_and_legacy_identity(self):
        self.update.enabled.unlink()
        shutil.rmtree(self.update.state)
        put(self.root / 'usr/share/liuqin/kernel.release', (self.old + '\n').encode())
        put(self.root / 'usr/share/liuqin/kernel-modules.manifest', (self.first[1] / 'modules.manifest').read_bytes())
        prefix = [] if os.geteuid() == 0 else ['unshare', '--user', '--map-root-user']
        if prefix and subprocess.run(prefix + ['true'], capture_output=True).returncode:
            self.skipTest('user namespace unavailable; CI may run this test as root')
        (PROJECT / 'out').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='v2-root-test-', dir=PROJECT / 'out') as directory:
            output = Path(directory) / 'result'
            command = prefix + [sys.executable, str(PROJECT / 'build/scripts/build-liuqin-custom-root.py'), '--root', str(self.root),
                '--kernel-out', str(self.second[0]), '--modules-dir', str(self.second[1]), '--out', str(output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((output / f'rootfs/usr/lib/modules/{self.old}/kernel/demo.ko').exists())
            self.assertEqual((output / 'rootfs/usr/share/liuqin/kernel.release').read_text().strip(), self.old)

    def test_device_service_update_does_not_bind_boot_to_old_contents(self):
        self.update.enabled.unlink(); shutil.rmtree(self.update.state)
        (self.root / f'usr/lib/modules/{self.old}/build').symlink_to('/usr/src/old-headers')
        put(self.root / 'usr/local/sbin/liuqin-gnome-storage-guard', b'#!/bin/sh\n# updated implementation\nexit 0\n', 0o755)
        bundle = self.base / 'bundle'; bundle.mkdir()
        shutil.copyfile(self.second[2], bundle / 'boot.img')
        (bundle / 'installer.img').write_bytes(installer_fixture())
        archive_root(self.root, bundle / 'rootfs.tar.gz')
        self.assertEqual(inspect_package(bundle)['kernel_release'], self.new)
        from liuqin_gui.engine import Engine
        engine = Engine(lambda event: None, self.base / 'gui-state')
        try:
            engine.validate_bundle(bundle)
            self.assertEqual(engine.manifest['root_profile'], 'native-updatable')
            self.assertEqual(engine.manifest['installer_interface'], 'liuqin-installer-v2')
        finally: engine.close()
        put(self.root / f'usr/lib/modules/{self.new}/kernel/demo.ko', b'incorrect')
        archive_root(self.root, bundle / 'rootfs.tar.gz')
        with self.assertRaisesRegex(ArtifactError, '内核模块'): inspect_package(bundle)

    def test_real_device_backend_refuses_this_host_without_writing(self):
        with patch.object(updater.os, 'uname', return_value=type('Host', (), {'machine': 'x86_64'})()):
            with self.assertRaisesRegex(RuntimeError, '真实 liuqin'): updater.Device().identity()


@unittest.skipUnless(shutil.which('dpkg-deb'), 'run inside Devbox for Debian package tests')
class PackageTests(unittest.TestCase):
    def test_two_kernel_versions_have_disjoint_payloads_and_do_not_replace_legacy_package(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); installed = base / 'root'; installed.mkdir()
            for index, release in enumerate(('6.17.0-liuqin1', '6.18.0-liuqin1')):
                fixture = kernel_fixture(base / release, release)
                output = base / ('debs' + str(index))
                packager.build(*fixture[:3], output, '0.1.' + str(index))
                packages = list(output.glob('*.deb')); self.assertEqual(len(packages), 4)
                for package in packages:
                    subprocess.run(['dpkg-deb', '-x', str(package), str(installed)], check=True)
                self.assertTrue((installed / f'usr/lib/modules/{release}/kernel/demo.ko').exists())
                self.assertTrue((installed / f'usr/lib/liuqin/boot-images/{release}/boot.img').exists())
                info = subprocess.check_output(['dpkg-deb', '-f', str(next(output.glob('liuqin-kernel-updates_*.deb'))), 'Depends'], text=True)
                self.assertIn('liuqin-kernel-image-' + release, info)
            self.assertTrue((installed / 'usr/lib/modules/6.17.0-liuqin1/kernel/demo.ko').exists())
            self.assertFalse((installed / 'usr/share/liuqin/kernel.release').exists())
            self.assertFalse((installed / 'etc/liuqin/boot-updates.json').exists())


if __name__ == '__main__': unittest.main(verbosity=2)
