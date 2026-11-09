#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import copy
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine,demo_report
from liuqin_gui.engine import InstallerError,cli
from liuqin_gui.partitioning import scan
from liuqin_gui.partition_demo import inspect,apply
from liuqin_partitions import addition_capacity,discover,plan_append,snapshot_id,validate_append
from liuqin_partition_editor import capabilities,plan_edit,validate_edit
from liuqin_android import managed_layout


def config(userdata=0,roots=1,size=16):
    return dict(schema_version=1,partition_only=True,userdata_gib=userdata,boot_gib=2,boot_action='install-only',
        roots=[dict(id=str(uuid.uuid4()),name='New Linux '+str(i),size_gib=size,filesystem='ext4') for i in range(roots)])


def reduced_report(userdata=True):
    disk=cli.layout.reference_stock_disk();lines=demo_report().split('Number  Start (sector)',1)[0].splitlines()
    lines.append('Number  Start (sector) End (sector) Size Code Name')
    for p in disk.partitions:
        if p.name=='userdata':
            if not userdata:continue
            p.last_sector=p.first_sector+32*1024**3//4096-1
        lines.append(f'{p.number} {p.first_sector} {p.last_sector} 1.0 GiB 8300 {p.name}')
    return '\n'.join(lines)+'\n'


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.events=[]
        self.engine=DemoEngine(self.events.append,self.root/'state');self.engine.pause=lambda _:None
        self.addCleanup(self.engine.close)
        for mock in (patch.object(socket,'socket',side_effect=AssertionError('hardware forbidden')),
                     patch.object(subprocess,'run',side_effect=AssertionError('hardware forbidden'))):
            mock.start();self.addCleanup(mock.stop)

    def test_stock_userdata_full_disk_is_read_only_and_cannot_create_root(self):
        scan(self.engine,dict(serial='DEMO-LIUQIN',bundle='ignored',layout='partition-only'))
        info=[e['inventory'] for e in self.events if e['type']=='partition_inventory'][-1]
        self.assertTrue(info['has_userdata']);self.assertFalse(info['free_regions'])
        self.assertFalse(self.engine.receipt['write_dispatched'])
        with self.assertRaisesRegex(ValueError,'未分配'):
            plan_append(inspect(self.engine),config(),'DEMO-LIUQIN',str(uuid.uuid4()))

    def test_shrunken_userdata_preserved_and_only_gpt_gaps_used(self):
        self.engine.demo_raw_report=reduced_report();state=inspect(self.engine);original=copy.deepcopy(state)
        plan=plan_append(state,config(),'DEMO-LIUQIN',str(uuid.uuid4()))
        self.assertEqual([p['role'] for p in plan['partitions']],['store','root'])
        self.assertEqual(state,original)
        end=state['inventory']['userdata']['start']+state['inventory']['userdata']['bytes']
        self.assertTrue(all(p['start']>=end for p in plan['partitions']))
        with self.assertRaisesRegex(ValueError,'已存在 userdata'):
            plan_append(state,config(userdata=32),'DEMO-LIUQIN',str(uuid.uuid4()))
        apply(self.engine,plan)
        self.assertEqual(inspect(self.engine)['inventory']['userdata'],state['inventory']['userdata'])

    def test_no_userdata_can_create_it_after_existing_store_and_root(self):
        self.engine.demo_raw_report=reduced_report(False)
        first=plan_append(inspect(self.engine),config(),'DEMO-LIUQIN',str(uuid.uuid4()));apply(self.engine,first)
        before=inspect(self.engine)
        second=plan_append(before,config(userdata=32,roots=0),'DEMO-LIUQIN',str(uuid.uuid4()))
        self.assertEqual([p['name'] for p in second['partitions']],['userdata'])
        apply(self.engine,second);after=inspect(self.engine)
        managed_layout(cli.layout.parse_print(after['report']),after['inventory'],after,'DEMO-LIUQIN')
        for name,part in before['inventory'].items():self.assertEqual(after['inventory'][name],part)

    def test_exact_root_fills_released_space_after_store_reservation(self):
        serial = 'DEMO-LIUQIN'
        state = inspect(self.engine)
        summary,rows = discover(state,serial),capabilities(state,serial)
        self.assertEqual(addition_capacity(summary,rows,[],config(roots=0),'linux'),0)
        userdata = state['inventory']['userdata']
        edits = [dict(name='userdata',partuuid=userdata['partuuid'],action='delete')]
        maximum = addition_capacity(summary,rows,edits,config(roots=0),'linux')
        self.assertGreater(maximum,16*cli.layout.GIB)
        self.assertNotEqual(maximum % cli.layout.GIB,0)
        self.assertEqual(maximum % cli.layout.ALIGNMENT,0)
        proposal = config(size=maximum//cli.layout.GIB)
        proposal['roots'][0]['size_bytes']=maximum
        plan=plan_edit(state,edits,proposal,serial,str(uuid.uuid4()))
        self.assertEqual([(p['role'],p['bytes']) for p in plan['partitions']],
                         [('store',2*cli.layout.GIB),('root',maximum)])
        validate_edit(plan,serial)
        self.assertEqual(addition_capacity(summary,rows,edits,proposal,'linux'),0)
        with self.assertRaisesRegex(ValueError,'连续空间不足'):
            addition_capacity(summary,rows,[],proposal,None)

    def test_exact_userdata_is_allocated_after_root_draft(self):
        serial = 'DEMO-LIUQIN'
        self.engine.demo_raw_report=reduced_report(False)
        state=inspect(self.engine)
        summary,rows=discover(state,serial),capabilities(state,serial)
        proposal=config(size=32)
        maximum=addition_capacity(summary,rows,[],proposal,'android')
        self.assertEqual(maximum,summary['largest_free_bytes']-34*cli.layout.GIB)
        proposal['userdata_bytes']=maximum
        plan=plan_edit(state,[],proposal,serial,str(uuid.uuid4()))
        self.assertEqual([p['role'] for p in plan['partitions']],['store','root','android'])
        self.assertEqual(plan['partitions'][-1]['bytes'],maximum)
        self.assertEqual(plan['partitions'][-1]['number'],35)
        validate_edit(plan,serial)

    def test_capacity_uses_contiguous_gaps_and_preserves_pending_userdata(self):
        serial = 'DEMO-LIUQIN'
        self.engine.demo_raw_report=reduced_report(False)
        first=plan_append(inspect(self.engine),dict(config(roots=3,size=32),boot_gib=3),serial,str(uuid.uuid4()))
        apply(self.engine,first)
        state=inspect(self.engine)
        summary,rows=discover(state,serial),capabilities(state,serial)
        part=first['partitions'][1]
        edits=[dict(name=part['name'],partuuid=part['partuuid'],action='delete')]
        proposal=config(roots=0)
        maximum=addition_capacity(summary,rows,edits,proposal,'linux')
        self.assertEqual(maximum,summary['largest_free_bytes'])
        self.assertLess(maximum,128*cli.layout.GIB)
        self.assertGreater(maximum+part['bytes'],128*cli.layout.GIB)
        with self.assertRaisesRegex(ValueError,'连续空间不足'):
            plan_edit(state,edits,config(size=128),serial,str(uuid.uuid4()))
        proposal['userdata_bytes']=80*cli.layout.GIB
        available=addition_capacity(summary,rows,edits,proposal,'linux')
        self.assertEqual(available,maximum-proposal['userdata_bytes'])
        proposal['roots']=config(size=available//cli.layout.GIB)['roots']
        proposal['roots'][0]['size_bytes']=available
        plan=plan_edit(state,edits,proposal,serial,str(uuid.uuid4()))
        self.assertEqual([p['role'] for p in plan['partitions']],['root','android'])
        self.assertEqual(plan['partitions'][0]['bytes'],available)
        validate_edit(plan,serial)

    def test_existing_userdata_can_initialize_store_without_any_linux(self):
        self.engine.demo_raw_report=reduced_report();before=inspect(self.engine)
        planned=plan_append(before,config(roots=0),'DEMO-LIUQIN',str(uuid.uuid4()))
        self.assertEqual([p['name'] for p in planned['partitions']],['linux_boot'])
        self.assertFalse(planned['candidate_registry']['rootfs_slots'])
        apply(self.engine,planned);state=inspect(self.engine)
        managed_layout(cli.layout.parse_print(state['report']),state['inventory'],state,'DEMO-LIUQIN')
        with self.assertRaisesRegex(ValueError,'没有需要新增'):
            plan_append(state,config(roots=0),'DEMO-LIUQIN',str(uuid.uuid4()))

    def test_unknown_layout_is_displayed_but_cannot_be_modified(self):
        self.engine.demo_raw_report=reduced_report().replace(' rescue\n',' mystery\n')
        state=inspect(self.engine);summary=discover(state,'DEMO-LIUQIN')
        self.assertFalse(summary['can_create']);self.assertTrue(summary['partitions'])
        with self.assertRaisesRegex(ValueError,'只读'):plan_append(state,config(),'DEMO-LIUQIN',str(uuid.uuid4()))

    def test_unreported_kernel_partition_and_guid_collision_are_rejected(self):
        self.engine.demo_raw_report=reduced_report();state=inspect(self.engine)
        extra=copy.deepcopy(state['inventory']['userdata']);extra['name']='unexpected'
        state['inventory']['unexpected']=extra
        self.assertFalse(discover(state,'DEMO-LIUQIN')['can_create'])
        state=inspect(self.engine);transaction=str(uuid.uuid4());configuration=config()
        configuration['roots'][0]['id']=str(uuid.uuid5(uuid.UUID(transaction),'boot-store'))
        with self.assertRaisesRegex(ValueError,'GUID'):
            plan_append(state,configuration,'DEMO-LIUQIN',transaction)

    def test_managed_append_retains_slots_registry_images_and_default(self):
        self.engine.demo_raw_report=reduced_report()
        first=plan_append(inspect(self.engine),config(),'DEMO-LIUQIN',str(uuid.uuid4()));apply(self.engine,first)
        before=inspect(self.engine);plan=plan_append(before,config(),'DEMO-LIUQIN',str(uuid.uuid4()))
        self.assertFalse(plan['create_store']);self.assertEqual(len(plan['partitions']),1)
        old=before['registry'];new=plan['candidate_registry']
        self.assertEqual(new['systems'],old['systems']);self.assertEqual(new['default_id'],old['default_id'])
        self.assertEqual(new['rootfs_slots'][:len(old['rootfs_slots'])],old['rootfs_slots'])
        apply(self.engine,plan);after=inspect(self.engine)
        for name,p in before['inventory'].items():self.assertEqual(after['inventory'][name],p)
        forged=copy.deepcopy(plan);forged['partitions'][0]['start']=before['inventory']['userdata']['start']
        with self.assertRaises(ValueError):validate_append(forged,'DEMO-LIUQIN')

    def prepare(self):
        self.engine.demo_raw_report=reduced_report()
        args=dict(serial='DEMO-LIUQIN',bundle='ignored',layout='partition-only')
        self.engine.create_backup(dict(args,backup_parent=str(self.root/'backups')))
        backup=[e['backup']['path'] for e in self.events if e['type']=='backup_ready'][-1]
        scan(self.engine,args);state=inspect(self.engine)
        request=dict(args,backup_dir=backup,partition_config=config(),partition_snapshot_id=snapshot_id(state))
        self.engine.prepare(request);return request

    def confirmation(self):
        return dict(session_id=self.engine.session_id,plan_id=self.engine.plan_id,confirmation=dict(
            serial='DEMO-LIUQIN',create_partitions=True,preserve_existing_partitions=True,allow_experimental=True))

    def test_full_host_append_requires_preservation_consent_not_erasure(self):
        self.prepare();before=copy.deepcopy(inspect(self.engine)['inventory']);transport=self.engine.transport
        bad=self.confirmation();bad['confirmation'].pop('preserve_existing_partitions')
        with self.assertRaises(InstallerError):self.engine.install(bad)
        self.assertNotIn('partition-apply',transport.calls)
        self.engine.install(self.confirmation())
        self.assertEqual(self.engine.state,'partition_complete')
        self.assertTrue(self.engine.receipt['existing_partitions_preserved']);self.assertFalse(self.engine.receipt['boot_written'])
        for name,p in before.items():self.assertEqual(inspect(self.engine)['inventory'][name],p)

    def test_layout_changed_after_scan_or_confirmation_has_no_write(self):
        request=self.prepare();transport=self.engine.transport
        self.engine.demo_raw_report=self.engine.demo_raw_report.replace('32.0 GiB','31.0 GiB')+'\n'
        with self.assertRaises((InstallerError,RuntimeError)):self.engine.install(self.confirmation())
        self.assertFalse(hasattr(self.engine,'demo_partition_report'))
        self.assertFalse(self.engine.receipt.get('layout_written'))

if __name__=='__main__':unittest.main(verbosity=2)
