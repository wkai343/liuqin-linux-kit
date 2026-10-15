#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproduce empty account startup files and exercise safe default restoration."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/lib'))
import shell_defaults


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root/'home'; self.home.mkdir()
        self.skel = self.root/'skel'; self.skel.mkdir()
        self.contents = {'.bashrc': b'if [ -f /etc/bashrc ]; then . /etc/bashrc; fi\n',
                         '.bash_profile': b'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi\n'}
        for name, data in self.contents.items():
            (self.skel/name).write_bytes(data)

    def tearDown(self):
        self.temp.cleanup()

    def ensure(self):
        return shell_defaults.ensure(self.home, self.skel, os.getuid(), os.getgid())

    def test_restores_zero_byte_and_missing_files_with_user_ownership(self):
        (self.home/'.bashrc').touch()
        self.assertEqual(set(self.ensure()), set(self.contents))
        for name, data in self.contents.items():
            p = self.home/name
            self.assertEqual(p.read_bytes(), data)
            self.assertEqual((p.stat().st_uid, p.stat().st_gid), (os.getuid(), os.getgid()))
            self.assertEqual(p.stat().st_mode & 0o777, 0o644)

    def test_custom_content_inode_permissions_and_timestamp_are_preserved(self):
        for name in self.contents:
            p = self.home/name; p.write_bytes(b'custom shell configuration\n'); p.chmod(0o640)
            os.utime(p, ns=(1234567890000000000, 1234567890000000000))
        before = {name: (self.home/name).stat() for name in self.contents}
        self.assertEqual(self.ensure(), [])
        for name, old in before.items():
            p = self.home/name; now = p.stat()
            self.assertEqual(p.read_bytes(), b'custom shell configuration\n')
            self.assertEqual((now.st_ino, now.st_mode, now.st_mtime_ns), (old.st_ino, old.st_mode, old.st_mtime_ns))

    def test_empty_template_stops_before_creating_any_user_file(self):
        (self.skel/'.bash_profile').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, '模板为空'):
            self.ensure()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_user_file_symlink_is_never_followed_or_replaced(self):
        outside = self.root/'outside'; outside.touch()
        (self.home/'.bashrc').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '不是普通文件'):
            self.ensure()
        self.assertTrue((self.home/'.bashrc').is_symlink())
        self.assertEqual(outside.read_bytes(), b'')

    def test_home_alias_is_rejected_without_writing_into_it(self):
        alias = self.root/'alias'; alias.symlink_to(self.home)
        with self.assertRaisesRegex(ValueError, '主目录不能通过符号链接'):
            shell_defaults.ensure(alias, self.skel, os.getuid(), os.getgid())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_write_failure_preserves_the_empty_target_and_cleans_staging_file(self):
        (self.home/'.bashrc').touch()
        with patch('shell_defaults.os.fsync', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.ensure()
        self.assertEqual((self.home/'.bashrc').read_bytes(), b'')
        self.assertEqual([p.name for p in self.home.iterdir()], ['.bashrc'])

    def test_template_symlink_is_rejected(self):
        outside = self.root/'outside'; outside.write_bytes(b'not a template')
        (self.skel/'.bashrc').unlink(); (self.skel/'.bashrc').symlink_to(outside)
        with self.assertRaises(OSError):
            self.ensure()
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
