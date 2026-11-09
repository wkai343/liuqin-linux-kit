#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Paired image, backend, packaging and kernel-less Recovery regression tests."""
import base64,contextlib,gzip,hashlib,http.server,importlib.util,json,os,struct,subprocess,sys,tempfile,unittest,urllib.request,uuid,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_recovery_image import add,pack,MARKER,BUSYBOX_PATH,inspect_paired
from liuqin_gui.engine import Engine,InstallerError
from liuqin_gui.recovery_transport import RecoveryTransport,AdbError
from liuqin_gui import artifacts,recovery,host
from liuqin_gui.engine import Transport

def sha(b):return hashlib.sha256(b).hexdigest()
def paired(path,broken=False):
 entries={};kernel=b'Linux version 5.10.1-test\0';bid='f'*64
 payload={'system/bin/liuqin-recovery':b'#!/system/bin/sh\n',BUSYBOX_PATH:b'busybox',
  'system/etc/liuqin-runtime.tar.xz':b'private-root','system/etc/liuqin-runtime.sha256':sha(b'private-root').encode()+b'\n',
  'system/etc/liuqin-build-id':bid.encode()+b'\n'}
 manifest=dict(schema=1,device='liuqin',backend='recovery-adb-v1',build_id=bid,kernel_release='5.10.1-test',
  kernel_sha256=sha(kernel),filesystems=['ext4'],payload_files={k:sha(v) for k,v in payload.items()})
 for n,b in payload.items():add(entries,n,b)
 add(entries,MARKER,json.dumps(manifest).encode());add(entries,'system/bin/recovery',b'recovery');add(entries,'prop.default',b'ro.product.device=liuqin\n')
 if broken:add(entries,'system/bin/liuqin-recovery',b'changed')
 body=gzip.compress(pack(entries));h=bytearray(4096);h[:8]=b'ANDROID!'
 struct.pack_into('<II',h,8,len(kernel),len(body));struct.pack_into('<I',h,20,1584);struct.pack_into('<I',h,40,4)
 path.write_bytes(h+kernel+bytes(-len(kernel)%4096)+body+bytes(-len(body)%4096));return manifest

