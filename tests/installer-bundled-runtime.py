#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import importlib.util
import hashlib
import json
import posixpath
import re
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from urllib.parse import unquote,urlsplit
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.demo import DemoEngine

def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
package=load('installer_package_runtime',PROJECT/'installer/package.py')
fixtures=load('recovery_fixture',PROJECT/'tests/recovery-installer.py')

class PackageTests(unittest.TestCase):
 def test_source_package_has_usable_documentation_and_unchanged_command_examples(self):
  with tempfile.TemporaryDirectory() as d:
   archive=package.package(Path(d)/'source.zip')
   prefix=package.INSTALLER_ARCHIVE_ROOT+'/'
   with zipfile.ZipFile(archive) as stream:
    names=set(stream.namelist())
    self.assertNotIn(prefix+'docs/STATUS.md',names)
    self.assertIn(prefix+'installer/STATUS.txt',names)
    self.assertIn(prefix+'docs/NATIVE-RECOVERY.md',names)
    self.assertIn(prefix+'docs/README.md',names)
    for name in sorted(names):
     if not name.endswith('.md'):continue
     body=stream.read(name).decode()
     source=PROJECT/name.removeprefix(prefix)
     if name.startswith(prefix+'docs/'):
      self.assertEqual(re.findall(r'```[^\n]*\n[\s\S]*?```',body),
                       re.findall(r'```[^\n]*\n[\s\S]*?```',source.read_text()),name)
     body=re.sub(r'```[^\n]*\n[\s\S]*?```','',body)
     for target in re.findall(r'\[[^\]]*\]\(([^)\n]+)\)',body):
      url=urlsplit(target)
      if url.scheme or url.netloc or not url.path:continue
      resolved=posixpath.normpath(posixpath.join(posixpath.dirname(name),unquote(url.path)))
      self.assertIn(resolved,names,(name,target))
 def test_native_hardware_receipt_is_bundled_only_for_its_exact_image(self):
  native=load('package_native_fixture',PROJECT/'tests/native-recovery.py')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);rec=root/'recovery.img';sources=native.adb_package_fixture(rec)
   manifest=json.loads(rec.with_name('native-recovery-manifest.json').read_text())
   record=dict(schema=1,device='liuqin',serial='TEST',build_id=manifest['build_id'],
               image_sha256=hashlib.sha256(rec.read_bytes()).hexdigest(),hardware_temporary_boot_verified=True)
   receipt=rec.with_name('hardware-boot-validation.json');receipt.write_text(json.dumps(record))
   archive=package.package(root/'tested.zip',native_recovery=rec,adbd_sources=sources)
   with zipfile.ZipFile(archive) as stream:
    self.assertEqual(stream.read(package.INSTALLER_ARCHIVE_ROOT+'/installer/runtime/hardware-boot-validation.json'),receipt.read_bytes())
   receipt.write_text(json.dumps(dict(record,image_sha256='f'*64)))
   with self.assertRaisesRegex(RuntimeError,'硬件验收记录'):package.package(root/'wrong.zip',native_recovery=rec,adbd_sources=sources)
   self.assertFalse((root/'wrong.zip').exists())
 def test_old_network_runtime_is_rejected_before_archive_creation(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);engine=DemoEngine(lambda _:None,root/'demo')
   try:
    with self.assertRaisesRegex(RuntimeError,'USB 网络'):package.package(root/'installer.zip',engine.demo_bundle/'installer.img')
    self.assertFalse((root/'installer.zip').exists())
   finally:engine.close()
 def test_bundled_adb_recovery_is_discovered_without_os_materials(self):
  paired=load('bundled_paired_fixture',PROJECT/'tests/paired-recovery.py')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);rec=root/'recovery.img';paired.paired(rec)
   archive=package.package(root/'installer.zip',recovery_image=rec)
   with zipfile.ZipFile(archive) as stream:
    names=stream.namelist();base=package.INSTALLER_ARCHIVE_ROOT+'/'
    self.assertIn(base+'installer/runtime/recovery.img',names)
    self.assertNotIn(base+'installer/runtime/installer.img',names)
    self.assertNotIn(base+'installer/windows-network.ps1',names)
    stream.extractall(root/'extracted')
   runtime=load('extracted_runtime',root/'extracted'/package.INSTALLER_ARCHIVE_ROOT/'installer/liuqin_gui/runtime.py')
   found=Path(runtime.locate());self.assertTrue(found.is_file());self.assertEqual(found.parent.name,'runtime')
   self.assertFalse((found.parent/'rootfs.tar.gz').exists())
 def test_wrong_recovery_is_rejected_before_archive_creation(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);rec=root/'wrong.img';rec.write_bytes(fixtures.make_image('pipa'))
   with self.assertRaisesRegex(RuntimeError,'机型'):package.package(root/'bad.zip',recovery_image=rec)
   self.assertFalse((root/'bad.zip').exists())

if __name__=='__main__':unittest.main(verbosity=2)
