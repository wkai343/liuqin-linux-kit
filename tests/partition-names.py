#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Firmware label boundary, legacy refusal, and actual boot-parser migration."""
import copy,hashlib,importlib.util,json,socket,subprocess,sys,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_partition_names import root_name,is_root_name,validate_name,validate_names
from liuqin_multiboot import plan,layout_payload
from liuqin_partitions import partition_arguments,discover
from liuqin_partition_editor import capabilities
from liuqin_layout import reference_stock_disk
from liuqin_boot_target import bind,current_helper,HELPER_PATH,LEGACY_HELPER_SHA256
from liuqin_artifacts import boot_interface,cpio_files,bounded_initramfs
from liuqin_gui.demo import DemoEngine,demo_image
from liuqin_gui.partition_demo import inspect
from liuqin_gui.recovery_transport import RecoveryTransport
from liuqin_gui.engine import InstallerError
from liuqin_gui.engine import Transport
spec=importlib.util.spec_from_file_location('names_device',PROJECT/'device/installer/android-device.py');device=importlib.util.module_from_spec(spec);spec.loader.exec_module(device)
OS_ID='9748571b-5637-4dfd-ad50-71c5d8d19d74'
LEGACY='linux_r_9748571b5637'

class NamesTests(unittest.TestCase):
 def config(self):return dict(schema_version=1,partition_only=True,userdata_gib=0,boot_gib=2,roots=[dict(id=OS_ID,name='Fedora',size_gib=64,filesystem='ext4')])
 def test_firmware_boundary_is_enforced_in_bytes(self):
  self.assertEqual(root_name(OS_ID),'lqr_9748571b5637');self.assertEqual(len(root_name(OS_ID).encode()),16)
  validate_name('xbl_sc_test_mode')
  for name in (LEGACY,'a'*17,'名字','a/b','',None):
   with self.subTest(name=name),self.assertRaises(ValueError):validate_name(name)
  self.assertTrue(is_root_name(root_name(OS_ID)))
  self.assertFalse(is_root_name(LEGACY))
 def test_all_planned_names_fit_both_fastboot_variables(self):
  p=plan(reference_stock_disk(),self.config(),str(uuid.uuid4()))
  for part in p['partitions']:
   for prefix in ('partition-size:','partition-type:'):
    self.assertLessEqual(len((prefix+part['name']).encode())+1,32)
  p['partitions'][-1]['name']=LEGACY
  with self.assertRaises(ValueError):layout_payload(p,'unused')
  p['source']={'inventory':{'userdata':{}}}
  with self.assertRaises(ValueError):partition_arguments(p)
 def test_device_rejects_untrusted_label_before_disk_access(self):
  hw=device.Hardware.__new__(device.Hardware);hw.sealed=Mock();hw.inventory=Mock();hw.node=Mock()
  p={'source':{'inventory':{'userdata':{}}},'partitions':[{'name':LEGACY}]}
  with patch.object(device,'set_ro') as ro,patch.object(device.subprocess,'run') as run:
   with self.assertRaises(ValueError):hw.append_gpt(p)
  hw.node.assert_not_called();hw.sealed.assert_not_called();ro.assert_not_called();run.assert_not_called()
 def test_distinct_ids_with_same_short_suffix_are_rejected(self):
  config=self.config();other=dict(config['roots'][0],id='9748571b-5637-4dfd-ad50-71c5d8d19d75')
  config['roots'].append(other)
  with self.assertRaisesRegex(ValueError,'重复'):plan(reference_stock_disk(),config,str(uuid.uuid4()))
 def test_old_ram_parser_is_rejected_before_layout_operations(self):
  t=Transport.__new__(Transport);t.engine=SimpleNamespace(manifest=dict(installer_interface='liuqin-installer-v2',installer_kernel_release='test',installer_multiboot_version=1))
  t.remote=Mock(side_effect=[b'liuqin-installer-v2\n',b'test\n',b'old  /usr/lib/liuqin/boot-target-v1.sh\n'])
  with self.assertRaisesRegex(InstallerError,'旧分区名称'):t.verify_ram_interface()
 def test_legacy_layout_is_readonly_and_never_auto_rebooted(self):
  with tempfile.TemporaryDirectory() as d:
   engine=DemoEngine(lambda _:None,Path(d));self.addCleanup(engine.close);state=inspect(engine)
   state['inventory'][LEGACY]=dict(name=LEGACY,parent='sda',number=37,start=0,bytes=4096)
   result=discover(state,'DEMO-LIUQIN');self.assertFalse(result['can_create']);self.assertIn('不兼容',result['reason'])
   row=next(p for p in capabilities(state,'DEMO-LIUQIN') if p['name']==LEGACY)
   self.assertEqual(row['role'],'protected')
   self.assertFalse(row['can_install']);self.assertFalse(row['can_delete']);self.assertFalse(row['can_format'])
  t=RecoveryTransport.__new__(RecoveryTransport);t.engine=SimpleNamespace();t.boot_id=str(uuid.uuid4());t.serial='tablet';t.adb=Mock()
  t.remote=Mock(side_effect=InstallerError('分区名不兼容'))
  with self.assertRaisesRegex(InstallerError,'不兼容'):t.return_fastboot()
  t.adb.assert_not_called()
 def test_known_old_boot_parser_is_upgraded_without_changing_kernel(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);engine=DemoEngine(lambda _:None,root/'state');self.addCleanup(engine.close)
   fresh=boot_interface(engine.demo_bundle/'boot.img')
   import struct
   raw=(engine.demo_bundle/'boot.img').read_bytes();ks,rs=struct.unpack_from('<I',raw,8)[0],struct.unpack_from('<I',raw,16)[0]
   start=4096+(ks+4095)//4096*4096;records=cpio_files(bounded_initramfs(raw[start:start+rs],256*1024**2))
   files={n:data for n,(mode,data) in records.items() if n!='TRAILER!!!'}
   old=current_helper().replace(b'bt_name=lqr_',b'bt_name=linux_r_')
   self.assertEqual(hashlib.sha256(old).hexdigest(),LEGACY_HELPER_SHA256)
   files[HELPER_PATH]=old;source=root/'old.img';source.write_bytes(demo_image(files,b'DEMO ONLY KERNEL'))
   target=dict(schema_version=1,os_id=OS_ID,root_partuuid=str(uuid.uuid4()),root_fsuuid=str(uuid.uuid4()),root_fstype='ext4',root_path='native-root',boot_generation=str(uuid.uuid4()))
   dest=root/'new.img';bind(source,dest,target)
   after=boot_interface(dest)
   self.assertEqual(after['kernel_sha256'],fresh['kernel_sha256']);self.assertEqual(after['contract'],fresh['contract'])
   self.assertEqual(after['boot_target_helper_sha256'],hashlib.sha256(current_helper()).hexdigest())
   files[HELPER_PATH]=b'unknown parser';bad=root/'bad.img';bad.write_bytes(demo_image(files,b'DEMO ONLY KERNEL'))
   with self.assertRaisesRegex(ValueError,'解析器'):bind(bad,root/'refused.img',target)
   self.assertFalse((root/'refused.img').exists())
if __name__=='__main__':unittest.main(verbosity=2)
