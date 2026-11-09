#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Partitions and OS installs are separate transactions on one simulated disk."""
import copy
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine
from liuqin_gui.engine import InstallerError,cli
from liuqin_gui.managed_linux import scan_layout
from liuqin_multiboot import plan
from liuqin_android import managed_layout


class StagedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.events=[]
        self.engine=DemoEngine(self.events.append,self.root/'state');self.engine.pause=lambda _:None
        self.addCleanup(self.engine.close)
        for target in (patch.object(subprocess,'run',side_effect=AssertionError('real commands forbidden')),
                       patch.object(socket,'socket',side_effect=AssertionError('real networking forbidden'))):
            target.start();self.addCleanup(target.stop)

    def backup(self,mode):
        self.engine.create_backup(dict(serial='DEMO-LIUQIN',bundle='ignored',layout=mode,
            backup_parent=str(self.root/'backups'),force_new=True))
        return [e['backup']['path'] for e in self.events if e['type']=='backup_ready'][-1]

    def confirm(self,**extra):
        return dict(session_id=self.engine.session_id,plan_id=self.engine.plan_id,confirmation=dict(
            serial='DEMO-LIUQIN',personal_data_backed_up=True,allow_experimental=True,**extra))

    def partition(self,userdata=32,roots=2):
        ids=[str(uuid.uuid4()) for _ in range(roots)]
        config=dict(schema_version=1,userdata_gib=userdata,boot_gib=2,roots=[
            dict(id=id,name='Root '+str(i+1),size_gib=32,filesystem='btrfs' if i else 'ext4') for i,id in enumerate(ids)],
            boot_action='install-only')
        original=self.backup('partition-reset')
        self.engine.prepare(dict(serial='DEMO-LIUQIN',bundle='ignored',layout='partition-reset',partition_config=config,backup_dir=original))
        self.assertFalse(self.engine.multi_registry['systems'])
        transport=self.engine.transport
        self.engine.install(self.confirm(erase_userdata=True,repartition_userdata=True))
        self.assertEqual(self.engine.state,'partition_complete')
        self.assertFalse(self.engine.receipt['root_installed'])
        self.assertFalse(any(c.startswith(('install_root','multiboot_default','fastboot:flash')) for c in transport.calls))
        self.original_backup=original
        return ids

    def linux(self,os_id,default=False,backup=None):
        backup=backup or self.backup('linux-managed')
        self.engine.prepare(dict(serial='DEMO-LIUQIN',bundle='ignored',layout='linux-managed',
            rootfs_id=os_id,set_default=default,backup_dir=backup))

    def test_partition_only_needs_no_linux_materials(self):
        (self.engine.demo_bundle/'boot.img').unlink();(self.engine.demo_bundle/'rootfs.tar.gz').unlink()
        self.partition(roots=0)
        scan=self.engine.demo_android_scan
        self.assertFalse(scan['registry']['systems']);self.assertFalse(scan['registry']['rootfs_slots'])
        self.assertTrue(scan['userdata_blank'])
        managed_layout(cli.layout.parse_print(self.engine.demo_partition_report),scan['inventory'],scan,'DEMO-LIUQIN')

    def test_android_only_install_and_later_preserve_without_any_linux(self):
        self.partition(roots=0)
        backup=self.backup('android-manage');before=copy.deepcopy(self.engine.demo_android_scan['inventory'])
        args=dict(serial='DEMO-LIUQIN',bundle='ignored',layout='android-manage',backup_dir=backup)
        self.engine.prepare(dict(args,android_config=dict(schema_version=1,mode='reinstall',rom_path='DEMO',set_default=True)))
        transport=self.engine.transport
        self.engine.install(self.confirm(erase_android_data=True,preserve_linux=True))
        self.assertEqual(self.engine.demo_android_scan['inventory'],before)
        self.assertEqual([s['type'] for s in self.engine.demo_android_scan['registry']['systems']],['android'])
        self.assertNotIn('multiboot_apply',transport.calls)
        self.engine.prepare(dict(args,android_config=dict(schema_version=1,mode='preserve',rom_path='',set_default=False)))
        self.engine.install(self.confirm(keep_android=True,preserve_linux=True))
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_linux_without_userdata_then_second_linux(self):
        ids=self.partition(userdata=0)
        self.assertNotIn('userdata',self.engine.demo_android_scan['inventory'])
        scan_layout(self.engine,dict(serial='DEMO-LIUQIN',bundle='ignored',layout='linux-managed'))
        self.assertEqual(len([e for e in self.events if e['type']=='managed_layout'][-1]['slots']),2)
        self.linux(ids[0],default=True);transport=self.engine.transport
        self.engine.install(self.confirm(erase_rootfs=True,preserve_other_systems=True))
        first=copy.deepcopy(self.engine.demo_android_scan['registry']['systems'][0]);boot=self.engine.demo_live_boot
        self.assertTrue(self.engine.receipt['boot_readback_verified'])
        self.assertNotIn('multiboot_apply',transport.calls)
        self.linux(ids[1]);self.engine.install(self.confirm(erase_rootfs=True,preserve_other_systems=True))
        self.assertEqual(self.engine.demo_live_boot,boot)
        self.assertEqual(self.engine.demo_android_scan['registry']['systems'][0],first)
        self.assertEqual([s['status'] for s in self.engine.demo_android_scan['registry']['rootfs_slots']],['installed','installed'])
        with self.assertRaisesRegex(InstallerError,'预留'):self.linux(ids[0])

    def test_android_and_linux_installed_in_separate_operations(self):
        ids=self.partition()
        backup=self.backup('android-manage')
        self.engine.prepare(dict(serial='DEMO-LIUQIN',bundle='ignored',layout='android-manage',backup_dir=backup,
            android_config=dict(schema_version=1,mode='reinstall',rom_path='DEMO',set_default=True)))
        self.engine.install(self.confirm(erase_android_data=True,preserve_linux=True))
        android=copy.deepcopy(self.engine.demo_android_scan['registry']['systems'][0]);boot=self.engine.demo_live_boot
        self.linux(ids[0]);self.engine.install(self.confirm(erase_rootfs=True,preserve_other_systems=True))
        self.assertEqual(self.engine.demo_android_scan['registry']['systems'][0],android)
        self.assertEqual(self.engine.demo_live_boot,boot)
        self.assertEqual(self.engine.demo_android_scan['registry']['rootfs_slots'][1]['status'],'reserved')

    def test_old_gpt_backup_is_rejected(self):
        ids=self.partition()
        with self.assertRaisesRegex(InstallerError,'分区|边界|布局|GPT'):
            self.linux(ids[0],backup=self.original_backup)
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_wrong_confirmation_and_changed_plan_never_stage(self):
        ids=self.partition();self.linux(ids[0]);transport=self.engine.transport
        with self.assertRaises(InstallerError):self.engine.install(self.confirm(erase_userdata=True))
        self.engine.linux_system['partition']['name']='userdata'
        with self.assertRaises(InstallerError):self.engine.install(self.confirm(erase_rootfs=True,preserve_other_systems=True))
        self.assertNotIn('android:linux-stage',transport.calls)

    def test_partial_linux_is_not_registered_or_defaulted(self):
        ids=self.partition();self.linux(ids[0],default=True);transport=self.engine.transport
        old=copy.deepcopy(self.engine.demo_android_scan['registry'])
        with patch.object(transport,'install_root',side_effect=RuntimeError('extraction failed')):
            with self.assertRaisesRegex(RuntimeError,'extraction'):self.engine.install(self.confirm(erase_rootfs=True,preserve_other_systems=True))
        self.assertEqual(self.engine.demo_android_scan['registry'],old)
        self.assertFalse(any(c.startswith('fastboot:flash') for c in transport.calls))

    def test_linux_default_flash_readback_and_commit_failures_restore_old_boot(self):
        from liuqin_gui.managed_linux import set_default
        ids=self.partition();self.linux(ids[0],default=True)
        self.engine.install(self.confirm(erase_rootfs=True,preserve_other_systems=True))
        self.linux(ids[1],default=True)
        engine=self.engine;transport=engine.transport
        old_scan=copy.deepcopy(engine.demo_android_scan)
        old_boot=engine.linux_previous_boot.read_bytes()
        original_fastboot=transport.fastboot;original_digest=transport.current_boot_digest;original_rpc=transport.android_rpc
        for failure in ('flash','readback','commit'):
            with self.subTest(failure=failure):
                engine.demo_android_scan=copy.deepcopy(old_scan);engine.demo_live_boot=old_boot
                engine.receipt.update(boot_flash_started=False,boot_written=False,boot_rollback_verified=False)
                failed=False
                def fastboot(*args):
                    nonlocal failed
                    original_fastboot(*args)
                    if failure=='flash' and args[:2]==('flash','boot_a') and not failed:
                        failed=True;raise RuntimeError('flash interrupted')
                def digest():
                    nonlocal failed
                    if failure=='readback' and original_digest()==engine.linux_plan['padded_sha256'] and not failed:
                        failed=True;return 'f'*64
                    return original_digest()
                def rpc(action,request):
                    nonlocal failed
                    result=original_rpc(action,request)
                    if failure=='commit' and action=='linux-commit' and not failed:
                        failed=True;raise RuntimeError('connection lost after registry commit')
                    return result
                with patch.object(transport,'fastboot',side_effect=fastboot),patch.object(transport,'current_boot_digest',side_effect=digest),patch.object(transport,'android_rpc',side_effect=rpc):
                    with self.assertRaises((RuntimeError,InstallerError)):set_default(engine)
                self.assertEqual(engine.demo_live_boot,old_boot)
                self.assertTrue(engine.receipt['boot_rollback_verified'])
                self.assertFalse(engine.receipt['boot_written'])
                self.assertEqual(engine.demo_android_scan['registry']['default_id'],old_scan['registry']['default_id'])
                self.assertEqual(engine.demo_android_scan['registry']['rootfs_slots'][0]['status'],'installed')

    def test_linux_default_failure_never_restores_boot_for_removed_userdata(self):
        from liuqin_gui.managed_linux import set_default,previous_default_survives
        ids=self.partition(userdata=0,roots=1);self.linux(ids[0],default=True)
        engine=self.engine;transport=engine.transport
        self.assertFalse(previous_default_survives(engine.linux_plan,engine.linux_previous_boot))
        with patch.object(transport,'boot_ram',side_effect=InstallerError('Recovery unavailable')):
            with self.assertRaises(InstallerError):set_default(engine)
        self.assertEqual(transport.current_boot_digest(),engine.linux_plan['padded_sha256'])
        self.assertEqual(len([c for c in transport.calls if c.startswith('fastboot:flash boot_a ')]),1)
        self.assertTrue(engine.receipt['boot_finalization_pending'])
        self.assertFalse(engine.receipt.get('boot_rollback_attempted',False))

    def test_no_userdata_refuses_android_and_no_targets_refuses_partitioning(self):
        self.partition(userdata=0)
        scan=self.engine.demo_android_scan
        with self.assertRaisesRegex(ValueError,'userdata'):
            managed_layout(cli.layout.parse_print(self.engine.demo_partition_report),scan['inventory'],scan,'DEMO-LIUQIN')
        with self.assertRaises(ValueError):
            plan(cli.layout.reference_stock_disk(),dict(schema_version=1,partition_only=True,
                roots=[],userdata_gib=0,boot_gib=2,boot_action='install-only'),str(uuid.uuid4()))

if __name__=='__main__':unittest.main(verbosity=2)
