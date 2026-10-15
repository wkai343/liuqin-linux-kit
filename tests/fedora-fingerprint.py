#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline FPC kernel identity checks. No fingerprint hardware or credentials."""
import hashlib,importlib.util,sys,tempfile,unittest
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'shared'))
spec=importlib.util.spec_from_file_location('fpc_stable',PROJECT/'device/fingerprint/oem/src/fingerprint/deployment/stable_module.py')
stable=importlib.util.module_from_spec(spec);spec.loader.exec_module(stable)
class KernelIdentityTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.release='6.17.0-liuqin-test'
  self.name='usr/lib/modules/'+self.release+'/kernel/drivers/misc/fpc1264_spi_diag.ko'
  self.module=self.root/self.name;self.module.parent.mkdir(parents=True);self.module.write_bytes(b'fixture kernel module')
  self.directory=self.root/'usr/share/liuqin/kernels'/self.release;self.directory.mkdir(parents=True)
  self.manifest=hashlib.sha256(self.module.read_bytes()).hexdigest()+'  '+self.name+'\n'
  self.manifest+='0'*64+'  usr/lib/modules/'+self.release+'/modules.dep\n'
  self.write_contract(self.manifest)
 def tearDown(self):self.temp.cleanup()
 def write_contract(self,manifest):
  (self.directory/'modules.manifest').write_text(manifest)
  (self.directory/'modules.contract').write_text('LIUQIN_ROOT_INTERFACE_V2\nkernel_release='+self.release+'\nimage_sha256='+'0'*64+'\nmodules_sha256='+hashlib.sha256(manifest.encode()).hexdigest()+'\n')
 def test_matching_running_kernel_uses_its_existing_module(self):
  self.assertEqual(stable.installed_kernel_module(self.root,self.release),self.module)
 def test_changed_module_is_rejected_without_loading_it(self):
  self.module.write_bytes(b'changed')
  with self.assertRaisesRegex(RuntimeError,'module hash'):stable.installed_kernel_module(self.root,self.release)
 def test_changed_manifest_cannot_replace_the_recorded_identity(self):
  (self.directory/'modules.manifest').write_text(self.manifest.replace('fpc1264','other'))
  with self.assertRaisesRegex(RuntimeError,'manifest mismatch'):stable.installed_kernel_module(self.root,self.release)
 def test_missing_fpc_support_is_rejected(self):
  self.write_contract(self.manifest.replace('fpc1264_spi_diag.ko','other.ko'))
  with self.assertRaisesRegex(RuntimeError,'no authenticated FPC'):stable.installed_kernel_module(self.root,self.release)
 def test_symlink_module_is_rejected(self):
  saved=self.module.with_name('outside.ko');self.module.rename(saved);self.module.symlink_to(saved)
  with self.assertRaisesRegex(RuntimeError,'module hash'):stable.installed_kernel_module(self.root,self.release)
 def test_display_selection_preserves_ubuntu_and_supports_fedora(self):
  self.assertEqual(stable.readiness_services({'deployment_mode':'installed'}),('gdm','iio-sensor-proxy'))
  self.assertEqual(stable.readiness_services({'deployment_mode':'installed','display_service':'display-manager'}),('display-manager','iio-sensor-proxy'))
  with self.assertRaisesRegex(ValueError,'display service'):
   stable.readiness_services({'deployment_mode':'installed','display_service':'arbitrary-service'})
if __name__=='__main__':unittest.main(verbosity=2)
