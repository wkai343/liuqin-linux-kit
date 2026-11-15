#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Touch handoff gating tests; no actual module loading or hardware access."""
import hashlib,importlib.util,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'shared'))
spec=importlib.util.spec_from_file_location('native_touch',PROJECT/'device/recovery/native-touch.py')
touch=importlib.util.module_from_spec(spec);spec.loader.exec_module(touch)
class TouchTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.value=dict(sealed=True,device='liuqin',slot='a',unlocked=True,kernel='test',boot_id='test-boot')
  self.status=patch.object(touch.status,'collect',return_value=self.value);self.status.start();self.addCleanup(self.status.stop)
  self.runner=Mock()
  self.now=0.0
  clock=patch.object(touch.time,'monotonic',side_effect=lambda:self.now);clock.start();self.addCleanup(clock.stop)
  def advance(seconds):self.now+=seconds
  sleep=patch.object(touch.time,'sleep',side_effect=advance);sleep.start();self.addCleanup(sleep.stop)
  self.put('proc/mounts','rootfs / rootfs rw 0 0\n')
  self.put('sys/class/graphics/fb0/name','simple\n')
  self.device=self.root/'sys/bus/spi/devices/spi0.0'
  self.put('sys/bus/spi/devices/spi0.0/of_node/compatible','novatek,NVT-ts-spi\0')
  self.put('sys/bus/spi/devices/spi0.0/touch_state','eng_reset_count=0 reset_ret=0 reset=a301 fw_ret=0 restore_ret=0 wdt_frames=0 wdt_events=0 wdt_streak=0')
  (self.device/'driver').mkdir()
  self.manifest=dict(touch_version=1,kernel_release='test',display_profile='bootloader-simplefb',inspection_only=True,payload_sha256={})
  for name in touch.FILES:
   self.put(name,'fixture '+name);self.manifest['payload_sha256'][name]=hashlib.sha256((self.root/name).read_bytes()).hexdigest()
  self.put('etc/liuqin-native-recovery.json',json.dumps(self.manifest))
 def put(self,name,text):
  path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
 def test_ready_uses_only_opt_in_ram_module(self):
  result=touch.start(self.root,self.runner)
  self.assertEqual(result['status'],'ready')
  self.runner.assert_called_once_with(['/bin/busybox','insmod',str(self.root/touch.MODULE),'bootloader_handoff=1'],check=True,timeout=10)
 def test_retained_scanout_selects_rek_before_releasing_touch_probe(self):
  parameter='sys/module/nt36523_ts/parameters/liuqin_tddi_recovery'
  self.put(parameter,'Y\n')
  self.put('sys/bus/spi/devices/spi0.0/touch_state','eng_reset_count=0 reset_ret=0 reset=a162 fw_ret=0 restore_ret=0 wdt_frames=0 wdt_events=0 wdt_streak=0')
  self.runner.side_effect=lambda *a,**k:self.assertEqual((self.root/parameter).read_text(),'0\n')
  self.assertEqual(touch.start(self.root,self.runner)['status'],'ready')
  self.runner.assert_called_once()
 def test_unverified_environment_never_changes_touch_policy(self):
  parameter='sys/module/nt36523_ts/parameters/liuqin_tddi_recovery'
  self.put(parameter,'Y\n');self.value['sealed']=False
  with self.assertRaises(RuntimeError):touch.start(self.root,self.runner)
  self.assertEqual((self.root/parameter).read_text(),'Y\n');self.runner.assert_not_called()
 def test_wrong_identity_or_unsealed_never_loads(self):
  for key,value in [('device','other'),('sealed',False),('slot','b'),('unlocked',False),('kernel','other')]:
   old=self.value[key];self.value[key]=value
   with self.subTest(key=key),self.assertRaises(RuntimeError):touch.start(self.root,self.runner)
   self.value[key]=old
  self.runner.assert_not_called()
 def test_display_and_rootfs_gates(self):
  for name,body in [('sys/class/graphics/fb0/name','msmdrmfb'),('proc/mounts','/dev/sda35 / ext4 rw 0 0\n')]:
   old=(self.root/name).read_text();self.put(name,body)
   with self.assertRaises(RuntimeError):touch.start(self.root,self.runner)
   self.put(name,old)
  self.runner.assert_not_called()
 def test_tampered_module_helper_or_firmware_never_loads(self):
  for name in touch.FILES:
   old=(self.root/name).read_text();self.put(name,'changed')
   with self.subTest(name=name),self.assertRaises(RuntimeError):touch.start(self.root,self.runner)
   self.put(name,old)
  self.runner.assert_not_called()
 def test_module_failure_does_not_attempt_reprobe(self):
  (self.device/'driver').rmdir();self.runner.side_effect=subprocess.CalledProcessError(1,'insmod')
  with self.assertRaises(subprocess.CalledProcessError):touch.start(self.root,self.runner)
  self.assertFalse((self.root/'sys/bus/spi/drivers_probe').exists())
 def test_existing_module_is_not_reloaded(self):
  (self.root/'sys/module/liuqin_bootloader_panel').mkdir(parents=True)
  self.assertEqual(touch.start(self.root,self.runner)['status'],'ready');self.runner.assert_not_called()
 def test_early_preparation_requires_readonly_ram_profile_but_not_storage_admission(self):
  self.value['sealed']=False
  with self.assertRaises(RuntimeError):touch.start(self.root,self.runner,prepare=True)
  self.manifest['recovery_fast_start_version']=1
  self.put('etc/liuqin-native-recovery.json',json.dumps(self.manifest))
  self.put('etc/liuqin-storage-mode','readonly\n')
  self.assertEqual(touch.start(self.root,self.runner,prepare=True)['status'],'ready')
  touch.status.collect.assert_called_with(self.root,include_battery=False)
  self.runner.reset_mock();self.put('etc/liuqin-storage-mode','persistent\n')
  with self.assertRaises(RuntimeError):touch.start(self.root,self.runner,prepare=True)
  self.runner.assert_not_called()
 def early_profile(self):
  self.manifest['recovery_fast_start_version']=1
  self.put('etc/liuqin-native-recovery.json',json.dumps(self.manifest))
  self.put('etc/liuqin-storage-mode','readonly\n')
 def watchdog(self):
  p=self.device/'touch_state';p.write_text(p.read_text().replace('wdt_frames=0','wdt_frames=11').replace('wdt_events=0','wdt_events=1'))
 def test_late_watchdog_is_rejected_without_replacing_the_driver(self):
  self.early_profile()
  def advance(seconds):
   self.now+=seconds
   if self.now>=.3:self.watchdog()
  with patch.object(touch.time,'sleep',side_effect=advance):
   with self.assertRaisesRegex(RuntimeError,'watchdog persists'):touch.start(self.root,self.runner,prepare=True)
  self.runner.assert_called_once_with(['/bin/busybox','insmod',str(self.root/touch.MODULE),'bootloader_handoff=1'],check=True,timeout=10)
 def test_persistent_watchdog_never_rebinds_even_before_admission(self):
  self.early_profile();self.watchdog()
  with self.assertRaisesRegex(RuntimeError,'watchdog persists'):touch.start(self.root,self.runner,prepare=True)
  self.assertEqual(self.runner.call_count,1)
 def test_watchdog_cannot_rebind_a_live_or_nonpreparing_session(self):
  self.watchdog()
  with self.assertRaisesRegex(RuntimeError,'watchdog persists'):touch.start(self.root,self.runner)
  self.assertEqual(self.runner.call_count,1)
  self.runner.reset_mock();self.early_profile()
  self.put('proc/sys/kernel/random/boot_id','test-boot')
  self.put('run/liuqin-native-startup','ready\ntest-boot\n')
  with self.assertRaisesRegex(RuntimeError,'after startup admission'):touch.start(self.root,self.runner,prepare=True)
  self.runner.assert_not_called()
if __name__=='__main__':unittest.main()
