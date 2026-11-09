#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""GUI-engine Android plans and mutations; all device/network calls are simulated."""
import copy
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine
from liuqin_gui.engine import InstallerError
from liuqin_android import ANDROID_PARTITIONS, managed_layout, normalize


class AndroidEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.events=[];self.engine=DemoEngine(self.events.append,self.root/'state');self.engine.pause=lambda _value:None
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.engine.close)
        for target in ((socket,'socket'),(subprocess,'run')):
            mock=patch.object(*target,side_effect=AssertionError('real device access forbidden'));mock.start();self.addCleanup(mock.stop)
        self.engine.create_backup(dict(serial='DEMO-LIUQIN',layout='android-manage',bundle='ignored',backup_parent=str(self.root/'backup')))
        self.backup=next(e['backup']['path'] for e in self.events if e['type']=='backup_ready')

    def prepare(self,mode='preserve',set_default=False,path='DEMO'):
        self.engine.prepare(dict(layout='android-manage',serial='DEMO-LIUQIN',bundle='ignored',backup_dir=self.backup,
            android_config=dict(schema_version=1,mode=mode,set_default=set_default,rom_path=path)))

    def consent(self):
        return dict(session_id=self.engine.session_id,plan_id=self.engine.plan_id,
            confirmation=dict(serial='DEMO-LIUQIN',preserve_linux=True,keep_android=True,
                erase_android_data=True,personal_data_backed_up=True,allow_experimental=True))

    def test_preserve_does_not_repartition_format_or_flash(self):
        self.prepare(path='');transport=self.engine.transport;old=copy.deepcopy(self.engine.android_plan)
        before=transport.current_boot_digest();self.engine.install(self.consent())
        self.assertEqual(self.engine.state,'android_complete')
        self.assertNotIn('android:install',transport.calls);self.assertNotIn('multiboot_apply',transport.calls)
        self.assertNotIn('android:stage',transport.calls);self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertNotIn('flash_verify_reboot',transport.calls);self.assertEqual(before,transport.current_boot_digest())
        self.assertEqual(old['inventory'],self.engine.demo_android_scan['inventory'])
        self.assertEqual([s for s in old['registry']['systems'] if s['type']=='linux'],
                         [s for s in self.engine.demo_android_scan['registry']['systems'] if s['type']=='linux'])
        self.assertTrue(self.engine.receipt['linux_preserved'])

    def test_reinstall_targets_only_android_and_keeps_default(self):
        self.prepare('reinstall');plan=self.engine.android_plan;transport=self.engine.transport
        self.assertEqual({p['partition']['name'] for p in plan['images']},ANDROID_PARTITIONS)
        roots=copy.deepcopy(self.engine.demo_android_scan['registry']['systems'][:2]);before=transport.current_boot_digest()
        self.engine.install(self.consent())
        self.assertIn('android:install',transport.calls);self.assertEqual(before,transport.current_boot_digest())
        self.assertEqual(roots,self.engine.demo_android_scan['registry']['systems'][:2])
        self.assertEqual(self.engine.state,'android_complete');self.assertTrue(self.engine.receipt['android_rom_written'])

    def test_explicit_android_default_is_committed_after_rom(self):
        self.prepare('reinstall',True);transport=self.engine.transport;self.engine.install(self.consent())
        self.assertLess(transport.calls.index('android:install'),transport.calls.index('flash_verify_reboot'))
        self.assertEqual(self.engine.state,'awaiting_first_boot')
        self.assertEqual(self.engine.receipt['default_id'],self.engine.android_plan['android_id'])

    def test_confirmations_and_plan_mutation_stop_before_writes(self):
        self.prepare('reinstall')
        for key in ('preserve_linux','erase_android_data','personal_data_backed_up','allow_experimental'):
            request=self.consent();request['confirmation'][key]=False
            with self.assertRaises(InstallerError):self.engine.install(request)
        self.assertEqual(self.engine.state,'prepared')
        self.engine.android_plan['images'][0]['partition']['name']='linux_boot'
        with self.assertRaises(InstallerError):self.engine.install(self.consent())
        self.assertNotIn('android:stage',self.engine.transport.calls)

    def test_changed_rom_or_default_never_reaches_stage(self):
        self.prepare('reinstall')
        path=Path(self.engine.android_sources['super.img']);path.write_bytes(b'changed')
        with self.assertRaises(InstallerError):self.engine.install(self.consent())
        self.assertNotIn('android:stage',self.engine.transport.calls)

    def test_incomplete_android_cannot_be_preserved(self):
        self.engine.demo_android_scan['pending_transactions']=['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa']
        with self.assertRaisesRegex(InstallerError,'未完成'):self.prepare()
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_wrong_root_uuid_and_unregistered_partition_refused(self):
        self.engine.demo_android_scan['inventory']['lqr_111111111111']['fsuuid']='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        with self.assertRaisesRegex(ValueError,'Linux rootfs'):self.prepare()
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_write_failure_does_not_commit_or_change_linux_default(self):
        self.prepare('reinstall');transport=self.engine.transport;original=transport.android_rpc
        def fail(action,request):
            if action=='install':raise RuntimeError('injected transfer interruption')
            return original(action,request)
        with patch.object(transport,'android_rpc',side_effect=fail):
            with self.assertRaises(RuntimeError):self.engine.install(self.consent())
        self.assertNotIn('android:commit',transport.calls);self.assertNotIn('flash_verify_reboot',transport.calls)

    def test_antirollback_and_slot_are_checked_before_ram_boot(self):
        from liuqin_gui.engine import Transport
        from liuqin_android import load_profile
        transport=object.__new__(Transport);transport.serial='TEST'
        transport.engine=SimpleNamespace(receipt={'operation':'android'},android_profile=load_profile())
        values={'product':'liuqin','unlocked':'yes','current-slot':'a',
                'partition-size:boot_a':str(192*1024**2),'max-download-size':str(256*1024**2),'anti':'1'}
        transport.getvar=values.__getitem__
        self.assertEqual(transport.check_device()['anti_version'],1)
        for anti in ('unknown','-1','2'):
            values['anti']=anti
            with self.assertRaises(InstallerError):transport.check_device()
        values['anti']='1';values['current-slot']='b'
        with self.assertRaises(InstallerError):transport.check_device()

    def test_default_failure_restores_current_boot_and_linux_default(self):
        from liuqin_gui import android_transport
        self.prepare('reinstall',True);engine=self.engine;plan=engine.android_plan
        old=plan['previous_boot_sha256'];target=engine.android_profile['images']['boot.img']['sha256']
        # This case models an intact, registered Linux default whose image is
        # exactly the saved boot snapshot, not merely an arbitrary old boot.
        default=next(s for s in plan['registry']['systems'] if s['id']==plan['registry']['default_id'])
        default['boot_sha256']=old
        class Transport:
            def __init__(self,failure):self.failure=failure;self.failed=False;self.ram_active=True;self.live=old;self.calls=[]
            def current_boot_digest(self):
                if self.failure=='readback' and self.live==target and not self.failed:
                    self.failed=True;return '0'*64
                return self.live
            def return_fastboot(self):self.ram_active=False
            def check_device(self):return engine.device
            def boot_ram(self):self.ram_active=True
            def fastboot(self,*args):
                self.calls.append(args)
                if args[:2]==('flash','boot_a'):
                    self.live=hashlib.sha256(Path(args[2]).read_bytes()).hexdigest()
                    if self.failure=='flash' and not self.failed:
                        self.failed=True;raise RuntimeError('interrupted flash')
        for failure in ('none','flash','readback','registry'):
            with self.subTest(failure=failure):
                transport=Transport(failure);commits=[]
                engine.receipt.update(boot_flash_started=False,boot_written=False,boot_rollback_verified=False)
                def rpc(_transport,action,request):
                    self.assertEqual(action,'commit');commits.append(request)
                    if failure=='registry' and not transport.failed:
                        transport.failed=True;raise RuntimeError('interrupted commit')
                    return dict(committed=True,linux_preserved=True)
                with patch.object(android_transport,'rpc',side_effect=rpc):
                    if failure=='none':
                        android_transport.set_default(transport,engine,engine.android_registry)
                        self.assertEqual(transport.calls[-1],('reboot',));self.assertEqual(transport.live,target)
                    else:
                        with self.assertRaises((RuntimeError,InstallerError)):
                            android_transport.set_default(transport,engine,engine.android_registry)
                        self.assertEqual(transport.live,old);self.assertNotIn(('reboot',),transport.calls)
                        self.assertTrue(engine.receipt['boot_rollback_verified'])
                        self.assertFalse(engine.receipt['boot_written'])
                        self.assertTrue(commits[-1]['restore_default'])
                        self.assertEqual(commits[-1]['registry']['default_id'],plan['registry']['default_id'])

    def test_rollback_requires_an_intact_matching_linux_default_after_reinstall(self):
        from liuqin_gui.android_transport import previous_boot_can_run
        self.prepare('reinstall',True);plan=copy.deepcopy(self.engine.android_plan)
        self.assertFalse(previous_boot_can_run(plan)) # Demo's arbitrary current boot is not the registered Linux image.
        default=next(s for s in plan['registry']['systems'] if s['id']==plan['registry']['default_id'])
        default['boot_sha256']=plan['previous_boot_sha256']
        self.assertTrue(previous_boot_can_run(plan))
        default['partition']['fsuuid']='different-root'
        self.assertFalse(previous_boot_can_run(plan))
        plan['registry']['default_id']=next(s['id'] for s in plan['registry']['systems'] if s['type']=='android')
        self.assertFalse(previous_boot_can_run(plan))
        plan['mode']='preserve'
        self.assertTrue(previous_boot_can_run(plan))

if __name__=='__main__':unittest.main(verbosity=2)
