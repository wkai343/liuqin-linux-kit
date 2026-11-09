#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Full ROM policies, scoped firmware writes and failure sequencing. No USB."""
import contextlib,copy,hashlib,io,importlib.util,json,os,struct,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'shared'),str(ROOT/'installer')]
from liuqin_android import load_profile,format_settings,write_partitions,CHUNK_BYTES
from liuqin_ota import validate,blockers,assess,install_order,LOGICAL
import liuqin_native_install as gate

def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
v1=load('full_rom_v1_fixture',ROOT/'tests/generic-ota.py');agent=load('full_rom_device',ROOT/'device/installer/android-device.py')
def fixture():
 p=v1.fixture();parts=p['ota']['partitions'];p.update(schema_version=3,generic_ota_version=2,id='ota2-'+p['archive']['sha256'][:32],firmware_policy='rom-package')
 p['ota']['ignored_partitions']=[];p['images'].update({n+'.img':dict(partition=n+'_a',**r) for n,r in parts.items() if n not in LOGICAL})
 p['install_images']=install_order(parts);p['material_images']=sorted(p['images']);p['boot_contract']['recovery']=dict(device='liuqin',kernel_bytes=0,header_version=4,sha256=parts['recovery']['sha256'])
 return validate(p)
def inventory(p):
 return {n:dict(name=n,node='sde'+str(i+1),dev='8:'+str(i+1),parent='sde',parent_dev='8:0',start=(i+1)*4096,bytes=size,partuuid=str(uuid.uuid4())) for i,(n,size) in enumerate([(r['partition'],r['bytes']) for r in p['images'].values()]+[('userdata',32*1024**3),('metadata',64*1024**2)])}
class Hardware:
 def __init__(self,p,root,fail=None):
  self.p=p;self.rows=inventory(p);self.serial='TEST';self.writes=[];self.formats=[];self.fail=fail;self.root=root
  for n in ('linux_boot','lqr_123456789012'):
   self.rows[n]=dict(name=n,parent='sda',partuuid=str(uuid.uuid4()),start=4096,bytes=2*1024**3)
 def inventory(self,*a,**kw):return copy.deepcopy(self.rows)
 def sealed(self,*a):pass
 def check_format_tools(self):pass
 def digest(self,part,offset=0,length=None):
  if length is None:return 'e'*64 if part['name']==self.fail and part['name'] in self.writes else '0'*64
  if part['name']=='super':return next(r['sha256'] for r in self.p['super']['verification_ranges'] if r['offset']==offset and r['bytes']==length)
  r=next(r for r in self.p['images'].values() if r['partition']==part['name']);return r['sha256'] if part['name'] in self.writes else 'f'*64
 def read(self,part,offset,n):
  b=bytearray(n)
  if part['name'].startswith('vbmeta'):b[:4]=b'AVB0'
  return bytes(b)
 @contextlib.contextmanager
 def writable(self,part,allowed):
  assert part['name'] in allowed and not part['name'].endswith('_b');assert part['name'] not in ('linux_boot','lqr_123456789012')
  self.writes.append(part['name']);yield part['name']
 def write(self,*a):pass
 def format(self,part,settings):self.formats.append(part['name'])
 @contextlib.contextmanager
 def mount_store(self,*a,**kw):yield self.root

def plans(p,rows):
 values=[]
 for name in p['install_images']:
  r=p['images'][name];part=rows[r['partition']];values.append(dict(partition=part,source_sha256=r['sha256'],source_bytes=r['bytes'],expanded_bytes=r['bytes'],whole_sha256='0'*64,chunks=[dict(offset=n,bytes=min(CHUNK_BYTES,part['bytes']-n),sha256='0'*64) for n in range(0,part['bytes'],CHUNK_BYTES)]))
 return values
