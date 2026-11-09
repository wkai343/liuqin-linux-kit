#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fixed Rec protection with ROM projection and ordinary-file block substitutes."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'shared'),str(ROOT/'installer')]
import liuqin_native_install as gate
import liuqin_recovery_preserve as preserve
from liuqin_gui import fixed_recovery
from liuqin_firmware import fingerprint_id


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


universal=module('preserve_universal_fixture',ROOT/'tests/universal-recovery.py')
full=module('preserve_full_fixture',ROOT/'tests/full-rom.py')


def part(name,size=4096):
    return dict(name=name,parent='sde',start=4096,bytes=size,partuuid=str(uuid.uuid4()))


def plan_for(rows,old,new):
    checks=[dict(partition=gate.identity(rows[n]),bytes=4096,sha256='a'*64) for n in sorted(preserve.CHAIN)]
    target=gate.identity(rows['recovery_a'])
    return dict(schema=1,image_sha256=new,image_bytes=preserve.CAPACITY,build_id='c'*64,
        partition=target,previous_sha256=old,backup=dict(identity=target,sha256=old),
        before=checks,after=copy.deepcopy(checks),adaptation='reuse')


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.before=universal.observed('miku26101322','miui-abl-20230422',3)
        self.after=copy.deepcopy(self.before)
        slot=self.before['slots']['a']
        self.rows={role+'_a':part(role+'_a',m['partition_bytes']) for role,m in slot['match'].items() if role!='layout'}
        self.rows['recovery_a']=part('recovery_a',preserve.CAPACITY)
        self.profile=dict(generic_ota_version=2)
        self.engine=SimpleNamespace(device={'serial':'unit-test'},transport=SimpleNamespace(boot_id=self.before['boot_id']),
            manifest={'native_recovery_manifest':{'fixed_recovery_restore_version':1}},receipt={'firmware_fingerprint':self.before})
        self.record=dict(path=str(self.root/'recovery.img'),qualification_path=str(self.root/'qualification.json'),qualification_sha256='5'*64,
            qualification={'firmware':copy.deepcopy(slot)},checked=dict(image_bytes=preserve.CAPACITY,image_sha256='6'*64,
                manifest=dict(build_id='7'*64,usage=gate.FIXED_USAGE,fixed_path_external_dtbo_required=False,universal_policy=copy.deepcopy(universal.POLICY))))
        self.backups={'recovery_a':dict(identity=gate.identity(self.rows['recovery_a']),sha256='6'*64)}
        from liuqin_ota import CRITICAL
        dependencies={}
        for n in CRITICAL:
            name=n+'_a'
            if name not in self.rows:self.rows[name]=part(name)
            digest=slot['match']['abl']['sha256'] if n=='abl' else 'b'*64
            self.backups[name]=dict(identity=gate.identity(self.rows[name]),sha256=digest)
            dependencies[name]=dict(bytes=self.rows[name]['bytes'],sha256=digest)
        self.record.update(dependencies=dependencies,compatibility_path=str(self.root/'restore.json'),compatibility_sha256='4'*64)
        self.images=[]
        self.project=patch('liuqin_gui.native_ota.project_firmware',side_effect=lambda *a:(self.after,{}))
        self.project.start();self.addCleanup(self.project.stop)
    def prepare(self):
        return fixed_recovery.prepare(self.engine,self.profile,{'boot.img':str(self.root/'boot.img')},
            {'inventory':self.rows},self.images,self.backups,records=[self.record])
    def test_same_rom_keeps_exact_image_and_other_recovery_is_out_of_scope(self):
        result=self.prepare();preserve.validate(result['plan'])
        self.assertEqual(result['action'],'保留');self.assertEqual(result['plan']['adaptation'],'reuse')
        self.assertEqual(result['plan']['partition']['name'],'recovery_a')
        self.assertFalse(result['hardware_boot_after_rom_verified'])
    def test_previously_overwritten_rec_gets_restore_plan_and_exact_backup(self):
        self.backups['recovery_a']['sha256']='8'*64
        result=self.prepare();self.assertEqual(result['action'],'恢复')
        self.assertEqual(result['plan']['backup']['sha256'],'8'*64)
    def new_metadata(self,flags=3):
        slot=self.after['slots']['a'];slot['avb'].update(normalized_flags_sha256='d'*64,flags=flags)
        slot['match']['vbmeta']['sha256']='e'*64;slot['avb']['partition_sha256']='e'*64
        slot['measured']=copy.deepcopy(slot['match']);slot['fingerprint_id']=fingerprint_id(slot['match'])
    def test_new_rom_metadata_reuses_embedded_kernel_dtbo_without_avb_patch(self):
        self.new_metadata();before=copy.deepcopy(self.after)
        result=self.prepare();self.assertEqual(result['plan']['adaptation'],'rom-metadata')
        self.assertEqual(self.after,before);self.assertNotEqual(result['plan']['before'],result['plan']['after'])
    def test_new_rom_reuse_stays_available_on_later_reinstalls(self):
        self.new_metadata();self.before.update(copy.deepcopy(self.after))
        self.assertIsNotNone(self.prepare())
    def test_unknown_metadata_with_verification_enabled_blocks_protection(self):
        self.new_metadata(flags=0)
        with self.assertRaisesRegex(RuntimeError,'无法证明'):self.prepare()
    def test_changed_abl_or_other_lower_firmware_blocks_protection(self):
        self.rows['tz_a']=part('tz_a',4096)
        self.images=[dict(partition=self.rows['tz_a'],whole_sha256='9'*64)]
        self.backups['tz_a']=dict(sha256='8'*64)
        with self.assertRaisesRegex(RuntimeError,'tz_a'):self.prepare()
        self.images=[]
        slot=self.after['slots']['a'];slot['match']['abl']['sha256']='f'*64
        slot['measured']=copy.deepcopy(slot['match']);slot['fingerprint_id']=fingerprint_id(slot['match'])
        with self.assertRaisesRegex(RuntimeError,'无法证明'):self.prepare()
    def test_preexisting_lower_firmware_change_is_not_treated_as_qualified(self):
        self.backups['tz_a']['sha256']='c'*64
        with self.assertRaisesRegex(RuntimeError,'冷启动验收基线不同'):self.prepare()
    def test_missing_backup_old_executor_and_unqualified_fixed_are_rejected(self):
        self.engine.manifest['native_recovery_manifest']['fixed_recovery_restore_version']=0
        with self.assertRaisesRegex(RuntimeError,'临时启动新版'):self.prepare()
        self.engine.manifest['native_recovery_manifest']['fixed_recovery_restore_version']=1
        self.backups.clear()
        with self.assertRaisesRegex(RuntimeError,'完整备份'):self.prepare()
        self.engine.manifest['native_recovery_manifest']['usage']=gate.FIXED_USAGE
        with self.assertRaisesRegex(RuntimeError,'冷启动验收'):
            fixed_recovery.prepare(self.engine,self.profile,{}, {},[],{},records=[])
    def test_no_fixed_install_does_not_add_a_new_recovery(self):
        self.assertIsNone(fixed_recovery.prepare(self.engine,self.profile,{}, {},[],{},records=[]))


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.profile=full.fixture();rows=full.inventory(self.profile)
        self.rows=rows
        rows['recovery_a']['bytes']=preserve.CAPACITY
        self.plan=plan_for(rows,'8'*64,'9'*64)
        names={r['partition'] for r in self.profile['images'].values()}
        self.scope=dict(kind='android',parts=[gate.identity(rows[n]) for n in sorted(names)],full_rom=True,
            android_boot_write=True,rom_targets=sorted(names),rom_backups={n:dict(identity=gate.identity(rows[n]),sha256='8'*64) for n in names},
            android_profile_sha256=gate.digest(self.profile),android_boot_sha256=self.profile['images']['boot.img']['sha256'],
            previous_boot_sha256='0'*64,gpt_sha256=None,fixed_recovery=self.plan)
    def test_only_recovery_a_with_backup_and_full_rom_is_admitted(self):
        gate.validate_scope(self.scope)
        for target in ('recovery_b','boot_a','abl_a','linux_boot'):
            s=copy.deepcopy(self.scope);s['fixed_recovery']['partition']['name']=target
            with self.assertRaises(ValueError):gate.validate_scope(s)
        s=copy.deepcopy(self.scope);s['full_rom']=False
        with self.assertRaises(ValueError):gate.validate_scope(s)
        s=copy.deepcopy(self.scope);s['fixed_recovery']['backup']['sha256']='f'*64
        with self.assertRaises(ValueError):gate.validate_scope(s)
    def test_readonly_rom_inspection_does_not_require_an_installation_token(self):
        with patch.dict(os.environ,{},clear=True),patch.object(gate,'require',side_effect=RuntimeError('no lease')) as require:
            self.assertEqual(preserve.active(optional=True),(None,None));require.assert_not_called()
            with self.assertRaisesRegex(RuntimeError,'no lease'):preserve.active()
    def test_rom_without_recovery_still_gets_only_the_explicit_restore_target(self):
        names=set(self.scope['rom_targets'])-{'recovery_a'}
        snapshot=dict(kind='android',target='userdata',source={},request={'set_default':True},default_target='userdata',
            recovery_mode='off',previous_boot_sha256='0'*64,full_rom=True,rom_targets=sorted(names),
            rom_backups={n:b for n,b in self.scope['rom_backups'].items() if n in names},
            android_profile_sha256=self.scope['android_profile_sha256'],android_boot_sha256=self.scope['android_boot_sha256'],
            fixed_recovery={'plan':self.plan},
            edit=dict(source={'inventory':self.rows},projected={'inventory':self.rows},formats=[],partitions=[],deletes=[],changes=False))
        result=gate.scope(snapshot);gate.validate_scope(result)
        self.assertIn(self.plan['partition'],result['parts'])
        self.assertNotIn('recovery_a',result['rom_targets'])
        snapshot.pop('fixed_recovery');self.assertNotIn(self.plan['partition'],gate.scope(snapshot)['parts'])
    def test_old_executor_and_unverified_backup_cannot_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);boot=str(uuid.uuid4())
            marker=dict(installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,
                usage='temporary-boot-only',generic_ota_version=2,build_id='a'*64)
            p=root/gate.MARKER;p.parent.mkdir(parents=True);p.write_text(json.dumps(marker))
            p=root/'proc/sys/kernel/random/boot_id';p.parent.mkdir(parents=True);p.write_text(boot)
            probe=dict(device='liuqin',serial='TEST',slot='a',unlocked=True,sealed=True,boot_id=boot,build_id='a'*64)
            request=dict(token='b'*32,plan_id='c'*64,boot_id=boot,build_id='a'*64,serial='TEST',
                scope=self.scope,android_profile=self.profile,rom_backups_verified=True)
            with self.assertRaisesRegex(ValueError,'自动保护'):gate.begin(request,probe,root)
            marker['fixed_recovery_restore_version']=1;(root/gate.MARKER).write_text(json.dumps(marker))
            with self.assertRaisesRegex(ValueError,'完整备份'):gate.begin(request,probe,root)
            gate.begin(dict(request,fixed_recovery_backup_verified=True),probe,root)
            with self.assertRaisesRegex(RuntimeError,'启动链回读'):
                gate.finish(dict(token=request['token'],success=True),probe,root)


class CandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=universal.UniversalTests(methodName='test_full_v2_roundtrip')
        universal.UniversalTests.setUpClass()
        cls.image,_,_,_=cls.fixture.fixture(installer=True)
        cls.checked=universal.inspect(cls.image)
    @classmethod
    def tearDownClass(cls):universal.UniversalTests.tearDownClass()
    def setUp(self):
        m=self.checked['manifest']
        self.qualification=dict(schema=1,device='liuqin',serial='TEST',slot='a',partition='recovery_a',
            image_sha256=self.checked['image_sha256'],build_id=m['build_id'],hardware_fixed_boot_verified=True,
            hardware_cold_boot_verified=True,readback_verified=True,storage_sealed_verified=True,
            firmware=universal.observed('miku26101322','miui-abl-20230422',3)['slots']['a'])
        from liuqin_ota import CRITICAL
        self.compatibility=dict(schema=1,device='liuqin',serial='TEST',image_sha256=self.checked['image_sha256'],build_id=m['build_id'],
            firmware_dependencies={n+'_a':dict(bytes=4096,sha256='a'*64) for n in CRITICAL})
        self.qpath=self.image.parent/'hardware-qualification.json';self.qpath.write_text(json.dumps(self.qualification))
        self.cpath=self.image.parent/'restore-compatibility.json';self.cpath.write_text(json.dumps(self.compatibility))
        self.engine=SimpleNamespace(device={'serial':'TEST'},runtime_path=str(self.image))
        p=patch('liuqin_gui.native_profiles.image_paths',return_value=[str(self.image)]);p.start();self.addCleanup(p.stop)
    def test_qualification_and_dependency_sidecar_bind_the_real_signed_image(self):
        rows=fixed_recovery.candidates(self.engine);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['checked']['image_sha256'],self.checked['image_sha256'])
        self.assertTrue(rows[0]['compatibility_sha256'])
    def test_foreign_device_and_missing_cold_boot_proof_are_not_adopted(self):
        self.engine.device['serial']='FOREIGN';self.assertFalse(fixed_recovery.candidates(self.engine))
        self.engine.device['serial']='TEST';self.qualification['hardware_cold_boot_verified']=False
        self.qpath.write_text(json.dumps(self.qualification))
        with self.assertRaisesRegex(RuntimeError,'验收记录'):fixed_recovery.candidates(self.engine)
    def test_missing_or_wrong_dependency_sidecar_stops_instead_of_flashing_rom_recovery(self):
        self.cpath.unlink()
        with self.assertRaisesRegex(RuntimeError,'恢复适配记录'):fixed_recovery.candidates(self.engine)
        self.compatibility['image_sha256']='f'*64;self.cpath.write_text(json.dumps(self.compatibility))
        with self.assertRaisesRegex(RuntimeError,'未绑定本机镜像'):fixed_recovery.candidates(self.engine)


