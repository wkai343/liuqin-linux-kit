#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Actual append-only sgdisk on a sparse file plus device registry transactions."""
import contextlib
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
import uuid
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'shared'))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
fixture=load('gpt_fixture',PROJECT/'tests/multiboot-device.py')
m=load('partition_device',PROJECT/'device/installer/android-device.py')
from liuqin_partitions import discover,plan_append,partition_arguments
from liuqin_layout import parse_print,GIB


class FileHardware:
    serial='FILE-LIUQIN'
    def __init__(self,test):
        self.test=test;self.root=test.root;self.store=self.root/'store';self.store.mkdir()
        self.store_image=self.root/'store.img';self.boot=b'KEEP CURRENT DEFAULT BOOT'
        self.created=[];self.operations=[];self.fail_after_gpt=False
    def gpt(self):
        report=self.test.run_sg('-p');disk=parse_print(report)
        return report,{str(p.number):self.test.run_sg('-i',str(p.number)) for p in disk.partitions}
    def inventory(self,allow_unknown_store=False):
        report,details=self.gpt();result={}
        for p in parse_print(report).partitions:
            guid=re.search(r'Partition unique GUID: (\S+)',details[str(p.number)])[1].lower()
            part=dict(name=p.name,node='sda'+str(p.number),dev='8:'+str(p.number),parent='sda',parent_dev='8:0',
                number=p.number,start=p.start,bytes=p.size,partuuid=guid)
            if p.name=='linux_boot':
                if self.store_image.exists():
                    with self.store_image.open('rb') as stream:stream.seek(1128);fsuuid=str(uuid.UUID(bytes=stream.read(16)))
                    part.update(filesystem='ext4',fsuuid=fsuuid)
                else:part.update(filesystem='unformatted',fsuuid=None)
            elif p.name.startswith('lqr_'):part.update(filesystem='unformatted',fsuuid=None)
            result[p.name]=part
        for i,name in enumerate(('boot_a','boot_b')):
            result[name]=dict(name=name,node='sde'+str(i+1),dev='8:'+str(65+i),parent='sde',parent_dev='8:64',
                number=i+1,start=i*4096,bytes=4096,partuuid=str(uuid.uuid5(uuid.NAMESPACE_DNS,name)))
        return result
    def sealed(self,parts):pass # Only ordinary files exist; no block nodes can be opened.
    @contextlib.contextmanager
    def mount_store(self,part,writable=False):
        if writable:self.operations.append('write-store')
        yield self.store
    def read(self,part,offset,length):
        with self.test.disk.open('rb') as stream:stream.seek(part['start']+offset);return stream.read(length)
    def append_gpt(self,plan):
        self.operations.append('append-gpt')
        self.test.run_sg(*partition_arguments(plan))
        if self.fail_after_gpt:raise RuntimeError('interrupted after GPT')
    def initialize_new_partition(self,part,planned):
        self.created.append(part['name'])
        if part['name']=='linux_boot':
            with self.store_image.open('wb') as out:out.truncate(part['bytes'])
            subprocess.run([shutil.which('mkfs.ext4'),'-q','-F','-U',planned['fsuuid'],str(self.store_image)],check=True,capture_output=True)
        elif part['name']=='userdata':
            with self.test.disk.open('r+b') as out:out.seek(part['start']);out.write(bytes(16*1024**2))
        else:raise AssertionError('must not format rootfs during partition step')


class PartitionDeviceTests(fixture.DeviceLayoutTests):
    # Reuse only the sparse disk/sgdisk adapter setup, not its inherited tests.
    test_real_gpt_write_preserves_prefix_and_userdata_guid=None
    test_corrupt_plan_and_changed_report_never_open_write_window=None
    test_userdata_and_store_without_linux=None
    test_linux_slots_without_userdata=None
    def setUp(self):
        super().setUp()
        disk=parse_print(self.run_sg('-p'));part=disk.by_name('userdata')
        guid=re.search(r'Partition unique GUID: (\S+)',self.run_sg('-i','35'))[1]
        self.run_sg('--delete=35',f'--new=35:{part.first_sector}:{part.first_sector+32*GIB//4096-1}',
            '--typecode=35:8300','--change-name=35:userdata','--partition-guid=35:'+guid)
        self.device=FileHardware(self);self.ops=m.PartitionOperations(self.device)
        self.sentinels={part.start:b'ANDROID USER DATA',part.start+16*GIB:b'PERSONAL FILES',part.start+32*GIB-4096:b'KEEP LAST DATA BLOCK'}
        with self.disk.open('r+b') as out:
            for offset,data in self.sentinels.items():out.seek(offset);out.write(data)
        self.before_details=self.device.gpt()[1]
    def new_config(self):
        return dict(schema_version=1,partition_only=True,userdata_gib=0,boot_gib=2,boot_action='install-only',
            roots=[dict(id=str(uuid.uuid4()),name='Linux',size_gib=16,filesystem='ext4')])
    def make_plan(self):return plan_append(self.ops.inspect(),self.new_config(),self.device.serial,str(uuid.uuid4()))
    def assert_original_preserved(self):
        details=self.device.gpt()[1]
        self.assertTrue(all(details[k]==v for k,v in self.before_details.items()))
        with self.disk.open('rb') as stream:
            for offset,data in self.sentinels.items():stream.seek(offset);self.assertEqual(stream.read(len(data)),data)
        self.assertEqual(self.device.boot,b'KEEP CURRENT DEFAULT BOOT')
    def test_two_append_transactions_preserve_userdata_and_store(self):
        self.ops.apply(self.make_plan());self.assert_original_preserved()
        self.assertEqual(self.device.created,['linux_boot'])
        (self.device.store/'keep.img').write_bytes(b'KEEP OLD BOOT IMAGE')
        old=m.files_snapshot(self.device.store);old_registry=m.read_json(self.device.store/'registry.json')
        second=self.make_plan();self.ops.apply(second);self.assert_original_preserved()
        m.preserve_files(self.device.store,old,mutable=('registry.json','SHA256SUMS'))
        new=m.read_json(self.device.store/'registry.json')
        self.assertEqual(new['rootfs_slots'][:-1],old_registry['rootfs_slots'])
        self.assertEqual(self.device.created,['linux_boot'],'existing boot store was formatted')
        self.assertTrue(discover(self.ops.inspect(),self.device.serial)['can_create'])
    def test_mutated_or_stale_plan_has_no_gpt_write(self):
        plan=self.make_plan();plan['partitions'][0]['start']=next(iter(self.sentinels))
        with self.assertRaises(ValueError):self.ops.apply(plan)
        self.assertNotIn('append-gpt',self.device.operations)
        plan=self.make_plan();self.run_sg('--attributes=35:set:2')
        with self.assertRaisesRegex(RuntimeError,'改变'):self.ops.apply(plan)
        self.assertNotIn('append-gpt',self.device.operations)
    def test_interrupted_append_leaves_original_data_and_refuses_automatic_takeover(self):
        self.device.fail_after_gpt=True
        with self.assertRaisesRegex(RuntimeError,'interrupted'):self.ops.apply(self.make_plan())
        self.assert_original_preserved();self.assertFalse(self.device.created)
        self.assertFalse(discover(self.ops.inspect(),self.device.serial)['can_create'])

if __name__=='__main__':unittest.main(verbosity=2)
