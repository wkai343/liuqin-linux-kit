#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real updater/binding/store transactions against ordinary files only."""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import struct
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from kernel_fixtures import PROJECT, kernel_fixture, install_fixture, put
from liuqin_gui.demo import demo_image
from liuqin_boot_target import bind, encode, parse, current_helper, HELPER_PATH
from liuqin_artifacts import boot_interface
from liuqin_boot_retention import encoded, build as retention_plan
from liuqin_kernel import digest
from liuqin_partition_names import root_name
import liuqin_boot_update_store as store_module

spec = importlib.util.spec_from_file_location('kernel_tests', PROJECT/'tests/kernel-updates.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
updater = fixture.updater


class StoreDevice(fixture.FileDevice):
    def __init__(self, path, release, store):
        super().__init__(path, release)
        self.store_path = store; self.store_ro = True; self.store_writes = 0

    @contextlib.contextmanager
    def mount_store(self, identity, writable=False):
        assert identity == self.identity() and self.store_ro
        if writable: self.store_ro = False; self.store_writes += 1
        try: yield self.store_path
        finally: self.store_ro = True


class StoreUpdateTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.root = self.base/'root'; self.root.mkdir()
        self.store = self.base/'store'; self.store.mkdir()
        self.old, self.new = '7.3.3-liuqin-test', '7.3.4-liuqin-test'
        self.sources = {}
        for release in [self.old, self.new]:
            data = kernel_fixture(self.base/release, release)
            image = demo_image({'etc/liuqin-storage-mode': b'persistent\n',
                'etc/liuqin-root-profile': b'native-updatable\n', 'etc/liuqin-native-root.contract': data[3],
                'etc/liuqin-filesystems': b'ext4\nbtrfs\n', 'etc/liuqin-multiboot': b'1\n',
                HELPER_PATH: current_helper()}, (data[0]/'arch/arm64/boot/Image').read_bytes())
            data[2].write_bytes(image); install_fixture(self.root, release, data)
            self.sources[release] = data[2]
        self.target = dict(schema_version=1, os_id=str(uuid.uuid4()), root_partuuid=str(uuid.uuid4()),
            root_fsuuid=str(uuid.uuid4()), root_fstype='ext4', root_path='native-root', boot_generation=str(uuid.uuid4()))
        self.part = dict(name=root_name(self.target['os_id']), partuuid=self.target['root_partuuid'],
            fsuuid=self.target['root_fsuuid'], filesystem='ext4', start=4096*1024, bytes=128*1024**3)
        self.storepart = dict(name='linux_boot', partuuid=str(uuid.uuid4()), fsuuid=str(uuid.uuid4()), start=4096*64, bytes=2*1024**3)
        self.system = dict(id=self.target['os_id'], name='Fedora', type='linux', target=self.target,
            partition=self.part, image=f"systems/{self.target['os_id']}/images/{self.target['boot_generation']}.img",
            kernel_release=self.old, source_boot_sha256=digest(self.sources[self.old]), status='installed-unverified')
        path = self.store/self.system['image']; path.parent.mkdir(parents=True)
        self.system['boot_sha256'] = bind(self.sources[self.old], path, self.target)
        otherid, othergen = str(uuid.uuid4()), str(uuid.uuid4())
        otherimage = f'systems/{otherid}/images/{othergen}.img'
        put(self.store/otherimage, path.read_bytes())
        self.other = dict(id=otherid, type='android', generation=othergen, image=otherimage, boot_sha256=digest(path), name='Android')
        self.registry = dict(schema_version=1, serial='SIMULATED', default_id=self.system['id'],
            default_generation=self.target['boot_generation'], systems=[self.other, self.system],
            partition_layout=[self.storepart, self.part],
            rootfs_slots=[dict(id=self.system['id'], target=self.target, partition=self.part)])
        put(self.store/'registry.json', encoded(self.registry))
        put(self.store/f"systems/{self.system['id']}/manifest.json", encoded(self.system))
        put(self.store/'SHA256SUMS', b'')
        journal=self.store/'transactions'/('linux-'+self.target['boot_generation'])
        put(journal/'root-installed.json', encoded(self.system))
        put(journal/'complete.json', encoded(dict(registry_sha256='a'*64, boot_sha256='b'*64)))
        put(self.root/'etc/liuqin-boot-target', encode(self.target))
        put(self.root/'etc/liuqin-system-id', (self.system['id']+'\n').encode())
        self.block = self.base/'boot_a'; self.block.write_bytes(path.read_bytes().ljust(65536,b'\0'))
        self.original = self.block.read_bytes()
        self.device = StoreDevice(self.block, self.old, self.store)
        self.device.details.update(system_id=self.system['id'], filesystem='ext4', fsuuid=self.target['root_fsuuid'],
            root=dict(name=self.part['name'], uuid=self.part['partuuid'], start=self.part['start']//512, size=self.part['bytes']),
            store=dict(uuid=self.storepart['partuuid'], start=self.storepart['start']//512, size=self.storepart['bytes'], fsuuid=self.storepart['fsuuid']))
        self.update = updater.Updater(self.root, self.device)
        self.update.enable()
        self.before = self.snapshot()

    def snapshot(self):
        return {p.relative_to(self.store).as_posix():p.read_bytes() for p in self.store.rglob('*') if p.is_file()}

    def assert_restored(self):
        self.assertEqual(self.block.read_bytes(), self.original)
        self.assertEqual((self.root/'etc/liuqin-boot-target').read_bytes(), encode(self.target))
        for name, data in self.before.items(): self.assertEqual((self.store/name).read_bytes(),data,name)
        self.assertTrue(self.device.ro and self.device.store_ro)

    def test_plan_binds_candidate_without_writing_boot_or_store(self):
        p=self.update.plan(self.new)
        self.assertEqual(p['system_id'], self.system['id'])
        self.assertEqual(p['writes'], ['boot_a','linux_boot','/etc/liuqin-boot-target'])
        self.assertNotEqual(p['generation'], self.target['boot_generation'])
        self.assertEqual(self.device.writes,0); self.assertEqual(self.device.store_writes,0)
        self.assertEqual(self.snapshot(), self.before)

    def test_update_confirm_and_rollback_are_one_consistent_transaction(self):
        record=self.update.apply(self.new)
        self.assertEqual(record['state'],'verified')
        reg=json.loads((self.store/'registry.json').read_text())
        self.assertEqual(reg['systems'][0],self.other)
        current=reg['systems'][1]
        self.assertEqual(boot_interface(self.block)['boot_target'],current['target'])
        self.assertEqual(parse((self.root/'etc/liuqin-boot-target').read_bytes()),current['target'])
        self.assertEqual(reg['rootfs_slots'][0]['target'],current['target'])
        self.assertTrue((self.store/self.system['image']).exists())
        with self.assertRaisesRegex(RuntimeError,'尚未启动'):self.update.confirm()
        self.device.release=self.new
        self.assertEqual(self.update.confirm()['state'],'confirmed')
        self.update.rollback(); self.assert_restored()

    def test_retention_recognizes_the_previous_kernel(self):
        record=self.update.apply(self.new); store=self.update.store(self.device.identity())
        reg=json.loads((self.store/'registry.json').read_text())
        rows=[dict(name=p.name,kind='linux',status='complete') for p in (self.store/'transactions').iterdir()]
        result=retention_plan(self.store,dict(registry=reg,files=store.files(self.store)),rows,
            lambda p:json.loads(p.read_text()),store.generation,
            lambda p,s:updater.full_hash(p,65536) if digest(p)==s else self.fail('image corrupt'))
        self.assertEqual({r['role'] for r in result['kept'] if r['id']==self.system['id']},{'current','previous-kernel'})
        self.assertIn('kernel-updates/'+record['store_update']['generation']+'/plan.json',result['protected'])

    def test_repeated_apply_preserves_backup_and_does_not_rebind(self):
        r=self.update.apply(self.new); before=self.snapshot(); counts=(self.device.writes,self.device.store_writes)
        self.update.apply(self.new)
        self.assertEqual(self.snapshot(),before);self.assertEqual((self.device.writes,self.device.store_writes),counts)
        self.assertEqual((self.update.state/r['transaction']/'previous.img').read_bytes(),self.original)

    def test_reinstall_after_confirmation_keeps_previous_kernel_and_generation(self):
        self.update.apply(self.new);self.device.release=self.new;record=self.update.confirm()
        before=self.snapshot();counts=(self.device.writes,self.device.store_writes)
        plan=self.update.plan(self.new)
        self.assertEqual(plan['writes'],[])
        self.assertEqual(plan['old_sha256'],plan['new_sha256'])
        self.assertEqual(plan['generation'],record['store_update']['generation'])
        self.assertEqual(self.update.apply(self.new),dict(state='unchanged',release=self.new))
        self.assertEqual(self.update.current(),record)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual((self.device.writes,self.device.store_writes),counts)
        with self.assertRaisesRegex(RuntimeError,'不能移除'):self.update.can_remove(self.old)
        self.update.rollback();self.assert_restored()

    def test_boot_write_corruption_restores_registry_and_binding(self):
        self.device.failure='corrupt'
        with self.assertRaisesRegex(RuntimeError,'回读'):self.update.apply(self.new)
        self.assertEqual(self.update.current()['state'],'rolled-back');self.assert_restored()

    def test_interruption_at_each_metadata_commit_point_recovers(self):
        for point in ['liuqin-boot-target','manifest.json','registry.json','root-installed.json','complete.json']:
            with self.subTest(point=point):
                original=store_module.atomic; tripped=False
                def fail(path,data):
                    nonlocal tripped
                    original(path,data)
                    # stage's previous-* materials do not match these names.
                    if path.name==point and not tripped:
                        tripped=True;raise InterruptedError('simulated commit power loss')
                with patch.object(store_module,'atomic',side_effect=fail), patch.object(self.update,'restore',side_effect=OSError('power lost')):
                    with self.assertRaises(InterruptedError):self.update.apply(self.new)
                self.assertEqual(self.update.current()['state'],'rollback-failed')
                self.update.rollback();self.assert_restored()

    def test_partial_boot_recovery_uses_original_backup(self):
        self.device.failure='interrupt'
        with patch.object(self.update,'restore',side_effect=OSError('power lost')):
            with self.assertRaises(InterruptedError):self.update.apply(self.new)
        before=self.device.writes
        with self.assertRaisesRegex(RuntimeError,'中断'):self.update.apply(self.new)
        self.assertEqual(self.device.writes,before)
        self.update.rollback();self.assert_restored()

    def test_explicit_rollback_interruption_has_a_durable_recovery_state(self):
        self.update.apply(self.new);self.device.release=self.new;self.update.confirm()
        original=self.device.write
        def checked_write(*args):
            self.assertEqual(self.update.current()['state'],'restoring')
            return original(*args)
        self.device.failure='interrupt'
        with patch.object(self.device,'write',side_effect=checked_write):
            with self.assertRaises(InterruptedError):self.update.rollback()
        self.assertEqual(self.update.current()['state'],'rollback-failed')
        with self.assertRaisesRegex(RuntimeError,'中断'):self.update.apply(self.new)
        self.update.rollback();self.assert_restored()

    def test_other_default_cannot_be_overwritten(self):
        reg=copy.deepcopy(self.registry);reg['default_id']=self.other['id'];reg['default_generation']=self.other['generation']
        put(self.store/'registry.json',encoded(reg))
        for action in [self.update.enable,lambda:self.update.plan(self.new),lambda:self.update.apply(self.new)]:
            with self.assertRaisesRegex(RuntimeError,'默认 Linux'):action()
        self.assertEqual(self.device.writes,0);self.assertEqual(self.device.store_writes,0)

    def test_unrelated_registry_change_blocks_rollback_before_boot_write(self):
        self.update.apply(self.new);reg=json.loads((self.store/'registry.json').read_text())
        reg['systems'][0]['name']='Changed elsewhere';put(self.store/'registry.json',encoded(reg))
        writes=self.device.writes
        with self.assertRaisesRegex(RuntimeError,'其他修改'):self.update.rollback()
        self.assertEqual(self.device.writes,writes)

    def test_unrelated_boot_write_is_not_treated_as_an_interrupted_transaction(self):
        self.update.apply(self.new);self.block.write_bytes(b'foreign boot'.ljust(65536,b'\0'))
        writes=self.device.writes
        with self.assertRaisesRegex(RuntimeError,'其他操作修改'):self.update.rollback()
        self.assertEqual(self.device.writes,writes)

    def test_corrupt_installation_receipt_stops_before_write(self):
        journal=self.store/'transactions'/('linux-'+self.target['boot_generation'])
        put(journal/'complete.json',b'{}')
        with self.assertRaisesRegex(RuntimeError,'安装事务'):self.update.apply(self.new)
        self.assertEqual(self.device.writes,0);self.assertEqual(self.device.store_writes,0)

    def test_store_filesystem_identity_change_requires_new_enable(self):
        self.device.details['store']['fsuuid']=str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError,'不匹配'):self.update.apply(self.new)
        self.assertEqual(self.device.writes,0)

    def test_already_bound_candidate_is_rejected(self):
        path=self.root/f'usr/lib/liuqin/boot-images/{self.new}/boot.img'
        other=self.base/'bound.img';bind(path,other,self.target);path.write_bytes(other.read_bytes())
        with self.assertRaisesRegex(ValueError,'已绑定'):self.update.apply(self.new)
        self.assertEqual(self.device.writes,0);self.assertEqual(self.device.store_writes,0)

    def test_bad_store_hash_or_symlink_is_rejected(self):
        path=self.store/self.system['image']; original=path.read_bytes();path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError,'校验失败'):self.update.plan(self.new)
        path.unlink();alias=self.base/'alias';alias.write_bytes(original);path.symlink_to(alias)
        with self.assertRaisesRegex(RuntimeError,'链接'):self.update.plan(self.new)
        self.assertEqual(self.device.writes,0);self.assertEqual(self.device.store_writes,0)

    def test_nonempty_sums_are_updated_and_restored_exactly(self):
        names=['registry.json',f"systems/{self.system['id']}/manifest.json",self.system['image']]
        put(self.store/'SHA256SUMS',''.join(digest(self.store/n)+'  '+n+'\n' for n in names).encode())
        self.before=self.snapshot();self.update.apply(self.new)
        for line in (self.store/'SHA256SUMS').read_text().splitlines():
            value,name=line.split('  ',1);self.assertEqual(digest(self.store/name),value)
        self.update.rollback();self.assert_restored()

    def test_removal_protection_survives_missing_local_update_record(self):
        self.update.apply(self.new);self.device.release=self.new
        (self.update.state/'current.json').unlink()
        for release in [self.new,self.old]:
            with self.assertRaisesRegex(RuntimeError,'不能移除'):self.update.can_remove(release)

    def test_shared_lock_accepts_root_sticky_runtime_but_rejects_unsafe_lock(self):
        runtime=self.root/'run';runtime.chmod(0o1777)
        self.update.plan(self.new)
        lock=runtime/'liuqin-android-operation.lock';lock.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError,'锁权限'):self.update.plan(self.new)
        self.assertEqual(self.device.writes,0)

    def test_foreign_symlink_does_not_create_directories_outside_store(self):
        outside=self.base/'outside';outside.mkdir();(self.store/'escape').symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError,'链接'):
            store_module.atomic(self.store/'escape'/'new'/'record',b'bad')
        self.assertEqual(list(outside.iterdir()),[])


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.real_path=Path
        self.target=dict(schema_version=1,os_id=str(uuid.uuid4()),root_partuuid=str(uuid.uuid4()),
            root_fsuuid=str(uuid.uuid4()),root_fstype='ext4',root_path='native-root',boot_generation=str(uuid.uuid4()))
        self.controller=self.base/'sys/devices/platform/soc@0/1d84000.ufshc';self.controller.mkdir(parents=True)
        driver=self.base/'sys/bus/platform/drivers/ufshcd-qcom';driver.mkdir(parents=True)
        (self.controller/'driver').symlink_to(driver)
        put(self.base/'sys/firmware/devicetree/base/compatible',b'qcom,cape-mtp\0qcom,cape\0qcom,mtp\0')
        put(self.base/'sys/firmware/devicetree/base/model',b'Liuqin based on Qualcomm Technologies, Inc SM8475\0')
        put(self.base/'proc/cmdline',b'androidboot.serialno=SIMULATED androidboot.slot_suffix=_a\n')
        (self.base/'proc/1').mkdir();(self.base/'proc/1/root').symlink_to(self.base)
        put(self.base/'etc/liuqin-system-id',(self.target['os_id']+'\n').encode())
        put(self.base/'etc/liuqin-boot-target',encode(self.target))
        self.partitions={}
        for name,dev in [('sda','8:0'),('sde','8:64')]:
            parent=self.controller/'host0'/name;parent.mkdir(parents=True)
            self.node(name,parent,dev,'',None,0,0)
        self.node('sda37',self.controller/'host0/sda/sda37','259:21',root_name(self.target['os_id']),self.target['root_partuuid'],8192,128*1024**3)
        self.node('sda36',self.controller/'host0/sda/sda36','259:20','linux_boot',str(uuid.uuid4()),4096,2*1024**3)
        self.node('sde14',self.controller/'host0/sde/sde14','8:78','boot_a',str(uuid.uuid4()),600112,192*1024**2)
        put(self.base/'proc/self/mountinfo',b'20 1 259:21 /native-root / rw - ext4 /dev/sda37 rw\n')
        self.device=updater.Device()
        def mapped(p='.'):
            p=str(p)
            if p.startswith('/') and not p.startswith(str(self.base)):
                return self.base/p.lstrip('/')
            return self.real_path(p)
        self.mapping=patch.object(updater,'Path',side_effect=mapped);self.mapping.start();self.addCleanup(self.mapping.stop)
        self.arch=patch.object(updater.os,'uname',return_value=type('Hardware',(),{'machine':'aarch64'})())
        self.arch.start();self.addCleanup(self.arch.stop)
        self.ids=patch.object(self.device,'blkid',side_effect=lambda p,f:
            'ext4' if f=='TYPE' else self.target['root_fsuuid'] if p['node']=='sda37' else '7b2e327e-09a7-513f-87fc-f6f06498c2b0')
        self.ids.start();self.addCleanup(self.ids.stop)

    def node(self,name,path,dev,label,partuuid,start,size):
        path.mkdir(parents=True,exist_ok=True)
        link=self.base/'sys/class/block'/name;link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to(path)
        for key,value in dict(dev=dev,uevent=f'PARTNAME={label}\nPARTUUID={partuuid}' if label else '',
                              start=start,size=size//512,ro=0 if name in ['sda','sda37'] else 1).items():
            put(path/key,(str(value)+'\n').encode())
        put(self.base/'dev'/name,b'fixture only')

    def test_actual_board_identity_and_generation_independent_binding(self):
        before=self.device.identity()
        self.assertEqual(before['root']['node'],'sda37');self.assertEqual(before['store']['node'],'sda36')
        self.target['boot_generation']=str(uuid.uuid4());put(self.base/'etc/liuqin-boot-target',encode(self.target))
        self.assertEqual(self.device.identity(),before)

    def test_wrong_mount_uuid_board_and_unsealed_partition_are_rejected(self):
        cases=[('proc/self/mountinfo',b'20 1 259:20 /native-root / rw - ext4 /dev/sda36 rw\n'),
               ('sys/firmware/devicetree/base/model',b'Another Cape device\0'),
               ('sys/class/block/sda36/ro',b'0\n'),('etc/liuqin-system-id',(str(uuid.uuid4())+'\n').encode())]
        for name,data in cases:
            p=self.base/name;old=p.read_bytes();p.write_bytes(data)
            with self.subTest(name=name),self.assertRaises(RuntimeError):self.device.identity()
            p.write_bytes(old)

    def test_btrfs_virtual_device_number_requires_a_single_bound_device(self):
        self.target['root_fstype']='btrfs';put(self.base/'etc/liuqin-boot-target',encode(self.target))
        put(self.base/'proc/self/mountinfo',b'20 1 0:42 /native-root / rw - btrfs /dev/sda37 rw\n')
        devices=self.base/'sys/fs/btrfs'/self.target['root_fsuuid']/'devices';devices.mkdir(parents=True)
        (devices/'sda37').symlink_to(self.base/'sys/class/block/sda37')
        self.assertEqual(self.device.identity()['filesystem'],'btrfs')
        (devices/'sde14').symlink_to(self.base/'sys/class/block/sde14')
        with self.assertRaisesRegex(RuntimeError,'单一'):self.device.identity()

    def test_userdata_with_unbound_templates_uses_legacy_identity(self):
        p=self.base/'sys/class/block/sda37/uevent'
        p.write_text(p.read_text().replace(root_name(self.target['os_id']),'userdata'))
        for name in ['liuqin-boot-target','liuqin-system-id']:put(self.base/'etc'/name,b'unbound\n')
        self.assertNotIn('store',self.device.identity())

    def test_chroot_and_other_slot_are_refused(self):
        p=self.base/'proc/1/root';p.unlink();other=self.base/'other-root';other.mkdir();p.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError,'chroot'):self.device.identity()
        p.unlink();p.symlink_to(self.base)
        put(self.base/'proc/cmdline',b'androidboot.serialno=SIMULATED androidboot.slot_suffix=_b\n')
        with self.assertRaisesRegex(RuntimeError,'A 槽'):self.device.identity()


