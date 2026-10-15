#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Filesystem capability, plan binding and CLI checks without any tablet access."""
import subprocess
import sys
import tempfile
import io
import tarfile
from pathlib import Path
import unittest

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
from liuqin_filesystems import kernel_support,parse_support,validate
from liuqin_gui.demo import DemoEngine,demo_image
from liuqin_gui.engine import InstallerError
from liuqin_artifacts import inspect_package,boot_interface

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.engine=DemoEngine(lambda _event:None,self.root/'state')
    def tearDown(self):
        self.engine.close();self.temp.cleanup()
    def prepare(self,filesystem):
        self.engine.create_backup(dict(serial='DEMO-LIUQIN',bundle='ignored',backup_parent=str(self.root/'backups')))
        backup=self.engine.receipt['backup_dir']
        self.engine.prepare(dict(serial='DEMO-LIUQIN',bundle='ignored',backup_dir=backup,filesystem=filesystem))
    def test_only_builtin_btrfs_with_acls_is_advertised(self):
        base='CONFIG_EXT4_FS=y\n'
        self.assertEqual(kernel_support(base+'CONFIG_BTRFS_FS=m\nCONFIG_BTRFS_FS_POSIX_ACL=y\n'),['ext4'])
        self.assertEqual(kernel_support(base+'CONFIG_BTRFS_FS=y\n'),['ext4'])
        self.assertEqual(kernel_support(base+'CONFIG_BTRFS_FS=y\nCONFIG_BTRFS_FS_POSIX_ACL=y\n'),['ext4','btrfs'])
        with self.assertRaises(ValueError):kernel_support('CONFIG_EXT4_FS=m\n')
    def test_support_records_and_choices_reject_arbitrary_formats(self):
        self.assertEqual(parse_support(None),['ext4'])
        for value in (b'',b'btrfs\n',b'ext4\nxfs\n',b'ext4\nbtrfs\nbtrfs\n'):
            with self.assertRaises(ValueError):parse_support(value)
        for value in ('xfs','btrfs -f',None):
            with self.assertRaises(ValueError):validate(value)
    def test_three_artifacts_must_agree_on_btrfs_support(self):
        self.assertEqual(inspect_package(self.engine.demo_bundle)['filesystems'],['ext4','btrfs'])
        path=self.engine.demo_bundle/'boot.img';info=boot_interface(path)
        path.write_bytes(demo_image({'etc/liuqin-storage-mode':b'persistent\n',
          'etc/liuqin-root-profile':b'native-updatable\n','etc/liuqin-native-root.contract':info['contract']},b'DEMO ONLY KERNEL'))
        self.assertEqual(inspect_package(self.engine.demo_bundle)['filesystems'],['ext4'])
    def test_btrfs_capability_requires_a_prelabelled_configuration_template(self):
        path=self.engine.demo_bundle/'rootfs.tar.gz'
        output=io.BytesIO()
        with tarfile.open(path) as original,tarfile.open(fileobj=output,mode='w:gz') as archive:
            for entry in original:
                if entry.name=='etc/liuqin-root-filesystem':continue
                archive.addfile(entry,original.extractfile(entry) if entry.isfile() else None)
        path.write_bytes(output.getvalue())
        with self.assertRaisesRegex(ValueError,'配置模板'):inspect_package(self.engine.demo_bundle)
    def test_btrfs_choice_is_bound_to_plan_and_receipt(self):
        self.prepare('btrfs')
        self.assertEqual(self.engine.plan_snapshot['filesystem'],'btrfs')
        self.assertEqual(self.engine.receipt['filesystem'],'btrfs')
        self.assertIn('format btrfs',self.engine.plan.table())
        self.engine.filesystem='ext4'
        with self.assertRaisesRegex(InstallerError,'文件系统选择发生变化'):
            self.engine.install({})
        self.assertFalse(self.engine.receipt['write_dispatched'])
    def test_unknown_choice_is_rejected_before_device_preparation(self):
        with self.assertRaises(InstallerError):self.engine.prepare({'filesystem':'xfs'})
        self.assertIsNone(self.engine.transport)
    def test_btrfs_cli_check_never_accesses_a_device(self):
        result=subprocess.run([sys.executable,PROJECT/'installer/install-liuqin.py','--bundle',
            self.engine.demo_bundle,'--check','--filesystem','btrfs'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_build_metadata_preserves_builtin_driver_choices(self):
        config=self.root/'config';out=self.root/'support'
        config.write_text('CONFIG_EXT4_FS=y\nCONFIG_BTRFS_FS=y\nCONFIG_BTRFS_FS_POSIX_ACL=y\n')
        subprocess.run([sys.executable,PROJECT/'build/lib/root_filesystems.py','--kernel-config',config,'--output',out],check=True)
        self.assertEqual(out.read_bytes(),b'ext4\nbtrfs\n')
        self.assertEqual(out.stat().st_mode&0o777,0o644)

if __name__=='__main__':unittest.main(verbosity=2)
