#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check Firefox media compatibility gates and reversible launcher installation."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/lib'))
import firefox_media

loader = importlib.machinery.SourceFileLoader('firefox_launcher', str(PROJECT/'build/firefox/liuqin-firefox'))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


def elf(digest=b'compatible-build'):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, 183)
    struct.pack_into('<Q', header, 32, 64)
    struct.pack_into('<HH', header, 54, 56, 1)
    note = struct.pack('<III', 4, len(digest), 3)+b'GNU\0'+digest
    entry = bytearray(56)
    struct.pack_into('<I', entry, 0, 4)
    struct.pack_into('<Q', entry, 8, 120)
    struct.pack_into('<Q', entry, 32, len(note))
    return bytes(header+entry)+note


class FirefoxMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lib = self.root/'usr/lib64/firefox'
        self.lib.mkdir(parents=True)
        for name in ('firefox', 'libxul.so', 'libmozavcodec.so', 'libmozavutil.so'):
            (self.lib/name).write_bytes(elf())
        self.bin = self.root/'usr/bin'
        self.bin.mkdir(parents=True)
        (self.bin/'firefox').write_text('#!/bin/sh\nexec firefox-bin "$@"\n')
        self.manifest = dict(schema=1, ffmpeg_release='tested', firefox=[dict(
            identity=launcher.identity(self.lib, self.bin/'firefox'))])

    def test_only_complete_tested_pair_is_accepted(self):
        args = (self.lib, self.bin/'firefox', self.manifest)
        self.assertTrue(launcher.compatible(*args, ['tested', 'tested'])[0])
        self.assertFalse(launcher.compatible(*args, ['tested', 'updated'])[0])
        (self.lib/'libxul.so').write_bytes(elf(b'new-firefox-build'))
        self.assertFalse(launcher.compatible(*args, ['tested', 'tested'])[0])

    def test_changed_vendor_library_or_launcher_does_not_pass(self):
        args = (self.lib, self.bin/'firefox', self.manifest, ['tested', 'tested'])
        (self.bin/'firefox').write_text('# new distribution launcher\n')
        self.assertFalse(launcher.compatible(*args)[0])
        self.manifest['firefox'][0]['identity'] = launcher.identity(self.lib, self.bin/'firefox')
        (self.lib/'libmozavutil.so').write_bytes(elf(b'changed-abi'))
        self.assertFalse(launcher.compatible(*args)[0])

    def test_bad_or_wrong_architecture_elf_is_rejected(self):
        for data in (b'not ELF', elf()[:65], elf()[:-3]):
            (self.lib/'firefox').write_bytes(data)
            with self.assertRaises(ValueError):launcher.build_id(self.lib/'firefox')
        data=bytearray(elf());struct.pack_into('<H', data, 18, 62)
        (self.lib/'firefox').write_bytes(data)
        with self.assertRaises(ValueError):launcher.build_id(self.lib/'firefox')

    def test_arguments_and_private_window_are_preserved(self):
        arguments=['--private-window', 'https://example.invalid/a?x=1&y=two words']
        cmd=launcher.command(True, launcher.LIB, launcher.BIN, arguments)
        self.assertEqual(cmd[-2:], arguments)
        self.assertNotIn('--disable-seccomp-filter-sandbox', cmd)
        self.assertIn(str(launcher.HELPER), cmd)

    def test_nested_fallback_restores_original_libraries_and_launcher(self):
        cmd=launcher.command(False, launcher.HOST/'firefox', launcher.HOST/'bin', ['--version'])
        self.assertIn(str(launcher.LIB), cmd)
        self.assertIn(str(launcher.BIN/'firefox'), cmd)
        self.assertIn('--unsetenv', cmd)
        self.assertNotIn('/usr/lib64/libavcodec.so.63', cmd)
        self.assertEqual(cmd[-2:], [str(launcher.HOST/'bin/firefox'), '--version'])
        self.assertEqual(launcher.command(False, launcher.LIB, launcher.BIN, ['--version']),
                         ['/usr/bin/firefox', '--version'])

    def test_unknown_manifest_reaches_unmodified_browser(self):
        data=self.root/'data';data.mkdir()
        for manifest in ('{}', '[]', '{"schema":1,"ffmpeg_release":"tested","firefox":null}', '{'):
            with self.subTest(manifest=manifest):
                (data/'compatibility.json').write_text(manifest)
                with patch.object(launcher, 'DATA', data), patch.dict(os.environ, {}, clear=True), \
                     patch.object(launcher.subprocess, 'check_output', return_value='tested\ntested\n'), \
                     patch.object(launcher.os, 'execv') as execute, patch.object(sys, 'argv', ['firefox','--version']):
                    launcher.main()
                execute.assert_called_once_with('/usr/bin/firefox', ['/usr/bin/firefox','--version'])

    def test_namespace_denied_still_launches_original_browser(self):
        data=self.root/'data';data.mkdir()
        (data/'compatibility.json').write_text(json.dumps(self.manifest))
        with patch.object(launcher, 'DATA', data), patch.dict(os.environ, {}, clear=True), \
             patch.object(launcher.subprocess, 'check_output', return_value='tested\ntested\n'), \
             patch.object(launcher, 'compatible', return_value=(True,'tested')), \
             patch.object(launcher.subprocess, 'run', side_effect=subprocess.CalledProcessError(1,['bwrap'])), \
             patch.object(launcher.os, 'execv') as execute, patch.object(sys, 'argv', ['firefox','about:blank']):
            launcher.main()
        execute.assert_called_once_with('/usr/bin/firefox', ['/usr/bin/firefox','about:blank'])

    def test_disable_bypasses_packages_and_unmatched_namespace_marker(self):
        with patch.dict(os.environ, {'LIUQIN_FIREFOX_SYSTEM_FFMPEG':'0',launcher.MARKER:'1'}, clear=True), \
             patch.object(launcher, 'HOST', self.root/'empty'), \
             patch.object(launcher.subprocess, 'check_output', side_effect=AssertionError('disabled')), \
             patch.object(launcher.os, 'execv') as execute, patch.object(sys, 'argv', ['firefox','--version']):
            launcher.main()
        execute.assert_called_once_with('/usr/bin/firefox', ['/usr/bin/firefox','--version'])

    def prerequisites(self):
        for name in ('usr/bin/bwrap','usr/lib64/libavcodec.so.63','usr/lib64/libavutil.so.61'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()

    def test_install_preserves_rpm_files_and_adds_only_local_entry(self):
        self.prerequisites()
        original=(self.bin/'firefox').read_bytes()
        self.assertTrue(firefox_media.install(self.root))
        self.assertEqual((self.bin/'firefox').read_bytes(),original)
        self.assertEqual((self.root/'usr/local/bin/firefox').resolve(),
                         self.root/'usr/local/libexec/liuqin-firefox')
        self.assertTrue((self.root/'usr/local/libexec/liuqin-firefox').stat().st_mode & 0o111)
        self.assertEqual(list((self.root/'usr/local/libexec/liuqin-firefox-host/bin').iterdir()),[])

    def test_existing_local_launcher_and_missing_dependency_are_preserved(self):
        self.assertFalse(firefox_media.install(self.root))
        self.prerequisites()
        path=self.root/'usr/local/bin/firefox';path.parent.mkdir(parents=True)
        path.write_text('custom launcher')
        self.assertFalse(firefox_media.install(self.root))
        self.assertEqual(path.read_text(),'custom launcher')
        self.assertFalse((self.root/'usr/local/libexec/liuqin-firefox').exists())

    def test_parent_path_escape_is_rejected(self):
        self.prerequisites()
        (self.root/'usr/local').symlink_to('/tmp')
        with self.assertRaisesRegex(ValueError,'escapes root'):firefox_media.install(self.root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
