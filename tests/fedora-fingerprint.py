#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline FPC kernel identity checks. No fingerprint hardware or credentials."""
import hashlib,importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'build/lib')]
spec=importlib.util.spec_from_file_location('fpc_stable',PROJECT/'device/fingerprint/oem/src/fingerprint/deployment/stable_module.py')
stable=importlib.util.module_from_spec(spec);spec.loader.exec_module(stable)
spec=importlib.util.spec_from_file_location('fpc_packaging',PROJECT/'build/lib/fedora_fingerprint.py')
packaging=importlib.util.module_from_spec(spec);spec.loader.exec_module(packaging)
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
 def test_display_selection_supports_explicit_display_services(self):
  self.assertEqual(stable.readiness_services({'deployment_mode':'installed'}),('gdm','iio-sensor-proxy'))
  self.assertEqual(stable.readiness_services({'deployment_mode':'installed','display_service':'display-manager'}),('display-manager','iio-sensor-proxy'))
  with self.assertRaisesRegex(ValueError,'display service'):
   stable.readiness_services({'deployment_mode':'installed','display_service':'arbitrary-service'})
 def test_release_payload_is_not_bound_to_any_build_host_account(self):
  root=self.root/'assembly';work=self.root/'work'
  libraries=root/packaging.PREFIX.lstrip('/')/'lib';libraries.mkdir(parents=True)
  (libraries/'libfprint-fixture.so').write_bytes(b'fixture library')
  for name in ('native/fprintd','native/fpc-oem-print','native/libfprint-tod-fpc1264-oem.so',
               'clients/fpc_build_info','clients/qsee-gatekeeper','clients/qsee-supplicant',
               'clients/qsee-app-loader','clients/ufs-rpmb-provider/liuqin-rpmb-supplicant'):
   p=work/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture compiled component')
  for name in ('fprintd-source','libfprint-v1.95.1'):(work/name).mkdir()
  payload=packaging.stage(root,work,self.root/'export')
  runtime=payload/packaging.PREFIX.lstrip('/')
  info=json.loads((runtime/'CANDIDATE.json').read_text())
  self.assertEqual(info['account_profile'],'fprintd-user')
  self.assertEqual(info['enrollment_interface'],'fprintd')
  self.assertTrue(info['supports_multiple_users'])
  self.assertFalse(info['supports_one_finger_per_user'])
  self.assertEqual(info['multi_finger_version'],1)
  self.assertEqual(info['max_fingers_per_user'],5)
  for key in ('linux_uid','linux_username','native_uid'):self.assertNotIn(key,info)
  self.assertEqual((runtime/'native_enrol.py').read_bytes(),
                   (PROJECT/'device/fingerprint/oem/src/fingerprint/oem/native_enrol.py').read_bytes())
  self.assertFalse((payload/'var/lib/liuqin-fingerprint').exists())
  self.assertFalse((payload/'var/lib/fprint').exists())
  self.assertFalse((payload/'etc/passwd').exists())
  self.assertFalse((payload/'usr/share/applications').exists())
  unit=(payload/'usr/lib/systemd/system/liuqin-fpc-stable-module.service').read_text()
  self.assertIn('After=display-manager.service iio-sensor-proxy.service',unit)
  self.assertNotIn('NetworkManager',unit)
  self.assertNotIn('ssh',unit)
  self.assertIn('UMask=0077',(payload/'etc/systemd/system/fprintd.service.d/60-liuqin-fpc-oem.conf').read_text())
  for name in ('pam-input','user_credentials.py','acceptance.py','enrol_publish.py'):
   self.assertFalse((runtime/name).exists(),name)
  with patch.object(packaging.subprocess,'run') as command:
   with self.assertRaisesRegex(ValueError,'多指能力'):
    packaging.pack(payload,self.root/'must-not-publish')
   command.assert_not_called()
if __name__=='__main__':unittest.main(verbosity=2)