class Tests(unittest.TestCase):
 def setUp(self):
  self.p=fixture();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def test_firmware_differences_and_all_valid_avb_flags_are_allowed(self):
  rows=inventory(self.p)
  def read(part,n):
   b=bytearray(n);b[:4]=b'AVB0';struct.pack_into('>Q',b,112,999);return bytes(b)
  for flags in range(4):
   self.p['boot_contract']['vbmeta']['flags']=flags
   result=assess(self.p,rows,lambda *a:'0'*64,read);self.assertTrue(result['compatible']);self.assertFalse(result['blockers']);self.assertEqual(self.p['boot_contract']['vbmeta']['flags'],flags)
   self.assertIn('abl_a',result['firmware_updates'])
 def test_no_firmware_bundle_does_not_require_a_rom_whitelist(self):
  from liuqin_ota import FIRMWARE
  for n in FIRMWARE:
   self.p['ota']['partitions'].pop(n,None);self.p['images'].pop(n+'.img',None)
  self.p['install_images']=install_order(self.p['ota']['partitions']);self.p['material_images']=sorted(self.p['images']);self.assertEqual(blockers(self.p),[])
 def test_wrong_slot_foreign_partition_and_missing_target_are_rejected(self):
  for target in ('abl_b','persist','lqr_123456789012'):
   p=copy.deepcopy(self.p);p['images']['abl.img']['partition']=target
   with self.assertRaises(ValueError):validate(p)
  rows=inventory(self.p);del rows['xbl_a'];r=assess(self.p,rows,lambda *a:'0'*64,lambda p,n:bytes(n));self.assertFalse(r['compatible'])
 def test_full_scope_needs_every_backup_and_new_rec(self):
  rows=inventory(self.p);targets={r['partition'] for r in self.p['images'].values()};parts=[gate.identity(rows[n]) for n in sorted(targets)]
  scope=dict(kind='android',parts=parts,full_rom=True,android_boot_write=True,rom_targets=sorted(targets),rom_backups={p['name']:dict(identity=p,sha256='0'*64) for p in parts},android_profile_sha256=gate.digest(self.p),android_boot_sha256=self.p['images']['boot.img']['sha256'],previous_boot_sha256='0'*64,gpt_sha256=None)
  gate.validate_scope(scope)
  broken=copy.deepcopy(scope);del broken['rom_backups']['abl_a']
  with self.assertRaises(ValueError):gate.validate_scope(broken)
  broken=copy.deepcopy(scope);broken['full_rom']=False
  with self.assertRaises(ValueError):gate.validate_scope(broken)
  boot=str(uuid.uuid4());m=dict(installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,usage='temporary-boot-only',generic_ota_version=2,build_id='a'*64)
  for name,value in [(gate.MARKER,json.dumps(m)),('proc/sys/kernel/random/boot_id',boot)]:
   q=self.root/name;q.parent.mkdir(parents=True,exist_ok=True);q.write_text(value)
  req=dict(scope=scope,android_profile=self.p,boot_id=boot,serial='TEST',build_id=m['build_id'],token='b'*32,plan_id='c'*64)
  probe=dict(device='liuqin',sealed=True,unlocked=True,slot='a',boot_id=boot,serial='TEST',build_id=m['build_id'])
  with self.assertRaisesRegex(ValueError,'备份'):gate.begin(req,probe,self.root)
  incomplete=copy.deepcopy(req);incomplete['scope'].update(full_rom=False,parts=[gate.identity(rows['boot_a'])],rom_targets=[],rom_backups={})
  with self.assertRaisesRegex(ValueError,'完整 ROM'):gate.begin(incomplete,probe,self.root)
  self.assertEqual(gate.begin(dict(req,rom_backups_verified=True),probe,self.root)['state'],'active')
  with self.assertRaises(RuntimeError):gate.authorize_target(dict(parts[0],name='abl_b'),req['token'],self.root)
 def run_writer(self,fail=None):
  h=Hardware(self.p,self.root,fail);op=agent.Operations(h,self.p);op.guard=lambda plan:h.inventory();op.staged=lambda *a:None
  transaction=str(uuid.uuid4());(self.root/'transactions'/('android-'+transaction)).mkdir(parents=True)
  (self.root/'registry.json').write_bytes(b'{}')
  plan=dict(mode='reinstall',images=plans(self.p,h.rows),transaction=transaction,registry_sha256=hashlib.sha256(b'{}').hexdigest(),store_files={})
  with patch.object(agent,'fetch',return_value=b'fixture'),contextlib.redirect_stdout(io.StringIO()):
   if fail:
    with self.assertRaises(RuntimeError):op.install(plan,'http://127.0.0.1/fixture')
   else:op.install(plan,'http://127.0.0.1/fixture')
  return h
 def test_writer_reaches_firmware_and_rom_recovery_without_touching_linux(self):
  h=self.run_writer();self.assertEqual(h.writes,[self.p['images'][n]['partition'] for n in self.p['install_images']]);self.assertEqual(h.formats,['userdata','metadata']);self.assertEqual(h.writes[-1],'abl_a')
  self.assertIn('recovery_a',h.writes);self.assertNotIn('linux_boot',h.writes);self.assertNotIn('lqr_123456789012',h.writes)
 def test_failed_readback_stops_later_firmware_and_formatting(self):
  h=self.run_writer('modem_a');self.assertNotIn('abl_a',h.writes);self.assertFalse(h.formats)
 def test_changed_write_order_is_rejected_before_opening_a_partition(self):
  from liuqin_android import validate_images
  rows=inventory(self.p);images=plans(self.p,rows);images[0],images[-1]=images[-1],images[0]
  with self.assertRaisesRegex(ValueError,'顺序'):validate_images(images,self.p,rows)
 def test_backup_failure_cannot_produce_a_complete_manifest(self):
  from types import SimpleNamespace
  from liuqin_gui.native_ota import backup_overwrites
  part=dict(name='abl_a',node='sde1',parent='sde',partuuid=str(uuid.uuid4()),start=4096,bytes=4096)
  p=dict(images={'abl.img':{'partition':'abl_a'}},archive={'sha256':'a'*64})
  for truncated in (False,True):
   directory=self.root/str(truncated);data=b'A'*(4095 if truncated else 4096)
   def remote(command,*args):return data if 'dd if=' in command else (hashlib.sha256(data).hexdigest()+'  /dev/sde1').encode()
   e=SimpleNamespace(device={'serial':'TEST'},transport=SimpleNamespace(boot_id=str(uuid.uuid4()),remote=remote),phase=Mock(),progress=Mock(),check_cancel=Mock())
   if truncated:
    with self.assertRaises(RuntimeError):backup_overwrites(e,p,{'abl_a':part},directory)
   else:self.assertIn('abl_a',backup_overwrites(e,p,{'abl_a':part},directory))
   self.assertEqual(json.loads((directory/'manifest.json').read_text())['complete'],not truncated)
 def test_full_rom_combined_scope_separates_rom_boot_from_linux_default(self):
  rows=inventory(self.p);root=dict(rows['boot_a'],name='lqr_123456789012',partuuid=str(uuid.uuid4()),parent='sda');roots={root['name']:'f'*64}
  targets={r['partition'] for r in self.p['images'].values()}-{'boot_a'};parts=[gate.identity(rows[n]) for n in sorted(targets|{'boot_a'})]+[gate.identity(root)]
  s=dict(kind='combined',parts=parts,full_rom=True,android_install=True,linux_installs=roots,default_target=root['name'],android_boot_write=False,
   rom_targets=sorted(targets),rom_backups={n:dict(identity=gate.identity(rows[n]),sha256='0'*64) for n in targets},
   android_profile_sha256=gate.digest(self.p),android_boot_sha256=self.p['images']['boot.img']['sha256'],previous_boot_sha256='0'*64,gpt_sha256=None)
  gate.validate_scope(s)
  broken=copy.deepcopy(s);broken['linux_installs']={'lqr_000000000000':'f'*64}
  with self.assertRaises(ValueError):gate.validate_scope(broken)
  boot=str(uuid.uuid4());m=dict(installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,usage='temporary-boot-only',generic_ota_version=2,combined_install_version=1,build_id='a'*64)
  for name,value in [(gate.MARKER,json.dumps(m)),('proc/sys/kernel/random/boot_id',boot)]:
   q=self.root/name;q.parent.mkdir(parents=True,exist_ok=True);q.write_text(value)
  req=dict(scope=s,android_profile=self.p,rom_backups_verified=True,boot_id=boot,serial='TEST',build_id=m['build_id'],token='b'*32,plan_id='c'*64)
  probe=dict(device='liuqin',sealed=True,unlocked=True,slot='a',boot_id=boot,serial='TEST',build_id=m['build_id'])
  gate.begin(req,probe,self.root)
  with patch.dict(os.environ,{gate.TOKEN:req['token']}):
   gate.authorize_root(root['name'],'f'*64,self.root)
   with self.assertRaises(RuntimeError):gate.authorize_root(root['name'],'e'*64,self.root)
   with self.assertRaises(RuntimeError):gate.authorize_root('lqr_000000000000','f'*64,self.root)
  gate.authorize_action('linux-stage',dict(native_token=req['token'],plan={'system':{'partition':root}}),self.root)
  gate.authorize_action('install',dict(native_token=req['token']),self.root)
  with self.assertRaises(RuntimeError):gate.authorize_action('linux-stage',dict(native_token=req['token'],plan={'system':{'partition':dict(root,name='lqr_000000000000')}}),self.root)
 def test_ext4_data_format_is_typed_not_arbitrary_command_arguments(self):
  self.p['boot_contract']['data_filesystems']['/data']='ext4';self.p['userdata']=format_settings('userdata','ext4');validate(self.p)
  self.p['userdata']['mkfs_args'].append('/dev/sda')
  with self.assertRaises(ValueError):validate(self.p)

