#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""File-backed retention, replacement/deletion and power-loss regressions."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'shared'))
import liuqin_boot_retention as retention
import liuqin_native_install as gate

spec=importlib.util.spec_from_file_location('store_fixture',PROJECT/'tests/boot-store.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


class PowerLoss(BaseException):pass


class RetentionTests(fixture.fixture.SwitchDeviceTests):
    report=fixture.StoreTests.report
    prepare_store=fixture.StoreTests.prepare_store
    run_store=fixture.StoreTests.run_store
    def setUp(self):
        super().setUp()
        self.addCleanup(patch.stopall)
        patch.object(gate,'marker',return_value=dict(boot_store_retention_version=1)).start()

    def old_system(self):
        old=self.system('linux','lqr_000000000001',7)
        # This system was removed from the registry, but its complete journal
        # and boot image survived the original partition deletion.
        self.save('systems/'+old['id']+'/manifest.json',old)
        return old

    def test_deleted_os_cleanup_keeps_three_live_systems_and_only_opens_store(self):
        old=self.old_system();before={s['image']:self.image(s) for s in self.registry['systems']}
        result=self.run_store(self.prepare_store('cleanup'))
        self.assertTrue(result['completed'])
        self.assertFalse((self.hardware.store/old['image']).exists())
        self.assertFalse((self.hardware.store/'systems'/old['id']).exists())
        for path,data in before.items():self.assertEqual((self.hardware.store/path).read_bytes(),data)
        self.assertEqual(self.hardware.state()['registry'],self.registry)
        self.assertEqual((self.hardware.blocks/'boot_a').read_bytes(),self.old_boot)
        self.assertEqual(set(self.hardware.opened),{'linux_boot'})
        self.assert_other_partitions()

    def test_zero_byte_cleanup_can_resume_and_unblock_writes(self):
        self.old_system()
        plan=self.prepare_store('cleanup')
        self.assertEqual(plan['reclaimable_bytes'],0)
        original=retention.os.rename
        def interrupted(source,target):
            original(source,target)
            raise PowerLoss()
        with patch.object(retention.os,'rename',side_effect=interrupted):
            with self.assertRaises(PowerLoss):self.run_store(plan)
        report=self.report()
        self.assertEqual(report['reclaimable_bytes'],0)
        self.assertTrue(report['cleanup_pending'])
        self.assertTrue(report['cleanup_available'])
        with self.assertRaisesRegex(RuntimeError,'先在引导存储管理'):
            fixture.native.require_store_ready(self.hardware)
        self.assertTrue(self.run_store(self.prepare_store('cleanup'))['completed'])
        fixture.native.require_store_ready(self.hardware)
        report=self.report()
        self.assertFalse(report['cleanup_pending'])
        self.assertFalse(report['cleanup_available'])
        self.assert_other_partitions()

    def test_repeated_replacement_does_not_accumulate_images(self):
        system=self.linux[1]
        for _ in range(12):
            new=copy.deepcopy(system);generation=str(uuid.uuid4())
            new['target']['boot_generation']=generation
            new['image']='systems/'+new['id']+'/images/'+generation+'.img'
            path=self.hardware.store/new['image'];path.write_bytes(self.image(system))
            self.save('transactions/linux-'+generation+'/root-installed.json',new)
            self.save('transactions/linux-'+generation+'/complete.json',dict(registry_sha256='a'*64,boot_sha256='b'*64))
            self.registry['systems']=[new if s['id']==new['id'] else s for s in self.registry['systems']]
            self.save('registry.json',self.registry)
            self.run_store(self.prepare_store('cleanup'));system=new
            self.assertEqual(len(list((self.hardware.store/'systems').glob('*/images/*.img'))),3)

    def test_pending_attempt_blocks_all_retirement(self):
        self.old_system();generation=str(uuid.uuid4())
        self.save('transactions/linux-'+generation+'/journal.json',dict(transaction=generation))
        before=self.hardware.state()
        with self.assertRaisesRegex(RuntimeError,'未结束'):self.prepare_store('cleanup')
        self.assertEqual(self.hardware.state(),before)
        self.assertFalse(self.hardware.opened)

    def test_only_one_previous_kernel_for_same_filesystem_is_kept(self):
        from liuqin_boot_target import encode
        current=self.linux[0]
        current['target'].update(schema_version=1,os_id=current['id'],root_fsuuid=str(uuid.uuid4()),
                                 root_fstype='ext4',root_path='native-root')
        self.save('transactions/linux-'+self.generation(current)+'/root-installed.json',current)
        self.save('registry.json',self.registry)
        previous=copy.deepcopy(current);generation=str(uuid.uuid4())
        previous['target']['boot_generation']=generation
        previous['image']='systems/'+current['id']+'/images/'+generation+'.img'
        (self.hardware.store/previous['image']).write_bytes(self.image(current))
        old=copy.deepcopy(self.registry)
        old['systems']=[previous if s['id']==previous['id'] else s for s in old['systems']]
        update='kernel-updates/'+self.generation(current)
        self.save(update+'/plan.json',dict(new_registry=self.registry,old_registry=old,
                    new_system=current,previous_boot_sha256=fixture.native._padded_image(
                        self.hardware.store/previous['image'],previous['boot_sha256'],fixture.fixture.CAPACITY)))
        self.save(update+'/previous-registry.json',old)
        self.save(update+'/complete.json',dict(registry_sha256=fixture.backend.checksum(gate.encoded(self.registry)),
                    boot_sha256=fixture.native._padded_image(self.hardware.store/current['image'],current['boot_sha256'],fixture.fixture.CAPACITY)))
        (self.hardware.store/update/'previous-root-target').write_bytes(encode(previous['target']))
        self.save('transactions/linux-'+generation+'/root-installed.json',previous)
        self.save('transactions/linux-'+generation+'/complete.json',dict(registry_sha256='a'*64,boot_sha256='b'*64))
        self.old_system()
        plan=self.prepare_store('cleanup')
        self.assertEqual(sum(s['role']=='previous-kernel' for s in plan['retention']['kept']),1)
        self.run_store(plan)
        self.assertTrue((self.hardware.store/previous['image']).exists())
        self.assertTrue((self.hardware.store/update/'plan.json').exists())
        self.assertEqual(len(list((self.hardware.store/'systems').glob('*/images/*.img'))),4)

    def test_unfinished_kernel_update_pins_entire_store(self):
        self.old_system();self.save('kernel-updates/'+str(uuid.uuid4())+'/plan.json',{})
        with self.assertRaises((FileNotFoundError,RuntimeError)):self.prepare_store('cleanup')
        self.assertFalse(self.hardware.opened)

    def test_active_image_corruption_or_changed_default_blocks_collection(self):
        self.old_system();path=self.hardware.store/self.linux[1]['image'];path.write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError,'引导校验'):self.prepare_store('cleanup')
        self.assertFalse(self.hardware.opened)

    def test_interrupted_directory_move_and_partial_delete_resume(self):
        for point in ('move','delete','receipt'):
            with self.subTest(point=point):
                old=self.old_system();plan=self.prepare_store('cleanup')
                if point=='move':
                    original=retention.os.rename
                    def rename(src,dst):original(src,dst);raise PowerLoss()
                    context=patch.object(retention.os,'rename',side_effect=rename)
                elif point=='delete':
                    original=Path.unlink
                    def unlink(path,*a,**kw):
                        original(path,*a,**kw)
                        if '/.retention/trash/' in str(path):raise PowerLoss()
                    context=patch.object(Path,'unlink',unlink)
                else:
                    original=self.agent.atomic
                    def atomic(path,data):
                        original(path,data)
                        if path.name=='last.json':raise PowerLoss()
                    context=patch.object(self.agent,'atomic',side_effect=atomic)
                with context:
                    with self.assertRaises(PowerLoss):self.run_store(plan)
                self.assertTrue((self.hardware.store/retention.ACTIVE).exists())
                result=self.run_store(self.prepare_store('cleanup'))
                self.assertTrue(result['completed'])
                self.assertFalse((self.hardware.store/retention.ACTIVE).exists())
                self.assertFalse((self.hardware.store/old['image']).exists())
                self.assertTrue(self.report()['default_matches'])

    def test_changed_registry_during_interrupted_cleanup_pins_materials(self):
        self.old_system();plan=self.prepare_store('cleanup');original=retention.os.rename
        def rename(src,dst):original(src,dst);raise PowerLoss()
        with patch.object(retention.os,'rename',side_effect=rename):
            with self.assertRaises(PowerLoss):self.run_store(plan)
        self.registry['extra']='concurrent-change';self.save('registry.json',self.registry)
        report=self.report()
        self.assertTrue(report['cleanup_pending'])
        self.assertFalse(report['cleanup_available'])
        with self.assertRaisesRegex(RuntimeError,'材料发生变化'):self.prepare_store('cleanup')
        self.assertTrue((self.hardware.store/retention.ACTIVE).exists())

    def test_interrupted_collection_blocks_another_installation(self):
        self.save(retention.ACTIVE,dict(schema=1,plan={}))
        with self.assertRaisesRegex(RuntimeError,'先在引导存储管理'):
            fixture.native.require_store_ready(self.hardware)
        self.assertFalse(self.hardware.opened)

    def test_gc_errors_are_deferred_after_successful_commit(self):
        self.old_system();plan=self.preview(self.linux[1]);self.apply(plan)
        for error in (RuntimeError('disk full'),TypeError('bad history')):
            with patch.object(retention,'apply',side_effect=error):
                result=fixture.backend.auto_cleanup(dict(token='c'*32,success=True),self.hardware,self.agent,fixture.native,self.probe)
            self.assertEqual(result,dict(status='deferred',message=str(error)))
        self.assertEqual(self.hardware.state()['registry']['default_id'],self.linux[1]['id'])

    def test_failure_and_non_store_sessions_never_collect(self):
        self.old_system();self.hardware.lease=dict(token='c'*32,scope=dict(kind='combined',parts=[]))
        for success in (False,True):
            result=fixture.backend.auto_cleanup(dict(token='c'*32,success=success),self.hardware,self.agent,fixture.native,self.probe)
            self.assertEqual(result['status'],'skipped')
        self.assertFalse(self.hardware.opened)


if __name__=='__main__':unittest.main(verbosity=2)
