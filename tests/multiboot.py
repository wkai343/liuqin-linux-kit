#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Multi-root geometry, actual boot specialization and prepare/commit regressions."""
import copy
import hashlib
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine, DemoTransport, demo_image
from liuqin_gui.engine import InstallerError, canonical, cli
from liuqin_gui import artifacts
from liuqin_boot_target import bind, encode, parse
from liuqin_multiboot import normalize, plan, verify_layout


def configuration():
    ids=[str(uuid.uuid4()),str(uuid.uuid4())]
    return dict(schema_version=1,userdata_gib=32,boot_gib=2,
        roots=[dict(id=ids[i],name=name,size_gib=32,filesystem=fs,bundle='',boot_image='')
               for i,(name,fs) in enumerate((('Fedora','ext4'),('Linux Btrfs','btrfs')))],
        boot_action='set-default',default_id=ids[1])


class MultibootTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.events=[];self.engine=DemoEngine(self.events.append,self.root/'state')
        self.engine.pause=lambda _seconds:None
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.engine.close)
        self.config=configuration()
        self.no_process=patch.object(subprocess,'run',side_effect=AssertionError('real command forbidden'))
        self.no_socket=patch.object(socket,'socket',side_effect=AssertionError('real network forbidden'))
        self.no_process.start();self.no_socket.start()
        self.addCleanup(self.no_process.stop);self.addCleanup(self.no_socket.stop)

    def prepare(self):
        self.engine.create_backup(dict(serial='DEMO-LIUQIN',bundle='ignored',backup_parent=str(self.root/'backups')))
        backup=next(e['backup']['path'] for e in self.events if e['type']=='backup_ready')
        self.engine.prepare(dict(serial='DEMO-LIUQIN',bundle='ignored',backup_dir=backup,
                                 layout='multi-root',partition_config=self.config))

    def confirmation(self):
        return dict(session_id=self.engine.session_id,plan_id=self.engine.plan_id,
            confirmation=dict(serial='DEMO-LIUQIN',personal_data_backed_up=True,erase_userdata=True,
                              repartition_userdata=True,allow_experimental=True))

    def test_capacity_variants_keep_prefix_and_unique_targets(self):
        for capacity in (128,256,512):
            disk=cli.layout.reference_stock_disk();disk.total_sectors=disk.total_sectors*capacity//256
            disk.last_usable=disk.total_sectors-6;disk.by_name('userdata').last_sector=disk.last_usable
            before=copy.deepcopy(disk)
            result=plan(disk,self.config,str(uuid.uuid4()))
            self.assertEqual(len(result['systems']),2)
            self.assertGreater(result['free_bytes'],0)
            self.assertEqual([(p.name,p.start,p.end) for p in disk.partitions],[(p.name,p.start,p.end) for p in before.partitions])
            self.assertEqual(len({p['partuuid'] for p in result['partitions'] if 'partuuid'in p}),3)
            for item in result['systems']:
                self.assertEqual(parse(encode(item['target'])),item['target'])
                self.assertEqual(item['partition']['start']%(4*1024**2),0)

    def test_invalid_and_oversized_configuration(self):
        for mutation in ('duplicate','zero-root','undersized-android','unknown-default','boolean-size','too-large'):
            config=copy.deepcopy(self.config)
            if mutation=='duplicate':config['roots'][1]['id']=config['roots'][0]['id']
            elif mutation=='zero-root':config['roots']=[]
            elif mutation=='undersized-android':config['userdata_gib']=1
            elif mutation=='unknown-default':config['default_id']=str(uuid.uuid4())
            elif mutation=='boolean-size':config['roots'][0]['size_gib']=True
            else:config['roots'][0]['size_gib']=4096
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                plan(cli.layout.reference_stock_disk(),config,str(uuid.uuid4()))

    def test_each_image_has_its_own_root_binding_and_preserves_kernel(self):
        self.prepare()
        systems=self.engine.multi_plan['systems']
        original=artifacts.boot_interface(self.engine.demo_bundle/'boot.img')
        for system in systems:
            boot=artifacts.boot_interface(Path(system['boot_image']))
            self.assertEqual(boot['boot_target'],system['target'])
            self.assertEqual(boot['kernel_sha256'],original['kernel_sha256'])
            self.assertEqual(boot['contract'],original['contract'])
            data=Path(system['boot_image']).read_bytes()
            align=lambda n:(n+4095)&~4095
            ks,rs,ds=(struct.unpack_from('<I',data,p)[0] for p in (8,16,1648))
            ram=4096+align(ks);dtb=ram+align(rs)
            digest=hashlib.sha1()
            for payload in (data[4096:4096+ks],data[ram:ram+rs],b'',b'',data[dtb:dtb+ds]):
                digest.update(payload);digest.update(struct.pack('<I',len(payload)))
            self.assertEqual(data[576:608],digest.digest()+bytes(12))
        self.assertNotEqual(systems[0]['boot_sha256'],systems[1]['boot_sha256'])
        self.assertNotIn('multiboot_apply',self.engine.transport.calls)

    def test_rebinding_and_malformed_targets_refused(self):
        self.prepare();system=self.engine.multi_plan['systems'][0]
        with self.assertRaises(ValueError):bind(Path(system['boot_image']),self.root/'rebound.img',system['target'])
        with self.assertRaises(ValueError):parse(encode(system['target'])+b'unknown=1\n')

    def test_default_is_written_after_all_roots_and_store(self):
        self.prepare();transport=self.engine.transport
        self.engine.install(self.confirmation())
        self.assertEqual(self.engine.state,'awaiting_first_boot')
        calls=transport.calls
        self.assertLess(calls.index('multiboot_apply'),calls.index('install_root:'+self.config['roots'][0]['id']))
        self.assertLess(calls.index('install_root:'+self.config['roots'][1]['id']),calls.index('multiboot_store:stage'))
        self.assertLess(calls.index('multiboot_store:stage'),calls.index('multiboot_default:'+self.config['default_id']))
        self.assertEqual(self.engine.receipt['default_id'],self.config['default_id'])

    def test_install_only_does_not_write_default(self):
        self.config['boot_action']='install-only';self.prepare();transport=self.engine.transport
        self.engine.install(self.confirmation())
        self.assertFalse(any(c.startswith('multiboot_default') or c=='flash_verify_reboot' for c in transport.calls))
        self.assertFalse(self.engine.receipt['boot_written'])

    def test_missing_partition_consent_has_no_writes(self):
        self.prepare();request=self.confirmation();request['confirmation'].pop('repartition_userdata')
        with self.assertRaises(InstallerError):self.engine.install(request)
        self.assertEqual(self.engine.state,'prepared')
        self.assertNotIn('multiboot_apply',self.engine.transport.calls)

    def test_bound_image_mutation_stops_before_partitioning(self):
        self.prepare();system=self.engine.multi_plan['systems'][1]
        Path(system['boot_image']).write_bytes(b'changed')
        with self.assertRaises(InstallerError):self.engine.install(self.confirmation())
        self.assertNotIn('multiboot_apply',self.engine.transport.calls)

    def test_plan_change_stops_before_partitioning(self):
        self.prepare();self.engine.multi_plan['configuration']['default_id']=self.config['roots'][0]['id']
        with self.assertRaises(InstallerError):self.engine.install(self.confirmation())
        self.assertNotIn('multiboot_apply',self.engine.transport.calls)

    def test_root_failure_never_changes_default(self):
        self.prepare();transport=self.engine.transport
        with patch.object(transport,'install_root',side_effect=RuntimeError('unpack failed')):
            with self.assertRaises(RuntimeError):self.engine.install(self.confirmation())
        self.assertFalse(any(c.startswith('multiboot_default') for c in transport.calls))

    def test_first_install_default_failure_never_restores_replaced_system_boot(self):
        from liuqin_gui.multiboot_transport import set_default
        self.prepare()
        engine=self.engine;system=engine.multi_plan['systems'][1]
        previous=engine.multi_registry['boot_transaction']['previous_boot_sha256']
        original_backup=Path(engine.backup_record['path'])/'boot_a.img'
        self.assertNotEqual(previous,hashlib.sha256(original_backup.read_bytes()).hexdigest(),
                            'transaction must use current boot, not an old reusable backup')
        class Transport:
            def __init__(self,failure):
                self.engine=engine;self.failure=failure;self.failed=False;self.ram_active=True
                self.calls=[];self.live=previous
            def current_boot_digest(self):return self.live
            def return_fastboot(self):self.calls.append('fastboot');self.ram_active=False
            def check_device(self):return engine.device
            def boot_ram(self):self.calls.append('ram');self.ram_active=True
            def fastboot(self,*args):
                self.calls.append(args)
                if args[:2]==('flash','boot_a'):
                    self.live=hashlib.sha256(Path(args[2]).read_bytes()).hexdigest()
                    if self.failure=='flash' and not self.failed:
                        self.failed=True;self.live='f'*64;raise RuntimeError('flash interrupted')
            def remote(self,*_args):
                if self.failure=='readback' and not self.failed:
                    self.failed=True;return b'bad  boot_a'
                return (self.live+'  boot_a').encode()
        for failure in ('none','flash','readback','registry'):
            with self.subTest(failure=failure):
                engine.receipt.update(boot_flash_started=False,boot_written=False,boot_rollback_verified=False)
                transport=Transport(failure);published=[]
                def publish(_transport,action,_store,payload,_digest,_expected):
                    import json
                    published.append(json.loads(payload))
                    if failure=='registry' and not transport.failed:
                        transport.failed=True;raise RuntimeError('registry interrupted')
                registry=canonical(dict(engine.multi_registry,default_id=system['id']))
                with patch('liuqin_gui.multiboot_transport.store_payload',side_effect=publish):
                    if failure=='none':
                        set_default(transport,system,engine.multi_plan['store'],registry,engine.backup_record)
                        self.assertEqual(transport.live,system['padded_sha256'])
                        self.assertEqual(transport.calls[-1],('reboot',))
                        self.assertEqual(published[-1]['default_id'],system['id'])
                    else:
                        with self.assertRaises((RuntimeError,InstallerError)):
                            set_default(transport,system,engine.multi_plan['store'],registry,engine.backup_record)
                        self.assertNotEqual(transport.live,previous)
                        self.assertFalse(engine.receipt.get('boot_rollback_attempted',False))
                        self.assertTrue(engine.receipt['boot_finalization_pending'])
                        self.assertEqual(len([c for c in transport.calls if isinstance(c,tuple) and c[:2]==('flash','boot_a')]),1)
                        self.assertNotIn(('reboot',),transport.calls)

    def test_device_plan_payload_change_stops_before_partitioning(self):
        self.prepare()
        self.engine.multi_layout_payload += b'changed\n'
        with self.assertRaises(InstallerError):self.engine.install(self.confirmation())
        self.assertNotIn('multiboot_apply',self.engine.transport.calls)

    def test_current_boot_change_stops_before_partitioning(self):
        self.prepare()
        with patch.object(self.engine.transport,'current_boot_digest',return_value='f'*64):
            with self.assertRaises(InstallerError):self.engine.install(self.confirmation())
        self.assertNotIn('multiboot_apply',self.engine.transport.calls)


if __name__=='__main__':unittest.main(verbosity=2)
