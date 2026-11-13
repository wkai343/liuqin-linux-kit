#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify libclc provenance rejection and the real replacement RPM interface."""
import copy
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/lib'))
import mesa_libclc as libclc
from source_build import sha
from fedora_flow import FedoraFlow


class LibclcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.info = copy.deepcopy(libclc.record())
        self.files = {name:self.work/name for name in self.info['files']}
        for name in libclc.SPIRV:
            self.files[name].write_bytes(b'\x03\x02\x23\x07fixture\0__clc_mesa_libclc_version\0')
        self.files['mesa-libclc.pc.in'].write_text('libexecdir=/original/path\nName: mesa-libclc\nVersion: 0.2.0\nLibs: -L${libexecdir}\n')
        self.archive = self.files['mesa-libclc-'+self.info['version']+'.tar.gz']
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name in ('LICENSE.TXT', 'CREDITS.TXT', 'README.md'):
                entry = tarfile.TarInfo('mesa-libclc-'+self.info['commit']+'/'+name)
                content = ('fixture '+name+'\n').encode()
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        for name, path in self.files.items():
            self.info['files'][name]['sha256'] = sha(path)

    def tearDown(self):
        self.temp.cleanup()

    def test_each_tampered_artifact_stops_before_payload_creation(self):
        for name, path in self.files.items():
            with self.subTest(name=name):
                original = path.read_bytes()
                path.write_bytes(original+b'corrupt')
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    libclc.payload(self.work/'payload', self.info, self.files)
                self.assertFalse((self.work/'payload').exists())
                path.write_bytes(original)

    def test_symlink_to_matching_content_is_rejected(self):
        name = libclc.SPIRV[0]
        path = self.files[name]
        backing = self.work/'backing'
        path.rename(backing)
        path.symlink_to(backing)
        with self.assertRaisesRegex(ValueError, '校验失败'):
            libclc.payload(self.work/'payload', self.info, self.files)

    def test_generic_upstream_spirv_is_rejected_even_with_new_hash(self):
        name = libclc.SPIRV[0]
        self.files[name].write_bytes(b'\x03\x02\x23\x07generic library')
        self.info['files'][name]['sha256'] = sha(self.files[name])
        with self.assertRaisesRegex(ValueError, '修正后的 SPIR-V'):
            libclc.payload(self.work/'payload', self.info, self.files)

    def test_rpm_replaces_only_spirv_and_preserves_fedora_dependency(self):
        built = libclc.build(self.work/'rpm', info=self.info, files=self.files)
        self.assertEqual(built.name, libclc.filename(self.info))
        files = subprocess.check_output(['rpm', '-qpl', str(built)], text=True).splitlines()
        runtime = {name for name in files if not name.startswith('/usr/share/licenses/')}
        self.assertEqual(runtime, {'/'+libclc.LIBDIR+'/'+name for name in libclc.SPIRV}
                         | {'/usr/share/pkgconfig/mesa-libclc.pc'})
        provides = subprocess.check_output(['rpm', '-qp', '--provides', str(built)], text=True).splitlines()
        self.assertIn(self.info['fedora_compatibility'], provides)
        self.assertIn('libclc22-spirv = '+self.info['version']+'-'+self.info['package_release'], provides)
        self.assertIn('pkgconfig(mesa-libclc) = 0.2.0', provides)
        self.assertIn('/usr/share/licenses/libclc22-spirv/'+self.archive.name, files)
        self.assertIn('/usr/share/licenses/libclc22-spirv/SOURCE.json', files)
        self.assertEqual(subprocess.check_output(['rpm', '-qp', '--scripts', str(built)], text=True), '')

    def test_payload_preserves_release_bytes_and_pkgconfig_interface(self):
        root = self.work/'payload'
        libclc.payload(root, self.info, self.files)
        for name in libclc.SPIRV:
            self.assertEqual(sha(root/libclc.LIBDIR/name), self.info['files'][name]['sha256'])
        pc = (root/'usr/share/pkgconfig/mesa-libclc.pc').read_text()
        self.assertIn('libexecdir=/usr/lib64/clc22\n', pc)
        self.assertIn('Version: 0.2.0\n', pc)
        self.assertNotIn('/original/path', pc)
        self.assertEqual(sha(root/'usr/share/licenses/libclc22-spirv'/self.archive.name), sha(self.archive))

    def test_non_opencl_recipe_does_not_fetch_or_build_libclc(self):
        flow = object.__new__(FedoraFlow)
        flow.recipe = {'packages':['kate']}
        with patch.object(libclc, 'sources', side_effect=AssertionError('unexpected download')):
            self.assertIsNone(flow.libclc_rpm())


if __name__ == '__main__':
    unittest.main()
