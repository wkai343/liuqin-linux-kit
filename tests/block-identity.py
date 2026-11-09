#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import importlib.util,subprocess,sys,tempfile,unittest,uuid,shutil
from pathlib import Path
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_block_identity import parse_information,partition_fields

class IdentityTests(unittest.TestCase):
 def test_real_sgdisk_batch_guid_name_and_range(self):
  tool=shutil.which('sgdisk')
  if not tool:self.skipTest('sgdisk required')
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'disk.img'
   with path.open('wb') as f:f.truncate(32*1024**2)
   ids=[str(uuid.uuid4()),str(uuid.uuid4())]
   subprocess.run([tool,'--clear','--new=1:2048:4095','--change-name=1:userdata','--partition-guid=1:'+ids[0],
    '--new=2:4096:8191','--change-name=2:linux_boot','--partition-guid=2:'+ids[1],str(path)],check=True,capture_output=True)
   text=subprocess.run([tool,'--info=1','--info=2',str(path)],check=True,capture_output=True,text=True).stdout
   expected=[dict(node='sda1',name='userdata',start=2048*512,bytes=2048*512),dict(node='sda2',name='linux_boot',start=4096*512,bytes=4096*512)]
   self.assertEqual(parse_information(text,expected,512),dict(zip(['sda1','sda2'],ids)))
   for key,value in [('name','other'),('start',0),('bytes',512)]:
    changed=[dict(expected[0],**{key:value}),expected[1]]
    with self.subTest(key=key),self.assertRaisesRegex(RuntimeError,'不一致'):parse_information(text,changed,512)
   with self.assertRaisesRegex(RuntimeError,'不完整'):parse_information(text,expected[:1],512)
   with self.assertRaisesRegex(RuntimeError,'字段缺失'):parse_information(text.replace('Partition unique GUID:','Missing:'),expected,512)
 def test_inventory_retains_hyphenated_firmware_as_protected(self):
  spec=importlib.util.spec_from_file_location('device_identity_fixture',PROJECT/'device/installer/android-device.py')
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);driver=root/'ufshcd-qcom';driver.mkdir();ufs=root/'ufs';ufs.mkdir();(ufs/'driver').symlink_to(driver)
   disk=ufs/'sde';disk.mkdir();(disk/'queue').mkdir();(disk/'queue/logical_block_size').write_text('4096');(disk/'dev').write_text('8:64')
   part=disk/'sde3';part.mkdir();guid=str(uuid.uuid4())
   for name,text in dict(partition='3',dev='8:67',start='2048',size='4096',uevent='PARTNAME=vm-linux_a\nPARTUUID='+guid+'\n').items():(part/name).write_text(text)
   sysroot=root/'block';sysroot.mkdir();(sysroot/'sde').symlink_to(disk);(sysroot/'sde3').symlink_to(part)
   hardware=module.Hardware.__new__(module.Hardware);hardware.sys_root=sysroot
   inventory=hardware.inventory();self.assertEqual(inventory['vm-linux_a']['partuuid'],guid)
   from liuqin_partition_editor import capabilities
   with patch('liuqin_partition_editor.discover',return_value={'can_create':True}):
    row=capabilities({'inventory':inventory,'registry':None},'TEST')[0]
   self.assertEqual(row['role'],'protected');self.assertFalse(row['can_delete']);self.assertFalse(row['can_format']);self.assertFalse(row['can_install'])
 def test_kernel_uuid_is_used_and_fallback_cached_only_for_this_read(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);parent=root/'sda';entry=parent/'sda1';entry.mkdir(parents=True)
   (entry/'partition').write_text('1');guid=str(uuid.uuid4());events=entry/'uevent'
   events.write_text('PARTNAME=userdata\nPARTUUID='+guid+'\n')
   with patch('liuqin_block_identity.disk_identities') as read:
    self.assertEqual(partition_fields(entry,{})['PARTUUID'],guid);read.assert_not_called()
   events.write_text('PARTNAME=userdata\n');cache={}
   with patch('liuqin_block_identity.disk_identities',return_value={'sda1':guid}) as read:
    self.assertEqual(partition_fields(entry,cache)['PARTUUID'],guid)
    self.assertEqual(partition_fields(entry,cache)['PARTUUID'],guid);self.assertEqual(read.call_count,1)
    partition_fields(entry,{});self.assertEqual(read.call_count,2)

if __name__=='__main__':unittest.main(verbosity=2)
