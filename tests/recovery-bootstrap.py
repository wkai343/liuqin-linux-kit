#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the actual gateway RAM check and packaged BusyBox dispatcher offline."""
import io,os,shlex,shutil,subprocess,sys,tarfile,tempfile,unittest
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_recovery_image import BUSYBOX_PATH,unpack,records,inspect_paired

class GatewayTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  source=shutil.which('busybox')
  if not source:self.skipTest('native BusyBox required (run in Devbox)')
  self.bb=self.root/'busybox';shutil.copyfile(source,self.bb);self.bb.chmod(0o755)
 def tearDown(self):self.temp.cleanup()
 def check(self,mounts,broken=False):
  mountinfo=self.root/'mountinfo';mountinfo.write_text(mounts)
  bb=self.bb
  if broken:
   bb=self.root/'liuqin-busybox';bb.write_text('#!/bin/sh\necho "liuqin-busybox: applet not found" >&2\nexit 127\n');bb.chmod(0o755)
  script=(PROJECT/'device/recovery/gateway.sh').read_text()
  script=script.replace('BB=/'+BUSYBOX_PATH,'BB='+shlex.quote(str(bb)))
  script=script.replace('/proc/self/mountinfo',str(mountinfo))
  # Only identity reads are mocked. The gateway's check_ram function, BusyBox
  # ELF and awk actually execute; check never enters the maintenance runtime.
  prefix='id() { printf "0\\n"; }; getprop() { printf "liuqin\\n"; };\n'
  return subprocess.run(['sh','-s','check'],input=prefix+script,capture_output=True,text=True)
 def test_actual_awk_check_accepts_only_tmpfs_at_tmp(self):
  result=self.check('1 0 0:12 / /tmp rw - tmpfs tmpfs rw\n')
  self.assertEqual(result.returncode,0,result.stderr);self.assertIn('"ram_ready":true',result.stdout)
  for mounts in ['1 0 8:1 / /tmp rw - ext4 /dev/sda1 rw\n','1 0 0:12 / /other rw - tmpfs tmpfs rw\n','']:
   result=self.check(mounts);self.assertNotEqual(result.returncode,0);self.assertIn('/tmp is not tmpfs',result.stderr)
 def test_renamed_binary_is_reported_as_tool_failure(self):
  result=self.check('1 0 0:12 / /tmp rw - tmpfs tmpfs rw\n',broken=True)
  self.assertNotEqual(result.returncode,0);self.assertIn('BusyBox cannot execute',result.stderr)
  self.assertNotIn('/tmp is not tmpfs',result.stderr)

class ImageTests(unittest.TestCase):
 def test_embedded_arm64_applets_and_runtime_archive(self):
  path=os.environ.get('LIUQIN_TEST_RECOVERY_IMAGE');qemu=shutil.which('qemu-aarch64')
  if not path or not qemu:self.skipTest('set LIUQIN_TEST_RECOVERY_IMAGE and provide qemu-aarch64 for real image test')
  manifest=inspect_paired(Path(path));items=records(unpack(path)[2])
  self.assertIn(('BB=/'+BUSYBOX_PATH).encode(),items['system/bin/liuqin-recovery'][1])
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);bb=root/BUSYBOX_PATH;bb.parent.mkdir(parents=True);bb.write_bytes(items[BUSYBOX_PATH][1]);bb.chmod(0o755)
   def run(*args,**kw):return subprocess.run([qemu,str(bb),*args],capture_output=True,check=True,**kw)
   wrong=root/'liuqin-busybox';shutil.copyfile(bb,wrong);wrong.chmod(0o755)
   broken=subprocess.run([qemu,str(wrong),'awk','BEGIN {print "OK"}'],capture_output=True)
   self.assertNotEqual(broken.returncode,0);self.assertIn(b'applet not found',broken.stderr)
   self.assertEqual(run('awk','BEGIN {print "OK"}').stdout,b'OK\n')
   self.assertEqual(run('base64','-d',input=b'T0sK').stdout,b'OK\n')
   archive=root/'runtime.tar.xz';archive.write_bytes(items['system/etc/liuqin-runtime.tar.xz'][1])
   self.assertIn(manifest['payload_files']['system/etc/liuqin-runtime.tar.xz'].encode(),run('sha256sum',str(archive)).stdout)
   extracted=root/'runtime';extracted.mkdir();run('tar','-xJf',str(archive),'-C',str(extracted))
   self.assertEqual((extracted/'usr/lib/liuqin/recovery-session.py').read_bytes(),(PROJECT/'device/recovery/recovery-session.py').read_bytes())
   self.assertEqual((extracted/'bin/busybox').read_bytes(),bb.read_bytes())
   reboot=root/'reboot';reboot.write_bytes(items['system/bin/liuqin/reboot'][1]);reboot.chmod(0o755)
   self.assertEqual(reboot.read_bytes(),(extracted/'usr/sbin/liuqin-reboot').read_bytes())
   # Invalid arguments exercise the static executable without any reboot syscall.
   result=subprocess.run([qemu,str(reboot),'invalid'],capture_output=True)
   self.assertEqual(result.returncode,64);self.assertIn(b'normal|bootloader',result.stderr)
   # list verifies the chroot applet exists without requesting host privileges.
   self.assertIn(b'chroot',run('--list').stdout.splitlines())

if __name__=='__main__':unittest.main(verbosity=2)
