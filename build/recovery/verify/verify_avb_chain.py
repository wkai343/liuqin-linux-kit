# SPDX-License-Identifier: MIT
"""Execute acquired ABL's avb_slot_verify with snapshot-only AvbOps callbacks."""
from pathlib import Path
import hashlib,json,struct,time,os
from emulator_core import Core,OLD,ROOT
HERE=Path(os.environ['LIUQIN_FIXED_EVIDENCE']).resolve()
INVENTORY=json.loads((HERE/'partition-inventory.json').read_text())['inventory']
def public_key(data):
 auth=struct.unpack_from('>Q',data,12)[0];off,size=struct.unpack_from('>QQ',data,64)
 return data[256+auth+off:256+auth+off+size]
class Avb(Core):
 def __init__(self,recovery,unlocked=True,missing_system=False,normal=False,slot="_a",rollback_value=0):
  super().__init__();self.unlocked=unlocked;self.rollback_value=rollback_value;self.rollback_writes={};self.slot=slot
  self.parts={'vbmeta_a':(OLD/'vbmeta_a.img').read_bytes(),'vbmeta_system_a':(HERE/'vbmeta_system_a.img').read_bytes(),'recovery_a':Path(recovery).read_bytes()}
  if missing_system:del self.parts['vbmeta_system_a']
  self.parts['dtbo_a']=(OLD/'dtbo_a.payload.img').read_bytes().ljust(25165824,b'\0')
  if normal:
   for n in ['boot_a','vendor_boot_a']:self.parts[n]=(HERE/(n+'.payload.img')).read_bytes()
  self.trusted=public_key(self.parts['vbmeta_a']);self.reads=[];self.callbacks=[]
  self.ops=0x300000;self.request=0x301000;self.output=0x302000
  for offset,fn in [(0x18,self.read_partition),(0x20,self.write_forbidden),(0x28,self.check_key),(0x30,self.rollback),(0x38,self.rollback_write),(0x40,self.device_unlocked),(0x48,self.guid),(0x50,self.part_size),(0x58,self.unsupported),(0x60,self.unsupported)]:
   pc=0x601000+offset*4;self.put64(self.ops+offset,pc);self.hook(pc,fn)
  self.put64(self.ops+0x58,0x13704)
  self.hook(0x12bf4,self.get_handle);self.hook(0x201f0,self.load_header)
  self.put64(0x306000+8,0x306100);self.put32(0x306100+12,4096)
  self.u.mem_write(0x301100,b'recovery\0');self.put64(self.request,0x301100);self.put64(self.request+8,0);
  if struct.unpack_from('<I',self.parts['recovery_a'],40)[0]==0:
   self.u.mem_write(0x301110,b'dtbo\0');self.put64(self.request+8,0x301110);self.put64(self.request+16,0)
  if normal:
   for i,n in enumerate(['boot','dtbo','vendor_boot']):
    np=0x301300+i*32;self.u.mem_write(np,n.encode()+b'\0');self.put64(self.request+i*8,np)
   self.put64(self.request+24,0)
  if slot!='_a':self.parts={n[:-2]+slot:b for n,b in self.parts.items()}
  self.u.mem_write(0x301200,slot.encode()+b'\0')
 def get_handle(self):
  name=self.string(self.reg(0))
  if name not in self.parts:self.ret(3);return
  out=self.reg(1);self.put64(out,0x306200);self.put64(out+8,0x306000);self.ret(0)
 def load_header(self):
  raw=self.read(self.reg(0),144);end=next(i for i in range(0,144,2) if raw[i:i+2]==b'\0\0');name=raw[:end].decode('utf-16-le');b=self.parts.get(name)
  if b is None:raise RuntimeError('missing header '+name)
  ptr=self.allocate(4096);self.u.mem_write(ptr,b[:4096]);self.put64(self.reg(1),ptr);self.put32(self.reg(2),4096);self.ret(0)
 def write_forbidden(self):raise RuntimeError('AVB write callback '+hex(self.pc())+' lr='+hex(self.u.reg_read(2))+' args='+repr([self.reg(i) for i in range(4)]))
 def unsupported(self):raise RuntimeError('unexpected AvbOps callback')
 def read_partition(self):
  _,name,offset,count,dest,out=(self.reg(i) for i in range(6));name=self.string(name)
  if offset>>63:offset-=1<<64
  b=self.parts.get(name)
  if b is None:self.reads.append(dict(name=name,missing=True));self.ret(3);return
  if offset<0:offset+=len(b)
  if offset<0 or offset+count>len(b):self.ret(4);return
  self.u.mem_write(dest,b[offset:offset+count]);self.put64(out,count);self.reads.append(dict(name=name,offset=offset,bytes=count));print('read',name,offset,count,flush=True);self.ret(0)
 def part_size(self):
  name=self.string(self.reg(1));b=self.parts.get(name)
  if b is None:self.ret(3);return
  self.put64(self.reg(2),len(b));self.ret(0)
 def check_key(self):
  actual=self.read(self.reg(1),self.reg(2));good=actual==self.trusted
  self.u.mem_write(self.reg(5),bytes([good]));self.callbacks.append(dict(root_key_matches_snapshot=good));self.ret(0)
 def rollback_write(self):
  if self.unlocked:raise RuntimeError("unexpected rollback write in unlocked scenario")
  self.rollback_writes[self.reg(1)]=self.reg(2);self.callbacks.append(dict(simulated_rollback_write_location=self.reg(1),value=self.reg(2),host_memory_only=True));self.ret(0)
 def rollback(self):
  index=self.reg(1);self.put64(self.reg(2),self.rollback_writes.get(index,self.rollback_value));self.callbacks.append(dict(rollback_index_location=index,simulated_stored_value=self.rollback_value));self.ret(0)
 def device_unlocked(self):self.u.mem_write(self.reg(1),bytes([self.unlocked]));self.ret(0)
 def guid(self):
  name=self.string(self.reg(1));p=INVENTORY.get(name)
  if p is None:raise RuntimeError('unknown GUID request '+name)
  b=p['partuuid'].encode()+b'\0';assert len(b)<=self.reg(3);self.u.mem_write(self.reg(2),b);self.ret(0)
 def verify(self):
  start=time.monotonic();result=self.run(0xd5e0,(self.ops,self.request,0x301200,int(self.unlocked),0,self.output),count=500000000)
  data=self.get64(self.output);report=dict(result=result,slot_data=bool(data),seconds=round(time.monotonic()-start,3),reads=self.reads,callbacks=self.callbacks,events=self.events,loaded_partitions=[])
  if data:
   images=self.get64(data+0x18);count=self.get64(data+0x20)
   for i in range(count):
    row=images+i*24;report['loaded_partitions'].append(dict(name=self.string(self.get64(row)),bytes=self.get64(row+16)))
   report['cmdline']=self.string(self.get64(data+0x28))
  return report
if __name__=='__main__':
 import argparse
 ap=argparse.ArgumentParser();ap.add_argument('--image',default=str(OLD/'OFFLINE-ONLY-native-recovery-with-avb.img'));ap.add_argument('--locked',action='store_true');ap.add_argument('--missing-system',action='store_true');a=ap.parse_args()
 machine=Avb(a.image,not a.locked,a.missing_system)
 try:r=machine.verify();print(json.dumps(r,indent=2));(HERE/'avb-latest.json').write_text(json.dumps(r,indent=2)+'\n')
 except Exception as e:print('ERROR',repr(e),'pc',hex(machine.pc()),'lr',hex(machine.u.reg_read(2)),'alloc',hex(machine.next_alloc),flush=True);raise
