#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package admission tests on synthetic files; no USB or device operations."""
import io
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from liuqin_gui.demo import DemoEngine, demo_image
from liuqin_gui.engine import Engine, InstallerError
from liuqin_gui.artifacts import ArtifactError, inspect_package, boot_interface


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.demo = DemoEngine(lambda event: None, self.root / 'demo')
        self.bundle = self.demo.demo_bundle
        self.engine = Engine(lambda event: None, self.root / 'real')

    def tearDown(self):
        self.engine.close()
        self.demo.close()
        self.temporary.cleanup()

    def rewrite_root(self, change):
        path = self.bundle / 'rootfs.tar.gz'
        stream = io.BytesIO()
        with tarfile.open(path) as source, tarfile.open(fileobj=stream, mode='w:gz') as target:
            for entry in source:
                content = source.extractfile(entry).read() if entry.isfile() else None
                entry, content = change(entry, content)
                if entry is not None:
                    if content is not None: entry.size = len(content)
                    target.addfile(entry, io.BytesIO(content) if content is not None else None)
        path.write_bytes(stream.getvalue())

    def test_three_files_without_json_accept_custom_root_without_snap_or_gnome(self):
        (self.bundle / 'bundle.json').unlink()
        self.engine.validate_bundle(self.bundle)
        self.assertEqual(self.engine.manifest['kernel_release'], '6.17-demo')
        self.assertEqual(self.engine.manifest['root_profile'], 'native-updatable')
        self.assertEqual(self.engine.manifest['status'], 'CUSTOM_UNVERIFIED')
        self.assertEqual(set(self.engine.manifest['files']), {'boot.img', 'installer.img', 'rootfs.tar.gz'})

    def test_cli_offline_check_accepts_independent_kernel(self):
        manifest_path = self.bundle / 'bundle.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['installer_interface'] = 'liuqin-installer-v2'
        manifest_path.write_text(json.dumps(manifest))
        cli = Path(__file__).resolve().parents[1] / 'installer/install-liuqin.py'
        result = subprocess.run([sys.executable, cli, '--bundle', self.bundle, '--check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no device access', result.stdout)

    def test_stale_or_malformed_json_does_not_bind_custom_artifacts(self):
        for metadata in ('not json', json.dumps({'kernel_release': 'other', 'files': {}, 'installer_layouts': []})):
            (self.bundle / 'bundle.json').write_text(metadata)
            self.engine.validate_bundle(self.bundle)
            self.assertEqual(self.engine.manifest['kernel_release'], '6.17-demo')

    def test_a_changed_valid_archive_changes_confirmation_identity(self):
        self.engine.validate_bundle(self.bundle)
        previous = self.engine.manifest_digest
        self.rewrite_root(lambda entry, data: (entry, data + b'# custom\n' if entry.name == 'etc/group' else data))
        self.engine.validate_bundle(self.bundle)
        self.assertNotEqual(previous, self.engine.manifest_digest)

    def test_independent_installer_accepts_a_different_target_kernel(self):
        path = self.bundle / 'installer.img'
        data = bytearray(path.read_bytes()); data[4096] ^= 1; path.write_bytes(data)
        result = inspect_package(self.bundle)
        self.assertEqual(result['installer_kernel_release'], '6.17-installer-demo')
        self.assertEqual(result['kernel_release'], '6.17-demo')

    def test_installer_is_reused_after_mutable_device_service_update(self):
        original = (self.bundle / 'installer.img').read_bytes()
        self.rewrite_root(lambda entry, data: (entry, b'UPDATED DEVICE GUARD\n'
                          if entry.name.endswith('/liuqin-gnome-storage-guard') else data))
        self.assertEqual(inspect_package(self.bundle)['kernel_release'], '6.17-demo')
        self.assertEqual((self.bundle / 'installer.img').read_bytes(), original)

    def test_installer_without_current_interface_is_rejected(self):
        boot = boot_interface(self.bundle / 'boot.img')
        records = {'etc/liuqin-root-profile': boot['profile'], 'etc/liuqin-native-root.contract': boot['contract'],
                   'etc/liuqin-storage-mode': b'readonly\n', 'etc/liuqin-installer': b'liuqin\n'}
        (self.bundle / 'installer.img').write_bytes(demo_image(records))
        with self.assertRaises(ArtifactError): inspect_package(self.bundle)

    def test_obsolete_target_root_profile_is_rejected(self):
        records = {'etc/liuqin-root-profile': b'native-custom\n',
                   'etc/liuqin-native-root.contract': b'LIUQIN_NATIVE_ROOT_CONTRACT_V1\n',
                   'etc/liuqin-storage-mode': b'persistent\n'}
        (self.bundle / 'boot.img').write_bytes(demo_image(records))
        with self.assertRaisesRegex(ArtifactError, '接口不受支持'): inspect_package(self.bundle)

    def test_unknown_independent_interface_and_stale_target_contract_are_rejected(self):
        records = {'etc/liuqin-storage-mode': b'readonly\n', 'etc/liuqin-installer': b'liuqin\n',
                   'etc/liuqin-installer-interface': b'liuqin-installer-v99\n',
                   'etc/liuqin-installer-kernel.release': b'6.17-installer\n'}
        (self.bundle / 'installer.img').write_bytes(demo_image(records))
        with self.assertRaisesRegex(ArtifactError, '接口版本'):
            inspect_package(self.bundle)
        records['etc/liuqin-installer-interface'] = b'liuqin-installer-v2\n'
        records['etc/liuqin-native-root.contract'] = boot_interface(self.bundle / 'boot.img')['contract']
        (self.bundle / 'installer.img').write_bytes(demo_image(records))
        with self.assertRaisesRegex(ArtifactError, '不能内置'):
            inspect_package(self.bundle)

    def test_wrong_device_tree_is_rejected(self):
        path = self.bundle / 'boot.img'
        path.write_bytes(path.read_bytes().replace(b'xiaomi,liuqin', b'xiaomi,otherx'))
        with self.assertRaisesRegex(ArtifactError, '设备树'):
            inspect_package(self.bundle)

    def test_wrong_installer_device_tree_is_rejected(self):
        path = self.bundle / 'installer.img'
        path.write_bytes(path.read_bytes().replace(b'xiaomi,liuqin', b'xiaomi,otherx'))
        with self.assertRaisesRegex(ArtifactError, '设备树'):
            inspect_package(self.bundle)

    def test_ram_installer_cannot_be_used_as_the_persistent_boot_image(self):
        (self.bundle / 'boot.img').write_bytes((self.bundle / 'installer.img').read_bytes())
        with self.assertRaisesRegex(ArtifactError, '角色不正确'):
            inspect_package(self.bundle)

    def test_truncated_boot_is_rejected(self):
        path = self.bundle / 'boot.img'; path.write_bytes(path.read_bytes()[:5000])
        with self.assertRaisesRegex(ArtifactError, '不完整'):
            inspect_package(self.bundle)

    def test_root_kernel_identity_mismatch_is_rejected(self):
        self.rewrite_root(lambda entry, data: (entry, data.replace(b'kernel_release=6.17-demo', b'kernel_release=6.18-other')
                                              if entry.name.endswith('/boot.contract') else data))
        with self.assertRaisesRegex(ArtifactError, '设备接口不匹配'):
            inspect_package(self.bundle)

    def test_pc_systemd_is_rejected_before_device_access(self):
        self.rewrite_root(lambda entry, data: (entry, b'x86 ELF' if entry.name.endswith('/systemd') else data))
        with self.assertRaisesRegex(ArtifactError, 'ARM64 systemd'):
            inspect_package(self.bundle)

    def test_missing_fastrpc_account_is_rejected(self):
        self.rewrite_root(lambda entry, data: (entry, b'root:x:0:\n' if entry.name == 'etc/group' else data))
        with self.assertRaisesRegex(ArtifactError, 'fastrpc'):
            inspect_package(self.bundle)

    def test_missing_storage_guard_activation_is_rejected(self):
        self.rewrite_root(lambda entry, data: (None, None) if 'basic.target.requires' in entry.name else (entry, data))
        with self.assertRaisesRegex(ArtifactError, '存储保护'):
            inspect_package(self.bundle)

    def test_archive_path_traversal_is_rejected(self):
        def change(entry, content):
            if entry.name == 'etc/group': entry.name = '../etc/group'
            return entry, content
        self.rewrite_root(change)
        with self.assertRaisesRegex(ArtifactError, '不安全'):
            inspect_package(self.bundle)

    def test_missing_artifact_is_rejected_without_hardware(self):
        (self.bundle / 'boot.img').unlink()
        with self.assertRaisesRegex(InstallerError, '文件缺失'):
            self.engine.validate_bundle(self.bundle)


if __name__ == '__main__':
    unittest.main(verbosity=2)
