#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Firmware identity, exact routing and bounded read-only capture regressions."""
import copy,hashlib,importlib.util,json,struct,sys,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_firmware import load_catalog,validate_snapshot,fingerprint_id,match_slot,ROLES,LAYOUT
from liuqin_gui import firmware,native_profiles as routes,compatibility
from liuqin_gui.engine import InstallerError,Cancelled,cli
PROFILE=copy.deepcopy(load_catalog()[0]);IMAGE=PROFILE['artifacts']['temporary']
PROBE=dict(device='liuqin',serial='tablet',boot_id=str(uuid.uuid4()),build_id=IMAGE['build_id'],slot='a',unlocked=True,sealed=True,kernel='test')
def snapshot():
 m=copy.deepcopy(PROFILE['match']);s=dict(status='complete',match=m,fingerprint_id=fingerprint_id(m))
 return dict(schema=1,device='liuqin',serial='tablet',boot_id=PROBE['boot_id'],runtime_build_id=PROBE['build_id'],active_slot='a',unlocked=True,sealed=True,slots={'a':copy.deepcopy(s),'b':copy.deepcopy(s)})
def rec(path='/chosen.img'):return dict(path=path,image_sha256=IMAGE['image_sha256'],build_id=IMAGE['build_id'])
def rows():
 result=[]
 for slot in ('a','b'):
  for role in (*ROLES,*LAYOUT):
   i=len(result)+1;size=PROFILE['match'][role]['partition_bytes'] if role in ROLES else PROFILE['match']['layout'][role]
   result.append(dict(name=role+'_'+slot,node='sde'+str(i),dev='8:'+str(i),size=size,start=i*4096,ro='1'))
 return result
class Remote:
 def __init__(self,before=None,after=None):self.before=rows() if before is None else before;self.after=self.before if after is None else after;self.count=0;self.commands=[]
 def __call__(self,cmd,timeout=0):
  self.commands.append(cmd)
  cli.frame_command('test "$(cat /proc/sys/kernel/random/boot_id)" = '+PROBE['boot_id']+' && '+cmd,'LIUQIN_'+'1'*32+'_START','LIUQIN_'+'1'*32+'_END')
  if cmd==firmware.command(firmware.INVENTORY_CODE):self.count+=1;return json.dumps(self.before if self.count==1 else self.after).encode()
  row=next(r for r in self.before if '"/dev/'+r['node']+'"' in cmd);role=row['name'].rsplit('_',1)[0]
  self.assert_readonly(cmd)
  if 'os.read(f,32)' in cmd:return struct.pack('>8I',0xd7b7ab1e,PROFILE['match']['dtbo']['hash_bytes'],32,32,38,32,4096,0).hex().encode()
  if 'h.hexdigest()' in cmd:return PROFILE['match'][role]['sha256'].encode()
  raise AssertionError('unexpected command '+cmd)
 def assert_readonly(self,cmd):
  assert 'os.O_RDONLY|os.O_NOFOLLOW' in cmd and 'stat.S_ISBLK' in cmd
  assert not any(s in cmd for s in ['O_WRONLY','O_RDWR','os.write','reboot','flash','mkfs','sgdisk'])
