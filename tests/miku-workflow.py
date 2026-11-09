#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise Miku profile routing through both GUI Android installation paths."""
import copy,hashlib,importlib.util,json,sys,unittest
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('workflow_fixture',PROJECT/'tests/workflow.py');f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
from liuqin_gui import workflow
from liuqin_gui.partition_demo import inspect
from liuqin_partitions import snapshot_id
from liuqin_android import ANDROID_PARTITIONS,canonical
from liuqin_gui.engine import InstallerError
from unittest.mock import patch

class MikuWorkflowTests(f.WorkflowTests):
 for name in list(vars(f.WorkflowTests)):
  if name.startswith('test_'):locals()[name]=None
 def setUp(self):
  super().setUp();p=copy.deepcopy(self.engine.demo_android_profile)
  p.update(id='miku-blooming-26101322',rom='MikuUI-DEMO',shared_firmware=[],firmware_policy='preserve-existing')
  p['material_images']=['boot.img',*p['install_images']]
  self.engine.demo_android_profile=p
 def android_request(self):
  request=self.request();request.update(target='userdata',rom_path=str(self.engine.demo_android_rom),set_default=False)
  return request
 def test_managed_android_uses_selected_profile_and_preserves_linux(self):
  workflow.prepare(self.engine,self.linux_request());workflow.install(self.engine,self.confirmation())
  before=copy.deepcopy(self.engine.demo_android_scan)
  request=self.android_request();workflow.prepare(self.engine,request)
  self.assertFalse(self.engine.workflow_snapshot['direct_android'])
  self.assertEqual(self.engine.workflow_snapshot['android_profile_id'],'miku-blooming-26101322')
  self.assertEqual({p['partition']['name'] for p in self.engine.workflow_snapshot['direct_images']},ANDROID_PARTITIONS)
  self.assertEqual(set(self.engine.workflow_snapshot['source']),{'boot.img',*self.engine.demo_android_profile['install_images']})
  workflow.install(self.engine,self.confirmation())
  after=self.engine.demo_android_scan
  self.assertTrue(self.engine.receipt['completed'])
  self.assertEqual([s for s in before['registry']['systems'] if s['type']=='linux'],[s for s in after['registry']['systems'] if s['type']=='linux'])
  for path,record in before['files'].items():
   if path.startswith('systems/') and any(path.startswith('systems/'+s['id']+'/') for s in before['registry']['systems'] if s['type']=='linux'):self.assertEqual(after['files'][path],record)
  android=next(s for s in after['registry']['systems'] if s['type']=='android');self.assertEqual(android['rom'],'MikuUI-DEMO')
 def test_direct_android_uses_miku_boot_without_touching_recovery(self):
  before=copy.deepcopy(self.engine.demo_recovery_partitions)
  request=self.android_request();request['set_default']=True;workflow.prepare(self.engine,request)
  self.assertTrue(self.engine.workflow_snapshot['direct_android'])
  self.assertEqual(self.engine.workflow_snapshot['android_profile_id'],'miku-blooming-26101322')
  transport=self.engine.transport;workflow.install(self.engine,self.confirmation())
  self.assertIn('android-direct',transport.calls);self.assertNotIn('partition-edit',transport.calls)
  self.assertTrue(self.engine.receipt['completed'])
  self.assertFalse(any('flash:recovery' in call for call in transport.calls))
 def test_selected_profile_mutation_stops_before_writes(self):
  request=self.android_request();request['set_default']=True;workflow.prepare(self.engine,request)
  self.engine.android_profile['rom']='changed-after-confirmation'
  with self.assertRaises(InstallerError):workflow.install(self.engine,self.confirmation())
  self.assertFalse(self.engine.receipt['write_dispatched'])
 def test_late_recovery_failure_keeps_miku_boot_and_records_completed_rom(self):
  request=self.android_request();request['set_default']=True;workflow.prepare(self.engine,request)
  transport=self.engine.transport;expected=self.engine.android_profile['images']['boot.img']['sha256']
  with patch.object(transport,'boot_ram',side_effect=InstallerError('simulated Recovery verification failure')):
   with self.assertRaises(InstallerError):workflow.install(self.engine,self.confirmation())
  self.assertEqual(transport.current_boot_digest(),expected)
  flashes=[c for c in transport.calls if c.startswith('fastboot:flash boot_a ')]
  self.assertEqual(len(flashes),1)
  for key in ('android_rom_written','android_data_initialized','system_installed','boot_flash_started','boot_written','boot_finalization_pending'):
   self.assertTrue(self.engine.receipt[key],key)
  self.assertFalse(self.engine.receipt.get('boot_rollback_attempted',False))

if __name__=='__main__':unittest.main(verbosity=2)
