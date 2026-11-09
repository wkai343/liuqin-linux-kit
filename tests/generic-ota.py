#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generic OTA contracts, preserved firmware and session binding; no hardware."""
import copy,hashlib,json,struct,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'shared'),str(ROOT/'installer')]
from liuqin_android import load_profile,canonical
from liuqin_ota import validate,blockers,requirements,assess,require_device
from liuqin_rom_admission import require_install,request_profile
import liuqin_native_install as gate
from liuqin_gui import host,native_profiles
from liuqin_gui.rom_preview import demo_capture

def fixture():
 p=copy.deepcopy(load_profile('lineage-24-20261022'));p.update(schema_version=2,generic_ota_version=1,id='ota-'+p['archive']['sha256'][:32],rom='OTA-'+p['archive']['sha256'][:16])
 p['ota'].update(payload_sha256='a'*64,metadata_sha256='b'*64)
 p['boot_contract']=dict(boot_header=4,vendor_header=4,data_filesystems={'/data':'f2fs','/metadata':'ext4'},vbmeta=dict(flags=3,rollback_index=0,metadata_sha256='c'*64),vbmeta_system=dict(flags=0,rollback_index=100,metadata_sha256='d'*64))
 return p

def hardware(p):
 rows={r['partition']:dict(name=r['partition'],bytes=r['bytes']) for r in requirements(p)}
 rows.update({r['partition']:dict(name=r['partition'],bytes=r['bytes']) for r in p['images'].values()})
 rows.update(userdata=dict(name='userdata',bytes=32*1024**3),metadata=dict(name='metadata',bytes=64*1024**2))
 rows.update({n+'_a':dict(name=n+'_a',bytes=131072) for n in ('vbmeta','vbmeta_system')})
 def measure(part,n):return next(r['sha256'] for r in requirements(p) if r['partition']==part['name'] and r['bytes']==n)
 def read(part,n):
  b=bytearray(256);b[:4]=b'AVB0';struct.pack_into('>Q',b,112,0);return bytes(b)
 return rows,measure,read