class RecoveryProjectionTests(unittest.TestCase):
    def test_missing_abl_preserves_baseline_and_replacement_uses_padded_digest(self):
        from liuqin_gui.native_ota import project_recovery
        from liuqin_gui.rom_preview import demo_capture

        snapshot, _ = demo_capture('TEST')
        original = copy.deepcopy(snapshot)
        size = snapshot['slots']['a']['match']['abl']['partition_bytes']
        snapshot['partitions'] = {'abl_a': {'size': size}}
        original['partitions'] = copy.deepcopy(snapshot['partitions'])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            kept = project_recovery({'images': {}}, directory, snapshot)
            self.assertEqual(kept['baseline_id'], 'miui-abl-20230422')
            self.assertEqual(kept['status'], 'requires-avb-mode')
            self.assertFalse(kept['recovery_from_rom'])

            payload = b'new ABL'
            path = directory/'abl.img'
            path.write_bytes(payload)
            profile = {'images': {'abl.img': {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()},
                                  'recovery.img': {}}}
            replaced = project_recovery(profile, directory, snapshot)
            self.assertEqual(replaced['projected_abl_sha256'], hashlib.sha256(payload.ljust(size, b'\0')).hexdigest())
            self.assertEqual(replaced['status'], 'unknown-baseline')
            self.assertTrue(replaced['recovery_from_rom'])
            self.assertFalse(replaced['fixed_install_allowed'])
            self.assertFalse(replaced['requirements_satisfied'])
            self.assertEqual(snapshot, original)

            path.write_bytes(b'bad ABL')
            with self.assertRaisesRegex(ValueError, 'ROM 镜像已变化'):
                project_recovery(profile, directory, snapshot)


class BackupReuseTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from liuqin_gui import native_ota
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backups = native_ota
        self.data = b'A' * 4096
        self.part = dict(name='abl_a', node='sde11', parent='sde',
                         partuuid=str(uuid.uuid4()), start=4096, bytes=len(self.data))
        self.profile = dict(images={'abl.img': {'partition': 'abl_a'}},
                            archive={'sha256': 'a'*64})
        self.commands = []

        def remote(command, *args):
            self.commands.append(command)
            if 'dd if=' in command:
                return self.data
            return (hashlib.sha256(self.data).hexdigest()+'  /dev/sde11').encode()

        self.engine = SimpleNamespace(device={'serial': 'TEST'},
            transport=SimpleNamespace(boot_id=str(uuid.uuid4()), remote=remote),
            phase=Mock(), progress=Mock(), check_cancel=Mock())

    def old_backup(self, name='previous'):
        directory = self.root/name
        directory.mkdir()
        (directory/'abl_a.img').write_bytes(self.data)
        manifest = dict(schema=1, complete=True, serial='TEST', boot_id=str(uuid.uuid4()),
            target_rom_sha256='b'*64, files={'abl_a': dict(identity=gate.identity(self.part),
                sha256=hashlib.sha256(self.data).hexdigest(), file='abl_a.img')})
        (directory/'manifest.json').write_text(json.dumps(manifest))
        return directory, manifest

    def save_manifest(self, directory, manifest):
        (directory/'manifest.json').write_text(json.dumps(manifest))

    def backup(self, destination, previous=()):
        return self.backups.backup_overwrites(self.engine, self.profile,
            {'abl_a': self.part}, destination, previous=previous)

    def test_verified_same_content_is_linked_without_usb_image_transfer(self):
        from types import SimpleNamespace
        previous, old_manifest = self.old_backup()
        before = (previous/'manifest.json').read_bytes()
        destination = self.root/'next'
        with patch.object(self.backups.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=512*1024**2)):
            result = self.backup(destination, [previous])
        self.assertTrue(result['abl_a']['reused'])
        self.assertEqual(os.stat(previous/'abl_a.img').st_ino, os.stat(destination/'abl_a.img').st_ino)
        self.assertFalse(any('dd if=' in command for command in self.commands))
        self.assertEqual(sum('sha256sum' in command for command in self.commands), 2)
        self.assertEqual((previous/'manifest.json').read_bytes(), before)
        current = json.loads((destination/'manifest.json').read_text())
        self.assertTrue(current['complete'])
        self.assertEqual(current['boot_id'], self.engine.transport.boot_id)
        self.assertEqual(current['target_rom_sha256'], self.profile['archive']['sha256'])
        self.assertNotEqual(current['boot_id'], old_manifest['boot_id'])

    def test_damaged_foreign_or_stale_candidates_are_read_again(self):
        cases = ('damaged', 'foreign', 'identity', 'incomplete', 'wrong_hash',
                 'bad_filename', 'symlink', 'bad_json')
        for case in cases:
            with self.subTest(case=case):
                previous, manifest = self.old_backup(case)
                if case == 'damaged':
                    (previous/'abl_a.img').write_bytes(b'Z' * len(self.data))
                elif case == 'foreign':
                    manifest['serial'] = 'OTHER'
                elif case == 'identity':
                    manifest['files']['abl_a']['identity']['partuuid'] = str(uuid.uuid4())
                elif case == 'incomplete':
                    manifest['complete'] = False
                elif case == 'wrong_hash':
                    manifest['files']['abl_a']['sha256'] = 'c'*64
                elif case == 'bad_filename':
                    manifest['files']['abl_a']['file'] = '../outside.img'
                elif case == 'symlink':
                    path = previous/'abl_a.img'
                    path.rename(previous/'original.img')
                    path.symlink_to('original.img')
                self.save_manifest(previous, manifest)
                if case == 'bad_json':
                    (previous/'manifest.json').write_text('{invalid')
                self.commands.clear()
                destination = self.root/(case+'-next')
                result = self.backup(destination, [previous])
                self.assertFalse(result['abl_a']['reused'])
                self.assertEqual((destination/'abl_a.img').read_bytes(), self.data)
                self.assertEqual(sum('dd if=' in command for command in self.commands), 1)

    def test_changed_device_content_is_saved_as_a_new_image(self):
        previous, _ = self.old_backup()
        self.data = b'B' * len(self.data)
        destination = self.root/'changed'
        result = self.backup(destination, [previous])
        self.assertFalse(result['abl_a']['reused'])
        self.assertEqual((previous/'abl_a.img').read_bytes(), b'A' * len(self.data))
        self.assertEqual((destination/'abl_a.img').read_bytes(), self.data)

    def test_cross_volume_reuse_copies_locally_and_budgets_its_bytes(self):
        import errno
        from types import SimpleNamespace
        previous, _ = self.old_backup()
        required = 512*1024**2 + len(self.data)
        with patch.object(self.backups.os, 'link', side_effect=OSError(errno.EXDEV, 'cross volume')):
            with patch.object(self.backups.shutil, 'disk_usage',
                              return_value=SimpleNamespace(free=required-1)):
                incomplete = self.root/'insufficient'
                with self.assertRaisesRegex(RuntimeError, '空间不足'):
                    self.backup(incomplete, [previous])
                self.assertFalse(json.loads((incomplete/'manifest.json').read_text())['complete'])
            with patch.object(self.backups.shutil, 'disk_usage',
                              return_value=SimpleNamespace(free=required)):
                destination = self.root/'copied'
                result = self.backup(destination, [previous])
        self.assertTrue(result['abl_a']['reused'])
        self.assertNotEqual(os.stat(previous/'abl_a.img').st_ino, os.stat(destination/'abl_a.img').st_ino)
        self.assertEqual((destination/'abl_a.img').read_bytes(), self.data)
        self.assertFalse(any('dd if=' in command for command in self.commands))

    def test_device_change_after_reuse_keeps_manifest_incomplete(self):
        previous, _ = self.old_backup()
        original = self.engine.transport.remote
        count = 0

        def changed(command, *args):
            nonlocal count
            if 'sha256sum' in command:
                count += 1
                if count == 2:
                    self.data = b'C' * len(self.data)
            return original(command, *args)

        self.engine.transport.remote = changed
        destination = self.root/'changed-during-backup'
        with self.assertRaisesRegex(RuntimeError, '回读不一致'):
            self.backup(destination, [previous])
        self.assertFalse(json.loads((destination/'manifest.json').read_text())['complete'])


if __name__=='__main__':unittest.main(verbosity=2)
