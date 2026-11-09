#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Boot-store maintenance with ordinary files, including simulated power loss."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
import liuqin_native_install as gate


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


fixture=load('boot_store_fixture',PROJECT/'tests/boot-switch-device.py')
backend=load('boot_store_backend',PROJECT/'device/recovery/native-store.py')
native=fixture.native
android=fixture.android


class PowerLoss(BaseException):
    pass


class StoreTests(fixture.SwitchDeviceTests):
    # Reuse the file-backed hardware; inherited switch tests also verify that
    # the new maintenance scope does not change the existing switch protocol.
    def report(self):
        return backend.inspect(self.hardware,self.agent,native,self.probe)

    def prepare_store(self,operation,transaction=''):
        report=self.report()
        request=dict(serial=self.hardware.serial,boot_id=self.boot_id,
                     snapshot_id=report['snapshot_id'],operation=operation,transaction=transaction)
        return backend.preview(request,self.hardware,self.agent,native,self.probe)['plan']

    def run_store(self,plan):
        self.hardware.lease=dict(token='c'*32,scope=gate.scope(dict(kind='maintenance',maintenance_plan=plan)))
        gate.validate_scope(self.hardware.lease['scope'])
        return backend.apply(dict(token='c'*32,plan=plan),self.hardware,self.agent,native,self.probe)

    def interrupt_switch(self,point):
        plan=self.preview(self.linux[1])
        if point in ('before-boot','partial-boot','after-boot'):
            original=native._write_switch_boot
            def write(hardware,parts,path,expected):
                if point=='partial-boot':
                    (hardware.blocks/'boot_a').write_bytes(b'BROKEN'.ljust(fixture.CAPACITY,b'\0'))
                elif point=='after-boot':
                    original(hardware,parts,path,expected)
                raise PowerLoss()
            with patch.object(native,'_write_switch_boot',side_effect=write):
                with self.assertRaises(PowerLoss):self.apply(plan)
        else:
            original=self.agent.atomic
            def atomic(path,data):
                if path.name=='complete.json':raise PowerLoss()
                return original(path,data)
            self.agent.atomic=atomic
            try:
                with self.assertRaises(PowerLoss):self.apply(plan)
            finally:self.agent.atomic=original
        return plan

    def test_report_is_readonly_and_counts_allocated_bytes(self):
        before=self.hardware.state()
        report=self.report()
        self.assertTrue(report['default_matches'])
        self.assertEqual(report['pending_count'],0)
        self.assertGreater(report['allocated_bytes'],0)
        self.assertEqual(self.hardware.state(),before)
        self.assertFalse(self.hardware.opened)

    def test_unknown_layout_cannot_be_taken_over_for_maintenance(self):
        with patch.object(fixture.liuqin_partitions,'discover',return_value=dict(can_create=False,reason='wrong serial')):
            with self.assertRaisesRegex(RuntimeError,'设备身份'):self.report()
        self.assertFalse(self.hardware.opened)

    def test_verified_later_linux_installation_supersedes_a_failed_attempt(self):
        generation=str(uuid.uuid4());old=copy.deepcopy(self.linux[0])
        old['target']['boot_generation']=generation
        old['partition'].pop('parent')  # Persistent plans omit device handles.
        self.save('transactions/linux-'+generation+'/journal.json',dict(transaction=generation,system=old))
        report=self.report()
        row=next(r for r in report['transactions'] if r['id']==generation)
        self.assertEqual(row['status'],'superseded');self.assertEqual(report['pending_count'],0)
        self.hardware.bad_root=True
        report=self.report();row=next(r for r in report['transactions'] if r['id']==generation)
        self.assertEqual(row['status'],'pending');self.assertEqual(report['pending_count'],1)

    def test_deleted_failed_target_needs_completed_exact_partition_edit_evidence(self):
        generation=str(uuid.uuid4());old=copy.deepcopy(self.linux[0])
        old.update(id=str(uuid.uuid4()))
        old['target']['boot_generation']=generation
        old['partition'].update(name='lqr_999999999999',partuuid=str(uuid.uuid4()))
        self.save('transactions/linux-'+generation+'/journal.json',dict(transaction=generation,system=old))
        edit_id=str(uuid.uuid4());edit=dict(transaction=edit_id,deletes=[old['partition']])
        journal='transactions/edit-'+edit_id+'/'
        self.save(journal+'plan.json',edit);self.save(journal+'complete.json',dict(status='complete'))
        with patch('liuqin_partition_editor.validate_edit') as validate:
            report=self.report()
            self.assertEqual(next(r for r in report['transactions'] if r['id']==generation)['status'],'superseded')
            validate.assert_called_once_with(edit,self.hardware.serial)
        self.save(journal+'complete.json',dict(status='unfinished'))
        self.assertEqual(next(r for r in self.report()['transactions'] if r['id']==generation)['status'],'pending')

    def test_recover_switch_before_during_and_after_boot_write(self):
        for point in ('before-boot','partial-boot','after-boot','after-registry'):
            with self.subTest(point=point):
                plan=self.interrupt_switch(point)
                report=self.report()
                pending=next(r for r in report['transactions'] if r['status']=='pending')
                self.assertIn('restore-switch',[r['operation'] for r in pending['actions']])
                result=self.run_store(self.prepare_store('restore-switch',pending['name']))
                self.assertTrue(result['completed'])
                self.assertEqual((self.hardware.blocks/'boot_a').read_bytes(),self.old_boot)
                self.assertEqual(android.read_json(self.hardware.store/'registry.json'),self.registry)
                self.assertEqual(self.report()['pending_count'],0)
                self.assert_other_partitions()

    def test_finish_switch_after_complete_boot_without_reflashing(self):
        self.interrupt_switch('after-boot')
        pending=next(r for r in self.report()['transactions'] if r['status']=='pending')
        plan=self.prepare_store('finish-switch',pending['name'])
        self.hardware.opened.clear()
        result=self.run_store(plan)
        self.assertFalse(result['boot_written'])
        self.assertEqual(set(self.hardware.opened),{'linux_boot'})
        self.assertEqual(android.read_json(self.hardware.store/'registry.json')['default_id'],self.linux[1]['id'])
        self.assertTrue(self.report()['default_matches'])

    def test_restore_android_uses_device_bound_fixed_witness_when_old_plan_has_no_override(self):
        recovery=self.hardware.add_partition('recovery_a',b'OLD ROM RECOVERY'.ljust(fixture.CAPACITY,b'\0'))
        journal,record,installed=self.android_record()
        installed['images']['recovery_a']=self.hardware.digest(recovery)
        record['targets']=sorted(installed['images'])
        self.save(journal.relative_to(self.hardware.store)/'journal.json',record)
        self.save(journal.relative_to(self.hardware.store)/'rom-installed.json',installed)
        self.registry.update(default_id=self.android['id'],default_generation=self.generation(self.android))
        self.save('registry.json',self.registry)
        self.old_boot=self.image(self.android).ljust(fixture.CAPACITY,b'\0')
        (self.hardware.blocks/'boot_a').write_bytes(self.old_boot)
        (self.hardware.blocks/'recovery_a').write_bytes(b'QUALIFIED FIXED RECOVERY'.ljust(fixture.CAPACITY,b'\0'))
        observed=self.hardware.digest(recovery)
        witness=dict(serial=self.hardware.serial,partition='recovery_a',image_sha256=observed)
        marker=dict(usage='temporary-boot-only',installer_version=1,inspection_only=False,
                    installer_capabilities=gate.CAPABILITIES,tablet_boot_switch_version=1,fixed_recovery_witness=witness)
        with patch.object(gate,'marker',return_value=marker),patch('liuqin_recovery_preserve.CAPACITY',fixture.CAPACITY):
            plan=self.interrupt_switch('after-boot')
            self.assertNotIn('fixed_recovery_sha256',plan)
            row=next(r for r in self.report()['transactions'] if r['status']=='pending')
            self.assertIn('restore-switch',{a['operation'] for a in row['actions']})
            witness['serial']='OTHER'
            self.assertFalse(next(r for r in self.report()['transactions'] if r['status']=='pending')['actions'])
            witness['serial']=self.hardware.serial
            self.run_store(self.prepare_store('restore-switch',row['name']))
            self.assertEqual((self.hardware.blocks/'boot_a').read_bytes(),self.old_boot)
            self.assertEqual(self.hardware.digest(recovery),observed)

    def test_partial_target_cannot_be_committed(self):
        self.interrupt_switch('partial-boot')
        pending=next(r for r in self.report()['transactions'] if r['status']=='pending')
        with self.assertRaisesRegex(RuntimeError,'不能执行'):
            self.prepare_store('finish-switch',pending['name'])

    def test_changed_identity_or_other_files_block_old_switch_recovery(self):
        self.interrupt_switch('after-boot')
        pending=next(r for r in self.report()['transactions'] if r['status']=='pending')
        self.save('new-file.json',{'later':'write'})
        report=self.report()
        self.assertFalse(next(r for r in report['transactions'] if r['status']=='pending')['actions'])
        with self.assertRaises(RuntimeError):self.prepare_store('restore-switch',pending['name'])
        self.assert_other_partitions()

    def test_corrupt_backup_and_parallel_pending_transactions_block_recovery(self):
        self.interrupt_switch('after-boot')
        pending=next(r for r in self.report()['transactions'] if r['status']=='pending')
        backup=self.hardware.store/'transactions'/pending['name']/'previous-boot.img'
        original=backup.read_bytes();backup.write_bytes(b'?'+original[1:])
        self.assertFalse(next(r for r in self.report()['transactions'] if r['status']=='pending')['actions'])
        backup.write_bytes(original)
        (self.hardware.store/'transactions'/('linux-'+str(uuid.uuid4()))).mkdir()
        self.assertTrue(all(not r['actions'] for r in self.report()['transactions'] if r['status']=='pending'))
        with self.assertRaises(RuntimeError):self.prepare_store('cleanup')

    def add_duplicate(self):
        journal=self.hardware.store/'transactions'/('linux-'+self.generation(self.linux[1]))
        backup=journal/'previous-boot.img';backup.write_bytes(self.old_boot)
        return backup

    def test_cleanup_keeps_equivalent_images_all_other_files_and_boot(self):
        backup=self.add_duplicate()
        before=self.hardware.state()['files']
        plan=self.prepare_store('cleanup')
        self.assertGreater(plan['reclaimable_bytes'],0)
        result=self.run_store(plan)
        self.assertEqual(result['retired_snapshots'],1)
        self.assertFalse(backup.exists())
        receipt=android.read_json(backup.with_name('previous-boot-reference.json'))
        self.assertEqual(receipt['source'],self.linux[0]['image'])
        self.assertEqual(receipt['sha256'],fixture.digest(self.old_boot))
        android.preserve_files(self.hardware.store,before,mutable=(str(backup.relative_to(self.hardware.store)),))
        self.assertEqual(self.report()['reclaimable_bytes'],0)
        with self.assertRaises(RuntimeError):self.run_store(plan)
        self.assert_other_partitions()

    def test_cleanup_retains_unmatched_and_pending_snapshots(self):
        backup=self.add_duplicate();backup.write_bytes(b'ANDROID!'+b'?'*(fixture.CAPACITY-8))
        self.assertEqual(self.report()['reclaimable_bytes'],0)
        backup.write_bytes(self.old_boot)
        backup.parent.joinpath('complete.json').unlink()
        self.assertEqual(self.report()['reclaimable_bytes'],0)
        with self.assertRaises(RuntimeError):self.prepare_store('cleanup')

    def test_cleanup_loss_after_receipt_is_resumable_and_keeps_backup(self):
        backup=self.add_duplicate();plan=self.prepare_store('cleanup')
        original=self.agent.atomic
        def atomic(path,data):
            original(path,data)
            if path.name=='previous-boot-reference.json':raise PowerLoss()
        self.agent.atomic=atomic
        try:
            with self.assertRaises(PowerLoss):self.run_store(plan)
        finally:self.agent.atomic=original
        self.assertTrue(backup.exists())
        self.run_store(self.prepare_store('cleanup'))
        self.assertFalse(backup.exists());self.assert_other_partitions()

    def test_maintenance_scope_cannot_authorize_root_or_firmware(self):
        self.add_duplicate();plan=self.prepare_store('cleanup')
        scope=gate.scope(dict(kind='maintenance',maintenance_plan=plan))
        for name in ('boot_a','userdata','abl_a','lqr_000000000001'):
            changed=dict(scope,parts=[gate.identity(self.hardware.parts[name])])
            with self.assertRaises(ValueError):gate.validate_scope(changed)
        self.hardware.lease=dict(token='c'*32,scope=scope)
        with self.assertRaises(RuntimeError):
            native.write_boot(dict(token='c'*32),self.hardware)


if __name__=='__main__':unittest.main(verbosity=2)