class WriterTests(unittest.TestCase):
    def test_managed_writer_never_writes_rom_recovery_and_records_effective_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);profile=full.fixture();hardware=full.Hardware(profile,root)
            hardware.rows['recovery_a']['bytes']=preserve.CAPACITY
            fixed=plan_for(hardware.rows,'8'*64,'9'*64)
            lease=dict(scope=dict(full_rom=True,rom_backups={},fixed_recovery=fixed))
            operation=full.agent.Operations(hardware,profile)
            operation.guard=lambda plan:hardware.inventory();operation.staged=lambda *a:None
            transaction=str(uuid.uuid4());(root/'transactions'/('android-'+transaction)).mkdir(parents=True)
            (root/'registry.json').write_bytes(b'{}')
            plan=dict(mode='reinstall',images=full.plans(profile,hardware.rows),transaction=transaction,
                registry_sha256=hashlib.sha256(b'{}').hexdigest(),store_files={})
            with patch.object(gate,'require',return_value=lease),patch.object(preserve,'ensure',return_value=fixed),\
                    patch.object(preserve,'verify',return_value={'recovery_readback_verified':True}),\
                    patch.object(full.agent,'fetch',return_value=b'fixture'),contextlib.redirect_stdout(io.StringIO()):
                result=operation.install(plan,'http://fixture')
            self.assertNotIn('recovery_a',hardware.writes)
            self.assertIn('super',hardware.writes);self.assertIn('abl_a',hardware.writes)
            self.assertEqual(hardware.formats,['userdata','metadata'])
            completion=json.loads((root/'transactions'/('android-'+transaction)/'rom-installed.json').read_text())
            self.assertEqual(completion['images']['recovery_a'],'9'*64)
            self.assertTrue(result['recovery_readback_verified'])
    def test_android_only_without_boot_store_also_skips_rom_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);profile=full.fixture();hardware=full.Hardware(profile,root)
            hardware.rows={n:p for n,p in hardware.rows.items() if n not in ('linux_boot','lqr_123456789012')}
            hardware.rows['recovery_a']['bytes']=preserve.CAPACITY
            fixed=plan_for(hardware.rows,'8'*64,'9'*64)
            digest=hardware.digest
            hardware.digest=lambda p,offset=0,length=None:'9'*64 if p['name']=='recovery_a' and length is None else digest(p,offset,length)
            operation=full.agent.Operations(hardware,profile)
            plan=dict(inventory=hardware.inventory(),images=full.plans(profile,hardware.rows),previous_boot_sha256='0'*64)
            with patch.object(preserve,'ensure',return_value=fixed),patch.object(preserve,'active',return_value=({},fixed)),\
                    patch.object(preserve,'verify',return_value={'recovery_readback_verified':True}),\
                    patch.object(full.agent,'PartitionOperations',return_value=SimpleNamespace(inspect=lambda:{})),\
                    patch('liuqin_partitions.discover',return_value={'can_create':True}),\
                    patch.object(full.agent,'fetch',return_value=b'fixture'),contextlib.redirect_stdout(io.StringIO()):
                result=operation.direct_install(plan,'http://fixture')
            self.assertNotIn('recovery_a',hardware.writes)
            self.assertIn('super',hardware.writes);self.assertIn('abl_a',hardware.writes)
            self.assertEqual(hardware.formats,['userdata','metadata'])
            self.assertTrue(result['recovery_readback_verified'])


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        patcher=patch.object(preserve,'CAPACITY',65536);patcher.start();self.addCleanup(patcher.stop)
        self.old=b'ANDROID!'+b'o'*(preserve.CAPACITY-8)
        self.new=b'ANDROID!'+b'n'*(preserve.CAPACITY-8)
        self.rows={n:part(n) for n in preserve.CHAIN};self.rows['recovery_a']=part('recovery_a',preserve.CAPACITY)
        self.plan=plan_for(self.rows,hashlib.sha256(self.old).hexdigest(),hashlib.sha256(self.new).hexdigest())
        for phase in ('before','after'):
            for row in self.plan[phase]:row['sha256']=hashlib.sha256(b'x'*4096).hexdigest()
        self.lease=dict(token='a'*32,scope={'fixed_recovery':self.plan})
        self.block=self.root/'block.img';self.block.write_bytes(self.old)
        self.writes=[];self.fail=False
        owner=self
        class Hardware:
            def inventory(self):return copy.deepcopy(owner.rows)
            def sealed(self,parts):pass
            def digest(self,p,offset=0,length=None):
                data=owner.block.read_bytes() if p['name']=='recovery_a' else b'x'*4096
                return hashlib.sha256(data[offset:offset+length] if length is not None else data[offset:]).hexdigest()
            def read(self,p,offset,n):return owner.block.read_bytes()[offset:offset+n]
            @contextlib.contextmanager
            def writable(self,p,allowed):
                owner.assertEqual(allowed,{'recovery_a'});owner.writes.append(p['name'])
                with owner.block.open('r+b') as stream:yield stream.fileno()
            def write(self,fd,offset,data):
                if owner.fail:
                    owner.fail=False;os.pwrite(fd,b'bad',0);raise OSError('write interrupted')
                os.pwrite(fd,data,offset)
        self.hardware=Hardware()
        for p in (patch.object(gate,'require',return_value=self.lease),patch.object(gate,'store'),
                patch.object(preserve,'active',side_effect=lambda:(self.lease,self.plan))):
            p.start();self.addCleanup(p.stop)
    def stage(self,data=None):
        return preserve.stage(dict(token=self.lease['token'],url='http://127.0.0.1:12345/'+'b'*32+'/boot'),
            self.hardware,lambda *a,**k:io.BytesIO(self.new if data is None else data),self.root)
    def test_restore_reads_back_exact_partition_and_verifies_chain(self):
        self.stage();self.assertFalse(self.writes)
        preserve.ensure(self.hardware,self.root)
        self.assertEqual(self.block.read_bytes(),self.new);self.assertEqual(self.writes,['recovery_a'])
        self.assertTrue(preserve.verify(self.hardware)['recovery_readback_verified'])
    def test_restore_keeps_the_durable_write_started_flag(self):
        self.stage();fresh=dict(self.lease,write_started=True)
        with patch.object(gate,'require',return_value=fresh):
            preserve.ensure(self.hardware,self.root)
        self.assertTrue(gate.store.call_args.args[0]['write_started'])
        self.assertTrue(gate.store.call_args.args[0]['fixed_recovery_applied'])
    def test_already_installed_image_is_kept_without_any_block_write(self):
        self.old=self.new;self.block.write_bytes(self.new)
        self.plan['previous_sha256']=self.plan['image_sha256'];self.plan['backup']['sha256']=self.plan['image_sha256']
        self.stage();preserve.ensure(self.hardware,self.root);self.assertFalse(self.writes)
    def test_truncated_or_corrupt_transfer_never_opens_a_block_writer(self):
        for data in (self.new[:-1],b'z'*len(self.new)):
            if preserve.directory(self.lease,self.root).exists():
                import shutil;shutil.rmtree(preserve.directory(self.lease,self.root))
            with self.assertRaises(RuntimeError):self.stage(data)
            self.assertFalse(self.writes);self.assertEqual(self.block.read_bytes(),self.old)
    def test_staged_material_or_partition_changes_stop_before_writing(self):
        self.stage();(preserve.directory(self.lease,self.root)/'recovery.img').write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError,'完整暂存'):preserve.ensure(self.hardware,self.root)
        self.assertFalse(self.writes)
    def test_restore_failure_rolls_back_before_rom_writes(self):
        self.stage();self.fail=True
        with self.assertRaisesRegex(RuntimeError,'原 Recovery 已回读恢复'):preserve.ensure(self.hardware,self.root)
        self.assertEqual(self.block.read_bytes(),self.old);self.assertFalse(self.lease.get('fixed_recovery_applied'))
    def test_after_chain_mismatch_does_not_complete(self):
        self.stage();preserve.ensure(self.hardware,self.root)
        next(r for r in self.plan['after'] if r['partition']['name']=='dtbo_a')['sha256']='f'*64
        with self.assertRaisesRegex(RuntimeError,'启动链'):preserve.verify(self.hardware)
        self.assertFalse(self.lease.get('fixed_recovery_verified'))
    def test_rom_completion_hash_records_fixed_image_not_skipped_rom_image(self):
        image=dict(partition=self.rows['recovery_a'],whole_sha256='f'*64)
        self.assertEqual(preserve.effective_hashes([image],self.lease),{'recovery_a':self.plan['image_sha256']})
        self.assertEqual(preserve.effective_hashes([image],None),{'recovery_a':'f'*64})


if __name__=='__main__':unittest.main(verbosity=2)
