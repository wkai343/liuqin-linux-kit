#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Classify sparse public ROM samples without embedding or capturing images."""
from pathlib import Path
import hashlib,json,subprocess,tempfile,random,unittest
PROJECT=Path(__file__).resolve().parents[1]
B=PROJECT/'tests/fixtures/boot-logo'
class Logo(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  manifest=json.loads((B/'manifest.json').read_text())
  for name,digest in manifest['files'].items():
   data=(B/name).read_bytes()
   assert len(data)==224 and hashlib.sha256(data).hexdigest()==digest,name
  cls.tmp=tempfile.TemporaryDirectory();cls.exe=Path(cls.tmp.name)/'logo-test'
  subprocess.run(['cc','-Wall','-Wextra','-Werror','-DLOGO_FIXTURE_TEST',str(PROJECT/'device/boot/splash/logo-orientation.c'),'-o',str(cls.exe)],check=True)
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()

 def run_values(self,v):
  with tempfile.NamedTemporaryFile(dir=self.tmp.name) as f:
   f.write(v);f.flush();return subprocess.run([str(self.exe),f.name],capture_output=True,text=True)
 def test_all_four_oem_directions_locked_and_unlocked(self):
  for angle in [0,90,180,270]:
   for idx in [0,1]:
    with self.subTest(angle=angle,index=idx):
     r=self.run_values((B/f'oem-{angle}-{idx}.bin').read_bytes());self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),str(angle))
 def test_fastboot_and_destroyed_screens_do_not_guess(self):
  for p in B.glob('oem-*.bin'):
   if p.stem.rsplit('-',1)[1] in ['2','3']:
    with self.subTest(p=p.name):self.assertNotEqual(self.run_values(p.read_bytes()).returncode,0)
 def test_blank_solid_midgrey_noise_and_bad_sizes(self):
  n=224;rng=random.Random(771)
  for v in [bytes(n),bytes([255])*n,bytes([127])*n,bytes([128])*n,bytes(rng.randrange(256) for _ in range(n)),bytes(n-1),bytes(n+1)]:
   self.assertNotEqual(self.run_values(v).returncode,0)
 def test_bounded_corruption_and_confidence(self):
  for angle in [0,90,180,270]:
   v=bytearray((B/f'oem-{angle}-1.bin').read_bytes())
   for i in range(8):v[i]=255-v[i]
   r=self.run_values(v);self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),str(angle))
   v[8]=255-v[8];self.assertNotEqual(self.run_values(v).returncode,0)
if __name__=='__main__':unittest.main()
