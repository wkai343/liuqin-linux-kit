#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise Linux store transactions against files, preserving other systems."""
import copy
import errno
import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch
PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('android_device_tests',PROJECT/'tests/android-device.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
m=fixtures.module
from liuqin_managed import next_linux_registry
from liuqin_android import canonical


class LinuxDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.engine=fixtures.DemoEngine(lambda _:None,root/'host');fixtures.initialize(self.engine,fixtures.cli.layout)
        self.addCleanup(self.engine.close)
        self.device=fixtures.FileHardware(root,self.engine);self.ops=m.LinuxOperations(self.device,self.engine.demo_android_profile)
        registry=m.read_json(self.device.store/'registry.json');old=registry['systems'].pop(0)
        registry['rootfs_slots']=[dict(id=s['id'],name=s['name'],partition=s['partition'],target=s['target'],status='installed')
            for s in registry['systems'] if s['type']=='linux']+[dict(id=old['id'],name=old['name'],partition=old['partition'],target=old['target'],status='reserved')]
        registry.update(default_id=registry['systems'][-1]['id'],default_generation=registry['systems'][-1]['generation'])
        (self.device.store/'registry.json').write_bytes(canonical(registry))
        self.name=old['partition']['name'];self.device.parts[self.name].update(filesystem='unformatted',fsuuid=None)
        generation=str(uuid.uuid4());system=copy.deepcopy(old)
        system.update(target=dict(old['target'],boot_generation=generation),image='systems/'+old['id']+'/images/'+generation+'.img')
        self.boot=b'NEW LINUX BOOT';system['boot_sha256']=hashlib.sha256(self.boot).hexdigest()
        self.scan=self.ops.inspect('layout')
        candidate=next_linux_registry(registry,system,generation,False)
        self.plan=dict(schema_version=1,transaction=generation,set_default=False,registry=registry,
            registry_sha256=self.scan['registry_sha256'],store_files=self.scan['files'],system=system,
            inventory=self.scan['inventory'],candidate_registry=candidate,boot_bytes=len(self.boot),
            previous_boot_sha256=self.device.digest(self.device.parts['boot_a']),padded_sha256=hashlib.sha256(self.boot).hexdigest())
        self.old_files=m.files_snapshot(self.device.store)
        self.protected={name:self.device.digest(p) for name,p in self.device.parts.items() if name!=self.name}
        self.extracted=None
        self.device.verify_linux=self.verify_root

    def verify_root(self,part,system):
        if part['name']!=self.name or self.extracted!=system['target']:
            raise RuntimeError('root binding does not match installed tree')

    def stage(self):
        with patch.object(m,'fetch',return_value=self.boot):self.ops.stage(self.plan,'http://fixture')

    def extract(self):
        part=self.device.parts[self.name]
        with self.device.writable(part,{self.name}) as fd:self.device.write(fd,0,b'NEW LINUX ROOT')
        part.update(filesystem=self.plan['system']['target']['root_fstype'],fsuuid=self.plan['system']['target']['root_fsuuid'])
        self.extracted=copy.deepcopy(self.plan['system']['target'])

    def test_append_one_linux_keeps_android_other_roots_and_all_old_boots(self):
        self.stage();self.ops.guard(self.plan);self.extract();self.ops.finish(self.plan)
        self.ops.commit(self.plan,self.plan['candidate_registry'],self.plan['previous_boot_sha256'])
        for name,digest in self.protected.items():self.assertEqual(self.device.digest(self.device.parts[name]),digest,name)
        m.preserve_files(self.device.store,self.old_files,mutable=('registry.json','SHA256SUMS'))
        self.assertEqual(set(self.device.writes),{self.name,'linux_boot:append'})
        self.assertTrue(all(flag==1 for flag in self.device.flags.values()))

    def test_incomplete_or_wrong_root_never_commits(self):
        self.stage()
        with self.assertRaises(RuntimeError):self.ops.finish(self.plan)
        self.extract();self.extracted['os_id']=str(uuid.uuid4())
        with self.assertRaises(RuntimeError):self.ops.finish(self.plan)
        with self.assertRaises(RuntimeError):self.ops.commit(self.plan,self.plan['candidate_registry'],self.plan['previous_boot_sha256'])
        self.assertEqual(m.read_json(self.device.store/'registry.json'),self.plan['registry'])

    def test_existing_android_and_other_linux_cannot_be_replaced(self):
        for name in ('userdata','linux_boot',self.plan['registry']['systems'][0]['partition']['name']):
            forged=copy.deepcopy(self.plan);forged['system']['partition']['name']=name
            with self.assertRaises(ValueError),patch.object(m,'fetch',side_effect=AssertionError('must reject before fetch')):
                self.ops.stage(forged,'http://fixture')
        forged=copy.deepcopy(self.plan);forged['candidate_registry']['systems'][0]['name']='changed'
        with self.assertRaises(ValueError):self.ops.stage(forged,'http://fixture')
        self.assertEqual(self.device.writes,[])

    def test_stale_registry_and_boundary_change_stop_before_root_install(self):
        self.stage();self.device.parts[self.name]['start']+=4096
        with self.assertRaises(RuntimeError):self.ops.guard(self.plan)
        self.device.parts[self.name]['start']-=4096
        (self.device.store/'extra/preserve.txt').write_text('changed after confirmation')
        with self.assertRaises(RuntimeError):self.ops.guard(self.plan)

    def test_sparse_stage_keeps_full_snapshot_with_lower_store_budget(self):
        space=SimpleNamespace(f_bavail=(16*1024**2+8192)//4096,f_frsize=4096)
        with patch.object(m.os,'statvfs',return_value=space):self.stage()
        self.ops.staged(self.plan,self.device.inventory())
        previous=self.device.store/'transactions'/('linux-'+self.plan['transaction'])/'previous-boot.img'
        self.assertEqual(previous.read_bytes(),self.device.files['boot_a'].read_bytes())
        self.assertLessEqual(previous.stat().st_blocks*512,4096)

    def test_default_rollback_keeps_new_linux_registered_and_old_default(self):
        from liuqin_managed import next_linux_registry
        self.plan['set_default']=True
        self.plan['candidate_registry']=next_linux_registry(self.plan['registry'],self.plan['system'],self.plan['transaction'],True)
        self.stage();self.extract();self.ops.finish(self.plan)
        self.device.files['boot_a'].write_bytes(self.boot)
        self.ops.commit(self.plan,self.plan['candidate_registry'],self.plan['padded_sha256'])
        previous=self.device.store/'transactions'/('linux-'+self.plan['transaction'])/'previous-boot.img'
        self.device.files['boot_a'].write_bytes(previous.read_bytes())
        restored=copy.deepcopy(self.plan['candidate_registry'])
        restored.update(default_id=self.plan['registry']['default_id'],default_generation=self.plan['registry']['default_generation'])
        self.ops.commit(self.plan,restored,self.plan['previous_boot_sha256'],True)
        self.assertEqual(m.read_json(self.device.store/'registry.json'),restored)
        m.preserve_files(self.device.store,self.old_files,mutable=('registry.json','SHA256SUMS'))

class LinuxRootVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.hardware=m.Hardware.__new__(m.Hardware)
        self.hardware.work=Path(self.temp.name)
        self.hardware.sealed=Mock()
        self.part=dict(name='lqr_0123456789ab',node='sda38')
        system_id=str(uuid.uuid4())
        self.system=dict(id=system_id,target=dict(schema_version=1,os_id=system_id,
            root_partuuid=str(uuid.uuid4()),root_fsuuid=str(uuid.uuid4()),
            root_fstype='btrfs',root_path='native-root',boot_generation=str(uuid.uuid4())))

    def verify(self,filesystem,binding_matches=True):
        from liuqin_boot_target import encode
        self.system['target']['root_fstype']=filesystem
        expected={'liuqin-system-id':(self.system['id']+'\n').encode(),
                  'liuqin-boot-target':encode(self.system['target'])}
        commands=[]

        def run(arguments,**kwargs):
            commands.append(arguments)
            if arguments[0]=='mount':
                options=set(arguments[arguments.index('-o')+1].split(','))
                if filesystem=='btrfs':
                    # The Recovery kernel rejects the old standalone flag.
                    if 'nologreplay' in options:
                        raise subprocess.CalledProcessError(255,arguments)
                    self.assertTrue({'ro','rescue=nologreplay','subvolid=5'}<=options)
                else:self.assertTrue({'ro','noload'}<=options)
                self.assertTrue({'nosuid','nodev','noexec'}<=options)
                self.assertEqual(arguments[-2],'/dev/sda38')
            return subprocess.CompletedProcess(arguments,0)

        def read(path):
            return expected[path.name] if binding_matches else b'wrong system\n'

        with patch.object(m.subprocess,'run',side_effect=run),patch.object(Path,'read_bytes',read):
            if binding_matches:self.hardware.verify_linux(self.part,self.system)
            else:
                with self.assertRaisesRegex(RuntimeError,'根绑定不匹配'):
                    self.hardware.verify_linux(self.part,self.system)
        self.hardware.sealed.assert_called_once_with([self.part])
        self.assertEqual([c[0] for c in commands],['mount','umount'])
        self.assertEqual(commands[1][-1],commands[0][-1])
        self.assertEqual(list(self.hardware.work.iterdir()),[])

    def test_btrfs_verification_disables_log_replay_with_supported_option(self):
        self.verify('btrfs')

    def test_ext4_verification_disables_journal_replay(self):
        self.verify('ext4')

    def test_failed_binding_verification_still_unmounts(self):
        self.verify('btrfs',binding_matches=False)


class BtrfsSuperblockTests(unittest.TestCase):
    def test_only_in_range_superblocks_are_cleared(self):
        with tempfile.TemporaryFile() as image:
            size=64*1024**2+8192;image.truncate(size)
            offsets=(64*1024,64*1024**2)
            for offset in offsets:
                os.pwrite(image.fileno(),b'KEEP'+b'X'*4096+b'KEEP',offset-4)
            m.clear_btrfs_superblocks(image.fileno(),size)
            for offset in offsets:
                self.assertEqual(os.pread(image.fileno(),4104,offset-4),b'KEEP'+bytes(4096)+b'KEEP')
            self.assertEqual(os.fstat(image.fileno()).st_size,size)

    def test_busy_device_is_rejected_before_clearing(self):
        with patch.object(m.os,'open',side_effect=OSError(errno.EBUSY,'busy')) as opened, \
             patch.object(m.os,'pwrite') as written:
            with self.assertRaises(OSError):m.clear_btrfs_superblocks(123,256*1024**2)
            self.assertTrue(opened.call_args.args[1]&os.O_EXCL)
            written.assert_not_called()

    def test_short_write_aborts_and_closes_exclusive_descriptor(self):
        with tempfile.TemporaryFile() as image:
            image.truncate(256*1024**2)
            with patch.object(m.os,'pwrite',return_value=1),patch.object(m.os,'close',wraps=os.close) as closed:
                with self.assertRaisesRegex(RuntimeError,'写入不完整'):
                    m.clear_btrfs_superblocks(image.fileno(),256*1024**2)
                closed.assert_called_once()


if __name__=='__main__':unittest.main(verbosity=2)
