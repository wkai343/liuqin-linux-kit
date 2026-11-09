#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the typed GPT writer against sparse files using real sgdisk.

Only /dev, /sys, proc identity and blockdev are modeled. sgdisk arguments use
4096-byte logical sectors in production; the adapter scales them to the
512-byte sectors supported by ordinary host files. No block device is opened.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'shared'))
import liuqin_layout as L
from liuqin_multiboot import plan,layout_payload,verify_layout

ADAPTER=r'''#!/usr/bin/env python3
import json,os,re,shutil,subprocess,sys
from pathlib import Path
root=Path(os.environ['FIXTURE']);disk=root/'dev/sda'
sys.path.insert(0,os.environ['SHARED'])
import liuqin_layout as L
real=os.environ['REAL_SGDISK']
def call(args):
    return subprocess.run([real,*args,str(disk)],capture_output=True,text=True)
def parts():
    return L.parse_print(call(['-p']).stdout)
def sync_nodes():
    current=parts()
    for node in (root/'sys/class/block').glob('sda[0-9]*'):shutil.rmtree(node)
    for part in current.partitions:
        node='sda'+str(part.number);folder=root/'sys/class/block'/node;folder.mkdir()
        info=call(['-i',str(part.number)]).stdout
        guid=re.search(r'^Partition unique GUID: (\S+)',info,re.M)[1].lower()
        (folder/'dev').write_text('8:'+str(part.number)+'\n')
        (folder/'partition').write_text(str(part.number)+'\n')
        (folder/'size').write_text(str(part.size//512)+'\n')
        (folder/'uevent').write_text('PARTNAME='+part.name+'\nPARTUUID='+guid+'\n')
        path=root/'dev'/node
        if not path.exists():path.touch()
args=sys.argv[1:]
if args[0]=='sync':sync_nodes();sys.exit()
if args[0]=='blockdev':
    operation,node=args[1:];name=Path(node).name
    with (root/'operations').open('a') as f:f.write(operation+' '+name+'\n')
    state=json.loads((root/'flags').read_text())
    if operation=='--getro':print(state.get(name,1))
    elif operation in ('--setro','--setrw'):
        state[name]=1 if operation=='--setro' else 0;(root/'flags').write_text(json.dumps(state))
    elif operation=='--rereadpt':sync_nodes()
    else:raise SystemExit('unexpected blockdev')
    sys.exit()
args=args[:-1]
if args==['-p']:
    data=parts();total=data.total_sectors//8
    print(f'Disk /dev/sda: {total} sectors')
    print('Sector size (logical/physical): 4096/4096 bytes')
    print(f'First usable sector is 6, last usable sector is {total-6}')
    print(f'Partition table holds up to {data.entry_count} entries')
    print('Number  Start (sector) End (sector) Size Code Name')
    for p in data.partitions:print(f'{p.number} {p.first_sector//8} {(p.last_sector+1)//8-1} 1.0 GiB 8300 {p.name}')
    sys.exit()
if args[:1]==['-i']:
    result=call(args);text=result.stdout
    for kind in ('First','Last'):
        def convert(match):
            value=int(match[1]);return kind+' sector: '+str(value//8 if kind=='First' else (value+1)//8-1)
        text=re.sub(r'^'+kind+r' sector: (\d+)',convert,text,flags=re.M)
    print(text,end='');sys.exit(result.returncode)
scaled=[]
for arg in args:
    if arg.startswith('--new='):
        number,first,last=arg.split('=',1)[1].split(':')
        arg=f'--new={number}:{int(first)*8}:{(int(last)+1)*8-1}'
    scaled.append(arg)
with (root/'operations').open('a') as f:f.write('sgdisk '+repr(args)+'\n')
result=call(scaled);print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr);sys.exit(result.returncode)
'''

class DeviceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.sg=shutil.which('sgdisk');self.bb=shutil.which('busybox')
        self.assertTrue(self.sg and self.bb,'run in Devbox')
        for directory in ('dev','sys/class/block/sda/queue','etc','run','bin'):(self.root/directory).mkdir(parents=True,exist_ok=True)
        (self.root/'dev/zero').symlink_to('/dev/zero')
        self.disk=self.root/'dev/sda'
        with self.disk.open('wb') as f:f.truncate(L.STOCK_TOTAL_SECTORS*4096)
        args=['--clear','--set-alignment=1','--resize-table=64']
        for number,first,last,name in L.STOCK_TABLE:
            args += [f'--new={number}:{first*8}:{(last+1)*8-1}',f'--typecode={number}:8300',f'--change-name={number}:{name}']
        subprocess.run([self.sg,*args,str(self.disk)],check=True,capture_output=True)
        self.env=dict(os.environ,FIXTURE=str(self.root),SHARED=str(PROJECT/'shared'),REAL_SGDISK=self.sg)
        self.adapter=self.root/'bin/adapter';self.adapter.write_text(ADAPTER);self.adapter.chmod(0o755)
        (self.root/'flags').write_text('{}');(self.root/'operations').touch()
        (self.root/'sys/class/block/sda/dev').write_text('8:0\n')
        (self.root/'sys/class/block/sda/queue/logical_block_size').write_text('4096\n')
        subprocess.run([self.adapter,'sync'],env=self.env,check=True)
        with (self.root/'dev/sda35').open('wb') as f:f.write(b'\xff'*(17*1024**2))
        (self.root/'boot_id').write_text('11111111-1111-4111-8111-111111111111\n')
        (self.root/'mountinfo').touch();(self.root/'etc/liuqin-installer').write_text('liuqin\n');(self.root/'etc/liuqin-multiboot').write_text('1\n')
        busy=self.root/'bin/busybox'
        busy.write_text('#!/bin/sh\nif [ "$1" = blockdev ]; then exec '+str(self.adapter)+' "$@"; fi\nif [ "$1" = mdev ]; then exec '+str(self.adapter)+' sync; fi\nexec '+self.bb+' "$@"\n');busy.chmod(0o755)
        text=(PROJECT/'device/installer/multiboot-layout.sh').read_text()
        for old,new in (('/bin/busybox',str(busy)),('/usr/sbin/sgdisk',str(self.adapter)),
                        ('/proc/sys/kernel/random/boot_id',str(self.root/'boot_id')),
                        ('/proc/self/mountinfo',str(self.root/'mountinfo')),
                        ('/sys/class/block',str(self.root/'sys/class/block')),('/dev/',str(self.root/'dev')+'/'),('/etc/',str(self.root/'etc')+'/')):
            text=text.replace(old,new)
        text=text.replace('case $plan in /run/*)', 'case $plan in '+str(self.root)+'/run/*)').replace('[ -b ', '[ -f ')
        self.script=self.root/'script';self.script.write_text(text)
        ids=[str(uuid.uuid4()),str(uuid.uuid4())]
        self.config=dict(schema_version=1,userdata_gib=32,boot_gib=2,boot_action='set-default',default_id=ids[0],
            roots=[dict(id=id,name='Linux',size_gib=32,filesystem='ext4') for id in ids])
        self.report=self.run_sg('-p');self.info=self.run_sg('-i','35')
        self.planned=plan(L.parse_print(self.report),self.config,str(uuid.uuid4()))

    def run_sg(self,*args):
        return subprocess.check_output([self.adapter,*args,str(self.disk)],env=self.env,text=True)

    def fingerprint(self):
        with self.disk.open('rb') as f:
            head=f.read(65536);f.seek(-65536,2);return hashlib.sha256(head+f.read()).hexdigest()

    def execute(self,payload=None,report_hash=None):
        payload=payload or layout_payload(self.planned,self.info)
        source=self.root/'run/plan';source.write_bytes(payload)
        return subprocess.run([self.bb,'sh',str(self.script),(self.root/'boot_id').read_text().strip(),str(source),
            hashlib.sha256(payload).hexdigest(),report_hash or hashlib.sha256(self.report.encode()).hexdigest(),
            hashlib.sha256(self.info.encode()).hexdigest(),'ERASE-LIUQIN-USERDATA'],env=self.env,capture_output=True,text=True)

    def test_real_gpt_write_preserves_prefix_and_userdata_guid(self):
        result=self.execute();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        verify_layout(L.parse_print(self.run_sg('-p')),self.planned)
        old=re.search(r'Partition unique GUID: (\S+)',self.info)[1]
        self.assertIn('Partition unique GUID: '+old,self.run_sg('-i','35'))
        self.assertEqual((self.root/'dev/sda35').read_bytes()[:16*1024**2],bytes(16*1024**2))
        self.assertTrue(all(v==1 for v in json.loads((self.root/'flags').read_text()).values()))

    def test_corrupt_plan_and_changed_report_never_open_write_window(self):
        payload=layout_payload(self.planned,self.info)
        for defect in ('range','identity','occupied','hash','role','long-name'):
            changed=payload
            if defect=='range':changed=payload.replace(b'linux_boot ',b'linux_boot ',1).replace(str(self.planned['store']['first_sector']).encode(),b'1',1)
            elif defect=='identity':changed=payload.replace(re.search(rb'Partition unique GUID: (\S+)',self.info.encode())[1],b'00000000-0000-0000-0000-000000000000')
            elif defect=='occupied':changed=payload.replace(f"{self.planned['store']['number']} linux_boot".encode(),b'1 linux_boot')
            elif defect=='role':changed=payload.replace(b'linux_boot',b'super')
            elif defect=='long-name':changed=payload.replace(self.planned['systems'][0]['partition']['name'].encode(),b'linux_r_111111111111')
            before=self.fingerprint();(self.root/'operations').write_text('')
            result=self.execute(changed,'0'*64 if defect=='hash' else None)
            self.assertNotEqual(result.returncode,0,defect)
            self.assertEqual(self.fingerprint(),before,defect)
            self.assertNotIn('--setrw',(self.root/'operations').read_text(),defect)

    def test_userdata_and_store_without_linux(self):
        self.config.update(partition_only=True, roots=[], boot_action='install-only')
        self.planned=plan(L.parse_print(self.report),self.config,str(uuid.uuid4()))
        result=self.execute();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        verify_layout(L.parse_print(self.run_sg('-p')),self.planned)
        self.assertFalse(any(p['role']=='root' for p in self.planned['partitions']))

    def test_linux_slots_without_userdata(self):
        self.config.update(partition_only=True, userdata_gib=0, boot_action='install-only')
        self.planned=plan(L.parse_print(self.report),self.config,str(uuid.uuid4()))
        result=self.execute();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        verify_layout(L.parse_print(self.run_sg('-p')),self.planned)
        self.assertFalse(any(p['name']=='userdata' for p in self.planned['partitions']))

if __name__=='__main__':unittest.main(verbosity=2)