class Tests(unittest.TestCase):
 def setUp(self):
  self.p=fixture();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  guard=patch.object(host,'run_process',side_effect=AssertionError('hardware forbidden'));guard.start();self.addCleanup(guard.stop)
 def test_generic_eligibility_does_not_use_a_rom_name_catalog(self):
  with patch('liuqin_rom_admission.catalog',side_effect=AssertionError('ROM whitelist forbidden')):
   self.assertEqual(validate(self.p),self.p);r=require_install(self.p);self.assertEqual(r['status'],'conditional');self.assertFalse(r['hardware_install_verified'])
 def test_profile_cannot_expand_partition_or_format_scope(self):
  for change in ('target','persist','format','super','missing-core','identity'):
   p=copy.deepcopy(self.p)
   if change=='target':p['images']['super.img']['partition']='abl_a'
   elif change=='persist':p['ota']['partitions']['persist']=dict(bytes=4096,sha256='e'*64)
   elif change=='format':p['userdata']['filesystem']='ext4'
   elif change=='super':p['super']['layout']['group_bytes']+=4096
   elif change=='missing-core':del p['ota']['partitions']['boot']
   else:p['id']='ota-'+'0'*32
   with self.subTest(change=change),self.assertRaises(ValueError):validate(p)
 def test_missing_firmware_or_unqualified_avb_stays_blocked(self):
  p=copy.deepcopy(self.p);del p['ota']['partitions']['abl'];p['ota']['ignored_partitions'].remove('abl')
  self.assertTrue(any('abl' in b for b in blockers(p)))
  with self.assertRaises(ValueError):require_install(p)
  p=copy.deepcopy(self.p);p['boot_contract']['vbmeta']['flags']=0
  with self.assertRaises(ValueError):require_install(p)
  self.assertEqual(p['boot_contract']['vbmeta']['flags'],0)
 def test_firmware_dtbo_and_rollback_differences_block_before_writes(self):
  rows,measure,read=hardware(self.p);result=assess(self.p,rows,measure,read);self.assertTrue(result['compatible'])
  for name in ('abl_a','dtbo_a','keymaster_a'):
   r=assess(self.p,rows,lambda part,n:'0'*64 if part['name']==name else measure(part,n),read)
   self.assertFalse(r['compatible']);self.assertTrue(any(name in b for b in r['blockers']))
  missing=copy.deepcopy(rows);del missing['xbl_a'];self.assertFalse(assess(self.p,missing,measure,read)['compatible'])
  def higher(part,n):
   b=bytearray(read(part,n));struct.pack_into('>Q',b,112,101);return bytes(b)
  self.assertFalse(assess(self.p,rows,measure,higher)['compatible'])
  h=Mock();h.inventory.return_value=rows;h.digest.side_effect=lambda part,off,n:'0'*64;h.read.side_effect=lambda part,off,n:read(part,n)
  with self.assertRaises(RuntimeError):require_device(self.p,h)
  h.writable.assert_not_called();h.write.assert_not_called();h.format.assert_not_called()
 def test_inline_profile_requires_native_runtime_and_matching_confirmed_scope(self):
  digest=hashlib.sha256(canonical(self.p)).hexdigest();r=dict(profile_id=self.p['id'],profile_sha256=digest,generated_profile=self.p,native_token='token')
  for m in (None,dict(generic_ota_version=1,inspection_only=True),dict(inspection_only=False)):
   with self.assertRaises(ValueError):request_profile('android-check',r,m)
  m=dict(generic_ota_version=1,inspection_only=False)
  self.assertEqual(request_profile('android-check',r,m),self.p)
  for bound in ('0'*64,digest):
   with patch.object(gate,'require',return_value=dict(scope={'android_profile_sha256':bound})):
    if bound==digest:self.assertEqual(request_profile('android-direct',r,m),self.p)
    else:
     with self.assertRaises(ValueError):request_profile('android-direct',r,m)
 def test_unknown_firmware_only_allows_conditional_generic_ota_preparation(self):
  from liuqin_firmware import fingerprint_id
  s,_=demo_capture('TEST');slot=s['slots']['a'];slot['match']['abl']['sha256']='f'*64;slot['measured']=copy.deepcopy(slot['match']);slot['fingerprint_id']=fingerprint_id(slot['match'])
  m=dict(installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,usage='temporary-boot-only',generic_ota_version=1)
  status=native_profiles.installer_status(s,m);self.assertTrue(status['ota_only']);self.assertTrue(status['install_allowed']);self.assertFalse(status['automatic_boot_allowed'])
 def test_new_android_metadata_does_not_disable_linux_on_same_boot_firmware(self):
  s,_=demo_capture('TEST');s['slots']['a']['avb']['normalized_flags_sha256']='f'*64
  m=dict(installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,usage='temporary-boot-only',generic_ota_version=1)
  status=native_profiles.installer_status(s,m);self.assertTrue(status['install_allowed']);self.assertFalse(status.get('ota_only',False));self.assertFalse(status['automatic_boot_allowed'])
 def test_new_session_binds_android_materials_and_forbids_combined_gpt(self):
  p=self.p;boot=str(uuid.uuid4());serial='TEST';bid='a'*64
  m=dict(installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,usage='temporary-boot-only',generic_ota_version=1,build_id=bid)
  for name,data in [(gate.MARKER,json.dumps(m)),('proc/sys/kernel/random/boot_id',boot)]:
   target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(data)
  scope=dict(kind='android',parts=[],gpt_sha256=None,previous_boot_sha256='b'*64,android_profile_sha256=gate.digest(p),android_boot_sha256=p['images']['boot.img']['sha256'])
  request=dict(token='c'*32,plan_id='d'*64,boot_id=boot,serial=serial,build_id=bid,scope=scope,android_profile=p)
  probe=dict(device='liuqin',unlocked=True,sealed=True,slot='a',build_id=bid,boot_id=boot,serial=serial)
  for key in ('android_profile_sha256','android_boot_sha256','gpt_sha256'):
   bad=copy.deepcopy(request);bad['scope'][key]='e'*64
   with self.assertRaises(ValueError):gate.begin(bad,probe,self.root)
  self.assertEqual(gate.begin(request,probe,self.root)['state'],'active')
 def test_super_verification_rejects_logical_and_unused_area_changes(self):
  from liuqin_super import build,verify,layout
  source=self.root/'system.img';source.write_bytes(b'S'*4096)
  spec=dict(bytes=3*1024**2,group='qti_dynamic_partitions',group_bytes=2*1024**2,partitions=[dict(name='system',bytes=4096,sha256=hashlib.sha256(source.read_bytes()).hexdigest())])
  target=self.root/'super.img';build(self.root,target,spec);self.assertEqual(verify(target,spec),hashlib.sha256(target.read_bytes()).hexdigest())
  for offset in (16,1024**2,2*1024**2):
   with target.open('r+b') as f:f.seek(offset);old=f.read(1);f.seek(offset);f.write(b'X')
   with self.assertRaises(ValueError):verify(target,spec)
   with target.open('r+b') as f:f.seek(offset);f.write(old)
if __name__=='__main__':unittest.main(verbosity=2)
