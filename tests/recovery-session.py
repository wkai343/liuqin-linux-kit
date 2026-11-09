#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""No device access: exercise refusal to unmount dependencies and busy mappings."""
import importlib.util,os,stat,subprocess,tempfile,unittest,sys
from pathlib import Path,PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'shared'))
spec=importlib.util.spec_from_file_location('recovery_session',PROJECT/'device/recovery/recovery-session.py')
session=importlib.util.module_from_spec(spec);spec.loader.exec_module(session)

class QuiesceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.base=self.root/'sys/class/block';self.base.mkdir(parents=True)
  driver=self.root/'sys/bus/platform/drivers/ufshcd-qcom';driver.mkdir(parents=True)
  ufs=self.root/'sys/devices/platform/ufs';ufs.mkdir(parents=True);(ufs/'driver').symlink_to(driver)
  for name,num in [('sda','8:0'),('sda1','8:1')]:
   node=ufs/'sda' if name=='sda' else ufs/'sda'/name;node.mkdir(exist_ok=True);(node/'dev').write_text(num)
   (self.base/name).symlink_to(node)
  (self.root/'proc/self').mkdir(parents=True);self.mount=self.root/'proc/self/mountinfo'
  (self.root/'tmp/liuqin-runtime/run').mkdir(parents=True)
 def tearDown(self):self.tmp.cleanup()
 def path(self,name):return self.root/str(name).lstrip('/')
 def run_session(self):
  with patch.object(session,'Path',self.path),patch.object(session.os,'geteuid',return_value=0),patch.object(session.os,'uname',return_value=SimpleNamespace(machine='aarch64')):
   session.quiesce()
 def test_live_system_dependency_stops_before_any_unmount(self):
  self.mount.write_text('1 0 8:1 / /system rw - ext4 /dev/sda1 rw\n')
  with patch.object(session.subprocess,'run') as unmount:
   with self.assertRaisesRegex(RuntimeError,'depends on'):self.run_session()
   unmount.assert_not_called()
 def test_busy_unmount_does_not_fall_back_to_force_or_lazy(self):
  self.mount.write_text('1 0 8:1 / /data rw - ext4 /dev/sda1 rw\n')
  with patch.object(session.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['umount'])) as unmount:
   with self.assertRaises(subprocess.CalledProcessError):self.run_session()
   self.assertEqual(unmount.call_args.args[0],['/system/bin/umount','/data'])
  self.assertFalse((self.root/'tmp/liuqin-runtime/run/global-mountinfo').exists())
 def test_foreign_disks_are_not_unmounted(self):
  text='1 0 259:1 / /external rw - ext4 /dev/nvme0n1p1 rw\n';self.mount.write_text(text)
  with patch.object(session.subprocess,'run') as unmount:self.run_session();unmount.assert_not_called()
  self.assertEqual((self.root/'tmp/liuqin-runtime/run/global-mountinfo').read_text(),text)
 def test_busy_mapper_is_not_force_removed(self):
  self.mount.write_text('')
  dm=self.base/'dm-0';dm.mkdir();(dm/'slaves').mkdir();(dm/'slaves/sda1').symlink_to(self.base/'sda1')
  (dm/'dev').write_text('253:0');(dm/'holders').mkdir();(dm/'holders/busy').touch()
  ctl=self.root/'dev/device-mapper';ctl.parent.mkdir();ctl.touch()
  with patch.object(session.fcntl,'ioctl') as ioctl:
   with self.assertRaisesRegex(RuntimeError,'dependency'):self.run_session()
   ioctl.assert_not_called()

 def test_kernel_logger_releases_only_qualified_ufs_without_force(self):
  self.mount.write_text('')
  mtd=self.root/'proc/mtd';mtd.write_text('dev: size erasesize name\nmtd0: 01000000 00200000 "block2mtd: /dev/block/sda1"\n')
  (self.root/'proc/modules').write_text('mtdoops 16384 0 - Live 0\nblock2mtd 16384 0 - Live 0\n')
  def run(command,**kwargs):
   if command[-1]=='block2mtd':mtd.write_text('dev: size erasesize name\n')
   return subprocess.CompletedProcess(command,0,b'',b'')
  with patch.object(session.subprocess,'run',side_effect=run) as calls:self.run_session()
  self.assertEqual([c.args[0] for c in calls.call_args_list],[
   ['/system/bin/liuqin/busybox','rmmod','mtdoops'],['/system/bin/liuqin/busybox','rmmod','block2mtd']])

 def test_unknown_mtd_and_busy_module_stop_maintenance(self):
  self.mount.write_text('');mtd=self.root/'proc/mtd'
  (self.root/'proc/modules').write_text('block2mtd 16384 0 - Live 0\n')
  for name in ('sda1','nvme0n1p1'):
   with self.subTest(name=name):
    mtd.write_text('header\nmtd0: 01000000 00200000 "block2mtd: /dev/block/'+name+'"\n')
    with patch.object(session.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['rmmod'])) as calls:
     with self.assertRaises((RuntimeError,subprocess.CalledProcessError)):self.run_session()
     if name=='sda1':self.assertEqual(calls.call_args.args[0],['/system/bin/liuqin/busybox','rmmod','block2mtd'])
     else:calls.assert_not_called()

 def test_only_exact_qseecomd_service_pid_may_be_stopped(self):
  for executable in ('/vendor/bin/qseecomd','/vendor/bin/unknown'):
   with self.subTest(executable=executable):
    process=SimpleNamespace(name='577',joinpath=lambda _:SimpleNamespace(resolve=lambda:PurePosixPath(executable)))
    # A Path is hashable, as required by the opener set.
    class Process:
     name=process.name
     def joinpath(self,name):return process.joinpath(name)
    fd=SimpleNamespace(stat=lambda:SimpleNamespace(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,1)),parent=SimpleNamespace(parent=Process()))
    proc=SimpleNamespace(glob=lambda _:[fd])
    def path(name):return proc if name=='/proc' else self.path(name)
    def run(command,**kwargs):return subprocess.CompletedProcess(command,0,'577\n' if 'init.svc_debug_pid.vendor.qseecomd' in command else 'stopped\n','')
    with patch.object(session,'Path',path),patch.object(session.subprocess,'run',side_effect=run) as calls:
     if executable.endswith('/unknown'):
      with self.assertRaisesRegex(RuntimeError,'refusing'):session.release_auxiliary_users(self.base,{'sda','sda1'})
      calls.assert_not_called()
     else:
      session.release_auxiliary_users(self.base,{'sda','sda1'})
      self.assertEqual(calls.call_args_list[1].args[0],['/system/bin/stop','vendor.qseecomd'])

if __name__=='__main__':unittest.main(verbosity=2)
