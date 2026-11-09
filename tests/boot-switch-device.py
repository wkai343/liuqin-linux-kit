#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the native boot-switch transaction with ordinary temporary files."""
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'shared'), str(PROJECT/'installer')]
import liuqin_native_install as gate
import liuqin_partitions


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native = load('switch_device_native', PROJECT/'device/recovery/native-install.py')
android = load('switch_device_android', PROJECT/'device/installer/android-device.py')
CAPACITY = 16 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FileHardware:
    serial = 'TEST'

    def __init__(self, directory):
        self.work = directory/'ram'
        self.work.mkdir()
        self.store = directory/'store'
        self.store.mkdir()
        self.blocks = directory/'blocks'
        self.blocks.mkdir()
        self.parts = {}
        self.opened = []
        self.verified_roots = []
        self.lease = None
        self.corrupt_boot_once = False
        self.bad_root = False
        self.pending = []
        self.write_window = False

    def add_partition(self, name, data):
        self.parts[name] = dict(name=name, parent='sde' if name=='boot_a' else 'sda',
            partuuid=str(uuid.uuid4()), start=4096*(len(self.parts)+1), bytes=len(data))
        (self.blocks/name).write_bytes(data)
        return self.parts[name]

    def inventory(self):
        return copy.deepcopy(self.parts)

    def sealed(self, parts):
        if self.write_window:
            raise AssertionError('storage was not resealed')
        for part in parts:
            if part != self.parts[part['name']]:
                raise RuntimeError('changed partition identity')

    def digest(self, part, offset=0, length=None):
        data = (self.blocks/part['name']).read_bytes()
        return digest(data[offset:] if length is None else data[offset:offset+length])

    def read(self, part, offset, length):
        return (self.blocks/part['name']).read_bytes()[offset:offset+length]

    @contextlib.contextmanager
    def writable(self, part, allowed):
        if part['name'] not in allowed:
            raise AssertionError('partition outside action')
        gate.authorize_target(part)
        gate.mark_write()
        self.opened.append(part['name'])
        self.write_window = True
        try:
            with (self.blocks/part['name']).open('r+b') as stream:
                yield stream.fileno()
                if self.corrupt_boot_once and part['name']=='boot_a':
                    os.pwrite(stream.fileno(), b'?', 0)
                    self.corrupt_boot_once = False
        finally:
            self.write_window = False

    def write(self, fd, offset, data):
        written = os.pwrite(fd, data, offset)
        if written != len(data):
            raise AssertionError('short ordinary-file write')
        os.fsync(fd)

    @contextlib.contextmanager
    def mount_store(self, part, writable=False):
        self.sealed([part])
        if writable:
            gate.authorize_target(part)
            gate.mark_write()
            self.opened.append('linux_boot')
        yield self.store

    def verify_linux(self, part, system):
        self.verified_roots.append(part['name'])
        if self.bad_root or system['partition'] != part or system['target']['root_partuuid'] != part['partuuid']:
            raise RuntimeError('installed root binding mismatch')

    def state(self):
        return dict(report='fixture GPT', details={}, inventory=self.inventory(),
            registry=android.read_json(self.store/'registry.json'),
            files=android.files_snapshot(self.store), free_bytes=64*1024**2,
            userdata_blank=False, userdata_linux_signature=False, pending_transactions=list(self.pending))


class SwitchDeviceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.hardware = FileHardware(self.directory)
        hardware = self.hardware
        for name in ('boot_a', 'linux_boot', 'lqr_000000000001', 'lqr_000000000002',
                     'userdata', 'metadata', 'super', 'vendor_boot_a', 'dtbo_a', 'vbmeta_a',
                     'vbmeta_system_a', 'abl_a'):
            hardware.add_partition(name, (name.encode()*CAPACITY)[:CAPACITY])
        self.linux = [self.system('linux', name, index) for index, name in enumerate(
            ('lqr_000000000001', 'lqr_000000000002'), 1)]
        self.android = self.system('android', 'userdata', 3)
        self.registry = dict(schema_version=1, serial=hardware.serial,
            layout_generation=str(uuid.uuid4()), systems=self.linux+[self.android],
            default_id=self.linux[0]['id'], default_generation=self.generation(self.linux[0]))
        self.save('registry.json', self.registry)
        (hardware.store/'SHA256SUMS').write_text('old-hash  registry.json\nkeep-other-hash  systems/existing\n')
        self.old_boot = self.image(self.linux[0]).ljust(CAPACITY, b'\0')
        (hardware.blocks/'boot_a').write_bytes(self.old_boot)
        self.boot_id = str(uuid.uuid4())
        self.probe = dict(serial=hardware.serial, boot_id=self.boot_id, sealed=True, slot='a', phase='complete')
        self.agent = SimpleNamespace(PartitionOperations=lambda _:SimpleNamespace(inspect=hardware.state),
            preserve_files=android.preserve_files, read_json=android.read_json,
            atomic=android.atomic, sha=android.sha, write_sparse_boot=android.write_sparse_boot)
        self.patches = [patch.object(native, 'BOOT_CAPACITY', CAPACITY),
                        patch.object(native, 'CHUNK', 4096),
                        patch.object(liuqin_partitions, 'discover', return_value={'can_create': True}),
                        patch.object(gate, 'require', side_effect=self.require),
                        patch.object(gate, 'mark_write', side_effect=self.mark_write)]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.original_blocks = {name:(hardware.blocks/name).read_bytes()
                                for name in hardware.parts if name!='boot_a'}

    def save(self, relative, value):
        path = self.hardware.store/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gate.encoded(value))

    def generation(self, system):
        return system['generation'] if system['type']=='android' else system['target']['boot_generation']

    def image(self, system):
        return (self.hardware.store/system['image']).read_bytes()

    def system(self, kind, partition, index):
        system_id = str(uuid.uuid4())
        generation = str(uuid.uuid4())
        data = b'ANDROID!'+bytes([index])*4088
        path = f'systems/{system_id}/images/{generation}.img'
        image = self.hardware.store/path
        image.parent.mkdir(parents=True)
        image.write_bytes(data)
        system = dict(id=system_id, name=kind+' '+str(index), type=kind,
                      image=path, boot_sha256=digest(data))
        if kind=='linux':
            part = copy.deepcopy(self.hardware.parts[partition])
            system.update(partition=part, target=dict(boot_generation=generation, root_partuuid=part['partuuid']))
            self.save('transactions/linux-'+generation+'/root-installed.json', system)
            self.save('transactions/linux-'+generation+'/complete.json',
                      dict(registry_sha256='a'*64, boot_sha256='b'*64))
        else:
            system.update(rom='OTA-'+'a'*16, generation=generation, slot='a',
                          userdata=copy.deepcopy(self.hardware.parts['userdata']),
                          metadata=copy.deepcopy(self.hardware.parts['metadata']))
            hashes = {name:self.hardware.digest(self.hardware.parts[name]) for name in
                      ('super','vendor_boot_a','dtbo_a','vbmeta_a','vbmeta_system_a','abl_a')}
            self.save('transactions/android-'+generation+'/journal.json',
                      dict(mode='reinstall', transaction=generation, rom=system['rom'],
                           targets=sorted(hashes), userdata=system['userdata'], metadata=system['metadata']))
            self.save('transactions/android-'+generation+'/rom-installed.json',
                      dict(transaction=generation, images=hashes))
            self.save('transactions/android-'+generation+'/complete.json',
                      dict(status='complete', registry_sha256='a'*64, boot_sha256='b'*64))
        return system

    def require(self, token=None, *args, **kwargs):
        if self.hardware.lease is None or token not in (None, self.hardware.lease['token']):
            raise RuntimeError('invalid test lease')
        return self.hardware.lease

    def mark_write(self):
        self.hardware.lease['write_started'] = True

    def preview(self, system, *, local=False):
        request = dict(serial=self.hardware.serial, boot_id=self.boot_id,
                       system_id=system['id'], transaction=str(uuid.uuid4()))
        return native.preview_switch(request, self.hardware, self.agent, self.probe, local=local)['plan']

    def apply(self, plan, *, local=False):
        self.hardware.lease = dict(token='c'*32, scope=gate.scope(dict(kind='switch', switch_plan=plan)))
        gate.validate_scope(self.hardware.lease['scope'])
        return native.apply_switch(dict(token=self.hardware.lease['token'], plan=plan),
                                   self.hardware, self.agent, self.probe, local=local)

    def assert_other_partitions(self):
        self.assertEqual(self.original_blocks, {name:(self.hardware.blocks/name).read_bytes()
                         for name in self.original_blocks})
        self.assertLessEqual(set(self.hardware.opened), {'boot_a','linux_boot'})
        self.assertFalse(self.hardware.write_window)

    def test_linux_preview_is_readonly_and_checks_root(self):
        before = self.hardware.state()
        plan = self.preview(self.linux[1])
        self.assertEqual(self.hardware.state(), before)
        self.assertEqual(plan['target_boot_sha256'], digest(self.image(self.linux[1]).ljust(CAPACITY,b'\0')))
        self.assertEqual([p['name'] for p in plan['parts']], ['boot_a','linux_boot'])
        self.assertEqual(self.hardware.verified_roots, [self.linux[1]['partition']['name']])
        self.assertFalse(self.hardware.opened)

    def test_local_switch_keeps_a_verified_old_image_in_the_store(self):
        plan = self.preview(self.linux[1], local=True)
        self.assertEqual(plan['previous_partition'], self.linux[0]['partition']['name'])
        self.assertEqual(plan['target_partition'], self.linux[1]['partition']['name'])
        self.assertTrue(self.apply(plan, local=True)['boot_readback_verified'])
        journal = self.hardware.store/'transactions'/('switch-'+plan['transaction'])
        old = android.read_json(journal/'previous-registry.json')
        system = next(row for row in old['systems'] if row['id'] == old['default_id'])
        self.assertEqual((self.hardware.store/system['image']).read_bytes().ljust(CAPACITY,b'\0'), self.old_boot)
        self.assert_other_partitions()

    def test_local_switch_needs_a_registered_previous_boot(self):
        self.save('registry.json', dict(self.registry, default_id=None, default_generation=None))
        with self.assertRaisesRegex(RuntimeError, '登记当前默认'):
            self.preview(self.linux[1], local=True)
        self.assertFalse(self.hardware.opened)

    def test_android_linux_round_trip_commits_only_default_fields(self):
        for system in (self.android, self.linux[1], self.android):
            old_files = self.hardware.state()['files']
            plan = self.preview(system)
            result = self.apply(plan)
            self.assertTrue(result['boot_readback_verified'])
            expected = dict(self.registry, default_id=system['id'], default_generation=self.generation(system))
            self.assertEqual(android.read_json(self.hardware.store/'registry.json'), expected)
            self.assertEqual((self.hardware.blocks/'boot_a').read_bytes(),self.image(system).ljust(CAPACITY,b'\0'))
            journal = self.hardware.store/'transactions'/('switch-'+plan['transaction'])
            self.assertTrue((journal/'complete.json').is_file())
            self.assertFalse((journal/'previous-boot.img').exists())
            android.preserve_files(self.hardware.store,old_files,mutable=('registry.json','SHA256SUMS'))
            self.assert_other_partitions()

    def test_changed_rom_or_firmware_rejected_before_writes(self):
        for name in ('super','vendor_boot_a','dtbo_a','vbmeta_a','vbmeta_system_a','abl_a'):
            with self.subTest(partition=name):
                plan = self.preview(self.android)
                path = self.hardware.blocks/name
                before = path.read_bytes()
                path.write_bytes(b'?'*len(before))
                try:
                    with self.assertRaisesRegex(RuntimeError,'ROM 内容'):
                        self.apply(plan)
                    self.assertFalse(self.hardware.opened)
                finally:
                    path.write_bytes(before)

    def test_userdata_content_can_change_but_identity_cannot(self):
        path = self.hardware.blocks/'userdata'
        path.write_bytes(b'U'*CAPACITY)
        plan = self.preview(self.android)
        part = self.hardware.parts['userdata']
        original = part['partuuid']
        part['partuuid'] = str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError,'数据分区身份'):
            self.apply(plan)
        part['partuuid'] = original
        self.assertFalse(self.hardware.opened)

    def android_record(self):
        journal = self.hardware.store/'transactions'/('android-'+self.android['generation'])
        return journal, android.read_json(journal/'journal.json'), android.read_json(journal/'rom-installed.json')

    def test_generic_version_evidence_distinguishes_legacy_v1_and_v2(self):
        journal,record,installed = self.android_record()
        # The fixture is an unversioned full ROM with ABL in its write list.
        self.preview(self.android)
        installed['images'].pop('abl_a')
        record['targets'] = sorted(installed['images'])
        self.save(journal.relative_to(self.hardware.store)/'rom-installed.json',installed)
        self.save(journal.relative_to(self.hardware.store)/'journal.json',record)
        with self.assertRaisesRegex(RuntimeError,'缺少底层固件'):
            self.preview(self.android)
        record.update(generic_ota_version=2,shared_firmware=[])
        self.save(journal.relative_to(self.hardware.store)/'journal.json',record)
        self.preview(self.android)
        self.assertFalse(self.hardware.opened)

    def test_v1_shared_firmware_requires_complete_typed_live_evidence(self):
        from liuqin_ota_v1 import ESSENTIAL
        journal,record,installed = self.android_record()
        installed['images'].pop('abl_a')
        record['targets'] = sorted(installed['images'])
        shared = []
        for name in sorted(ESSENTIAL):
            name += '_a'
            if name not in self.hardware.parts:
                self.hardware.add_partition(name, b'F'*CAPACITY)
            shared.append(dict(partition=name,bytes=CAPACITY,
                               sha256=self.hardware.digest(self.hardware.parts[name])))
        record.update(generic_ota_version=1,shared_firmware=shared)
        relative = journal.relative_to(self.hardware.store)
        self.save(relative/'journal.json',record)
        self.save(relative/'rom-installed.json',installed)
        self.preview(self.android)
        image = self.hardware.blocks/'xbl_a'
        original = image.read_bytes()
        image.write_bytes(b'?'*len(original))
        with self.assertRaisesRegex(RuntimeError,'配套固件已变化'):
            self.preview(self.android)
        image.write_bytes(original)
        record['shared_firmware'] = shared[:-1]
        self.save(relative/'journal.json',record)
        with self.assertRaisesRegex(RuntimeError,'缺少必要'):
            self.preview(self.android)
        self.assertFalse(self.hardware.opened)

    def test_malformed_shared_firmware_evidence_is_rejected(self):
        journal,original,_ = self.android_record()
        entry = dict(partition='abl_a',bytes=CAPACITY,
                     sha256=self.hardware.digest(self.hardware.parts['abl_a']))
        changes = [dict(generic_ota_version=True,shared_firmware=[]),
                   dict(generic_ota_version=2,shared_firmware='invalid'),
                   dict(generic_ota_version=2,shared_firmware=[entry,entry])]
        for field,value in [('partition','persist'),('partition',[]),('bytes',0),
                            ('bytes',True),('bytes',512*1024**2+1),('sha256','bad')]:
            changes.append(dict(generic_ota_version=2,shared_firmware=[dict(entry,**{field:value})]))
        for change in changes:
            with self.subTest(change=change):
                self.save(journal.relative_to(self.hardware.store)/'journal.json',dict(original,**change))
                with self.assertRaisesRegex(RuntimeError,'记录.*无效'):
                    self.preview(self.android)
        self.assertFalse(self.hardware.opened)

    def test_malformed_or_pending_android_installation_is_rejected(self):
        generation = self.android['generation']
        base = self.hardware.store/'transactions'/('android-'+generation)
        for filename in ('journal.json','rom-installed.json','complete.json'):
            path = base/filename
            original = path.read_bytes()
            path.write_text('{}')
            try:
                with self.assertRaises((RuntimeError,ValueError)):
                    self.preview(self.android)
            finally:
                path.write_bytes(original)
        self.hardware.pending = [str(uuid.uuid4())]
        with self.assertRaisesRegex(RuntimeError,'事务状态'):
            self.preview(self.android)
        self.assertFalse(self.hardware.opened)

    def test_linux_binding_and_incomplete_commit_are_rejected(self):
        self.hardware.bad_root = True
        with self.assertRaisesRegex(RuntimeError,'binding'):
            self.preview(self.linux[1])
        self.hardware.bad_root = False
        complete = self.hardware.store/'transactions'/('linux-'+self.generation(self.linux[1]))/'complete.json'
        complete.write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'尚未提交'):
            self.preview(self.linux[1])
        self.assertFalse(self.hardware.opened)

    def test_changed_boot_registry_or_target_image_stops_before_write(self):
        plan = self.preview(self.android)
        boot = self.hardware.blocks/'boot_a'
        boot.write_bytes(b'?'*CAPACITY)
        with self.assertRaisesRegex(RuntimeError,'当前 boot_a'):
            self.apply(plan)
        boot.write_bytes(self.old_boot)
        self.save('registry.json', dict(self.registry, annotation='changed after preview'))
        with self.assertRaisesRegex(RuntimeError,'确认后'):
            self.apply(plan)
        self.save('registry.json',self.registry)
        target = self.hardware.store/self.android['image']
        target.write_bytes(b'ANDROID!'+b'Z'*4088)
        with self.assertRaisesRegex(RuntimeError,'引导镜像校验'):
            self.apply(plan)
        self.assertFalse(self.hardware.opened)

    def test_readback_failure_restores_old_boot_and_registry(self):
        plan = self.preview(self.android)
        self.hardware.corrupt_boot_once = True
        with self.assertRaisesRegex(RuntimeError,'旧 boot_a 和默认登记已恢复'):
            self.apply(plan)
        self.assertEqual((self.hardware.blocks/'boot_a').read_bytes(),self.old_boot)
        self.assertEqual(android.read_json(self.hardware.store/'registry.json'),self.registry)
        journal = self.hardware.store/'transactions'/('switch-'+plan['transaction'])
        self.assertTrue((journal/'previous-boot.img').is_file())
        self.assertTrue((journal/'rolled-back.json').is_file())
        self.assertFalse((journal/'complete.json').exists())
        self.assert_other_partitions()

    def test_registry_failure_restores_old_boot_and_registry(self):
        plan = self.preview(self.android)
        original_atomic = android.atomic
        failed = False
        def atomic(path, content):
            nonlocal failed
            if path == self.hardware.store/'registry.json' and not failed:
                failed = True
                raise OSError('simulated full store')
            return original_atomic(path,content)
        self.agent.atomic = atomic
        with self.assertRaisesRegex(RuntimeError,'旧 boot_a 和默认登记已恢复'):
            self.apply(plan)
        self.assertTrue(failed)
        self.assertEqual((self.hardware.blocks/'boot_a').read_bytes(),self.old_boot)
        self.assertEqual(android.read_json(self.hardware.store/'registry.json'),self.registry)
        self.assert_other_partitions()

    def test_scope_tampering_replay_and_generic_boot_write_are_rejected(self):
        plan = self.preview(self.android)
        self.hardware.lease = dict(token='c'*32,scope=gate.scope(dict(kind='switch',switch_plan=plan)))
        altered = dict(plan,target_boot_sha256='a'*64)
        with self.assertRaisesRegex(RuntimeError,'不匹配'):
            native.apply_switch(dict(token=self.hardware.lease['token'],plan=altered),self.hardware,self.agent,self.probe)
        with self.assertRaisesRegex(RuntimeError,'切换引导会话'):
            native.write_boot(dict(token=self.hardware.lease['token']),self.hardware)
        self.assertFalse(self.hardware.opened)
        self.apply(plan)
        with self.assertRaisesRegex(RuntimeError,'已开始执行'):
            native.apply_switch(dict(token=self.hardware.lease['token'],plan=plan),self.hardware,self.agent,self.probe)

    def test_unfinished_switch_and_insufficient_capacity_are_rejected(self):
        pending = self.hardware.store/'transactions'/('switch-'+str(uuid.uuid4()))
        pending.mkdir()
        with self.assertRaisesRegex(RuntimeError,'未结束'):
            self.preview(self.android)
        pending.rmdir()
        original = self.hardware.state
        self.hardware.state = lambda:dict(original(),free_bytes=0)
        with self.assertRaisesRegex(RuntimeError,'空间不足'):
            self.preview(self.android)
        self.assertFalse(self.hardware.opened)

    def test_fixed_recovery_override_is_exact_and_keeps_rom_firmware_checks(self):
        recovery=self.hardware.add_partition('recovery_a',b'ROM RECOVERY'.ljust(CAPACITY,b'\0'))
        journal,record,installed=self.android_record()
        installed['images']['recovery_a']=self.hardware.digest(recovery)
        record['targets']=sorted(installed['images'])
        self.save(str((journal/'journal.json').relative_to(self.hardware.store)),record)
        self.save(str((journal/'rom-installed.json').relative_to(self.hardware.store)),installed)
        (self.hardware.blocks/'recovery_a').write_bytes(b'FIXED RECOVERY'.ljust(CAPACITY,b'\0'))
        request=dict(serial=self.hardware.serial,boot_id=self.boot_id,system_id=self.android['id'],
                     transaction=str(uuid.uuid4()),fixed_recovery_sha256=self.hardware.digest(recovery))
        with self.assertRaisesRegex(RuntimeError,'ROM 内容'):self.preview(self.android)
        with patch.object(gate,'marker',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'固定 Recovery'):
                native.preview_switch(request,self.hardware,self.agent,self.probe)
        marker=dict(usage=gate.FIXED_USAGE,fixed_installer_version=1,installer_version=1,
                    inspection_only=False,installer_capabilities=gate.CAPABILITIES)
        with patch.object(gate,'marker',return_value=marker):
            forged=dict(request,fixed_recovery_sha256='0'*64)
            with self.assertRaisesRegex(RuntimeError,'ROM 内容'):
                native.preview_switch(forged,self.hardware,self.agent,self.probe)
            plan=native.preview_switch(request,self.hardware,self.agent,self.probe)['plan']
            firmware=self.hardware.blocks/'abl_a';before=firmware.read_bytes();firmware.write_bytes(b'?'*len(before))
            with self.assertRaisesRegex(RuntimeError,'ROM 内容'):
                native.preview_switch(request,self.hardware,self.agent,self.probe)
            firmware.write_bytes(before)
            self.apply(plan)
        self.assertEqual(self.hardware.digest(recovery),request['fixed_recovery_sha256'])
        self.assert_other_partitions()

    def test_local_android_switch_checks_device_and_exact_fixed_image(self):
        recovery = self.hardware.add_partition('recovery_a', b'ROM RECOVERY'.ljust(CAPACITY,b'\0'))
        journal, record, installed = self.android_record()
        installed['images']['recovery_a'] = self.hardware.digest(recovery)
        record['targets'] = sorted(installed['images'])
        self.save(str((journal/'journal.json').relative_to(self.hardware.store)), record)
        self.save(str((journal/'rom-installed.json').relative_to(self.hardware.store)), installed)
        (self.hardware.blocks/'recovery_a').write_bytes(b'FIXED RECOVERY'.ljust(CAPACITY,b'\0'))
        observed = self.hardware.digest(recovery)
        witness = dict(serial='OTHER', partition='recovery_a', image_sha256=observed)
        marker = dict(usage='temporary-boot-only', installer_version=1, inspection_only=False,
                      installer_capabilities=gate.CAPABILITIES, tablet_boot_switch_version=1,
                      fixed_recovery_witness=witness)
        with patch.object(gate,'marker',return_value=marker), patch('liuqin_recovery_preserve.CAPACITY',CAPACITY):
            with self.assertRaisesRegex(RuntimeError, 'ROM 内容'):
                self.preview(self.android, local=True)
            witness['serial'] = self.hardware.serial
            plan = self.preview(self.android, local=True)
            self.assertEqual(plan['fixed_recovery_sha256'], observed)
            (self.hardware.blocks/'recovery_a').write_bytes(b'UNKNOWN'.ljust(CAPACITY,b'\0'))
            with self.assertRaisesRegex(RuntimeError, 'ROM 内容'):
                self.apply(plan, local=True)
            self.assertFalse(self.hardware.opened)

    def test_gui_temporary_witness_is_revalidated_through_apply(self):
        recovery=self.hardware.add_partition('recovery_a',b'ROM RECOVERY'.ljust(CAPACITY,b'\0'))
        journal,record,installed=self.android_record()
        installed['images']['recovery_a']=self.hardware.digest(recovery)
        record['targets']=sorted(installed['images'])
        self.save(journal.relative_to(self.hardware.store)/'journal.json',record)
        self.save(journal.relative_to(self.hardware.store)/'rom-installed.json',installed)
        image=self.hardware.blocks/'recovery_a'
        original=b'FIXED RECOVERY'.ljust(CAPACITY,b'\0');image.write_bytes(original)
        observed=self.hardware.digest(recovery)
        witness=dict(serial=self.hardware.serial,partition='recovery_a',image_sha256=observed)
        marker=dict(usage='temporary-boot-only',installer_version=1,inspection_only=False,
                    installer_capabilities=gate.CAPABILITIES,tablet_boot_switch_version=1,
                    fixed_recovery_witness=witness)
        with patch.object(gate,'marker',return_value=marker),patch('liuqin_recovery_preserve.CAPACITY',CAPACITY):
            for key,bad in [('serial','OTHER'),('partition','recovery_b'),('image_sha256','0'*64)]:
                old=witness[key];witness[key]=bad
                with self.assertRaisesRegex(RuntimeError,'ROM 内容'):self.preview(self.android)
                witness[key]=old
            plan=self.preview(self.android)
            self.assertEqual(plan['fixed_recovery_sha256'],observed)
            with self.assertRaisesRegex(RuntimeError,'校验依据'):
                native.preview_switch(dict(plan,fixed_recovery_sha256='0'*64),self.hardware,self.agent,self.probe)
            firmware=self.hardware.blocks/'abl_a';before=firmware.read_bytes();firmware.write_bytes(b'?'*CAPACITY)
            with self.assertRaisesRegex(RuntimeError,'ROM 内容'):self.apply(plan)
            firmware.write_bytes(before)
            image.write_bytes(b'UNKNOWN'.ljust(CAPACITY,b'\0'))
            with self.assertRaisesRegex(RuntimeError,'校验依据'):self.apply(plan)
            image.write_bytes(original)
            self.assertFalse(self.hardware.opened)
            result=self.apply(plan)
            self.assertTrue(result['boot_readback_verified'])
            self.assertEqual(self.hardware.state()['registry']['default_id'],self.android['id'])
            self.assertEqual(self.hardware.digest(recovery),observed)
            self.assert_other_partitions()

    def test_sparse_previous_boot_fits_below_dense_snapshot_budget(self):
        original=self.hardware.state
        self.hardware.state=lambda:dict(original(),free_bytes=16*1024**2+4096)
        written=[]
        def save(path,chunks,size,digest):
            android.write_sparse_boot(path,chunks,size,digest)
            self.assertEqual(path.stat().st_size,CAPACITY)
            self.assertLessEqual(path.stat().st_blocks*512,4096)
            written.append(path.name)
        self.agent.write_sparse_boot=save
        self.apply(self.preview(self.linux[1]))
        self.assertEqual(written,['previous-boot.img'])
        self.assert_other_partitions()


if __name__=='__main__':
    unittest.main(verbosity=2)
