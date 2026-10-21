#!/usr/bin/env python3
"""Offline firmware transaction/package staging tests; no hardware or host services."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


firmware = load("firmware", ROOT / "device/fingerprint/oem/src/fingerprint/deployment/provision_firmware.py")
package = load("release_package", ROOT / "build/scripts/build-liuqin-fingerprint-deb.py")
image_builder = load("firmware_image", ROOT / "build/scripts/extract-liuqin-fingerprint-firmware.py")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.cache = self.base / "firmware"
        self.cache.mkdir(mode=0o700)
        self.data = {name: (name + " fixture").encode() for name in firmware.NAMES}
        self.hashes = {name: hashlib.sha256(data).hexdigest() for name, data in self.data.items()}
        (self.cache / "SHA256.json").write_text(json.dumps(self.hashes))
        self.mountpoint = self.base / "mountpoint"
        self.mountpoint.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def fake_command(self, argv, **kwargs):
        if argv[0] == "mount":
            self.assertEqual(argv[-2], "/dev/disk/by-partlabel/modem_a")
            self.assertEqual(argv[1:5], ["-t", "vfat", "-o",
                "ro,nodev,nosuid,noexec,codepage=437,iocharset=iso8859-1"])
            image = self.mountpoint / "image"
            image.mkdir()
            for name, value in self.data.items():
                (image / name).write_bytes(value)
        else:
            self.assertEqual(argv, ["umount", str(self.mountpoint)])
            for path in (self.mountpoint / "image").iterdir():
                path.unlink()
            (self.mountpoint / "image").rmdir()
        return SimpleNamespace(returncode=0)

    def provision(self):
        original = tempfile.mkdtemp

        def temporary(*args, **kwargs):
            if kwargs.get("dir") == "/run":
                return str(self.mountpoint)
            return original(*args, **kwargs)

        with patch.object(firmware.tempfile, "mkdtemp", side_effect=temporary), \
             patch.object(firmware.subprocess, "run", side_effect=self.fake_command) as command:
            firmware.provision(self.cache)
            return command.call_args_list

    def test_verified_cache_does_not_mount(self):
        for name, data in self.data.items():
            (self.cache / name).write_bytes(data)
        with patch.object(firmware.subprocess, "run") as command:
            firmware.provision(self.cache)
            command.assert_not_called()

    def test_first_boot_copies_fixed_members_read_only_and_unmounts(self):
        self.assertEqual(len(self.provision()), 2)
        self.assertFalse(self.mountpoint.exists())
        for name, value in self.data.items():
            self.assertEqual((self.cache / name).read_bytes(), value)
            self.assertEqual((self.cache / name).stat().st_mode & 0o777, 0o600)

    def test_bad_stock_leaves_existing_cache_unchanged_and_unmounts(self):
        prior = self.cache / firmware.NAMES[0]
        prior.write_bytes(b"existing-invalid-cache")
        self.data[firmware.NAMES[-1]] = b"wrong stock image"
        with self.assertRaisesRegex(RuntimeError, "stock FPC firmware hash mismatch"):
            self.provision()
        self.assertFalse(self.mountpoint.exists())
        self.assertEqual(prior.read_bytes(), b"existing-invalid-cache")
        self.assertEqual({path.name for path in self.cache.iterdir()}, {prior.name, "SHA256.json"})

    def test_manifest_cannot_expand_extraction(self):
        self.hashes["../secret"] = "0" * 64
        (self.cache / "SHA256.json").write_text(json.dumps(self.hashes))
        with patch.object(firmware.subprocess, "run") as command:
            with self.assertRaises(ValueError):
                firmware.provision(self.cache)
            command.assert_not_called()

    def test_verified_rom_directory_never_mounts_or_changes_partitions(self):
        source=self.base/'rom';source.mkdir()
        for name,data in self.data.items():(source/name).write_bytes(data)
        with patch.object(firmware.subprocess,'run',side_effect=AssertionError('no device access')):
            firmware.provision_from_directory(source,self.cache)
        for name,data in self.data.items():
            self.assertEqual((self.cache/name).read_bytes(),data)
            self.assertEqual((self.cache/name).stat().st_mode&0o777,0o600)

    def test_corrupt_rom_directory_keeps_existing_cache(self):
        source=self.base/'rom';source.mkdir()
        for name,data in self.data.items():(source/name).write_bytes(data)
        (source/firmware.NAMES[-1]).write_bytes(b'bad input')
        prior=self.cache/firmware.NAMES[0];prior.write_bytes(b'prior cache')
        with self.assertRaisesRegex(RuntimeError,'hash mismatch'):
            firmware.provision_from_directory(source,self.cache)
        self.assertEqual(prior.read_bytes(),b'prior cache')
        self.assertEqual({p.name for p in self.cache.iterdir()},{'SHA256.json',prior.name})

    def image_fixture(self):
        project = self.base / 'project'
        image = self.base / 'NON-HLOS.bin'
        image.write_bytes(b'synthetic FAT image')
        reference = project / 'shared/liuqin-rom-images.json'
        reference.parent.mkdir(parents=True)
        reference.write_text(json.dumps({'images': {'NON-HLOS.bin': {
            'bytes': image.stat().st_size, 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}}}))
        manifest = project / 'device/fingerprint/oem/firmware/SHA256.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps(self.hashes))
        provisioner = project / 'device/fingerprint/oem/src/fingerprint/deployment/provision_firmware.py'
        provisioner.parent.mkdir(parents=True)
        shutil.copyfile(Path(firmware.__file__), provisioner)
        return project, image

    def fake_mcopy(self, argv, **kwargs):
        self.assertEqual(argv[0], '/fixture/mcopy')
        self.assertEqual(argv[1], '-i')
        name = argv[3].removeprefix('::/image/')
        self.assertIn(name, firmware.NAMES)
        Path(argv[4]).write_bytes(self.data[name])
        return SimpleNamespace(returncode=0)

    def image_root(self, root):
        cache = root / 'usr/local/lib/liuqin-fpc-oem/firmware'
        cache.mkdir(parents=True, mode=0o700)
        (cache / 'SHA256.json').write_text(json.dumps(self.hashes))
        return cache

    def test_image_contains_all_verified_files_and_boot_needs_no_modem(self):
        project, image = self.image_fixture()
        root = self.base / 'root'
        cache = self.image_root(root)
        with patch.object(image_builder, 'PROJECT', project), \
             patch.object(image_builder.shutil, 'which', return_value='/fixture/mcopy'), \
             patch.object(image_builder.subprocess, 'run', side_effect=self.fake_mcopy):
            image_builder.install(image, root)
        for name, data in self.data.items():
            self.assertEqual((cache / name).read_bytes(), data)
            self.assertEqual((cache / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(((cache / name).stat().st_uid, (cache / name).stat().st_gid), (0, 0))
        with patch.object(firmware.subprocess, 'run', side_effect=AssertionError('boot must use embedded cache')):
            firmware.provision(cache)
        self.assertFalse((root / 'var').exists())

    def test_bad_image_is_rejected_before_extraction(self):
        project, image = self.image_fixture()
        image.write_bytes(b'changed stock image')
        root = self.base / 'root'
        cache = self.image_root(root)
        with patch.object(image_builder, 'PROJECT', project), patch.object(image_builder.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'NON-HLOS.bin'):
                image_builder.install(image, root)
            command.assert_not_called()
        self.assertEqual({p.name for p in cache.iterdir()}, {'SHA256.json'})

    def test_bad_extracted_file_keeps_image_cache_unchanged(self):
        project, image = self.image_fixture()
        root = self.base / 'root'
        cache = self.image_root(root)
        prior = cache / firmware.NAMES[0]
        prior.write_bytes(b'prior cache')
        self.data[firmware.NAMES[-1]] = b'corrupt extracted file'
        with patch.object(image_builder, 'PROJECT', project), \
             patch.object(image_builder.shutil, 'which', return_value='/fixture/mcopy'), \
             patch.object(image_builder.subprocess, 'run', side_effect=self.fake_mcopy):
            with self.assertRaisesRegex(ValueError, '固件校验失败'):
                image_builder.install(image, root)
        self.assertEqual(prior.read_bytes(), b'prior cache')
        self.assertEqual({p.name for p in cache.iterdir()}, {'SHA256.json', prior.name})

    def test_image_install_refuses_host_root_and_symlink_parents(self):
        project, image = self.image_fixture()
        root = self.base / 'root'
        cache = self.image_root(root)
        local = root / 'usr/local'
        outside = self.base / 'outside'
        local.rename(outside)
        local.symlink_to(outside, target_is_directory=True)
        with patch.object(image_builder.subprocess, 'run') as command:
            for destination in (Path('/'), root):
                with self.assertRaises(ValueError):
                    image_builder.install(image, destination)
            command.assert_not_called()
        self.assertEqual({p.name for p in (outside / 'lib/liuqin-fpc-oem/firmware').iterdir()}, {'SHA256.json'})

    def test_default_fedora_root_archive_embeds_firmware_before_publication(self):
        sys.path[:0] = [str(ROOT / 'build/lib'), str(ROOT / 'shared')]
        from fedora_flow import FedoraFlow
        from source_build import Cache
        project, image = self.image_fixture()
        apps = self.base / 'apps'
        apps.mkdir()
        (apps / 'root.tar.gz').write_bytes(b'fixture desktop')
        rpms = self.base / 'rpms'
        rpms.mkdir()
        pipeline = FedoraFlow(dict(rootfs_config=str(ROOT / 'build/rootfs/fedora-plasma.json'),
            kernel_fragments=[], kernel_localversion='-liuqin-test', kernel_source='sources/test'), None)
        def target(action, archive, output, **kwargs):
            self.assertEqual(action, 'kernel-files')
            self.image_root(output / 'rootfs')
        def run(argv, **kwargs):
            self.assertTrue(kwargs['root'])
            root = Path(argv[argv.index('--root') + 1])
            if argv[1].endswith('extract-liuqin-fingerprint-firmware.py'):
                image_builder.install(Path(argv[argv.index('--image') + 1]), root)
            else:
                output = Path(argv[argv.index('--out') + 1])
                output.mkdir()
                with tarfile.open(output / 'rootfs.tar.gz', 'w:gz') as tar:
                    tar.add(root / 'usr', arcname='usr')
                (output / 'native-root.contract').write_text('fixture contract')
        pipeline.run = run
        pipeline.cache = Cache(self.base / 'cache', run)
        with patch.object(pipeline, 'fingerprint_image', return_value=image), \
             patch.object(pipeline, 'apps', return_value=apps), \
             patch.object(pipeline, 'kernel_debs', return_value=rpms), \
             patch.object(pipeline, 'kernel', return_value=(self.base, self.base)), \
             patch.object(pipeline, 'modules', return_value=self.base), \
             patch.object(pipeline, 'target_action', side_effect=target), \
             patch.object(image_builder, 'PROJECT', project), \
             patch.object(image_builder.shutil, 'which', return_value='/fixture/mcopy'), \
             patch.object(image_builder.subprocess, 'run', side_effect=self.fake_mcopy):
            packed = pipeline.rootfs()
        with tarfile.open(packed / 'rootfs.tar.gz') as tar:
            for name, data in self.data.items():
                member = tar.getmember('usr/local/lib/liuqin-fpc-oem/firmware/' + name)
                self.assertEqual((member.uid, member.gid, member.mode), (0, 0, 0o600))
                self.assertEqual(tar.extractfile(member).read(), data)
        self.assertFalse(any(p.name.startswith('.attempt-') for p in pipeline.cache.root.iterdir()))

    def test_missing_fedora_firmware_stops_before_desktop_build(self):
        sys.path[:0] = [str(ROOT / 'build/lib'), str(ROOT / 'shared')]
        from fedora_flow import FedoraFlow
        pipeline = FedoraFlow(dict(rootfs_config=str(ROOT / 'build/rootfs/fedora-plasma.json'),
            rom_images=str(self.base / 'missing'), kernel_fragments=[],
            kernel_localversion='-liuqin-test', kernel_source='sources/test'), None)
        with patch.object(pipeline, 'apps', side_effect=AssertionError('must check firmware first')):
            with self.assertRaisesRegex(ValueError, 'NON-HLOS.bin'):
                pipeline.rootfs()

    def test_root_staging_has_no_account_services_or_credentials(self):
        payload = self.base / "payload"
        (payload / "deployment").mkdir(parents=True)
        conf = ROOT / "device/fingerprint/oem/src/fingerprint/libfprint-tod/fprintd-fpc1264-oem.conf"
        (payload / "deployment/fprintd-fpc1264-oem.conf").write_text(conf.read_text())
        root = self.base / "root"
        with patch.object(package.subprocess, "run") as command:
            package.stage_root(payload, root, {"deployment_mode": "installed"})
            command.assert_not_called()
        self.assertFalse((root / "var").exists())
        self.assertFalse((root / "etc/passwd").exists())
        self.assertFalse((root / "usr/share/applications").exists())
        self.assertIn("UMask=0077", (root / "etc/systemd/system/fprintd.service.d/60-liuqin-fpc-oem.conf").read_text())
        unit = (root / "usr/lib/systemd/system/liuqin-fpc-stable-module.service").read_text()
        self.assertNotIn("ssh", unit)
        self.assertNotIn("NetworkManager", unit)
        self.assertTrue((root / "etc/systemd/system/multi-user.target.wants/liuqin-fpc-stable-module.service").is_symlink())


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run as root in an isolated test container/user namespace")
    unittest.main()
