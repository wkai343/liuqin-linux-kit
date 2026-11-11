#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import copy
import json
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine,DemoTransport
from liuqin_gui import boot_switch,workflow
from liuqin_gui.engine import InstallerError
from liuqin_gui.partition_demo import inspect
from liuqin_partitions import snapshot_id
from liuqin_partition_editor import plan_edit,validate_edit,capabilities
from liuqin_android import managed_layout
from liuqin_gui.engine import cli


class WorkflowTests(unittest.TestCase):
    def test_delete_only_cannot_remove_current_default(self):
        state=dict(registry=dict(default_id='linux-id',systems=[dict(id='linux-id',type='linux',partition=dict(name='rootfs'))]))
        edit=dict(source=state,deletes=[dict(name='rootfs')],formats=[])
        with self.assertRaisesRegex(InstallerError,'先切换'):
            workflow.check_preserved_default(edit,[],'',None,'')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.engine=DemoEngine(lambda _:None,self.root/'state');self.engine.pause=lambda _:None;self.addCleanup(self.engine.close)
        for mock in (patch.object(socket,'socket',side_effect=AssertionError('network forbidden')),
                     patch.object(subprocess,'run',side_effect=AssertionError('real process forbidden'))):mock.start();self.addCleanup(mock.stop)
        workflow.scan(self.engine,dict(serial='DEMO-LIUQIN'))
    def request(self):
        return dict(serial='DEMO-LIUQIN',snapshot_id=snapshot_id(inspect(self.engine)),backup_parent=str(self.root/'backups'),actions=[],
            partition_config=dict(roots=[],userdata_gib=0,boot_gib=2),target='',recovery_mode='off',set_default=False)
    def confirmation(self):
        return dict(session_id=self.engine.session_id,plan_id=self.engine.plan_id,
            confirmation=dict(serial='DEMO-LIUQIN',accept_operations=True,allow_experimental=True))
    def linux_request(self):
        request=self.request();old=inspect(self.engine)['inventory']['userdata'];id=str(uuid.uuid4())
        request['actions']=[dict(name='userdata',partuuid=old['partuuid'],action='delete')]
        request['partition_config']=dict(userdata_gib=32,boot_gib=2,roots=[dict(id=id,name='Fedora',size_gib=32,filesystem='btrfs')])
        request.update(target='lqr_'+id.replace('-','')[:12],linux_bundle=str(self.engine.demo_bundle),set_default=True)
        return request
    def test_scan_requires_no_system_materials(self):
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertFalse(self.engine.receipt.get('backup_dir'))
        self.assertFalse(inspect(self.engine)['registry'])
    def test_native_scan_exposes_no_edit_or_install_actions(self):
        events=[];self.engine.emit_callback=events.append
        validate=self.engine.validate_installer
        def native(*args):
            validate(*args);self.engine.manifest['native_recovery_manifest']={'inspection_only':True}
        with patch.object(self.engine,'validate_installer',side_effect=native):
            workflow.scan(self.engine,dict(serial='DEMO-LIUQIN'))
        inventory=next(e for e in events if e['type']=='wizard_inventory')
        self.assertTrue(inventory['inspection_only']);self.assertFalse(inventory['summary']['can_create'])
        self.assertTrue(all(not r['can_install'] and not r['can_delete'] and not r['can_format'] for r in inventory['partitions']))
        self.assertFalse(self.engine.receipt['write_dispatched'])
    def test_linux_package_does_not_need_installer_image_inside_it(self):
        from liuqin_gui.engine import Engine
        materials=self.root/'linux-only';materials.mkdir()
        for name in ('boot.img','rootfs.tar.gz'):shutil.copyfile(self.engine.demo_bundle/name,materials/name)
        standalone=Engine(lambda _:None,self.root/'inspect-only')
        standalone.runtime_path=str(self.engine.demo_bundle/'installer.img')
        standalone.validate_bundle(str(materials));standalone.close()
        self.assertNotIn('installer.img',{p.name for p in materials.iterdir()})
    def test_create_and_install_new_linux_then_reinstall_same_slot(self):
        request=self.linux_request();workflow.prepare(self.engine,request)
        self.assertFalse(self.engine.receipt['write_dispatched'])
        workflow.install(self.engine,self.confirmation());self.assertTrue(self.engine.receipt['completed'])
        before=inspect(self.engine);system=before['registry']['systems'][0];old_fsuuid=system['target']['root_fsuuid']
        request=self.request();request.update(target=system['partition']['name'],linux_bundle=str(self.engine.demo_bundle))
        with self.assertRaisesRegex(InstallerError,'当前默认系统'):
            workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),before)
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.engine.finish_cancel()
        request['set_default']=True
        workflow.prepare(self.engine,request)
        self.assertEqual(len(self.engine.workflow_snapshot['edit']['formats']),1)
        workflow.install(self.engine,self.confirmation());after=inspect(self.engine)
        self.assertEqual(len(after['registry']['systems']),1)
        self.assertNotEqual(after['registry']['systems'][0]['target']['root_fsuuid'],old_fsuuid)
        self.assertEqual(after['inventory']['userdata'],before['inventory']['userdata'])
    def test_material_or_plan_change_stops_before_edits(self):
        request=self.linux_request();workflow.prepare(self.engine,request);transport=self.engine.transport
        (self.engine.demo_bundle/'boot.img').write_bytes(b'changed')
        with self.assertRaises(InstallerError):workflow.install(self.engine,self.confirmation())
        self.assertNotIn('partition-edit',transport.calls)
    def test_old_boot_snapshot_and_device_backup_are_checked_before_edits(self):
        for directory in ('run_dir','backup_dir'):
            workflow.prepare(self.engine,self.linux_request())
            before=inspect(self.engine);transport=self.engine.transport
            (Path(self.engine.receipt[directory])/'boot_a.img').write_bytes(b'changed')
            with self.assertRaises(InstallerError):workflow.install(self.engine,self.confirmation())
            self.assertFalse(self.engine.receipt['write_dispatched'])
            self.assertEqual(inspect(self.engine),before)
            self.assertNotIn('partition-edit',transport.calls)
            self.engine.finish_cancel()
    def test_rejects_protected_partitions_and_in_use_store(self):
        state=inspect(self.engine)
        for name in ('persist','super','boot_a','recovery_a'):
            with self.assertRaises(ValueError):plan_edit(state,[dict(name=name,partuuid=state['inventory'][name]['partuuid'],action='delete')],dict(roots=[],userdata_gib=0),'DEMO-LIUQIN',str(uuid.uuid4()))
        request=self.linux_request();workflow.prepare(self.engine,request);workflow.install(self.engine,self.confirmation())
        state=inspect(self.engine);store=state['inventory']['linux_boot']
        with self.assertRaisesRegex(ValueError,'仍被系统使用'):
            plan_edit(state,[dict(name='linux_boot',partuuid=store['partuuid'],action='format')],dict(roots=[],userdata_gib=0),'DEMO-LIUQIN',str(uuid.uuid4()))
    def test_delete_all_roots_and_store_requires_a_new_default(self):
        request=self.linux_request();workflow.prepare(self.engine,request);workflow.install(self.engine,self.confirmation())
        state=inspect(self.engine);actions=[dict(name=p['name'],partuuid=p['partuuid'],action='delete') for p in state['inventory'].values() if p['name']=='linux_boot' or p['name'].startswith('lqr_')]
        request=self.request();request['actions']=actions
        with self.assertRaisesRegex(InstallerError,'先切换'):
            workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),state)
        self.assertFalse(self.engine.receipt['write_dispatched'])
    def test_stock_android_does_not_require_boot_store_or_linux(self):
        request=self.request();request.update(target='userdata',rom_path='DEMO',set_default=True)
        workflow.prepare(self.engine,request)
        self.assertTrue(self.engine.workflow_snapshot['direct_android']);self.assertFalse(self.engine.workflow_snapshot['edit']['changes'])
        transport=self.engine.transport;workflow.install(self.engine,self.confirmation())
        self.assertIn('android-direct',transport.calls);self.assertNotIn('partition-edit',transport.calls)
        self.assertTrue(self.engine.receipt['system_installed']);self.assertNotIn('linux_boot',inspect(self.engine)['inventory'])

    def test_reprepare_reuses_base_backup_but_reads_current_boot(self):
        request=self.request();request.update(target='userdata',rom_path='DEMO',set_default=True)
        workflow.prepare(self.engine,request)
        backup=self.engine.receipt['backup_dir'];first_run=self.engine.receipt['run_dir']
        self.engine.finish_cancel()
        self.engine.demo_live_boot=b'NEW CURRENT BOOT'.ljust(self.engine.device['boot_bytes'],b'\0')
        with patch.object(DemoTransport,'backup',side_effect=AssertionError('duplicate backup transfer')):
            workflow.prepare(self.engine,request)
        snapshot=self.engine.workflow_snapshot
        self.assertEqual(self.engine.receipt['backup_dir'],backup)
        self.assertNotEqual(self.engine.receipt['run_dir'],first_run)
        self.assertTrue(snapshot['backup_reused'])
        self.assertEqual(snapshot['previous_boot_sha256'],self.engine.transport.current_boot_digest())
        self.assertEqual((Path(self.engine.receipt['run_dir'])/'boot_a.img').read_bytes(),self.engine.demo_live_boot)

    def test_changed_persist_forces_new_base_backup(self):
        request=self.request();request.update(target='userdata',rom_path='DEMO',set_default=True)
        workflow.prepare(self.engine,request);backup=self.engine.receipt['backup_dir'];self.engine.finish_cancel()
        original=DemoTransport.verify_backup
        def different_persist(transport,record):
            result=original(transport,record)
            result.update(persist_snapshot_matches=False)
            return result
        with patch.object(DemoTransport,'verify_backup',side_effect=different_persist,autospec=True):
            workflow.prepare(self.engine,request)
        self.assertNotEqual(self.engine.receipt['backup_dir'],backup)
        self.assertFalse(self.engine.workflow_snapshot['backup_reused'])

    def combined_request(self,android=True):
        request=self.linux_request();second=str(uuid.uuid4());name='lqr_'+second.replace('-','')[:12]
        request['partition_config']['roots'].append(dict(id=second,name='Linux Btrfs',size_gib=32,filesystem='ext4'))
        roots=[request['target'],name]
        request.update(install_targets=(['userdata'] if android else [])+roots,rom_path='DEMO',
            linux_bundles={n:str(self.engine.demo_bundle) for n in roots},default_target=roots[-1])
        return request

    def test_android_and_two_linux_share_one_plan_and_default_is_last(self):
        events=[];self.engine.emit_callback=events.append;request=self.combined_request()
        original=DemoTransport.backup
        with patch.object(DemoTransport,'backup',autospec=True,side_effect=original) as backup_calls:
            workflow.prepare(self.engine,request)
            snapshot=copy.deepcopy(self.engine.workflow_snapshot)
            self.assertEqual(backup_calls.call_count,1)
            self.assertEqual(snapshot['kind'],'combined');self.assertEqual(len(snapshot['installations']),3)
            self.assertFalse(self.engine.receipt['write_dispatched'])
            plan=next(e['plan'] for e in events if e['type']=='wizard_plan')
            self.assertEqual(len(plan['installations']),3);self.assertTrue(plan['android_writes'])
            workflow.install(self.engine,self.confirmation())
            self.assertEqual(backup_calls.call_count,2)
        after=inspect(self.engine)
        systems=after['registry']['systems'];self.assertEqual(len(systems),3)
        default=next(s for s in systems if s['id']==after['registry']['default_id'])
        self.assertEqual(default['partition']['name'],request['default_target'])
        self.assertEqual(self.engine.receipt['installed_targets'][-1],request['default_target'])
        self.assertEqual(len(self.engine.receipt['children']),3)

    def test_multiple_linux_do_not_require_a_rom_and_android_can_be_default(self):
        request=self.combined_request(android=False);request.pop('rom_path')
        workflow.prepare(self.engine,request);self.assertNotIn('android_profile_sha256',self.engine.workflow_snapshot)
        workflow.install(self.engine,self.confirmation());before=copy.deepcopy(inspect(self.engine))
        self.assertEqual(len(before['registry']['systems']),2)
        request=self.request();roots=[s['partition']['name'] for s in before['registry']['systems']]
        request.update(install_targets=['userdata',roots[0]],rom_path='DEMO',linux_bundle=str(self.engine.demo_bundle),default_target='userdata',set_default=True)
        workflow.prepare(self.engine,request);workflow.install(self.engine,self.confirmation())
        after=inspect(self.engine);default=next(s for s in after['registry']['systems'] if s['id']==after['registry']['default_id'])
        self.assertEqual(default['type'],'android');self.assertEqual(self.engine.receipt['installed_targets'][-1],'userdata')
        self.assertEqual(before['inventory'][roots[1]],after['inventory'][roots[1]])
        self.assertEqual(next(s for s in before['registry']['systems'] if s['partition']['name']==roots[1]),next(s for s in after['registry']['systems'] if s.get('partition',{}).get('name')==roots[1]))
        request=self.request();request.update(target='userdata',rom_path='DEMO')
        with self.assertRaisesRegex(InstallerError,'当前默认系统'):
            workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),after)
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_switch_installed_android_and_linux_without_reinstalling(self):
        workflow.prepare(self.engine,self.combined_request())
        workflow.install(self.engine,self.confirmation())
        original=inspect(self.engine)
        registry=original['registry']
        android=next(system for system in registry['systems'] if system['type']=='android')
        linux=next(system for system in registry['systems'] if system['id']==registry['default_id'])
        for target in (android,linux):
            before=inspect(self.engine)
            boot_switch.prepare(self.engine,dict(serial='DEMO-LIUQIN',snapshot_id=snapshot_id(before),system_id=target['id']))
            transport=self.engine.transport
            snapshot=self.engine.workflow_snapshot
            self.assertEqual(inspect(self.engine),before)
            backup=Path(snapshot['previous_boot_path'])
            self.assertEqual(backup.read_bytes(),self.engine.demo_live_boot)
            if target==android:
                rejected=self.confirmation()
                rejected['confirmation']['accept_operations']=False
                with self.assertRaises(InstallerError):workflow.install(self.engine,rejected)
                self.assertNotIn('native:switch-apply',transport.calls)
                self.assertFalse(self.engine.receipt['write_dispatched'])

                stored=self.engine.demo_android_scan['files'][target['image']]
                original_digest=stored['sha256']
                stored['sha256']='f'*64
                try:
                    with self.assertRaisesRegex(InstallerError,'登记在确认后改变'):
                        workflow.install(self.engine,self.confirmation())
                finally:
                    stored['sha256']=original_digest
                self.assertNotIn('native:switch-apply',transport.calls)
                self.assertFalse(self.engine.receipt['write_dispatched'])
                self.assertEqual(inspect(self.engine),before)

            workflow.install(self.engine,self.confirmation())
            after=inspect(self.engine)
            generation=target['generation'] if target['type']=='android' else target['target']['boot_generation']
            self.assertTrue(self.engine.receipt['boot_switch_completed'])
            self.assertEqual(transport.current_boot_digest(),snapshot['switch_plan']['target_boot_sha256'])
            self.assertEqual(after['inventory'],original['inventory'])
            self.assertEqual(after['registry'],dict(registry,default_id=target['id'],default_generation=generation))
            self.assertFalse(any(call.startswith(('partition-edit','install_root:','fastboot:')) for call in transport.calls))

    def test_keep_unidentified_boot_when_replacing_android_is_rejected(self):
        request=self.combined_request();request['default_target']=''
        before=inspect(self.engine)
        with self.assertRaisesRegex(InstallerError,'无法确认现有 boot_a'):
            workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),before)
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_linux_binding_is_verified_before_edits(self):
        request=self.linux_request();before=inspect(self.engine)
        with patch.object(workflow,'bind',side_effect=ValueError('invalid boot trailer')):
            with self.assertRaisesRegex(InstallerError,'引导镜像无法绑定'):
                workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),before)
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_live_store_space_is_rechecked_before_edits(self):
        workflow.prepare(self.engine,self.linux_request());workflow.install(self.engine,self.confirmation())
        before=inspect(self.engine);root=before['registry']['systems'][0]['partition']['name']
        request=self.request();request.update(target=root,linux_bundle=str(self.engine.demo_bundle),set_default=True)
        workflow.prepare(self.engine,request)
        transport=self.engine.transport
        self.engine.demo_android_scan['free_bytes']=0
        self.assertEqual(snapshot_id(inspect(self.engine)),snapshot_id(before))
        with self.assertRaisesRegex(InstallerError,'linux_boot.*空间不足'):
            workflow.install(self.engine,self.confirmation())
        self.assertNotIn('partition-edit',transport.calls)
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_all_materials_are_checked_before_joint_partition_edits(self):
        request=self.combined_request();request['linux_bundles'][request['default_target']]=str(self.root/'missing')
        before=copy.deepcopy(inspect(self.engine))
        with self.assertRaises(InstallerError):workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),before);self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_unselected_default_and_duplicate_targets_are_rejected(self):
        for change in (dict(default_target='lqr_123456789012'),dict(install_targets=['userdata','userdata'])):
            request=dict(self.combined_request(),**change)
            with self.assertRaises(InstallerError):workflow.prepare(self.engine,request)
            self.assertFalse(self.engine.receipt['write_dispatched'])
            self.engine.finish_cancel()

    def test_joint_install_failure_preserves_completed_child_and_never_defaults(self):
        request=self.combined_request();workflow.prepare(self.engine,request);original=self.engine.install;count=0
        def install(payload):
            nonlocal count
            count+=1
            if count==2:raise InstallerError('second system failed')
            return original(payload)
        with patch.object(self.engine,'install',side_effect=install):
            with self.assertRaisesRegex(InstallerError,'second system failed'):workflow.install(self.engine,self.confirmation())
        self.assertEqual(self.engine.receipt['installed_targets'],['userdata'])
        self.assertTrue(self.engine.receipt['android_rom_written']);self.assertTrue(self.engine.receipt['failed'])
        self.assertFalse(self.engine.receipt.get('boot_written'));self.assertTrue(self.engine.receipt['recovery_required'])
    def test_unqualified_destination_firmware_stops_before_partition_edits(self):
        request=self.request();request.update(target='userdata',rom_path='DEMO',set_default=True)
        old=inspect(self.engine)['inventory']['userdata']
        request['actions']=[dict(name='userdata',partuuid=old['partuuid'],action='delete')]
        request['partition_config']['userdata_gib']=32
        before=copy.deepcopy(inspect(self.engine))
        with patch.object(type(self.engine.transport),'validate_android_install',create=True,
                          side_effect=InstallerError('target firmware not qualified')):
            with self.assertRaisesRegex(InstallerError,'not qualified'):workflow.prepare(self.engine,request)
        self.assertEqual(inspect(self.engine),before)
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertNotIn('partition-edit',self.engine.transport.calls)
    def test_child_failure_after_partition_edits_is_a_failed_workflow(self):
        workflow.prepare(self.engine,self.linux_request())
        with patch.object(self.engine,'install',side_effect=InstallerError('child install refused')):
            with self.assertRaisesRegex(InstallerError,'child'):
                workflow.install(self.engine,self.confirmation())
        self.assertEqual(self.engine.state,'failed')
        self.assertTrue(self.engine.receipt['write_dispatched'])
        self.assertTrue(self.engine.receipt['failed'])
        self.assertFalse(self.engine.workflow_committing)

    def test_pending_edit_tampering_is_rejected(self):
        request=self.linux_request();state=inspect(self.engine)
        plan=plan_edit(state,request['actions'],request['partition_config'],'DEMO-LIUQIN',str(uuid.uuid4()))
        plan['deletes'][0]['name']='persist'
        with self.assertRaises(ValueError):validate_edit(plan,'DEMO-LIUQIN')

    def pending_initialization(self):
        request=self.linux_request();request['partition_config']['userdata_gib']=0
        workflow.prepare(self.engine,request)
        planned=self.engine.workflow_snapshot['edit'];state=copy.deepcopy(planned['projected'])
        state.update(registry=None,files={});state.pop('registry_sha256',None)
        for p in planned['partitions']:
            state['inventory'][p['name']].update(filesystem='unformatted',fsuuid=None)
        self.engine.demo_android_scan=state;self.engine.demo_partition_report=state['report']
        self.engine.receipt.update(failed=True,write_dispatched=True,gpt_written=True)
        self.engine.save_receipt();self.engine.state='failed'
        path=Path(self.engine.receipt['run_dir'])/'receipt.json'
        return dict(serial='DEMO-LIUQIN',backup_parent=str(self.root/'backups'),resume_receipt=str(path))

    def test_resume_initializes_exact_pending_store_without_repeating_gpt_or_os(self):
        request=self.pending_initialization()
        workflow.prepare_resume(self.engine,request);transport=self.engine.transport
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertIn('partition-resume-check',transport.calls)
        workflow.install(self.engine,self.confirmation())
        self.assertIn('partition-resume',transport.calls);self.assertNotIn('partition-edit',transport.calls)
        self.assertFalse(any(c.startswith(('install_root','fastboot:flash')) for c in transport.calls))
        state=inspect(self.engine);self.assertEqual(state['inventory']['linux_boot']['filesystem'],'ext4')
        self.assertEqual(state['registry']['rootfs_slots'][0]['status'],'reserved')
        with self.assertRaises((RuntimeError,InstallerError)):workflow.prepare_resume(self.engine,request)

    def test_resume_rejects_changed_guid_or_initialized_partition_before_write(self):
        request=self.pending_initialization()
        self.engine.demo_android_scan['inventory']['linux_boot']['partuuid']=str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError,'mismatch'):workflow.prepare_resume(self.engine,request)
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_resume_rejects_tampered_receipt_before_device_connection(self):
        request=self.pending_initialization();path=Path(request['resume_receipt'])
        receipt=json.loads(path.read_text());receipt['workflow']['previous_boot_sha256']='0'*64
        path.write_text(json.dumps(receipt))
        with patch.object(self.engine,'create_backup') as backup:
            with self.assertRaises(InstallerError):workflow.prepare_resume(self.engine,request)
            backup.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
