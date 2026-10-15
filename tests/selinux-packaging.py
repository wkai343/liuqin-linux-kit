#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Target policy/tool selection and labeling before rootfs publication."""
import contextlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'build/lib'), str(PROJECT/'tests')]
import source_build
from kernel_fixtures import kernel_fixture, install_fixture, archive_root
spec = importlib.util.spec_from_file_location('root_packer', PROJECT/'build/scripts/build-liuqin-custom-root.py')
packer = importlib.util.module_from_spec(spec); spec.loader.exec_module(packer)


class SelinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'root'
        for name in ('usr/sbin/setfiles', 'usr/sbin/matchpathcon', 'usr/bin/xargs',
                     'etc/selinux/targeted/contexts/files/file_contexts',
                     'etc/selinux/targeted/policy/policy.35'):
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')

    def lookup(self, command, **kwargs):
        if '/usr/bin/xargs' in command:
            count = kwargs['input'].count(b'\0')
            return subprocess.CompletedProcess(command, 0, stdout=b'system_u:object_r:fixture_t:s0\n'*count)
        return subprocess.CompletedProcess(command, 0)

    def test_target_policy_and_tool_are_used_without_mounting_host_runtime(self):
        with patch.object(source_build, 'arm64_execution', contextlib.nullcontext), \
             patch.object(packer.subprocess, 'run', side_effect=self.lookup) as run:
            labels = packer.label_selinux_root(self.root)
        self.assertEqual(run.call_args_list[0].args[0], [
            'chroot', str(self.root), '/usr/sbin/setfiles', '-n', '-F', '-m', '-c',
            '/etc/selinux/targeted/policy/policy.35',
            '/etc/selinux/targeted/contexts/files/file_contexts', '/'])
        self.assertTrue(run.call_args_list[0].kwargs['check'])
        self.assertEqual(run.call_count, 2)
        self.assertIn('usr/sbin/matchpathcon', labels)
        self.assertEqual(labels['.'], 'system_u:object_r:fixture_t:s0')

    def test_policy_selection_is_numeric(self):
        (self.root/'etc/selinux/targeted/policy/policy.9').write_bytes(b'older')
        with patch.object(source_build, 'arm64_execution', contextlib.nullcontext), \
             patch.object(packer.subprocess, 'run', side_effect=self.lookup) as run:
            packer.label_selinux_root(self.root)
        self.assertIn('/etc/selinux/targeted/policy/policy.35', run.call_args_list[0].args[0])

    def test_filename_newlines_and_spaces_use_nul_input_and_label_only_output(self):
        name = 'space and\nnewline'; (self.root/name).write_bytes(b'fixture')
        with patch.object(source_build, 'arm64_execution', contextlib.nullcontext), \
             patch.object(packer.subprocess, 'run', side_effect=self.lookup) as run:
            labels = packer.label_selinux_root(self.root)
        self.assertIn(os.fsencode('/'+name)+b'\0', run.call_args.kwargs['input'])
        self.assertIn(name, labels)

    def test_incomplete_lookup_output_is_rejected(self):
        with patch.object(source_build, 'arm64_execution', contextlib.nullcontext), \
             patch.object(packer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'')), \
             self.assertRaisesRegex(ValueError, '数量'):
            packer.label_selinux_root(self.root)

    def test_missing_policy_stops_before_execution(self):
        (self.root/'etc/selinux/targeted/policy/policy.35').unlink()
        with patch.object(packer.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, '编译策略缺失'):
            packer.label_selinux_root(self.root)
        run.assert_not_called()

    def test_symlink_policy_is_rejected(self):
        path = self.root/'etc/selinux/targeted/policy/policy.35'; path.unlink()
        outside = self.base/'outside'; outside.write_bytes(b'outside'); path.symlink_to(outside)
        with patch.object(packer.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, '普通目标文件'):
            packer.label_selinux_root(self.root)
        run.assert_not_called()

    def test_contexts_cannot_escape_the_image(self):
        path = self.root/'etc/selinux/targeted/contexts/files/file_contexts'; path.unlink()
        outside = self.base/'outside'; outside.write_text('outside'); path.symlink_to(outside)
        with patch.object(packer.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, '缺失或越界'):
            packer.label_selinux_root(self.root)
        run.assert_not_called()

    def test_mount_below_image_stops_before_relabel(self):
        mount = str(self.root/'dev').replace(' ', '\\040')
        with patch.object(Path, 'read_text', return_value=f'1 0 0:0 / {mount} rw - tmpfs tmpfs rw\n'), \
             patch.object(packer.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, '需卸载'):
            packer.label_selinux_root(self.root)
        run.assert_not_called()

    def test_host_root_is_never_a_label_target(self):
        with self.assertRaisesRegex(ValueError, '主机根目录'):
            packer.label_selinux_root(Path('/'))

    def exercise_pack(self, fail):
        release = '6.17.0-liuqin-label-test'
        fixture = kernel_fixture(self.base/'kernel', release)
        root = self.base/'template'; root.mkdir(); install_fixture(root, release, fixture)
        PROJECT.joinpath('out').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='selinux-pack-test-', dir=PROJECT/'out') as directory:
            output = Path(directory)/'packed'
            calls = []
            real_run = subprocess.run
            def label(target):
                calls.append('label')
                self.assertEqual((target/'etc/liuqin-root-interface').read_bytes(), b'liuqin-root-v2\n')
                self.assertTrue((target/f'usr/share/liuqin/kernels/{release}/boot.contract').is_file())
                self.assertTrue((target/f'usr/share/liuqin/kernels/{release}/modules.manifest').is_file())
                if fail:
                    raise subprocess.CalledProcessError(1, ['setfiles'])
            def run(command, **kwargs):
                if command[0]=='sh' and 'rootfs-archive.sh' in command[1]:
                    calls.append('archive'); archive_root(Path(command[3]), Path(command[4]))
                    return subprocess.CompletedProcess(command, 0)
                return real_run(command, **kwargs)
            def pack(target, archive, labels):
                calls.append('archive'); archive_root(target, archive)
            argv = ['packer', '--root', str(root), '--kernel-out', str(fixture[0]),
                    '--modules-dir', str(fixture[1]), '--out', str(output), '--selinux-labels']
            with patch.object(sys, 'argv', argv), patch.object(packer.os, 'geteuid', return_value=0), \
                 patch.object(packer.os, 'chown'), patch.object(packer, 'label_selinux_root', side_effect=label), \
                 patch.object(packer, 'pack_labeled_root', side_effect=pack), \
                 patch.object(packer.subprocess, 'run', side_effect=run):
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):packer.main()
                else:
                    packer.main()
            self.assertEqual(calls, ['label'] if fail else ['label','archive'])
            self.assertEqual((output/'rootfs.tar.gz').exists(), not fail)

    def test_all_new_interface_files_exist_before_labeling_and_archiving(self):
        self.exercise_pack(False)

    def test_failed_labeling_cannot_publish_an_archive(self):
        self.exercise_pack(True)

    def test_target_only_labels_live_in_archive_and_preserve_binary_xattrs_and_links(self):
        root = self.base/'archive-input'; root.mkdir()
        program = root/'program'; program.write_bytes(b'content\n'); program.chmod(0o751)
        os.setxattr(program, 'user.binary-fixture', b'\0\xff\x00')
        os.link(program, root/'hardlink'); (root/'link').symlink_to('program')
        label = 'system_u:object_r:liuqin_fpc_program_t:s0'
        labels = {'.': 'system_u:object_r:root_t:s0', 'program': label, 'hardlink': label, 'link': label}
        before = {name: os.getxattr(program, name) for name in os.listxattr(program)}
        archive = self.base/'root.tar.gz'; packer.pack_labeled_root(root, archive, labels)
        with tarfile.open(archive, 'r:gz') as tar:
            entries = {Path(m.name).as_posix(): m for m in tar}
            for name, context in labels.items():
                self.assertEqual(entries[name].pax_headers['SCHILY.xattr.security.selinux'], context+'\0')
            data_entry = entries['program'] if entries['program'].isfile() else entries['hardlink']
            raw = data_entry.pax_headers['SCHILY.xattr.user.binary-fixture']
            self.assertEqual(raw.encode('utf-8', 'surrogateescape'), b'\0\xff\x00')
            self.assertEqual(tar.extractfile(entries['program']).read(), b'content\n')
            self.assertEqual(entries['program'].mode, 0o751)
            self.assertEqual(entries['program'].uid, os.getuid())
            self.assertTrue(entries['hardlink'].islnk() or entries['program'].islnk())
            self.assertEqual(entries['link'].linkname, 'program')
        self.assertEqual(before, {name: os.getxattr(program, name) for name in os.listxattr(program)})

    def test_missing_label_never_publishes_an_archive(self):
        root = self.base/'archive-input'; root.mkdir(); (root/'missing').write_bytes(b'fixture')
        archive = self.base/'root.tar.gz'
        with self.assertRaisesRegex(ValueError, '缺少'):
            packer.pack_labeled_root(root, archive, {'.': 'system_u:object_r:root_t:s0'})
        self.assertFalse(archive.exists()); self.assertEqual(list(self.base.glob('root.tar.gz.*')), [])

    def test_hardlinks_with_conflicting_labels_cannot_publish(self):
        root = self.base/'archive-input'; root.mkdir()
        (root/'a').write_bytes(b'fixture'); os.link(root/'a', root/'b')
        archive = self.base/'root.tar.gz'
        with self.assertRaisesRegex(ValueError, '硬链接'):
            packer.pack_labeled_root(root, archive, {'.': None, 'a': 'system_u:object_r:one_t:s0',
                                                   'b': 'system_u:object_r:two_t:s0'})
        self.assertFalse(archive.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
