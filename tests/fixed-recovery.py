#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline fixed-image format, signature, payload and GUI-rejection checks."""
import copy,gzip,hashlib,json,shutil,struct,subprocess,sys,tempfile,unittest
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
from liuqin_fixed_recovery import LIMIT,USAGE,encoded,sha,align,pack_v0,kernel_and_dtb,avb_info,inspect
from liuqin_native_recovery import MARKER,AGENT,BOARD_HELPER,UI_FILES,TOUCH_FILES
from liuqin_recovery_image import pack,add
from liuqin_artifacts import boot_interface
from liuqin_gui.recovery import inspect_image
from liuqin_gui.engine import InstallerError

class FixedTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  if not shutil.which('openssl'):raise unittest.SkipTest('openssl required for ephemeral test signatures')
  cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.key=cls.root/'test.pem'
  subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(cls.key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  text=subprocess.check_output(['openssl','rsa','-in',str(cls.key),'-modulus','-noout'],text=True)
  cls.n=int(text.strip().split('=')[1],16)
  cls.pub=struct.pack('>II',2048,(-pow(cls.n,-1,2**32))%(2**32))+cls.n.to_bytes(256,'big')+pow(2,4096,cls.n).to_bytes(256,'big')
 @classmethod
 def tearDownClass(cls):cls.temp.cleanup()
 def fixture(self,usage=USAGE,flags=0):
  kernel=b'Linux test ARM64 kernel\0'*100;dtb=b'\xd0\x0d\xfe\xed'+bytes(36)+b'xiaomi,liuqin\0';dtb=dtb[:4]+struct.pack('>I',len(dtb))+dtb[8:]
  profile=dict(device='liuqin',partition_bytes=LIMIT,unlocked_required=True,boot_header_version=0,kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),recovery_rollback_index=1)
  entries={}
  for name in ('init',AGENT,BOARD_HELPER,*UI_FILES,*TOUCH_FILES):add(entries,name,('fixture '+name).encode(),0o100755)
  add(entries,'etc/liuqin-storage-mode',b'readonly\n')
  payload={name:sha(value[1]) for name,value in entries.items() if name in ('init',AGENT,BOARD_HELPER,*UI_FILES,*TOUCH_FILES)}
  identity=dict(kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),payload_sha256=payload,fixed_profile=profile,avb_public_key_sha256=sha(self.pub))
  m=dict(schema=1,interface='liuqin-native-recovery-v1',device='liuqin',build_id=sha(encoded(identity)),usage=usage,status='DEVICE_UNTESTED',inspection_only=True,
   kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),payload_sha256=payload,board_identity_version=1,ui_version=1,touch_version=1,
   fixed_version=1,fixed_boot_header=0,fixed_profile=profile,identity=identity)
  add(entries,MARKER,encoded(m));header=bytearray(4096);header[:8]=b'ANDROID!';struct.pack_into('<I',header,36,4096)
  raw=pack_v0(gzip.compress(kernel,mtime=0),gzip.compress(pack(entries),mtime=0),dtb,header)
  return self.sign_raw(raw,flags)
 def sign_raw(self,raw,flags=0):
  salt=b's'*32;name=b'recovery';body=struct.pack('>Q',len(raw))+b'sha256'.ljust(32,b'\0')+struct.pack('>4I',len(name),len(salt),32,0)+bytes(60)+name+salt+hashlib.sha256(salt+raw).digest()
  body+=bytes(-len(body)%8);desc=struct.pack('>QQ',2,len(body))+body;aux=self.pub+desc;aux+=bytes(-len(aux)%64)
  h=bytearray(256);h[:4]=b'AVB0';struct.pack_into('>IIQQI',h,4,1,0,320,len(aux),1)
  for off,value in [(32,0),(40,32),(48,32),(56,256),(64,0),(72,len(self.pub)),(96,len(self.pub)),(104,len(desc)),(112,1)]:struct.pack_into('>Q',h,off,value)
  struct.pack_into('>I',h,120,flags)
  material=bytes(h)+aux;(self.root/'signed.bin').write_bytes(material)
  sig=subprocess.check_output(['openssl','dgst','-sha256','-sign',str(self.key),str(self.root/'signed.bin')])
  meta=bytes(h)+hashlib.sha256(material).digest()+sig+bytes(32)+aux
  result=bytearray(LIMIT);result[:len(raw)]=raw;result[len(raw):len(raw)+len(meta)]=meta
  result[-64:]=struct.pack('>4sIIQQQ',b'AVBf',1,0,len(raw),len(raw),len(meta))+bytes(28)
  path=self.root/'candidate.img';path.write_bytes(result);return path,result
 def test_round_trip_and_non_oem_trust(self):
  path,_=self.fixture();r=inspect(path);self.assertTrue(r['avb']['self_signature_verified']);self.assertFalse(r['avb']['oem_trust_verified']);self.assertEqual(r['manifest']['usage'],USAGE)
 def test_tampered_payload_or_signature_is_rejected(self):
  _,b=self.fixture()
  for offset in [4096,struct.unpack_from('>Q',b,len(b)-64+20)[0]+256+40]:
   bad=bytearray(b);bad[offset]^=1
   with self.assertRaises(ValueError):avb_info(bad)
 def test_footer_bounds_and_verification_flags_are_rejected(self):
  _,b=self.fixture();bad=bytearray(b);struct.pack_into('>Q',bad,len(bad)-64+20,LIMIT+4096)
  with self.assertRaises(ValueError):avb_info(bad)
  with self.assertRaises(ValueError):avb_info(b[:-1])
  _,disabled=self.fixture(flags=2)
  with self.assertRaises(ValueError):avb_info(disabled)
 def test_wrong_role_is_rejected_even_when_signed(self):
  path,_=self.fixture(usage='temporary-boot-only')
  with self.assertRaises(ValueError):inspect(path)
 def test_gui_does_not_make_fixed_candidate_eligible(self):
  path,_=self.fixture()
  with self.assertRaises(ValueError):boot_interface(path,installer=True)
  with self.assertRaises(InstallerError):inspect_image(path)
 def test_gzip_tail_and_oversized_kernel_are_rejected(self):
  for b in [gzip.compress(b'kernel'),gzip.compress(b'kernel')+b'not a dtb']:
   with self.assertRaises(ValueError):kernel_and_dtb(b)
if __name__=='__main__':unittest.main()
