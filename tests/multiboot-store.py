#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Boot-store script tests on a real ext4 image with simulated mounts only."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.bb=shutil.which('busybox');self.mkfs=shutil.which('mkfs.ext4');self.assertTrue(self.bb and self.mkfs)
        for name in ('dev/disk/by-partlabel','sys/class/block/sda','sys/class/block/sda36','etc','bin','backing'):
            (self.root/name).mkdir(parents=True,exist_ok=True)
        self.partuuid='11111111-1111-4111-8111-111111111111';self.fsuuid='22222222-2222-4222-8222-222222222222'
        self.image=self.root/'dev/sda36'
        with self.image.open('wb') as f:f.truncate(64*1024**2)
        (self.root/'dev/sda').touch();(self.root/'dev/disk/by-partlabel/boot_a').write_bytes(b'current-boot')
        (self.root/'sys/class/block/sda/dev').write_text('8:0\n')
        (self.root/'sys/class/block/sda36/dev').write_text('8:36\n')
        (self.root/'sys/class/block/sda36/size').write_text(str(2*1024**3//512)+'\n')
        (self.root/'sys/class/block/sda36/uevent').write_text('PARTNAME=linux_boot\nPARTUUID='+self.partuuid+'\n')
        (self.root/'etc/liuqin-installer').write_text('liuqin\n');(self.root/'etc/liuqin-multiboot').write_text('1\n')
        (self.root/'boot_id').write_text('33333333-3333-4333-8333-333333333333\n')
        (self.root/'mountinfo').touch();(self.root/'meminfo').write_text('MemAvailable: 8388608 kB\n')
        (self.root/'flags').write_text('{}');(self.root/'operations').touch()
        self.env=dict(os.environ,FIXTURE=str(self.root),REAL_BUSYBOX=self.bb,PATH=str(self.root/'bin')+':'+os.environ['PATH'])
        helper=self.root/'bin/helper'
        helper.write_text('''#!/usr/bin/env python3
import json,os,shutil,sys
from pathlib import Path
root=Path(os.environ['FIXTURE']);args=sys.argv[1:]
if args[0]=='blockdev':
    state=json.loads((root/'flags').read_text());op,node=args[1:];name=Path(node).name
    with (root/'operations').open('a') as f:f.write(op+' '+name+'\\n')
    if op=='--getro':print(state.get(name,1))
    else:state[name]=1 if op=='--setro' else 0;(root/'flags').write_text(json.dumps(state))
elif args[0]=='wget':shutil.copyfile(root/'payload',args[2])
elif args[0]=='mount':shutil.copytree(root/'backing',args[-1],dirs_exist_ok=True)
elif args[0]=='umount':
    shutil.rmtree(root/'backing');shutil.copytree(args[-1],root/'backing')
    shutil.rmtree(args[-1]);Path(args[-1]).mkdir()
else:os.execv(os.environ['REAL_BUSYBOX'],[os.environ['REAL_BUSYBOX'],*args])
''');helper.chmod(0o755)
        for name in ('mount','umount'):
            p=self.root/'bin'/name;p.write_text('#!/bin/sh\nexec '+str(helper)+' '+name+' "$@"\n');p.chmod(0o755)
        text=(PROJECT/'device/installer/multiboot-store.sh').read_text()
        for old,new in (('/bin/busybox',str(helper)),('/usr/sbin/mkfs.ext4',self.mkfs),('/usr/bin/tar',shutil.which('tar')),
            ('/proc/sys/kernel/random/boot_id',str(self.root/'boot_id')),('/proc/self/mountinfo',str(self.root/'mountinfo')),
            ('/proc/meminfo',str(self.root/'meminfo')),('/sys/class/block',str(self.root/'sys/class/block')),
            ('/dev/',str(self.root/'dev')+'/'),('/etc/',str(self.root/'etc')+'/'),('/run/liuqin-multiboot-store',str(self.root/'run/store'))):
            text=text.replace(old,new)
        text=text.replace('[ -b ','[ -f ')
        self.script=self.root/'script';self.script.write_text(text)
        result=self.execute('initialize');self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def execute(self,action,content=None,expected='-'):
        args=[self.bb,'sh',str(self.script),(self.root/'boot_id').read_text().strip(),action,self.partuuid,self.fsuuid,str(2*1024**3)]
        if content is not None:
            (self.root/'payload').write_bytes(content)
            args+=['http://fixture/payload',hashlib.sha256(content).hexdigest(),str(len(content)),expected]
        return subprocess.run(args,env=self.env,capture_output=True,text=True)

    def archive(self,bad_hash=False):
        files={'registry.json':b'{"default_id":null}', 'systems/example/images/boot.img':b'boot-image',
               'transactions/example/previous-boot.img':b'previous'}
        sums=''.join(('0'*64 if bad_hash else hashlib.sha256(data).hexdigest())+'  '+name+'\n' for name,data in files.items())
        files['SHA256SUMS']=sums.encode();stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            for name,data in files.items():
                entry=tarfile.TarInfo(name);entry.size=len(data);archive.addfile(entry,io.BytesIO(data))
        return stream.getvalue()

    def test_stage_and_publish_with_readback(self):
        result=self.execute('stage',self.archive());self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'backing/systems/example/images/boot.img').read_bytes(),b'boot-image')
        content=b'{"default_id":"selected"}'
        expected=hashlib.sha256(b'current-boot').hexdigest()
        result=self.execute('publish',content,expected);self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'backing/registry.json').read_bytes(),content)
        self.assertNotIn('registry.json',(self.root/'backing/SHA256SUMS').read_text())
        self.assertTrue(all(v==1 for v in json.loads((self.root/'flags').read_text()).values()))

    def test_bad_image_hash_and_wrong_boot_do_not_commit(self):
        result=self.execute('stage',self.archive(bad_hash=True));self.assertNotEqual(result.returncode,0)
        self.assertTrue(all(v==1 for v in json.loads((self.root/'flags').read_text()).values()))
        before=(self.root/'backing/registry.json').read_bytes()
        (self.root/'operations').write_text('')
        result=self.execute('publish',b'{"default_id":"wrong"}','0'*64)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.root/'backing/registry.json').read_bytes(),before)
        self.assertNotIn('--setrw',(self.root/'operations').read_text())

if __name__=='__main__':unittest.main(verbosity=2)
