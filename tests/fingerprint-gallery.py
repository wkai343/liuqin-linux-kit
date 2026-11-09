#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real private-file gallery transactions with fake TAs; no sensor/TEE access."""
import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
OEM=PROJECT/'device/fingerprint/oem/src/fingerprint/oem'
sys.path.insert(0,str(OEM))
import gallery_state as gallery
import gallery_match as matcher
import native_enrol as native

FAKE=r'''
import hashlib,json,os,pathlib,signal,struct,sys,time
base=pathlib.Path(__file__).parent;mode=(base/'mode').read_text()
def write(path,data):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:os.write(fd,data);os.fsync(fd)
 finally:os.close(fd)
def metadata(path,before,after,id,retry=0):
 data=b'FPCMETA2'+struct.pack('<I5II5III',len(before),*(before+[0]*(5-len(before))),len(after),*(after+[0]*(5-len(after))),id,retry)
 write(path,data)
def record(kind):
 with (base/'calls').open('a') as f:f.write(kind+'\n')
cleanup='oem_runtime_cleanup firmware_path_restored=1 sensor_power_off=1 listener_stopped=1'
if '--state-dir' in sys.argv:
 state=pathlib.Path(sys.argv[2]);operation=sys.argv[3];uid=sys.argv[-2]
 secret=os.read(int(sys.argv[-1]),33);assert len(secret)==32
 record(operation+':'+uid+':'+hashlib.sha256(secret).hexdigest())
 if operation=='--create':write(state/uid/'gatekeeper.handle',b'opaque fake handle')
 else:
  assert operation=='--enrol-gallery'
  source,output,active,meta=sys.argv[4:8]
  before=json.loads(pathlib.Path(source).read_text())['ids'] if source!='-' else []
  wire=pathlib.Path(active).read_bytes();count=struct.unpack_from('<I',wire)[0]
  ids=before if count==0xffffffff else list(struct.unpack_from('<'+'I'*count,wire,4))
  new=next(i for i in range(1,100) if i not in ids);after=ids+[new]
  if mode=='bad-inventory':after=[new]
  write(output,json.dumps({'ids':after,'revision':0}).encode());metadata(meta,before,after,new)
  print('enrol_progress_status=1 remaining=2',flush=True)
 if mode!='missing-cleanup':print(cleanup,flush=True)
 sys.exit(1 if mode=='runtime-fail' else 0)
assert sys.argv[1:3]==['--ufs-rpmb-authenticated','--match-gallery']
source,output,meta=map(pathlib.Path,sys.argv[3:6]);state=json.loads(source.read_text());ids=state['ids']
record('match')
if mode=='cancel':
 def cancel(sig,frame):time.sleep(.1);print(cleanup,flush=True);sys.exit(1)
 signal.signal(signal.SIGTERM,cancel);(base/'ready').write_text('ready')
 while True:time.sleep(.02)
if mode=='no-result':print(cleanup,flush=True);sys.exit(1)
matched=int((base/'match-id').read_text()) if (base/'match-id').exists() else ids[0]
retry=mode=='retry';matched=0 if retry else matched
metadata(meta,ids,ids,matched,int(retry));state['revision']=state.get('revision',0)+1
write(output,json.dumps(state).encode())
if (base/'delete-during-match').exists():pathlib.Path((base/'delete-during-match').read_text()).unlink()
if mode!='missing-cleanup':print(cleanup,flush=True)
sys.exit(1 if retry else 0 if matched else 3)
'''
DECODER=r'''#!/usr/bin/python3
import os,sys
assert sys.argv[1]=='--decode'
data=os.read(int(sys.argv[3]),17*1024*1024)
os.write(int(sys.argv[4]),data)
'''


class GalleryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.bundle=self.root/'bundle';self.bundle.mkdir(mode=0o700)
  self.output=self.root/'output';self.output.mkdir(mode=0o700)
  for name in ('native_credentials.py','oem_runtime.py'):(self.bundle/name).write_text(FAKE)
  decoder=self.bundle/'fpc-oem-print';decoder.write_text(DECODER);decoder.chmod(0o700)
  (self.bundle/'gallery_state.py').write_bytes((OEM/'gallery_state.py').read_bytes())
  (self.bundle/'mode').write_text('ok')
  (self.bundle/'CANDIDATE.json').write_text(json.dumps(dict(account_profile='fprintd-user',enrollment_interface='fprintd',multi_finger_version=1)))
  self.accounts={'alice':SimpleNamespace(pw_name='alice',pw_uid=1000),'bob':SimpleNamespace(pw_name='bob',pw_uid=1001)}
  self.state=self.root/'state';self.prints=self.root/'fprint';self.number=0
  self.patches=[patch.object(native,'__file__',str(self.bundle/'native_enrol.py')),
   patch.object(matcher,'__file__',str(self.bundle/'gallery_match.py')),
   patch.object(native,'STATE_ROOT',self.state),patch.object(matcher,'STATE_ROOT',self.state),
   patch.object(native,'FPRINT_ROOT',self.prints),patch.object(matcher,'FPRINT_ROOT',self.prints),
   patch.object(gallery.pwd,'getpwnam',side_effect=lambda name:self.accounts[name]),
   patch.object(gallery.pwd,'getpwuid',side_effect=lambda uid:next(u for u in self.accounts.values() if u.pw_uid==uid))]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.temp.cleanup()
 def invoke(self,module,args):
  log=io.StringIO()
  with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):status=module.main(args)
  return status,log.getvalue()
 def account(self,user='alice'):
  return self.state/str(0x60000000|self.accounts[user].pw_uid)
 def catalog(self,user='alice'):
  return gallery.read_catalog(self.bundle,user,self.accounts[user].pw_uid,self.prints)
 def path(self,finger,user='alice'):
  return self.prints/user/'fpc1264_oem/liuqin-fpc1264-oem'/format(finger,'x')
 def publish(self,finger,data,version=2,user='alice'):
  path=self.path(finger,user);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  gallery.private_write(path,struct.pack('<II',version,finger)+data)
 def enrol(self,finger,user='alice',publish=True):
  self.number+=1;output=self.output/str(self.number)
  rc,log=self.invoke(native,['--enrol',user,str(finger),str(output)])
  if not rc and publish:self.publish(finger,output.read_bytes(),user=user)
  return rc,log,output
 def identify(self,items=None,user='alice'):
  items=self.catalog(user) if items is None else items
  uid=self.accounts[user].pw_uid;name=user.encode()
  data=b'FPCREQ2\0'+struct.pack('<III',uid,len(items),len(name))+name
  for item in items:data+=struct.pack('<III',item['finger'],item['version'],len(item['data']))+item['data']
  self.number+=1;request=self.output/('request'+str(self.number));result=request.with_suffix('.result')
  gallery.private_write(request,data)
  rc,log=self.invoke(matcher,['--request',str(request),'--result',str(result)])
  return rc,struct.unpack('<II',result.read_bytes()[8:]) if result.exists() else None,log
 def state_record(self,user='alice'):
  return gallery.read_state(self.account(user),user,self.accounts[user].pw_uid)
 def calls(self):
  p=self.bundle/'calls';return p.read_text().splitlines() if p.exists() else []
 def test_multiple_fingers_match_in_one_operation_and_preserve_prior_prints(self):
  self.assertEqual(self.enrol(2)[0],0);first=self.path(2).read_bytes()
  credential=(self.account()/'credential.json').read_bytes()
  self.assertEqual(self.enrol(7)[0],0)
  self.assertEqual(self.path(2).read_bytes(),first)
  self.assertEqual((self.account()/'credential.json').read_bytes(),credential)
  self.assertEqual(sum(c.startswith('--create') for c in self.calls()),1)
  for index,item in enumerate(self.catalog()):
   binding=self.state_record()['bindings'][str(item['finger'])]
   (self.bundle/'match-id').write_text(str(binding['id']))
   previous=self.calls().count('match');rc,result,_=self.identify()
   self.assertEqual((rc,result),(0,(1,index)))
   self.assertEqual(self.calls().count('match'),previous+1)
  self.assertEqual(self.path(2).read_bytes(),first)
 def test_deleted_or_unrequested_template_never_authenticates(self):
  self.enrol(2);self.enrol(7);state=self.state_record()
  old=self.catalog()[0];deleted_id=state['bindings']['2']['id']
  self.path(2).unlink();(self.bundle/'match-id').write_text(str(deleted_id))
  self.assertEqual(self.identify()[1],(0,0xffffffff))
  previous=len(self.calls());self.assertNotEqual(self.identify([old])[0],0)
  self.assertEqual(len(self.calls()),previous)
 def test_delete_reenroll_can_reuse_hardware_id_but_not_old_reference(self):
  self.enrol(2);self.enrol(7);old=self.catalog()[0]
  old_id=self.state_record()['bindings']['2']['id'];self.path(2).unlink()
  self.assertEqual(self.enrol(2)[0],0)
  self.assertEqual(self.state_record()['bindings']['2']['id'],old_id)
  self.assertNotEqual(self.catalog()[0]['data'],old['data'])
  self.assertNotEqual(self.identify([old])[0],0)
 def test_unpublished_new_finger_is_not_authorized_and_is_pruned_on_retry(self):
  self.enrol(2);rc,_,unpublished=self.enrol(7,publish=False);self.assertEqual(rc,0)
  orphan=self.state_record()['bindings']['7'];(self.bundle/'match-id').write_text(str(orphan['id']))
  self.assertEqual(self.identify()[1],(0,0xffffffff))
  self.assertEqual(self.enrol(7)[0],0)
  self.assertNotEqual(self.path(7).read_bytes()[8:],unpublished.read_bytes())
 def test_capacity_is_per_account_and_deletion_releases_a_slot(self):
  for finger in (1,3,5,7,10):self.assertEqual(self.enrol(finger)[0],0)
  before=len(self.calls());self.assertNotEqual(self.enrol(9)[0],0)
  self.assertEqual(len(self.calls()),before)
  self.assertEqual(self.enrol(9,user='bob')[0],0)
  self.path(3).unlink();self.assertEqual(self.enrol(9)[0],0)
 def test_cross_account_reference_and_credential_reassignment_are_rejected(self):
  self.enrol(2);self.enrol(2,user='bob');bob=self.catalog('bob')
  before=len(self.calls());self.assertNotEqual(self.identify(bob)[0],0)
  self.assertEqual(len(self.calls()),before)
  original=(self.account()/'credential.json').read_bytes()
  self.accounts['new-owner']=SimpleNamespace(pw_name='new-owner',pw_uid=1000);del self.accounts['alice']
  self.assertNotEqual(self.enrol(3,user='new-owner')[0],0)
  self.assertEqual((self.state/str(0x600003e8)/'credential.json').read_bytes(),original)
 def test_old_opaque_print_format_is_rejected_without_creating_credentials(self):
  self.publish(2,json.dumps({'ids':[77]}).encode(),version=1)
  original=self.path(2).read_bytes()
  self.assertNotEqual(self.enrol(7)[0],0)
  self.assertEqual(self.path(2).read_bytes(),original);self.assertEqual(self.calls(),[])
 def test_existing_print_with_missing_credential_is_preserved_without_new_identity(self):
  self.publish(2,json.dumps({'ids':[77]}).encode(),version=1);old=self.path(2).read_bytes()
  self.assertNotEqual(self.enrol(7)[0],0);self.assertEqual(self.calls(),[])
  self.assertEqual(self.path(2).read_bytes(),old)
 def test_invalid_inventory_or_cleanup_cannot_replace_existing_gallery(self):
  self.enrol(2);original=(self.account()/'gallery.json').read_bytes();old_print=self.path(2).read_bytes()
  for mode in ('bad-inventory','runtime-fail','missing-cleanup'):
   (self.bundle/'mode').write_text(mode);self.assertNotEqual(self.enrol(7)[0],0)
   self.assertEqual((self.account()/'gallery.json').read_bytes(),original)
   self.assertEqual(self.path(2).read_bytes(),old_print)
 def test_miss_retry_and_unknown_match_id_never_authorize(self):
  self.enrol(2);(self.bundle/'match-id').write_text('0');self.assertEqual(self.identify()[1],(0,0xffffffff))
  (self.bundle/'mode').write_text('retry');self.assertEqual(self.identify()[1],(2,0xffffffff))
  (self.bundle/'mode').write_text('ok');(self.bundle/'match-id').write_text('999')
  self.assertNotEqual(self.identify()[0],0)
 def test_publication_change_during_matching_discards_result(self):
  self.enrol(2);original=(self.account()/'gallery.json').read_bytes()
  (self.bundle/'delete-during-match').write_text(str(self.path(2)))
  self.assertNotEqual(self.identify()[0],0)
  self.assertEqual((self.account()/'gallery.json').read_bytes(),original)
 def test_cancel_waits_for_cleanup_and_does_not_publish_authentication(self):
  self.enrol(2);old=(self.account()/'gallery.json').read_bytes();(self.bundle/'mode').write_text('cancel')
  def cancel():
   deadline=time.monotonic()+5
   while time.monotonic()<deadline:
    if (self.bundle/'ready').exists():os.kill(os.getpid(),signal.SIGTERM);return
    time.sleep(.01)
  thread=threading.Thread(target=cancel);thread.start();rc,result,_=self.identify();thread.join()
  self.assertNotEqual(rc,0);self.assertIsNone(result);self.assertEqual((self.account()/'gallery.json').read_bytes(),old)
 def test_real_credential_and_authorization_bridges_preserve_gallery_paths_and_fds(self):
  self.enrol(2);uid=0x600003e8
  modules=[]
  for name in ('native_credentials','authorize_oem','oem_runtime'):
   spec=importlib.util.spec_from_file_location('bridge_'+name,OEM/(name+'.py'))
   module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);modules.append(module)
  credentials,authorize,runtime=modules
  for name in ('fpc_build_info','qsee-gatekeeper'):
   path=self.bundle/name;path.write_text('fixture');path.chmod(0o700)
  secret=os.memfd_create('fixture-secret',os.MFD_CLOEXEC);os.write(secret,b's'*32);os.lseek(secret,0,0)
  calls=[]
  class Child:
   def wait(self,timeout=None):return 0
   def poll(self):return 0
   def send_signal(self,signum):raise AssertionError('unexpected signal')
  def spawn(argv,**kwargs):
   for fd in kwargs.get('pass_fds',()):os.fstat(fd)
   calls.append(argv);return Child()
  paths=[str(self.output/name) for name in ('input','output','active','metadata')]
  handlers={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
  try:
   argv=['native_credentials.py','--state-dir',str(self.state),'--enrol-gallery',*paths,str(uid),str(secret)]
   with patch.object(credentials,'__file__',str(self.bundle/'native_credentials.py')),patch.object(sys,'argv',argv),patch.object(credentials.subprocess,'Popen',side_effect=spawn):
    self.assertEqual(credentials.main(),0)
   self.assertEqual(calls[0][2:8],['--ufs-rpmb-authenticated','--enrol-gallery',*paths])
   handle=os.open(self.account()/'gatekeeper.handle',os.O_RDONLY)
   try:
    calls.clear();argv=['authorize_oem.py','--enrol-gallery',*paths,str(uid),str(handle),str(secret)]
    with patch.object(authorize,'__file__',str(self.bundle/'authorize_oem.py')),patch.object(sys,'argv',argv),patch.object(authorize.subprocess,'Popen',side_effect=spawn):
     self.assertEqual(authorize.main(),0)
    self.assertEqual(calls[0][1:3],['--verify-enrol',str(uid)])
    self.assertEqual(calls[1][1:6],['--bio-gallery',*paths])
    reached=[]
    def device_path(path):
     self.assertEqual(path,'/dev/fpc1020');reached.append(path);return SimpleNamespace(exists=lambda:False)
    argv=['oem_runtime.py','--ufs-rpmb-authenticated','--enrol-gallery',*paths,str(uid),str(handle),str(secret)]
    with patch.object(sys,'argv',argv),patch.object(runtime,'Path',side_effect=device_path),contextlib.redirect_stderr(io.StringIO()):
     self.assertEqual(runtime.main(),2)
    self.assertEqual(reached,['/dev/fpc1020'])
   finally:os.close(handle)
  finally:
   os.close(secret)
   for sig,handler in handlers.items():signal.signal(sig,handler)
 def test_failed_durable_commit_restores_previous_gallery(self):
  self.enrol(2);state=self.state_record();original=(self.account()/'gallery.json').read_bytes()
  real=gallery.sync_directory;calls=0
  def fail_once(path):
   nonlocal calls
   calls+=1
   if calls==2:raise OSError('fixture fsync failure')
   return real(path)
  updated=gallery.new_state('alice',1000,state['bindings'],b'updated encrypted fixture')
  with patch.object(gallery,'sync_directory',side_effect=fail_once):
   with self.assertRaises(OSError):gallery.save_state(self.account(),updated,state)
  self.assertEqual((self.account()/'gallery.json').read_bytes(),original)
  self.assertFalse((self.account()/'gallery.previous').exists())
 def test_cancel_before_replace_removes_only_the_temporary_backup(self):
  self.enrol(2);state=self.state_record();original=(self.account()/'gallery.json').read_bytes()
  checks=iter((False,True))
  with self.assertRaises(InterruptedError):
   gallery.save_state(self.account(),state,state,cancelled=lambda:next(checks))
  self.assertEqual((self.account()/'gallery.json').read_bytes(),original)
  self.assertFalse((self.account()/'gallery.previous').exists())
  self.assertIsNotNone(self.state_record())
 def test_proven_interrupted_commit_completes_without_recreating_credentials(self):
  self.enrol(2);original=(self.account()/'credential.json').read_bytes()
  os.link(self.account()/'gallery.json',self.account()/'gallery.previous')
  self.assertEqual(self.identify()[1],(1,0))
  self.assertFalse((self.account()/'gallery.previous').exists())
  self.assertEqual((self.account()/'credential.json').read_bytes(),original)
  os.replace(self.account()/'gallery.json',self.account()/'gallery.previous')
  self.assertEqual(self.identify()[1],(1,0))
  self.assertTrue((self.account()/'gallery.json').is_file())
  self.assertEqual((self.account()/'credential.json').read_bytes(),original)
 def test_bad_digest_symlink_or_pending_commit_blocks_before_hardware(self):
  self.enrol(2);path=self.account()/'gallery.json';original=path.read_bytes()
  obj=json.loads(original);obj['sha256']='0'*64;path.write_text(json.dumps(obj));path.chmod(0o600)
  before=len(self.calls());self.assertNotEqual(self.identify()[0],0);self.assertEqual(len(self.calls()),before)
  path.write_bytes(original);gallery.private_write(self.account()/'gallery.previous',b'unknown interrupted state')
  self.assertNotEqual(self.identify()[0],0);self.assertEqual(len(self.calls()),before)
  (self.account()/'gallery.previous').unlink();path.unlink()
  outside=self.root/'external';gallery.private_write(outside,original);path.symlink_to(outside)
  self.assertNotEqual(self.identify()[0],0);self.assertEqual(len(self.calls()),before)


if __name__=='__main__':
 if os.geteuid()!=0:raise SystemExit('Run: unshare -Ur python3 tests/fingerprint-gallery.py')
 unittest.main(verbosity=2)
