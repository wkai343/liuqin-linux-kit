#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Kernel view transitions: fail before GPT writes and retain primary errors."""
import contextlib,errno,importlib.util,stat,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'shared'))
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
m=load('reread_device',PROJECT/'device/installer/android-device.py')
r=load('reread_session',PROJECT/'device/recovery/recovery-session.py')
from liuqin_partitions import snapshot_id

class RereadTests(unittest.TestCase):
 def test_busy_preflight_never_invokes_gpt_writer_and_keeps_original_error(self):
  hardware=m.Hardware.__new__(m.Hardware);hardware.sealed=Mock();hardware.refresh_partition_nodes=Mock(side_effect=RuntimeError('cleanup identity error'));hardware.seal_data_partitions=Mock()
  @contextlib.contextmanager
  def node(*args,**kwargs):yield 7
  hardware.node=node;hardware.inventory=Mock(return_value={'userdata':{}})
  plan={'source':{'inventory':{'userdata':{'parent':'sda','parent_dev':'8:0'}}},'deletes':[],'partitions':[]}
  with patch.object(m.fcntl,'ioctl',side_effect=OSError(errno.EBUSY,'device busy')),patch.object(m.os,'sync'),patch.object(m,'set_ro') as ro,patch.object(m.subprocess,'run') as run:
   with self.assertRaises(OSError) as failure:hardware.append_gpt(plan)
  self.assertEqual(failure.exception.errno,errno.EBUSY);self.assertIn('cleanup identity error',' '.join(failure.exception.__notes__))
  run.assert_not_called();ro.assert_called_once_with(7,1)
 def test_private_nodes_change_minor_only_in_explicit_reconcile(self):
  path=Mock();path.lstat.return_value=SimpleNamespace(st_mode=stat.S_IFBLK|0o600,st_rdev=r.os.makedev(259,1))
  with patch.object(r.os,'mknod') as make:
   with self.assertRaisesRegex(RuntimeError,'alias changed'):r.ensure_node(path,259,82)
   make.assert_not_called();path.unlink.assert_not_called()
   r.ensure_node(path,259,82,True);path.unlink.assert_called_once();make.assert_called_once_with(path,stat.S_IFBLK|0o600,r.os.makedev(259,82))
  for mode in (stat.S_IFREG,stat.S_IFLNK):
   path.lstat.return_value=SimpleNamespace(st_mode=mode,st_rdev=0)
   with self.assertRaisesRegex(RuntimeError,'not a block'):r.ensure_node(path,259,82,True)
 def test_snapshot_uses_guid_and_geometry_not_dynamic_minor(self):
  state={'report':'same','details':{},'registry':None,'files':{},'inventory':{'root':dict(name='root',parent='sda',node='sda37',dev='259:20',partuuid='same-guid',start=4096,bytes=8192)}}
  before=snapshot_id(state);state['inventory']['root']['dev']='259:101';self.assertEqual(snapshot_id(state),before)
  state['inventory']['root']['partuuid']='other-guid';self.assertNotEqual(snapshot_id(state),before)

if __name__=='__main__':unittest.main(verbosity=2)
