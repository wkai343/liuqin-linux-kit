#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run partition delete/format transactions with real GPT and mkfs file fixtures."""
import contextlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'shared'))
spec=importlib.util.spec_from_file_location('partition_fixtures',PROJECT/'tests/partition-device.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
from liuqin_partition_editor import plan_edit
from liuqin_partitions import partition_arguments,discover
from liuqin_android import managed_layout
from liuqin_layout import parse_print


class Hardware(f.FileHardware):
    def __init__(self,test):super().__init__(test);self.formatted={};self.formats=[]
    def inventory(self,allow_unknown_store=False):
        result=super().inventory(allow_unknown_store)
        for name,metadata in self.formatted.items():
            if name in result:result[name].update(metadata)
        return result
    def append_gpt(self,plan):
        self.operations.append('edit-gpt')
        self.test.run_sg(*['--delete='+str(p['number']) for p in plan.get('deletes',[])],*partition_arguments(plan))
    @contextlib.contextmanager
    def writable(self,part,allowed):
        if part['name'] not in allowed:raise AssertionError('wrong target')
        path=self.root/(part['name']+'.fs')
        with path.open('wb') as out:out.truncate(256*1024**2)
        fd=os.open(path,os.O_RDWR)
        try:yield fd
        finally:os.close(fd)
    def format_root(self,part,filesystem,fsuuid):
        original=f.m.subprocess.run
        def run(args,**kwargs):return original([shutil.which(Path(args[0]).name),*args[1:]],**kwargs)
        with patch.object(f.m.subprocess,'run',side_effect=run):f.m.Hardware.format_root(self,part,filesystem,fsuuid)
        self.formats.append(part['name']);self.formatted[part['name']]=dict(filesystem=filesystem,fsuuid=fsuuid)
    def initialize_new_partition(self,part,planned):
        super().initialize_new_partition(part,planned)
        if part['name']=='linux_boot':shutil.rmtree(self.store);self.store.mkdir()


class EditorDeviceTests(f.PartitionDeviceTests):
    test_two_append_transactions_preserve_userdata_and_store=None
    test_mutated_or_stale_plan_has_no_gpt_write=None
    test_interrupted_append_leaves_original_data_and_refuses_automatic_takeover=None
    def setUp(self):
        super().setUp();self.device.__class__=Hardware;self.device.formatted={};self.device.formats=[]
        self.ops.apply(self.make_plan())
    def edit_plan(self,actions):return plan_edit(self.ops.inspect(),actions,dict(roots=[],userdata_gib=0),self.device.serial,str(uuid.uuid4()))
    def test_format_root_and_delete_it_preserve_userdata_and_existing_boot_files(self):
        state=self.ops.inspect();root=next(p for n,p in state['inventory'].items() if n.startswith('lqr_'))
        (self.device.store/'keep.img').write_bytes(b'old boot image')
        old=f.m.files_snapshot(self.device.store)
        planned=self.edit_plan([dict(name=root['name'],partuuid=root['partuuid'],action='format',filesystem='ext4')])
        self.ops.edit(planned);after=self.ops.inspect()
        self.assertEqual(after['inventory'][root['name']]['filesystem'],'ext4')
        self.assertEqual(after['registry']['rootfs_slots'][0]['status'],'reserved')
        managed_layout(parse_print(after['report']),after['inventory'],after,self.device.serial)
        f.m.preserve_files(self.device.store,old,mutable=('registry.json','SHA256SUMS'));self.assert_original_preserved()
        self.ops.edit(self.edit_plan([dict(name=root['name'],partuuid=root['partuuid'],action='delete')]))
        after=self.ops.inspect();self.assertNotIn(root['name'],after['inventory']);self.assertFalse(after['registry']['rootfs_slots'])
        self.assert_original_preserved();self.assertEqual((self.device.store/'keep.img').read_bytes(),b'old boot image')
    def test_empty_store_format_and_delete_are_explicit(self):
        state=self.ops.inspect();root=next(p for n,p in state['inventory'].items() if n.startswith('lqr_'))
        store=state['inventory']['linux_boot']
        actions=[dict(name=root['name'],partuuid=root['partuuid'],action='delete'),dict(name='linux_boot',partuuid=store['partuuid'],action='format')]
        self.ops.edit(self.edit_plan(actions));after=self.ops.inspect()
        self.assertTrue(discover(after,self.device.serial)['can_create']);self.assertFalse(after['registry']['systems'])
        self.ops.edit(self.edit_plan([dict(name='linux_boot',partuuid=store['partuuid'],action='delete')]))
        after=self.ops.inspect();self.assertNotIn('linux_boot',after['inventory']);self.assertIsNone(after['registry']);self.assert_original_preserved()

class ResumeEditorTests(f.PartitionDeviceTests):
    for _name in list(vars(f.PartitionDeviceTests)):
        if _name.startswith('test_'):locals()[_name]=None
    def test_finish_exact_written_layout_survives_new_minors_without_rewriting_gpt(self):
        self.device.__class__=Hardware;self.device.formatted={};self.device.formats=[]
        source=self.ops.inspect();old=source['inventory']['userdata']
        config=self.make_plan()['configuration']
        plan=plan_edit(source,[dict(name='userdata',partuuid=old['partuuid'],action='delete')],config,self.device.serial,str(uuid.uuid4()))
        self.device.append_gpt(plan)
        original=self.device.inventory
        def inventory(allow_unknown_store=False):
            value=original(allow_unknown_store)
            for part in value.values():
                if part['parent']=='sda':part['dev']='259:'+str(part['number']+100)
            return value
        self.device.inventory=inventory
        before=list(self.device.operations)
        self.assertTrue(self.ops.finish_edit(plan,resume=True,check_only=True)['resumable'])
        self.assertEqual(self.device.operations,before)
        result=self.ops.finish_edit(plan,resume=True)
        self.assertTrue(result['edited']);self.assertEqual(self.device.operations.count('edit-gpt'),before.count('edit-gpt'))
        self.assertEqual(self.ops.inspect()['registry'],plan['candidate_registry'])
        with self.assertRaisesRegex(RuntimeError,'已有文件系统'):self.ops.finish_edit(plan,resume=True)

if __name__=='__main__':unittest.main(verbosity=2)
