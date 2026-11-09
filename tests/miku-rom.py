#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Full OTA admission, bounded extraction and platform-independent super assembly."""
import base64,bz2,copy,gzip,hashlib,json,lzma,os,struct,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_android import load_profile,identify_profile,ANDROID_PARTITIONS,verify_rom,validate_images
from liuqin_payload import metadata,extract,fields
from liuqin_super import build,layout
from liuqin_gui.android import import_rom
from liuqin_gui.engine import InstallerError

def sha(data):return hashlib.sha256(data).hexdigest()
def vint(n):
 b=bytearray()
 while n>127:b.append((n&127)|128);n>>=7
 b.append(n);return bytes(b)
def field(n,v):return vint(n*8+(2 if isinstance(v,bytes) else 0))+(vint(len(v))+v if isinstance(v,bytes) else vint(v))
def msg(*pairs):return b''.join(field(k,v) for k,v in pairs)

class PayloadTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.profile=copy.deepcopy(load_profile('miku-blooming-26101322'));self.profile['rom']='Miku-fixture'
  self.data={n:(n.encode()+b'\0').ljust(4096,bytes([65+i])) for i,n in enumerate(self.profile['ota']['partitions'])}
  self.path=self.root/'fixture.zip';self.cache=self.root/'cache'
 def make(self,defect=None):
  payload=bytearray();parts=[];expected={}
  for i,(name,data) in enumerate(self.data.items()):
   kind=(0,1,8)[i%3];packed=data if kind==0 else bz2.compress(data) if kind==1 else lzma.compress(data)
   ext=msg((1,0),(2,len(data)//4096));typ=4 if defect=='source-copy' and i==0 else kind
   op=msg((1,typ),(2,len(payload)),(3,len(packed)),(6,ext),(8,hashlib.sha256(packed).digest()))
   if defect=='overlap' and i==0:op+=field(6,ext)
   if defect=='data-corrupt' and i==0:packed=b'X'+packed[1:]
   payload.extend(packed);p=msg((1,name.encode()),(7,msg((1,len(data)),(2,hashlib.sha256(data).digest()))),(8,op))
   if name in ('system','vendor'):p+=msg((2,1),(9,0 if defect=='required-hook' else 1))
   parts.append(field(13,p));expected[name]=dict(bytes=len(data),sha256=sha(data))
  group=self.profile['super']['layout']['group'];logical=[p['name'] for p in self.profile['super']['layout']['partitions']]
  dynamic=msg((1,msg((1,group.encode()),(2,16*1024**2),*[(3,n.encode()) for n in logical])),(2,1))
  manifest=msg((3,4096),(12,1 if defect=='incremental' else 0),(15,dynamic))+b''.join(parts)
  header=b'CrAU'+struct.pack('>QQI',2,len(manifest),0);body=header+manifest+payload
  props='FILE_SIZE='+str(len(body))+'\nFILE_HASH='+base64.b64encode(hashlib.sha256(body).digest()).decode()+'\nMETADATA_SIZE='+str(len(header+manifest))+'\nMETADATA_HASH='+base64.b64encode(hashlib.sha256(header+manifest).digest()).decode()+'\n'
  with zipfile.ZipFile(self.path,'w') as z:
   z.writestr('payload.bin',body);z.writestr('payload_properties.txt',props)
   z.writestr('META-INF/com/android/metadata','pre-device='+('pipa' if defect=='device' else 'liuqin')+'\nota-type=AB\npost-build='+self.profile['ota']['fingerprint']+'\npost-sdk-level=36\n')
   z.writestr('../../escape.sh',b'never execute or extract')
  self.profile['archive'].update(bytes=self.path.stat().st_size,sha256=sha(self.path.read_bytes()))
  self.profile['ota']['partitions']=expected
  self.profile['super']['layout'].update(bytes=32*1024**2,group_bytes=16*1024**2,partitions=[dict(name=n,**expected[n]) for n in logical])
  reference=self.root/'reference';reference.mkdir(exist_ok=True)
  for n,data in self.data.items():(reference/(n+'.img')).write_bytes(data)
  sp=reference/'super.img'
  if sp.exists():sp.unlink()
  ranges=build(reference,sp,self.profile['super']['layout'])
  for name,entry in self.profile['images'].items():
   p=sp if name=='super.img' else reference/name
   entry.update(bytes=p.stat().st_size,sha256=sha(p.read_bytes()))
  self.profile['super'].update(source_sha256=self.profile['images']['super.img']['sha256'],expanded_bytes=sp.stat().st_size,verification_ranges=ranges)
 def test_zip_import_has_only_admitted_materials_and_keeps_recovery_out(self):
  self.make();result=import_rom(self.path,self.profile,self.cache,lambda:None)
  self.assertEqual(set(result),set(self.profile['material_images']));self.assertNotIn('recovery.img',result)
  destination=self.cache/self.profile['rom'];self.assertEqual(set(p.name for p in destination.iterdir()),set(result))
  self.assertFalse((self.root/'escape.sh').exists());self.assertEqual(import_rom(self.path,self.profile,self.cache,lambda:None),result)
  self.assertEqual({self.profile['images'][n]['partition'] for n in self.profile['install_images']},ANDROID_PARTITIONS)
 def test_changed_archive_and_changed_cache_are_rejected(self):
  self.make();p=copy.deepcopy(self.profile);p['archive']['sha256']='0'*64
  with self.assertRaises(InstallerError):import_rom(self.path,p,self.cache,lambda:None)
  result=import_rom(self.path,self.profile,self.cache,lambda:None);Path(result['boot.img']).write_bytes(b'changed')
  with self.assertRaises(ValueError):import_rom(self.path,self.profile,self.cache,lambda:None)
 def test_incremental_wrong_device_source_copy_overlap_and_hook_are_rejected(self):
  for defect in ('incremental','device','source-copy','overlap','required-hook','data-corrupt'):
   self.make(defect)
   with self.subTest(defect=defect),self.assertRaises((ValueError,InstallerError)):import_rom(self.path,self.profile,self.cache,lambda:None)
   self.assertFalse((self.cache/self.profile['rom']).exists())
 def test_super_metadata_and_ranges_are_bounded(self):
  self.make();spec=self.profile['super']['layout'];prefix,ranges=layout(spec)
  self.assertEqual(len(prefix),405504)
  for i in range(6):self.assertEqual(prefix[12288+i*65536:12288+(i+1)*65536],prefix[12288:12288+65536])
  bad=copy.deepcopy(spec);bad['bytes']=1024**2
  with self.assertRaises(ValueError):layout(bad)
  bad=copy.deepcopy(spec);bad['partitions'][0]['name']='../../userdata'
  with self.assertRaises(ValueError):layout(bad)
 def test_metadata_decoder_rejects_truncation_and_unknown_profile(self):
  for data in (b'\x80',b'\x0a\x04x',b'\x00'):
   with self.assertRaises(ValueError):fields(data)
  with self.assertRaises(ValueError):load_profile('../../untrusted-profile')
  with self.assertRaises(ValueError):identify_profile(self.path)

if __name__=='__main__':unittest.main(verbosity=2)