def reader(remote):return SimpleNamespace(serial='tablet',engine=SimpleNamespace(check_cancel=Mock()),remote=remote)
class FirmwareTests(unittest.TestCase):
 def test_complete_capture_and_os_independent_fingerprints(self):
  remote=Remote();result=firmware.collect(reader(remote),PROBE,lambda:dict(PROBE))
  self.assertEqual(result['slots']['a']['match'],PROFILE['match']);self.assertEqual(result['slots']['a']['fingerprint_id'],result['slots']['b']['fingerprint_id'])
  for os_name in ('MIUI','HyperOS','Fedora'):
   value=copy.deepcopy(result);value['display_os']=os_name;self.assertEqual(match_slot(value,'a',[PROFILE])['status'],'matched')
  self.assertEqual(remote.count,2)
 def test_missing_b_slot_does_not_invent_hashes(self):
  remote=Remote(before=[r for r in rows() if r['name']!='abl_b']);value=firmware.collect(reader(remote),PROBE,lambda:dict(PROBE))
  self.assertEqual(value['slots']['a']['status'],'complete');self.assertEqual(value['slots']['b']['status'],'incomplete');self.assertNotIn('fingerprint_id',value['slots']['b'])
 def test_node_replacement_reboot_and_seal_change_discard_capture(self):
  changed=rows();changed[0]['node']='sdf1';changed[0]['dev']='9:1'
  for remote,after in [(Remote(after=changed),PROBE),(Remote(),dict(PROBE,boot_id=str(uuid.uuid4()))),(Remote(),dict(PROBE,sealed=False))]:
   with self.assertRaises(InstallerError):firmware.collect(reader(remote),PROBE,lambda:after)
 def test_duplicate_partition_and_writable_storage_rejected(self):
  for change in ('duplicate','writable','injected_node'):
   bad=rows()
   if change=='duplicate':bad.append(copy.deepcopy(bad[0]))
   elif change=='writable':bad[0]['ro']='0'
   else:bad[0]['node']='sde1; reboot'
   with self.assertRaises(InstallerError):firmware.inventory(Remote(before=bad))
 def test_cancel_does_not_become_an_incomplete_fingerprint(self):
  remote=Remote();t=reader(remote);t.engine.check_cancel.side_effect=Cancelled('cancel')
  with self.assertRaises(Cancelled):firmware.collect(t,PROBE,lambda:PROBE)
  self.assertEqual(len(remote.commands),1)
 def test_bad_dtbo_is_incomplete_not_guessed(self):
  remote=Remote();base=remote.__call__
  def bad(cmd,**kw):
   if 'os.read(f,32)' in cmd:return bytes(32).hex().encode()
   return base(cmd,**kw)
  value=firmware.collect(reader(bad),PROBE,lambda:PROBE);self.assertEqual(value['slots']['a']['status'],'incomplete')
 def test_avb_metadata_is_bound_to_full_partition_and_fits_transport(self):
  row=next(r for r in rows() if r['name']=='vbmeta_a');body=bytearray(256);body[:4]=b'AVB0';struct.pack_into('>II',body,4,1,0);struct.pack_into('>I',body,120,3)
  digest=hashlib.sha256(body+bytes(row['size']-len(body))).hexdigest()
  def read(cmd,**kw):
   Remote().assert_readonly(cmd)
   cli.frame_command('test "$(cat /proc/sys/kernel/random/boot_id)" = '+PROBE['boot_id']+' && '+cmd,'LIUQIN_'+'1'*32+'_START','LIUQIN_'+'1'*32+'_END')
   self.assertIn('n='+str(row['size']),cmd);self.assertIn('[:65536]',cmd)
   return (digest+':'+body.hex()).encode()
  result=firmware.avb_metadata(read,row,digest)
  self.assertEqual(result['flags'],3);self.assertEqual(result['partition_sha256'],digest);self.assertEqual(result['metadata_bytes'],256)
  with self.assertRaises(firmware.FirmwareChanged):firmware.avb_metadata(read,row,'f'*64)
 def test_avb_read_bounds_and_change_abort_capture(self):
  row=next(r for r in rows() if r['name']=='vbmeta_a');digest=PROFILE['match']['vbmeta']['sha256']
  for response in (b'x'*140001,b'not-a-measurement',(digest+':00').encode()):
   with self.assertRaises((InstallerError,ValueError)):firmware.avb_metadata(lambda *a,**kw:response,row,digest)
  remote=Remote()
  with patch.object(firmware,'avb_metadata',side_effect=firmware.FirmwareChanged('changed')):
   with self.assertRaises(firmware.FirmwareChanged):firmware.collect(reader(remote),PROBE,lambda:PROBE)
 def test_exact_match_selects_artifact_not_filename(self):
  path,status=routes.selection(snapshot(),'/wrong.img',[rec('/right.img')],[PROFILE])
  self.assertEqual(path,'/right.img');self.assertTrue(status['auto_selected']);self.assertFalse(status['fixed_install_allowed']);self.assertFalse(status['fixed_candidate']['install_allowed'])
 def test_unknown_mismatch_missing_artifact_and_unverified_slot(self):
  for case in ('unknown','mismatch','missing','slot_b'):
   value=snapshot();assets=[rec()]
   if case in ('unknown','mismatch'):
    m=value['slots']['a']['match'];m['abl' if case=='unknown' else 'dtbo']['sha256']='f'*64;value['slots']['a']['fingerprint_id']=fingerprint_id(m)
   elif case=='missing':assets=[]
   else:value['active_slot']='b'
   _,status=routes.selection(value,'/current.img',assets,[PROFILE]);self.assertFalse(status['auto_selected'],case);self.assertFalse(status['automatic_boot_allowed']);self.assertFalse(status['fixed_install_allowed'])
 def test_snapshot_session_binding_and_digest_tampering(self):
  value=snapshot()
  for kwargs in [dict(serial='other'),dict(boot_id=str(uuid.uuid4())),dict(build_id='f'*64)]:
   with self.assertRaises(ValueError):validate_snapshot(value,**kwargs)
  bad=copy.deepcopy(value);bad['slots']['a']['match']['abl']['sha256']='e'*64
  with self.assertRaises(ValueError):validate_snapshot(bad)
  env=dict(kind='native-recovery',serial='tablet',properties=dict(boot_id=str(uuid.uuid4()),build_id=PROBE['build_id']),firmware=value)
  with self.assertRaises(ValueError):compatibility.select_image(env,'/current.img')
 def test_catalog_rejects_ambiguous_or_unqualified_profiles(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'catalog.json'
   samples=[[PROFILE,dict(PROFILE,id='duplicate-content')]]
   bad=copy.deepcopy(PROFILE);bad['artifacts']['temporary']['validation']='DEVICE_UNTESTED';samples.append([bad])
   bad=copy.deepcopy(PROFILE);del bad['match']['abl'];samples.append([bad])
   for profiles in samples:
    p.write_text(json.dumps(dict(schema=1,profiles=profiles)))
    with self.assertRaises(ValueError):load_catalog(p)
 def test_fixed_header_is_not_a_temporary_candidate(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'recovery.img';data=bytearray(4096);data[:8]=b'ANDROID!';struct.pack_into('<I',data,40,0);p.write_bytes(data)
   self.assertIsNone(routes.native_image(p))
 def test_native_variant_package_contains_routing_data_and_bound_images(self):
  import gzip,zipfile
  from liuqin_recovery_image import records,pack,MARKER
  def load(name,path):
   spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
  fixture=load('native_firmware_fixture',PROJECT/'tests/native-recovery.py')
  packager=load('native_firmware_packager',PROJECT/'installer/package.py')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);first=root/'first.img';second=root/'second.img';sources=fixture.adb_package_fixture(first);fixture.adb_package_fixture(second)
   # Distinct valid image bytes exercise digest-based directory discovery.
   second.write_bytes(second.read_bytes()+bytes(4096))
   out=packager.package(root/'kit.zip',native_recovery=first,native_variants=[second],adbd_sources=sources)
   with zipfile.ZipFile(out) as z:
    prefix=packager.INSTALLER_ARCHIVE_ROOT+'/';digest=hashlib.sha256(second.read_bytes()).hexdigest()
    self.assertEqual(z.read(prefix+'installer/runtime/native-recovery.img'),first.read_bytes())
    self.assertEqual(z.read(prefix+'installer/runtime/native-recoveries/'+digest+'/recovery.img'),second.read_bytes())
    self.assertIn(prefix+'shared/liuqin-native-recovery-profiles.json',z.namelist())
    self.assertIn(prefix+'docs/RECOVERY-FIRMWARE-PROFILES.md',z.namelist())
   with self.assertRaises(RuntimeError):packager.package(root/'invalid.zip',native_variants=[second])
 def test_host_snapshot_is_new_private_file_and_not_cached_qualification(self):
  with tempfile.TemporaryDirectory() as d:
   e=SimpleNamespace(state_dir=Path(d));a=Path(routes.save_snapshot(e,snapshot()));b=Path(routes.save_snapshot(e,snapshot()))
   self.assertNotEqual(a,b);self.assertEqual(a.stat().st_mode&0o777,0o600);validate_snapshot(json.loads(a.read_text()))
if __name__=='__main__':unittest.main(verbosity=2)
