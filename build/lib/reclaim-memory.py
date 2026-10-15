#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Remove only the audited Liuqin MTE reservation; never change firmware RAM ranges."""
import argparse
from pathlib import Path
import shutil
import struct
import subprocess

REGION='/reserved-memory/memory@c0000000'
EXPECTED=(0,0xc0000000,0,0x20000000)

def tree(path):
    data=Path(path).read_bytes();header=struct.unpack_from('>10I',data)
    if header[0]!=0xd00dfeed or header[1]>len(data):raise ValueError('无效 DTB。')
    strings=data[header[3]:header[3]+header[8]];pos=header[2];stack=[];nodes={}
    while True:
        token=struct.unpack_from('>I',data,pos)[0];pos+=4
        if token==1:
            end=data.index(0,pos);stack.append(data[pos:end].decode());pos=(end+4)&~3
            nodes['/'+'/'.join(stack[1:])]={}
        elif token==2:stack.pop()
        elif token==3:
            length,offset=struct.unpack_from('>II',data,pos);pos+=8;end=strings.index(0,offset)
            nodes['/'+'/'.join(stack[1:])][strings[offset:end].decode()]=data[pos:pos+length];pos=(pos+length+3)&~3
        elif token==4:pass
        elif token==9:break
        else:raise ValueError('DTB 结构令牌无效。')
    return nodes

def apply(source,output,fdtput='fdtput'):
    source,output=Path(source),Path(output)
    if output.exists() or output.is_symlink():raise ValueError('输出已存在，未覆盖。')
    before=tree(source)
    if b'xiaomi,liuqin' not in before['/'].get('compatible',b'').split(b'\0'):
        raise ValueError('内存回收仅适用于 liuqin。')
    props=before.get(REGION)
    if props is not None:
        if props.get('reg')!=struct.pack('>4I',*EXPECTED) or 'no-map' not in props:
            raise ValueError('MTE 地址、大小或属性已改变，停止回收。')
        if 'phandle' in props or 'linux,phandle' in props:
            raise ValueError('MTE 已具有消费者句柄，停止回收。')
    output.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,output)
    if props is not None:subprocess.run([str(fdtput),'-r',str(output),REGION],check=True)
    expected={p:dict(v) for p,v in before.items() if p!=REGION}
    if tree(output)!=expected:raise ValueError('回收过程改变了其他设备树属性。')
    return 512 if props is not None else 0

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--fdtput',default='fdtput');args=parser.parse_args()
    print('MTE 预留清理：'+str(apply(args.input,args.output,args.fdtput))+' MiB；固件内存映射保持原样。')
