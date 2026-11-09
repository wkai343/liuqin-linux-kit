#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""ROM analysis never grants write authority; no hardware or network access."""
import copy,hashlib,importlib.util,json,socket,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'shared'),str(ROOT/'installer')]
from liuqin_android import load_profile,identify_profile,canonical
from liuqin_rom_admission import catalog,assessment,require_install,request_profile
from liuqin_rom_analysis import analyze,reference
from liuqin_rom_preview import build
from liuqin_gui.rom_preview import demo_capture
from liuqin_gui.android import select_profile
from liuqin_gui import host,native_profiles

def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  p=patch.object(socket,'socket',side_effect=AssertionError('network forbidden'));p.start();self.addCleanup(p.stop)
  p=patch.object(host,'run_process',side_effect=AssertionError('device access forbidden'));p.start();self.addCleanup(p.stop)
 def test_catalog_separates_analysis_and_experimental_install(self):
  self.assertEqual({r['id'] for r in catalog()},{'stock','miku-blooming-26101322','lineage-24-20261022'})
  for pid in ('stock','miku-blooming-26101322'):
   r=require_install(load_profile(pid));self.assertTrue(r['write_allowed']);self.assertFalse(r['hardware_install_verified'])
  self.assertFalse(assessment(load_profile('lineage-24-20261022'))['write_allowed'])
 def test_blocked_rom_cannot_accompany_any_write_rpc(self):
  p=load_profile('lineage-24-20261022');request=dict(profile_id=p['id'],profile_sha256=hashlib.sha256(canonical(p)).hexdigest())
  self.assertEqual(request_profile('partition-inspect',request),p)
  for action in ('stage','install','commit','android-direct','partition-edit','partition-resume','linux-stage','made-up-write'):
   with self.subTest(action=action),self.assertRaisesRegex(ValueError,'禁止写入'):request_profile(action,request)
  with self.assertRaises(ValueError):request_profile('partition-inspect',dict(request,profile_id='stock'))
 def test_device_operations_reject_before_opening_hardware(self):
  m=load('multi_rom_device',ROOT/'device/installer/android-device.py');device=Mock();op=m.Operations(device,load_profile('lineage-24-20261022'))
  for method,args in [('direct_install',({},'')),('guard',({},)),('stage',({},'')),('install',({},'')),('commit',({},{},''))]:
   with self.assertRaisesRegex(ValueError,'禁止写入'):getattr(op,method)(*args)
  self.assertEqual(device.mock_calls,[])
 def test_zip_identity_uses_content_not_name_or_size(self):
  p=self.root/'lineage-renamed.zip';p.write_bytes(b'good');profile=copy.deepcopy(load_profile('miku-blooming-26101322'))
  profile['archive'].update(bytes=4,sha256=hashlib.sha256(b'good').hexdigest())
  with patch('liuqin_rom_admission.catalog',return_value=[dict(id=profile['id'])]),patch('liuqin_android.load_profile',return_value=profile):
   self.assertEqual(identify_profile(p),profile['id']);p.write_bytes(b'evil')
   with self.assertRaises(ValueError):identify_profile(p)
 def test_lineage_preview_lists_firmware_and_blocks_execution(self):
  snapshot,rows=demo_capture('TEST');r=build(reference('lineage241022'),snapshot,rows)
  self.assertFalse(r['executable']);self.assertFalse(r['admission']['write_allowed'])
  self.assertFalse(r['rec_after']['requirements_satisfied']);self.assertTrue(any('ABL/XBL' in b for b in r['blockers']))
  self.assertNotIn('recovery_a',[p['partition'] for p in r['writes']]);self.assertIn('linux_boot',r['protected'])
 def test_unknown_full_ota_gets_diagnostics_not_a_write_plan(self):
  fixture=load('multi_rom_preview_fixture',ROOT/'tests/rom-preview.py');f=fixture.PreviewTests();f.setUp();self.addCleanup(f.tmp.cleanup)
  try:
   payload,_=f.make();analysis=analyze(payload.path);self.assertTrue(analysis['profile']['unadmitted'])
   snapshot,rows=demo_capture('TEST');r=build(analysis,snapshot,rows)
   self.assertFalse(r['executable']);self.assertNotIn('plan_id',r);self.assertEqual(r['rec_after']['status'],'unqualified-rom')
  finally:f.doCleanups()
 def test_host_material_selection_stops_blocked_rom_before_preparation(self):
  from types import SimpleNamespace
  e=SimpleNamespace(simulation=False,manifest={})
  with patch('liuqin_android.identify_profile',return_value='lineage-24-20261022'):
   with self.assertRaisesRegex(RuntimeError,'新版完整 ROM'):select_profile(e,'fixture.zip')
if __name__=='__main__':unittest.main(verbosity=2)
