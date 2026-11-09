#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Recover real staged Android/Linux transactions without replaying OS writes."""
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
import liuqin_native_install as gate


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


fixture=load('transaction_linux_fixture',PROJECT/'tests/managed-linux-device.py')
backend=load('transaction_recovery_backend',PROJECT/'device/recovery/native-store.py')
native=load('transaction_native',PROJECT/'device/recovery/native-install.py')


class RecoveryMixin:
    def recover(self,plan,mutate=False):
        device=self.device;agent=self.ops.__class__.__module__
        module=fixture.m if hasattr(self,'name') else fixture.fixtures.module
        state=self.ops.inspect('layout')
        parts=state['inventory']
        journal=device.store/'transactions'/((('linux-' if hasattr(self,'name') else 'android-'))+plan['transaction'])
        row=dict(name=journal.name,kind='linux' if hasattr(self,'name') else 'android',id=plan['transaction'])
        change=backend._installation_commit(device.store,row,state,device.digest(parts['boot_a']),device,module)
        self.assertFalse(change['boot_source'])
        if mutate:
            with device.mount_store(parts['linux_boot'],True) as root:
                module.atomic(root/'SHA256SUMS',change['sums'])
                module.atomic(root/'registry.json',change['registry'])
        return change


class LinuxRecoveryTests(RecoveryMixin,fixture.LinuxDeviceTests):
    def test_confirmed_api_commits_receipt_and_only_opens_boot_store(self):
        self.stage();self.extract();self.ops.finish(self.plan)
        module=fixture.m
        def state():
            return dict(self.ops.inspect('layout'),report='fixture GPT',details={})
        agent=SimpleNamespace(PartitionOperations=lambda _:SimpleNamespace(inspect=state),
            read_json=module.read_json,preserve_files=module.preserve_files,sha=module.sha,atomic=module.atomic)
        probe=dict(boot_id='11111111-1111-4111-8111-111111111111')
        with patch('liuqin_partitions.discover',return_value={'can_create':True}):
            report=backend.inspect(self.device,agent,native,probe)
            row=next(r for r in report['transactions'] if r['status']=='pending')
            self.assertEqual(row['actions'],[dict(operation='finish-install',label='完成系统登记')])
            request=dict(operation='finish-install',transaction=row['name'],serial=self.device.serial,
                         boot_id=probe['boot_id'],snapshot_id=report['snapshot_id'])
            plan=backend.preview(request,self.device,agent,native,probe)['plan']
            lease=dict(token='c'*32,scope=gate.scope(dict(kind='maintenance',maintenance_plan=plan)))
            self.device.writes.clear()
            with patch.object(gate,'require',return_value=lease):
                result=backend.apply(dict(token='c'*32,plan=plan),self.device,agent,native,probe)
            self.assertTrue(result['completed']);self.assertFalse(result['boot_written'])
            self.assertEqual(set(self.device.writes),{'linux_boot:append'})
            self.assertTrue(all(flag==1 for flag in self.device.flags.values()))
            self.assertEqual(backend.inspect(self.device,agent,native,probe)['pending_count'],0)

    def test_registration_can_resume_without_reextracting_root(self):
        self.stage();self.extract();self.ops.finish(self.plan)
        before={n:self.device.digest(p) for n,p in self.device.parts.items()}
        self.recover(self.plan,mutate=True)
        self.assertEqual(fixture.m.read_json(self.device.store/'registry.json'),self.plan['candidate_registry'])
        self.assertEqual(before,{n:self.device.digest(p) for n,p in self.device.parts.items()})
        # Also handles response loss after registry was atomically published.
        self.recover(self.plan)

    def test_root_not_verified_or_changed_is_not_committed(self):
        self.stage();self.extract()
        with self.assertRaises((ValueError,OSError)):self.recover(self.plan)
        self.ops.finish(self.plan);self.extracted['os_id']='changed'
        with self.assertRaisesRegex(RuntimeError,'binding'):self.recover(self.plan)

    def test_missing_or_changed_intent_and_later_files_are_rejected(self):
        self.stage();self.extract();self.ops.finish(self.plan)
        intent=self.device.store/'transactions'/('linux-'+self.plan['transaction'])/'commit-intent.json'
        original=intent.read_bytes();intent.write_text('{}')
        with self.assertRaises((KeyError,RuntimeError)):self.recover(self.plan)
        intent.write_bytes(original)
        (self.device.store/'later-file').write_text('later mutation')
        with self.assertRaisesRegex(RuntimeError,'其他引导存储写入'):self.recover(self.plan)


class AndroidRecoveryTests(RecoveryMixin,fixture.fixtures.DeviceTests):
    def test_reinstall_completion_recovers_registration_without_formatting(self):
        self.current,new=self.plan()
        module=fixture.fixtures.module
        with patch.object(module,'fetch',side_effect=self.fetch):
            self.ops.stage(self.current,'http://fixture');self.ops.install(self.current,'http://fixture')
        before={n:self.device.digest(p) for n,p in self.device.parts.items()}
        self.recover(self.current,mutate=True)
        self.assertEqual(module.read_json(self.device.store/'registry.json'),new)
        self.assertEqual(before,{n:self.device.digest(p) for n,p in self.device.parts.items()})
        self.recover(self.current)
        state=self.ops.inspect('layout')
        rows=backend._transactions(self.device.store,module)
        backend._classify_superseded(self.device.store,rows,state,self.device,module,native)
        self.assertEqual(next(r for r in rows if r['id']==self.current['transaction'])['status'],'pending')

    def test_incomplete_rom_or_changed_partition_cannot_commit(self):
        self.current,_=self.plan();module=fixture.fixtures.module
        with patch.object(module,'fetch',side_effect=self.fetch):self.ops.stage(self.current,'http://fixture')
        with self.assertRaises((OSError,ValueError)):self.recover(self.current)
        with patch.object(module,'fetch',side_effect=self.fetch):self.ops.install(self.current,'http://fixture')
        self.device.files['dtbo_a'].write_bytes(b'CORRUPTED')
        with self.assertRaisesRegex(RuntimeError,'ROM 内容已变化'):self.recover(self.current)


if __name__=='__main__':unittest.main(verbosity=2)
