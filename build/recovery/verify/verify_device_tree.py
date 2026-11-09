# SPDX-License-Identifier: MIT
"""Original ABL SoC/board DT matching with reported board identity inputs."""
from pathlib import Path
import hashlib,json,struct,zlib,os
from emulator_core import Core,OLD,ROOT
HERE=Path(os.environ['LIUQIN_FIXED_EVIDENCE']).resolve()
class Trees(Core):
 def __init__(self,project=16,foundry=0,subtype=0,version=0x10000,ddr=0,pmic0=0x2f,soc_id=531):
  super().__init__();self.pmic0=pmic0;self.pmic_queries=[]
  # These are input data fields, not patches to matching instructions.
  for offset,value in [(0,8),(4,version),(8,subtype),(0x10,soc_id),(0x24,0x10000),(0x28,foundry),(0x2c,ddr)]:self.put32(0xeeb24+offset,value)
  self.put32(0xeeb5c+0x10,project)
  self.hook(0x18008,self.pmic)
 def pmic(self):
  index=self.reg(0);model=self.pmic0 if index==0 else 0
  self.u.mem_write(self.reg(1),struct.pack("<III",model,0,0));self.put64(self.reg(2),0x10004);self.pmic_queries.append(index);self.ret(0)
 def choose(self,image_path,matching_only=False):
  b=Path(image_path).read_bytes();ks=struct.unpack_from('<I',b,8)[0];header_version=struct.unpack_from('<I',b,40)[0]
  assert header_version==0
  kernel=b[4096:4096+ks];gzip=zlib.decompressobj(31);decompressed=gzip.decompress(kernel);assert gzip.eof
  dtb_offset=len(kernel)-len(gzip.unused_data);dtb=gzip.unused_data
  assert dtb[:4]==b'\xd0\x0d\xfe\xed'
  base=self.allocate(len(kernel)+4096);self.u.mem_write(base,kernel)
  out=self.allocate(2*1024**2)
  soc=self.run(0x22d00,(base,len(kernel),dtb_offset,out))
  dtbo=(OLD/'dtbo_a.payload.img').read_bytes();table=self.allocate(len(dtbo)+4096);self.u.mem_write(table,dtbo)
  board=self.run(0x24318,(0x302000,table))
  entries=[]
  for i in range(struct.unpack_from('>I',dtbo,16)[0]):
   size,off=struct.unpack_from('>II',dtbo,32+32*i)
   if board==table+off:entries.append(dict(index=i,offset=off,bytes=size,sha256=hashlib.sha256(dtbo[off:off+size]).hexdigest()))
  if matching_only:
   return dict(soc_match=bool(soc),board_matches=entries)
  info=0x302000;params=0x303000;full=self.allocate(len(b)+4096);self.u.mem_write(full,b)
  self.u.mem_write(info,b'\1\1');self.put32(info+0x458,0);self.put64(info+0x428,2)
  for i,(name,ptr,size) in enumerate([('recovery',full,len(b)),('dtbo',table,len(dtbo))]):
   nameptr=0x304000+i*128;self.u.mem_write(nameptr,name.encode()+b'\0')
   self.u.mem_write(info+0x128+24*i,struct.pack('<QQQ',nameptr,ptr,size))
  for off,val in [(8,full),(16,len(b)),(0x58,out)]:self.put64(params+off,val)
  for off,val in [(0x6c,4096),(0x70,ks),(0x78,struct.unpack_from('<I',b,16)[0]),(0x84,dtb_offset)]:self.put32(params+off,val)
  result=self.run(0x1d090,(info,params),count=1000000000)
  merged=None
  if result==0:
   size=struct.unpack('>I',self.read(out+4,4))[0];assert size<=2*1024**2
   merged=self.read(out,size);self.merged_dtb=merged
  return dict(full_load_status=hex(result),merged_sha256=hashlib.sha256(merged).hexdigest() if merged else None,soc_match=bool(soc),soc_offset=soc-base if soc else None,expected_soc_offset=dtb_offset,dtb_sha256=hashlib.sha256(dtb).hexdigest(),kernel_sha256=hashlib.sha256(decompressed).hexdigest(),board_matches=entries,pmic_query_count=len(self.pmic_queries))
if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--image',type=Path,default=HERE/'OFFLINE-v0.img');a=parser.parse_args()
 machine=Trees()
 try:r=machine.choose(a.image);print(json.dumps(r,indent=2));(HERE/'dt-latest.json').write_text(json.dumps(r,indent=2)+'\n')
 except Exception as e:print('ERROR',repr(e),'pc',hex(machine.pc()),'lr',hex(machine.u.reg_read(2)),flush=True);raise