class StoreMountTests(unittest.TestCase):
    def test_umount_failure_preserves_mounted_files_and_restores_readonly(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);mount=base/'mount';mount.mkdir();control=base/'block';control.touch()
            identity=dict(store=dict(node='sda36',dev='8:999',size=65536))
            device=updater.Device();flags=[]
            @contextlib.contextmanager
            def node(*args,**kwargs):
                fd=os.open(control,os.O_RDONLY)
                try:yield fd
                finally:os.close(fd)
            def command(args,**kwargs):
                if args[0]=='mount':put(mount/'precious-image',b'never delete this')
                else:raise subprocess.CalledProcessError(1,args)
            with patch.object(device,'identity',return_value=identity),patch.object(device,'node',side_effect=node), \
                 patch.object(updater,'ro',return_value=1),patch.object(updater,'set_ro',side_effect=lambda fd,value:flags.append(value)), \
                 patch.object(updater.fcntl,'ioctl',return_value=struct.pack('Q',65536)), \
                 patch.object(updater.tempfile,'mkdtemp',return_value=str(mount)),patch.object(updater.subprocess,'run',side_effect=command):
                with self.assertRaises(subprocess.CalledProcessError):
                    with device.mount_store(identity,writable=True):pass
            self.assertEqual(flags,[0,1]);self.assertEqual((mount/'precious-image').read_bytes(),b'never delete this')


if __name__=='__main__':unittest.main(verbosity=2)
