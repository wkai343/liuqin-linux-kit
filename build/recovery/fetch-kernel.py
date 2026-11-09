#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fetch only the pinned stock boot through official Xiaomi OTA byte ranges.

The complete output must match the independent Fastboot ROM boot.img hash.
No updater scripts are run and no unselected partition data is extracted.
Payload schema: https://android.googlesource.com/platform/system/update_engine/+/refs/heads/main/update_metadata.proto
"""
import argparse,bz2,concurrent.futures,hashlib,io,json,lzma,struct,time,urllib.request,zipfile
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]
SOURCE=json.loads((PROJECT/'build/recovery/source.json').read_text())['kernel_ota']
URL=SOURCE['url'];SIZE=SOURCE['bytes']
cache=PROJECT/'inputs/downloads/liuqin-stock-boot-ota'

def get(start,length):
 if length==0:return b''
 if start<0 or length<0 or length>64*1024**2 or start+length>SIZE:raise ValueError('invalid remote range')
 p=cache/f'{start}-{length}'
 if p.exists() and p.stat().st_size==length:return p.read_bytes()
 for i in range(4):
  try:
   req=urllib.request.Request(URL,headers={'Range':f'bytes={start}-{start+length-1}'})
   with urllib.request.urlopen(req,timeout=30) as r:
    if r.status!=206 or r.headers['Content-Range']!=f'bytes {start}-{start+length-1}/{SIZE}':raise ValueError('bad range')
    b=r.read(length+1)
   if len(b)!=length:raise ValueError('short read')
   p.write_bytes(b);return b
  except Exception as e:print('retry',start,length,e,flush=True)
 raise RuntimeError('fetch failed')
class Remote:
 def __init__(self):self.pos=0
 def seekable(self):return True
 def seek(self,p,whence=0):self.pos=p if whence==0 else self.pos+p if whence==1 else SIZE+p;return self.pos
 def tell(self):return self.pos
 def read(self,n=-1):n=SIZE-self.pos if n<0 else n;b=get(self.pos,n);self.pos+=len(b);return b

def varint(b,pos):
 v=0;s=0
 while True:
  x=b[pos];pos+=1;v|=(x&127)<<s
  if x<128:return v,pos
  s+=7
  if s>64:raise ValueError('bad varint')
def proto(b):
 pos=0;fields={}
 while pos<len(b):
  k,pos=varint(b,pos);field,typ=k>>3,k&7
  if typ==0:v,pos=varint(b,pos)
  elif typ==2:n,pos=varint(b,pos);v=b[pos:pos+n];pos+=n
  elif typ in (1,5):n=8 if typ==1 else 4;v=b[pos:pos+n];pos+=n
  else:raise ValueError('bad wire')
  fields.setdefault(field,[]).append(v)
 return fields
def fetch(dest):
 cache.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(Remote()) as z:
  info=z.getinfo('payload.bin');print(info.filename,info.file_size,info.compress_type,info.header_offset,flush=True)
  if info.compress_type!=0:raise ValueError('payload must be stored')
  local=get(info.header_offset,30);n,e=struct.unpack_from('<HH',local,26);offset=info.header_offset+30+n+e
  h=get(offset,24)
  if h[:4]!=b'CrAU' or struct.unpack_from('>Q',h,4)[0]!=2:raise ValueError('unsupported payload')
  msize,ssize=struct.unpack_from('>QI',h,12);m=proto(get(offset+24,msize));blob=offset+24+msize+ssize;block=m.get(3,[4096])[0]
  parts=[proto(p) for p in m[13]];p=next(p for p in parts if p[1]==[b'boot']);target=proto(p[7][0]);length=target[1][0]
  expected=json.loads((PROJECT/'shared/liuqin-rom-images.json').read_text())['images']['boot.img']
  print('boot',length,target[2][0].hex(),'expected',expected,flush=True)
  if length!=expected['bytes'] or target[2][0].hex()!=expected['sha256']:raise ValueError('OTA boot differs from pinned boot')
  ops=[proto(o) for o in p[8]];print('operations',len(ops),flush=True);out=bytearray(length)
  def apply(op):
   typ=op[1][0];extents=[proto(e) for e in op[6]];n=sum(e[2][0]*block for e in extents)
   if n>length or any(e[1][0]*block+e[2][0]*block>length for e in extents):raise ValueError('extent overflow')
   if typ in (6,7):data=bytes(n)
   else:
    packed=get(blob+op[2][0],op[3][0])
    if hashlib.sha256(packed).digest()!=op[8][0]:raise ValueError('operation hash mismatch')
    data=packed if typ==0 else bz2.decompress(packed) if typ==1 else lzma.decompress(packed) if typ==8 else None
    if data is None:raise ValueError('unsupported operation '+str(typ))
   if len(data)!=n:raise ValueError('extent size mismatch')
   pos=0
   for e in extents:
    start=e[1][0]*block;size=e[2][0]*block
    if start+size>length:raise ValueError('range overflow')
    out[start:start+size]=data[pos:pos+size];pos+=size
  with concurrent.futures.ThreadPoolExecutor(12) as pool:list(pool.map(apply,ops))
  if hashlib.sha256(out).hexdigest()!=expected['sha256']:raise ValueError('BOOT HASH MISMATCH')
  dest.parent.mkdir(parents=True,exist_ok=True);pending=dest.with_suffix('.img.pending');pending.write_bytes(out);pending.replace(dest)
  print('VERIFIED BOOT',hashlib.sha256(out).hexdigest(),flush=True)


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--out',type=Path,default=PROJECT/'inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images/boot.img')
 args=parser.parse_args();expected=json.loads((PROJECT/'shared/liuqin-rom-images.json').read_text())['images']['boot.img']
 if args.out.exists():
  if args.out.stat().st_size==expected['bytes'] and hashlib.sha256(args.out.read_bytes()).hexdigest()==expected['sha256']:
   print('Verified cached stock boot:',args.out);return
  raise ValueError('existing boot.img does not match pin; not overwritten')
 fetch(args.out)

if __name__=='__main__':main()
