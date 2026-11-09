#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute Android operations against regular files, including real mkfs tools."""
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine,cli
from liuqin_gui.android_demo import initialize
from liuqin_android import canonical,image_plan,next_registry,ANDROID_PARTITIONS,FORMAT_PARTITIONS
spec=importlib.util.spec_from_file_location('android_device',PROJECT/'device/installer/android-device.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class FileHardware:
    serial='DEMO-LIUQIN'
    def __init__(self,root,engine):
        self.root=root;self.work=root/'work';self.work.mkdir();self.store=root/'store';self.store.mkdir()
        self.parts=copy.deepcopy(engine.demo_android_scan['inventory']);self.flags={name:1 for name in self.parts}
        self.files={};self.writes=[];self.fd_names={};self.failure=False
        for name,part in self.parts.items():
            path=root/(name+'.img');self.files[name]=path
            with path.open('wb') as f:
                if name=='userdata':f.truncate(512*1024**2);f.write(b'ANDROID ENCRYPTED DATA')
                elif name=='metadata':f.truncate(64*1024**2)
                else:f.write(('DO NOT CHANGE '+name).encode().ljust(65536,b'!'))
        for filename,entry in engine.demo_android_profile['images'].items():
            if entry['partition']=='boot_a':continue
            part=self.parts[entry['partition']];data=(engine.demo_android_rom/filename).read_bytes()
            self.files[entry['partition']].write_bytes(data+bytes(part['bytes']-len(data)))
        self.files['boot_a'].write_bytes(b'DEMO CURRENT BOOT'.ljust(16384,b'\0'))
        registry=engine.demo_android_scan['registry']
        (self.store/'registry.json').write_bytes(canonical(registry))
        (self.store/'SHA256SUMS').write_text(hashlib.sha256(canonical(registry)).hexdigest()+'  registry.json\n')
        for system in registry['systems']:
            path=self.store/system['image'];path.parent.mkdir(parents=True)
            path.write_bytes((engine.demo_android_rom/'boot.img').read_bytes() if system['type']=='android' else ('DEMO LINUX '+system['id']).encode())
        (self.store/'extra').mkdir();(self.store/'extra/preserve.txt').write_text('existing user store file')

    def inventory(self):return copy.deepcopy(self.parts)
    def sealed(self,parts):
        for part in parts:
            if self.flags[part['name']]!=1:raise RuntimeError('unsealed '+part['name'])
    def read(self,part,offset,length):
        with self.files[part['name']].open('rb') as f:f.seek(offset);return f.read(length)
    def digest(self,part,offset=0,length=None):
        h=hashlib.sha256()
        with self.files[part['name']].open('rb') as f:
            f.seek(offset)
            if length is None:
                while data:=f.read(4*1024**2):h.update(data)
            else:
                while length:
                    data=f.read(min(length,4*1024**2))
                    if not data:raise RuntimeError('fixture truncated')
                    h.update(data);length-=len(data)
        return h.hexdigest()
    @contextlib.contextmanager
    def writable(self,part,allowed):
        name=part['name']
        if name not in allowed:raise RuntimeError('not allowed')
        assert self.flags[name]==1
        self.flags[name]=0;self.writes.append(name)
        fd=os.open(self.files[name],os.O_RDWR);self.fd_names[fd]=name
        try:yield fd
        finally:os.close(fd);self.fd_names.pop(fd);self.flags[name]=1
    def write(self,fd,offset,data):
        os.pwrite(fd,data,offset)
        if self.failure:raise RuntimeError('injected write interruption')
        os.fsync(fd)
    @contextlib.contextmanager
    def mount_store(self,part,writable=False):
        assert self.flags['linux_boot']==1
        if writable:self.writes.append('linux_boot:append');self.flags['linux_boot']=0
        try:yield self.store
        finally:self.flags['linux_boot']=1
    def check_format_tools(self):
        for name in ('mkfs.f2fs','mkfs.ext4'):
            if not shutil.which(name): raise RuntimeError('missing test format tool')

    def format(self,part,settings):
        if part['name'] not in FORMAT_PARTITIONS:raise RuntimeError('bad formatter target')
        actual_run = module.subprocess.run
        def run(arguments, **kwargs):
            arguments = [shutil.which(Path(arguments[0]).name) or arguments[0], *arguments[1:]]
            return actual_run(arguments, **kwargs)
        with patch.object(module.subprocess, 'run', side_effect=run):
            module.Hardware.format(self,part,settings)


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.engine=DemoEngine(lambda _event:None,self.root/'host');initialize(self.engine,cli.layout);self.addCleanup(self.engine.close)
        self.profile=self.engine.demo_android_profile;self.device=FileHardware(self.root,self.engine)
        self.ops=module.Operations(self.device,self.profile)
        self.scan=self.ops.inspect('preserve');self.initial=module.files_snapshot(self.device.store)
        self.protected={name:self.device.digest(part) for name,part in self.device.parts.items() if name not in ANDROID_PARTITIONS|FORMAT_PARTITIONS|{'boot_a'}}
        self.boot_before=self.device.digest(self.device.parts['boot_a'])

    def plan(self,mode='reinstall'):
        generation=str(uuid.uuid4());old=self.scan['registry'];android=next(s for s in old['systems'] if s['type']=='android')
        item=copy.deepcopy(android);item.update(generation=generation,image='systems/'+android['id']+'/images/'+generation+'.img')
        new=next_registry(old,item,generation,mode,False)
        images=[]
        if mode=='reinstall':
            for name in self.profile['install_images']:
                entry=self.profile['images'][name]
                images.append(image_plan(self.engine.demo_android_rom/name,self.device.parts[entry['partition']],entry))
        return dict(schema_version=1,mode=mode,set_default=False,transaction=generation,android_id=android['id'],
            registry=old,registry_sha256=self.scan['registry_sha256'],store_files=self.scan['files'],
            candidate_registry=copy.deepcopy(new),candidate_registry_sha256=hashlib.sha256(canonical(new)).hexdigest(),
            inventory=self.device.inventory(),previous_boot_sha256=self.boot_before,images=images),new

    def fetch(self,url,length,digest):
        if url.endswith('/boot'):data=(self.engine.demo_android_rom/'boot.img').read_bytes()
        else:
            name,index=url.split('/')[-2:];image=next(i for i in self.current['images'] if i['partition']['name']==name);chunk=image['chunks'][int(index)]
            filename=next(k for k,v in self.profile['images'].items() if v['partition']==name)
            raw=(self.engine.demo_android_rom/filename).read_bytes()
            data=(raw+bytes(image['partition']['bytes']-len(raw)))[chunk['offset']:chunk['offset']+length]
        assert len(data)==length and hashlib.sha256(data).hexdigest()==digest
        return data

    def assert_protected(self):
        for name,digest in self.protected.items():self.assertEqual(self.device.digest(self.device.parts[name]),digest,name)
        self.assertEqual(self.device.digest(self.device.parts['boot_a']),self.boot_before)
        module.preserve_files(self.device.store,self.initial,mutable=('registry.json','SHA256SUMS'))
        self.assertTrue(all(value==1 for value in self.device.flags.values()))

    def test_reinstall_real_filesystems_preserves_linux_and_store(self):
        self.current,new=self.plan()
        with patch.object(module,'fetch',side_effect=self.fetch):
            self.ops.stage(self.current,'http://fixture');self.ops.install(self.current,'http://fixture')
        self.ops.commit(self.current,new,self.boot_before)
        self.assert_protected()
        self.assertEqual(set(self.device.writes)-{'linux_boot:append'},ANDROID_PARTITIONS|FORMAT_PARTITIONS)
        self.assertEqual(self.device.read(self.device.parts['userdata'],1024,4),b'\x10\x20\xf5\xf2')
        self.assertEqual(self.device.read(self.device.parts['metadata'],1080,2),b'\x53\xef')

    def test_sparse_boot_store_preserves_trailing_footer_and_full_snapshot_hash(self):
        from liuqin_android import BOOT_STORE_CHUNK, sparse_boot_bytes, sparse_boot_file_bytes

        chunks = (b'ANDROID!'.ljust(BOOT_STORE_CHUNK, b'\0'), bytes(BOOT_STORE_CHUNK),
                  b'AVBf'.rjust(BOOT_STORE_CHUNK, b'\0'))
        image = self.root/'sparse-boot.img'
        logical = b''.join(chunks)
        expected_sha = hashlib.sha256(logical).hexdigest()
        module.write_sparse_boot(image, iter(chunks), len(logical), expected_sha)
        self.assertEqual(image.read_bytes(), logical)
        self.assertEqual(sparse_boot_bytes(chunks), BOOT_STORE_CHUNK + 4096)
        self.assertEqual(sparse_boot_file_bytes(image), BOOT_STORE_CHUNK + 4096)

    def test_generic_v1_keeps_preserved_firmware_evidence_for_later_switches(self):
        self.profile['generic_ota_version']=1
        self.profile['shared_firmware']=[]
        self.current,_=self.plan()
        requirements=[
            dict(partition='abl_a',bytes=4096,sha256='a'*64),
            dict(partition='dtbo_a',bytes=4096,sha256='b'*64),
        ]
        with patch('liuqin_ota.require_device'),patch('liuqin_rom_admission.require_install'), \
             patch('liuqin_ota.requirements',return_value=requirements),patch.object(module,'fetch',side_effect=self.fetch):
            self.ops.stage(self.current,'http://fixture')
        journal=self.device.store/'transactions'/('android-'+self.current['transaction'])/'journal.json'
        record=module.read_json(journal)
        self.assertEqual(record['generic_ota_version'],1)
        self.assertEqual(record['shared_firmware'],requirements[:1])

    def test_full_rom_skips_only_verified_identical_partition_content(self):
        from liuqin_native_install import identity

        self.profile.update(generic_ota_version=2,shared_firmware=[])
        self.current,new=self.plan()
        images={image['partition']['name']:image for image in self.current['images']}
        backups={name:dict(identity=identity(image['partition']),sha256=image['whole_sha256'])
                 for name,image in images.items()}
        lease=dict(scope=dict(full_rom=True,rom_backups=backups))
        changed='vbmeta_system_a'
        with self.device.files[changed].open('r+b') as stream:
            stream.seek(-1,os.SEEK_END);stream.write(b'X')
        fetched=[]

        def fetch(url,length,digest):
            fetched.append(url)
            return self.fetch(url,length,digest)

        with patch('liuqin_ota.require_device'),patch('liuqin_rom_admission.require_install'), \
             patch('liuqin_ota.write_names',return_value=set(images)), \
             patch('liuqin_native_install.require',return_value=lease), \
             patch.object(module,'fetch',side_effect=fetch):
            self.ops.stage(self.current,'http://fixture')
            result=self.ops.install(self.current,'http://fixture')
            self.ops.commit(self.current,new,self.boot_before)

        self.assertTrue(result['rom_written'] and result['android_data_initialized'])
        self.assertEqual(set(self.device.writes)-{'linux_boot:append'},{changed}|FORMAT_PARTITIONS)
        self.assertEqual({url.split('/')[-2] for url in fetched if '/images/' in url},{changed})
        journal=self.device.store/'transactions'/('android-'+self.current['transaction'])
        self.assertEqual(module.read_json(journal/'rom-installed.json')['images'],
                         {name:image['whole_sha256'] for name,image in images.items()})
        self.assert_protected()

    def test_full_rom_skip_requires_approved_source_and_zero_padding(self):
        from liuqin_native_install import identity

        plan,_=self.plan()
        image=next(image for image in plan['images'] if image['partition']['name']=='vbmeta_system_a')
        part=image['partition']
        self.assertGreater(part['bytes'],image['source_bytes'])
        backup=dict(identity=identity(part),sha256=image['whole_sha256'])
        self.assertTrue(self.ops.matches_full_rom_image(part,image,backup))
        self.assertFalse(self.ops.matches_full_rom_image(part,image,dict(backup,identity={})))
        for offset in (0,part['bytes']-1):
            with self.subTest(offset=offset), self.device.files[part['name']].open('r+b') as stream:
                stream.seek(offset);original=stream.read(1)
                stream.seek(offset);stream.write(b'X');stream.flush()
                wrong=self.device.digest(part)
                self.assertFalse(self.ops.matches_full_rom_image(part,dict(image,whole_sha256=wrong),
                                                                dict(backup,sha256=wrong)))
                stream.seek(offset);stream.write(original)

    def test_preserve_never_calls_block_writer_or_formatter(self):
        self.current,new=self.plan('preserve')
        before={name:self.device.digest(self.device.parts[name]) for name in FORMAT_PARTITIONS}
        with patch.object(module,'fetch',side_effect=self.fetch):self.ops.stage(self.current,'http://fixture')
        with self.assertRaises(RuntimeError):self.ops.install(self.current,'http://fixture')
        self.ops.commit(self.current,new,self.boot_before);self.assert_protected()
        self.assertEqual(set(self.device.writes),{'linux_boot:append'})
        self.assertEqual(before,{name:self.device.digest(self.device.parts[name]) for name in FORMAT_PARTITIONS})

    def test_linux_or_store_cannot_be_in_image_allowlist(self):
        self.current,_new=self.plan()
        for name in ('linux_boot','lqr_111111111111','userdata','boot_b','persist'):
            candidate=copy.deepcopy(self.current);candidate['images'][0]['partition']=self.device.parts[name]
            with self.subTest(name=name),self.assertRaises(ValueError):self.ops.install(candidate,'http://fixture')
        self.assertFalse(self.device.writes);self.assert_protected()

    def test_interruption_reseals_and_keeps_linux_default(self):
        self.current,_new=self.plan();self.device.failure=True
        with patch.object(module,'fetch',side_effect=self.fetch):
            self.ops.stage(self.current,'http://fixture')
            with self.assertRaises(RuntimeError):self.ops.install(self.current,'http://fixture')
        self.assert_protected();self.assertEqual(module.read_json(self.device.store/'registry.json'),self.scan['registry'])
        self.assertFalse(any(name in FORMAT_PARTITIONS for name in self.device.writes))

    def test_changed_uuid_shared_firmware_or_registry_stops(self):
        self.current,_new=self.plan()
        key='lqr_111111111111';original=self.device.parts[key]['partuuid'];self.device.parts[key]['partuuid']=str(uuid.uuid4())
        with self.assertRaises(RuntimeError):self.ops.guard(self.current)
        self.device.parts[key]['partuuid']=original
        raw=(self.device.store/'registry.json').read_bytes();(self.device.store/'registry.json').write_text('{}')
        with self.assertRaises(RuntimeError):self.ops.guard(self.current)
        (self.device.store/'registry.json').write_bytes(raw)
        self.device.files['modem_a'].write_bytes(b'changed'.ljust(4096,b'\0'))
        with self.assertRaises(RuntimeError):self.ops.guard(self.current)
        self.assertFalse(self.device.writes)

    def test_formatter_rejects_injected_target_arguments(self):
        settings=copy.deepcopy(self.profile['userdata']);settings['mkfs_args'].append('/dev/sda')
        with self.assertRaises(ValueError):self.device.format(self.device.parts['userdata'],settings)
        self.assertFalse(self.device.writes);self.assert_protected()

    def test_incomplete_reinstall_cannot_be_committed(self):
        self.current,new=self.plan()
        with patch.object(module,'fetch',side_effect=self.fetch):self.ops.stage(self.current,'http://fixture')
        with self.assertRaises((RuntimeError,ValueError,OSError)):self.ops.commit(self.current,new,self.boot_before)
        self.assertEqual(module.read_json(self.device.store/'registry.json'),self.scan['registry'])
        self.assert_protected()

    def test_registry_cannot_drop_or_change_linux_systems(self):
        self.current,new=self.plan('preserve')
        with patch.object(module,'fetch',side_effect=self.fetch):self.ops.stage(self.current,'http://fixture')
        bad=copy.deepcopy(new);bad['systems'][0]['name']='changed'
        with self.assertRaises((ValueError,RuntimeError)):self.ops.commit(self.current,bad,self.boot_before)
        self.assertEqual(module.read_json(self.device.store/'registry.json'),self.scan['registry'])
        self.assert_protected()

    def test_forged_old_registry_is_rejected_before_stage(self):
        self.current,_new=self.plan('preserve')
        self.current['registry']=copy.deepcopy(self.current['registry'])
        self.current['registry']['systems']=[s for s in self.current['registry']['systems'] if s['type']=='android']
        with self.assertRaises(RuntimeError):self.ops.stage(self.current,'http://fixture')
        self.assertFalse(self.device.writes)

    def test_btrfs_inventory_reads_magic_and_uuid_from_correct_offsets(self):
        root=self.root/'sys-fixture';sysroot=root/'class/block';sysroot.mkdir(parents=True)
        driver=root/'drivers/ufshcd-qcom';driver.mkdir(parents=True)
        controller=root/'devices/ufs';controller.mkdir(parents=True)
        (controller/'driver').symlink_to(driver)
        disk=controller/'block/sda';(disk/'queue').mkdir(parents=True)
        (disk/'queue/logical_block_size').write_text('4096\n');(disk/'dev').write_text('8:0\n')
        (sysroot/'sda').symlink_to(disk)
        part=disk/'sda35';part.mkdir();(sysroot/'sda35').symlink_to(part)
        target_uuid='11111111-1111-4111-8111-111111111111';fs_uuid='22222222-2222-4222-8222-222222222222'
        for name,value in {'partition':'35','dev':'8:35','start':'2048','size':str(32*1024**3//512),
            'uevent':'PARTNAME=lqr_111111111111\nPARTUUID='+target_uuid}.items():(part/name).write_text(value+'\n')
        header=bytearray(65608);header[65568:65584]=uuid.UUID(fs_uuid).bytes;header[65600:65608]=b'_BHRfS_M'
        class InventoryHardware(module.Hardware):
            def read(self,_part,offset,length):return bytes(header[offset:offset+length])
        device=object.__new__(InventoryHardware);device.sys_root=sysroot
        result=device.inventory()['lqr_111111111111']
        self.assertEqual(result['filesystem'],'btrfs');self.assertEqual(result['fsuuid'],fs_uuid)

if __name__=='__main__':unittest.main(verbosity=2)
