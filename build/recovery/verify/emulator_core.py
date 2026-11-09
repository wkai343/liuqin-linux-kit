# SPDX-License-Identifier: MIT
"""Pinned ABL sandbox: original instructions, host RAM and explicit service hooks."""
from pathlib import Path
import struct,hashlib,os
from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE,UC_HOOK_MEM_INVALID
from unicorn.arm64_const import *
ROOT=Path(__file__).resolve().parents[3]
OLD=Path(os.environ['LIUQIN_ABL_EVIDENCE']).resolve()
CODE=(OLD/'LinuxLoader.mapped.bin').read_bytes()
assert hashlib.sha256(CODE).hexdigest()=='6e8336e6de07964862374758047ab10e4866f0d93921916cf42850f3c35e3df5'
STOP=0x600000
class Core:
 def __init__(self):
  self.u=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
  for addr,size in [(0,0x200000),(0x200000,0x300000),(0x600000,0x10000),(0x10000000,0x30000000)]:self.u.mem_map(addr,size)
  self.u.mem_write(0,CODE);self.u.reg_write(UC_ARM64_REG_CPACR_EL1,3<<20)
  self.u.reg_write(UC_ARM64_REG_SP,0x2ff000);self.put64(0x400000,0x4ff000)
  self.next_alloc=0x10000000;self.allocs={};self.hooks={};self.events=[];self.stopped=False
  self.hook(STOP,self.stop)
  self.hook(0x50570,lambda:self.ret(0x400000))
  self.hook(0x8974,self.ascii_len)
  self.hook(0x62ce0,self.copy)
  for pc in [0x6c0c,0x6c34,0x6658]:self.hook(pc,lambda:self.ret(0))
  self.hook(0x5e00,self.malloc);self.hook(0x5f34,self.free)
  self.hook(0x5a4c,lambda:self.ret(0))
  for pc in [0x6d44,0x6a54,0x9390,0x16438,0x8f88]:self.hook(pc,lambda pc=pc:(_ for _ in ()).throw(RuntimeError('firmware assertion at '+hex(pc))))
  self.u.hook_add(UC_HOOK_MEM_INVALID,self.invalid)
  self.put64(0xeeaf8,0x307000)
  for offset,pc,fn in [(0x40,0x602300,self.allocate_pool),(0x48,0x602400,lambda:self.ret(0)),(0x140,0x602000,self.locate),(0x160,0x602100,self.copy),(0x168,0x602200,self.setmem)]:
   self.put64(0x307000+offset,pc);self.hook(pc,fn)
  for offset,fn in [(0x10,self.hash_init),(0x18,self.hash_update),(0x20,self.hash_final)]:
   pc=0x603000+offset;self.put64(0x305000+offset,pc);self.hook(pc,fn)
 def hook(self,pc,fn):
  self.hooks[pc]=fn
  self.u.hook_add(UC_HOOK_CODE,lambda u,addr,size,ctx:fn(),begin=pc,end=pc)
 def ascii_len(self):
  ptr=self.reg(0);offset=0
  while offset<1024**2:
   block=self.read(ptr+offset,64);end=block.find(b'\0')
   if end>=0:self.ret(offset+end);return
   offset+=64
  raise RuntimeError('unbounded AsciiStrLen input')
 def allocate_pool(self):self.put64(self.reg(2),self.allocate(self.reg(1)));self.ret(0)
 def locate(self):
  guid=self.read(self.reg(0),16)
  if guid!=self.read(0xe934c,16):raise RuntimeError('unsupported EFI protocol '+guid.hex())
  self.put64(self.reg(2),0x305000);self.ret(0)
 def hash_init(self):
  assert self.read(self.reg(1),16)==self.read(0xe935c,16),'unexpected hash algorithm'
  self.hasher=hashlib.sha256();self.ret(0)
 def hash_update(self):self.hasher.update(self.read(self.reg(1),self.reg(2)));self.ret(0)
 def hash_final(self):self.u.mem_write(self.reg(1),self.hasher.digest());self.ret(0)
 def copy(self):
  dst,src,size=(self.reg(i) for i in range(3));assert size<=256*1024**2
  self.u.mem_write(dst,self.read(src,size));self.ret(dst)
 def setmem(self):
  ptr,size,byte=(self.reg(i) for i in range(3));assert size<=256*1024**2
  self.u.mem_write(ptr,bytes([byte&255])*size);self.ret(ptr)
 def invalid(self,u,access,addr,size,value,ctx):raise RuntimeError(f'unmapped access {access} at {addr:x} pc {self.pc():x}')
 def pc(self):return self.u.reg_read(UC_ARM64_REG_PC)
 def reg(self,n):return self.u.reg_read(UC_ARM64_REG_X0+n)
 def args(self,*values):
  for i,v in enumerate(values):self.u.reg_write(UC_ARM64_REG_X0+i,v)
 def ret(self,v=0):self.u.reg_write(UC_ARM64_REG_X0,v&((1<<64)-1));self.u.reg_write(UC_ARM64_REG_PC,self.u.reg_read(UC_ARM64_REG_LR))
 def stop(self):self.stopped=True;self.u.emu_stop()
 def malloc(self):
  size=self.reg(0)
  if size>256*1024**2:self.events.append(dict(oversized_allocation=size));self.ret(0)
  else:self.ret(self.allocate(size))
 def allocate(self,size):
  assert 0<=size<=256*1024**2,size
  ptr=self.next_alloc;self.next_alloc+=(size+63)//64*64+64
  assert self.next_alloc<0x40000000
  self.allocs[ptr]=size;return ptr
 def free(self):self.ret(0)
 def put32(self,p,v):self.u.mem_write(p,struct.pack('<I',v))
 def put64(self,p,v):self.u.mem_write(p,struct.pack('<Q',v))
 def get32(self,p):return struct.unpack('<I',self.u.mem_read(p,4))[0]
 def get64(self,p):return struct.unpack('<Q',self.u.mem_read(p,8))[0]
 def read(self,p,n):return bytes(self.u.mem_read(p,n))
 def string(self,p):return self.read(p,512).split(b'\0')[0].decode()
 def run(self,pc,args=(),count=100000000):
  self.args(*args);self.u.reg_write(UC_ARM64_REG_LR,STOP);self.stopped=False
  self.u.emu_start(pc,0x610000,count=count)
  if not self.stopped:raise RuntimeError('instruction limit at '+hex(self.pc()))
  return self.reg(0)