def load(name,p):
 s=importlib.util.spec_from_file_location(name,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

class PairedTests(unittest.TestCase):
 def test_runtime_dispatch_and_tamper(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'recovery.img';paired(p)
   value=artifacts.boot_interface(p,installer=True)
   self.assertEqual(value['usb_network'],'adb');self.assertEqual(value['filesystems'],['ext4'])
   e=Engine(lambda _:None,Path(d)/'state');e.runtime_path=str(p);e.validate_installer(d)
   self.assertEqual(e.manifest['runtime_file'],'recovery.img');self.assertNotIn('installer.img',e.manifest['files'])
   paired(p,broken=True)
   with self.assertRaisesRegex(ValueError,'payload mismatch'):inspect_paired(p)
 def test_kernel_less_is_fixed_only(self):
  f=load('rec_fixture',PROJECT/'tests/recovery-installer.py')
  with tempfile.TemporaryDirectory() as d:
   data=f.make_image();ks=struct.unpack_from('<I',data,8)[0];h=bytearray(data[:4096]);struct.pack_into('<I',h,8,0)
   p=Path(d)/'rec.img';p.write_bytes(h+data[4096+(ks+4095)//4096*4096:])
   self.assertFalse(recovery.inspect_image(p)['temporary_boot'])
 def test_pair_package_needs_no_installer_image(self):
  pkg=load('paired_package',PROJECT/'installer/package.py')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=root/'recovery.img';paired(p)
   z=pkg.package(root/'kit.zip',recovery_image=p)
   with zipfile.ZipFile(z) as stream:
    names=stream.namelist();base=pkg.INSTALLER_ARCHIVE_ROOT+'/'
    self.assertIn(base+'installer/runtime/recovery.img',names)
    self.assertFalse(any(n.endswith('/installer.img') for n in names));stream.extractall(root/'kit')
    self.assertIn('历史 TWRP 配套后端',stream.read(base+'README.md').decode())
   module=load('paired_locator',root/'kit'/pkg.INSTALLER_ARCHIVE_ROOT/'installer/liuqin_gui/runtime.py')
   self.assertEqual(Path(module.locate()).name,'recovery.img')
 def transport(self):
  e=SimpleNamespace(check_cancel=lambda:None,receipt={},save_receipt=lambda:None,
    manifest={'recovery_manifest':{'build_id':'f'*64},'files':{'recovery.img':'e'*64}})
  t=RecoveryTransport.__new__(RecoveryTransport);t.engine=e;t.serial='tablet';t.boot_id=str(uuid.uuid4())
  t.read_environment=lambda:dict(kind='recovery',label='配套 Recovery',properties={})
  return t
 def test_commands_are_encoded_and_exit_status_is_checked(self):
  t=self.transport();calls=[]
  t.shell=lambda args,timeout,observe:calls.append(args) or b'OK'
  command="printf '%s' 'quoted `data` $(not-executed)'"
  self.assertEqual(t.remote(command),b'OK');self.assertEqual(base64.b64decode(calls[0][-1]).decode(),command)
  self.assertEqual(calls[0][1:3],['run',t.boot_id])
  t.boot_id=None
  with self.assertRaises(InstallerError):t.remote('true')
 def test_probe_wrong_serial_version_slot_or_lock_fails(self):
  t=self.transport();good=dict(serial='tablet',build_id='f'*64,boot_id=t.boot_id,slot='a',locked='0')
  for key,value in [('serial','other'),('build_id','e'*64),('slot','b'),('locked','1')]:
   t.adb=lambda *_,**kw:json.dumps(dict(good,**{key:value})).encode()
   with self.assertRaises(InstallerError):t.probe()
 def test_missing_flash_lock_property_uses_same_boot_avb_evidence(self):
  t=self.transport();good=dict(serial='tablet',build_id='f'*64,boot_id=t.boot_id,slot='a',locked='')
  responses=[json.dumps(good).encode(),('unlocked\norange\n'+t.boot_id+'\n').encode()]
  calls=[]
  def adb(*args,**kw):calls.append(args);return responses.pop(0)
  t.adb=adb
  self.assertEqual(t.probe()['unlock_source'],'vbmeta.device_state + verifiedbootstate')
  self.assertIn('getprop ro.boot.vbmeta.device_state',calls[1][-1])
 def test_missing_or_contradictory_avb_evidence_is_not_unlocked(self):
  for state,verified,boot in [('', '', None),('locked','green',None),('unlocked','green',None),('locked','orange',None),('unlocked','orange','other-boot')]:
   t=self.transport();good=dict(serial='tablet',build_id='f'*64,boot_id=t.boot_id,slot='a',locked='')
   responses=[json.dumps(good).encode(),(state+'\n'+verified+'\n'+(boot or t.boot_id)+'\n').encode()]
   t.adb=lambda *a,**kw:responses.pop(0)
   with self.subTest(state=state,verified=verified,boot=boot),self.assertRaises(InstallerError):t.probe()
 def test_rom_with_disabled_avb_may_omit_vbmeta_device_state(self):
  t=self.transport();value=dict(serial='tablet',build_id='f'*64,boot_id=t.boot_id,slot='a',locked='')
  responses=[json.dumps(value).encode(),('\norange\n'+t.boot_id+'\n').encode()]
  t.adb=lambda *a,**kw:responses.pop(0)
  self.assertEqual(t.probe()['unlock_source'],'verifiedbootstate=orange')
 def test_recovery_adb_state_is_ready(self):
  t=self.transport();t.adb=lambda *a,**kw:b'recovery\n';t.probe=lambda:{'ready':'fixture'}
  self.assertEqual(t.wait_recovery(),{'ready':'fixture'})
 def test_gpt_warning_retries_reads_without_accepting_corruption(self):
  from unittest.mock import Mock
  t=self.transport();t.engine.emit=Mock();t.engine.pause=Mock()
  warning=InstallerError('Recovery 命令失败：读取 GPT 身份时收到警告：backup CRC mismatch')
  t.shell=Mock(side_effect=[warning,b'validated'])
  self.assertEqual(t.enter_recovery('build'),b'validated');self.assertEqual(t.shell.call_count,2)
  self.assertTrue(all(call.args[0][1:] == ['enter','build'] for call in t.shell.call_args_list))
  t.shell=Mock(side_effect=warning)
  with self.assertRaises(InstallerError):t.enter_recovery('build')
  self.assertEqual(t.shell.call_count,3)
  t.shell=Mock(side_effect=InstallerError('runtime hash mismatch'))
  with self.assertRaises(InstallerError):t.enter_recovery('build')
  self.assertEqual(t.shell.call_count,1)
 def test_identity_and_gateway_errors_are_not_retried(self):
  for error in [InstallerError('wrong build'),InstallerError('wrong slot'),AdbError('/system/bin/liuqin-recovery: not found'),AdbError('error: device unauthorized')]:
   t=self.transport();t.adb=lambda *a,**kw:b'recovery\n'
   t.engine.pause=lambda _:self.fail('permanent failure was retried')
   def probe():raise error
   t.probe=probe
   with self.subTest(error=str(error)),self.assertRaises(type(error)):t.wait_recovery()
 def test_disappearing_device_is_retried_with_visible_status(self):
  t=self.transport();responses=[AdbError("error: device 'tablet' not found"),b'recovery\n'];events=[]
  def adb(*a,**kw):
   result=responses.pop(0)
   if isinstance(result,Exception):raise result
   return result
  t.adb=adb;t.probe=lambda:{'ready':True};t.engine.pause=lambda _:None
  t.engine.emit=lambda kind,**values:events.append((kind,values))
  self.assertEqual(t.wait_recovery(),{'ready':True});self.assertEqual(events[0][0],'notice')
 def test_existing_recovery_is_identified_before_returning_to_fastboot(self):
  t=self.transport();t.adb_path='mock-adb';calls=[]
  t.probe=lambda:calls.append('probe') or dict(boot_id=t.boot_id,build_id='f'*64)
  t.enter_recovery=lambda _:calls.append('enter') or json.dumps(dict(ready=True,serial=t.serial,boot_id=t.boot_id)).encode()
  t.verify_ram_interface=lambda:None
  t.return_fastboot=lambda:calls.append('return')
  with patch.object(host,'run_process',return_value=SimpleNamespace(stdout='List of devices attached\ntablet\trecovery\n')),\
       patch.object(Transport,'check_device',return_value={'product':'liuqin'}) as check:
   self.assertEqual(t.check_device(),{'product':'liuqin'})
  self.assertEqual(calls,['probe','enter','return']);check.assert_called_once()
 def test_reboot_disconnect_is_success_only_after_same_fastboot_serial(self):
  t=self.transport();t.fastboot_path='mock-fastboot';t.ram_active=True
  t.ensure_fastboot_names=lambda:None
  def disconnected(*a,**kw):raise AdbError('')
  t.adb=disconnected
  with patch.object(host,'run_process',return_value=SimpleNamespace(stdout='tablet\tfastboot\n')):
   t.return_fastboot()
  self.assertFalse(t.ram_active);self.assertIsNone(t.boot_id)
 def test_existing_recovery_readonly_scan_does_not_require_or_reboot_fastboot(self):
  from unittest.mock import Mock
  t=self.transport();t.adb_path='mock-adb';t.engine.receipt={'operation':'scan-partitions'}
  t.engine.save_receipt=Mock();t.engine.emit=Mock()
  t.probe=lambda:dict(boot_id=t.boot_id,build_id='f'*64,slot='a')
  t.enter_recovery=lambda _:json.dumps(dict(ready=True,serial=t.serial,boot_id=t.boot_id)).encode()
  t.verify_ram_interface=lambda:None;t.remote=Mock(return_value=b'201326592\n');t.return_fastboot=Mock()
  with patch.object(host,'run_process',return_value=SimpleNamespace(stdout='tablet\trecovery\n')),patch.object(Transport,'check_device') as fb:
   self.assertEqual(t.check_device()['product'],'liuqin');fb.assert_not_called()
  boot=t.boot_id;t.boot_ram();self.assertEqual(t.boot_id,boot);t.cancel_cleanup()
  t.return_fastboot.assert_not_called();self.assertTrue(t.engine.receipt['recovery_retained'])
 def test_reboot_disconnect_without_same_device_still_fails(self):
  t=self.transport();t.fastboot_path='mock-fastboot';t.ram_active=True;t.engine.pause=lambda _:None
  t.ensure_fastboot_names=lambda:None
  def disconnected(*a,**kw):raise AdbError('disconnected')
  t.adb=disconnected
  with patch.object(host,'run_process',return_value=SimpleNamespace(stdout='other\tfastboot\n')),\
       patch('liuqin_gui.recovery_transport.time.monotonic',side_effect=[0,1,91]):
   with self.assertRaisesRegex(InstallerError,'超时'):t.return_fastboot()
  self.assertTrue(t.ram_active)
 def test_device_picker_lists_recovery_and_deduplicates_fastboot(self):
  native_probe=patch('liuqin_gui.native_adb.probe_adb',side_effect=OSError('native USB probe disabled in offline test'))
  native_probe.start();self.addCleanup(native_probe.stop)
  with tempfile.TemporaryDirectory() as d:
   events=[];engine=Engine(events.append,d)
   outputs=[SimpleNamespace(stdout='same fastboot\n'),SimpleNamespace(stdout='List of devices attached\nsame recovery\ntablet recovery\nother offline\n')]
   with patch.object(host,'fastboot_path',return_value='mock-fastboot'),patch.object(host,'adb_path',return_value='mock-adb'),patch.object(host,'run_process',side_effect=outputs):engine.list_devices()
   self.assertEqual([v['serial'] for v in events[-1]['devices']],['same','tablet'])
 def test_shell_failure_propagates_real_process_exit_status(self):
  t=self.transport();t.adb_path='mock-adb';spawn=subprocess.Popen;seen=[]
  def fake(arguments,**kwargs):
   seen.extend(arguments)
   return spawn([sys.executable,'-c','import sys; print("remote failure"); sys.exit(7)'],**kwargs)
  with patch('liuqin_gui.recovery_transport.subprocess.Popen',side_effect=fake):
   with self.assertRaisesRegex(InstallerError,'remote failure'):t.shell(['gateway','run'])
  self.assertEqual(seen[3:5],['shell','-T'])
 def test_shell_success_preserves_raw_output(self):
  t=self.transport();t.adb_path='mock-adb';spawn=subprocess.Popen
  def fake(arguments,**kwargs):return spawn([sys.executable,'-c','import sys; sys.stdout.buffer.write(b"raw\\x00\\xff\\n")'],**kwargs)
  with patch('liuqin_gui.recovery_transport.subprocess.Popen',side_effect=fake):
   self.assertEqual(t.shell(['gateway','run']),b'raw\x00\xff\n')
 def test_reverse_is_loopback_only_and_removed_on_failure(self):
  t=self.transport();calls=[];t.adb=lambda *a,**k:calls.append(a) or b''
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'checked')
  with self.assertRaisesRegex(ValueError,'failure'):
   with t.http_server(Handler) as base:
    self.assertTrue(base.startswith('http://127.0.0.1:'))
    with urllib.request.urlopen(base) as r:self.assertEqual(r.read(),b'checked')
    raise ValueError('failure')
  self.assertEqual(calls[0][:2],('reverse','--no-rebind'));self.assertEqual(calls[-1],('reverse','--remove',calls[0][2]))
 def test_windows_adb_does_not_need_rndis(self):
  with patch.object(host,'IS_WINDOWS',True):host.check_gui_recovery_support({'installer_usb_network':'adb'})

if __name__=='__main__':unittest.main(verbosity=2)
