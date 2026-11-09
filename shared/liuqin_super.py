# SPDX-License-Identifier: MIT
"""Deterministic single-device Virtual A/B super image for admitted full ROMs.

Format: AOSP system/core/fs_mgr/liblp/include/liblp/metadata_format.h.
Only a clean slot-A layout is built; no live metadata or snapshots are edited.
"""
import hashlib,struct
from pathlib import Path

ALIGN=1024**2
MAX_METADATA=65536
SLOTS=3

def named(value):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_]{1,35}',value):raise ValueError('invalid logical partition/group name')
    return value.encode().ljust(36,b'\0')

def layout(spec):
    size=spec['bytes'];group=spec['group'];maximum=spec['group_bytes'];parts=spec['partitions']
    if size%4096 or size<ALIGN or not 0<maximum<=size or not parts or len(parts)>32:raise ValueError('invalid super geometry')
    if len({p['name'] for p in parts})!=len(parts):raise ValueError('duplicate logical partition')
    if any(type(p['bytes']) is not int or p['bytes']<=0 or p['bytes']%4096 for p in parts):raise ValueError('unaligned logical partition')
    if sum(p['bytes'] for p in parts)>maximum:raise ValueError('logical group overflow')
    partitions=bytearray();extents=bytearray();ranges=[];cursor=ALIGN;index=0
    for part in parts:
        cursor=(cursor+ALIGN-1)//ALIGN*ALIGN
        if cursor+part['bytes']>size:raise ValueError('super overflow')
        partitions.extend(struct.pack('<36s4I',named(part['name']+'_a'),1,index,1,1))
        extents.extend(struct.pack('<QIQI',part['bytes']//512,0,cursor//512,0));index+=1
        partitions.extend(struct.pack('<36s4I',named(part['name']+'_b'),1,index,0,2))
        ranges.append(dict(name=part['name'],offset=cursor,bytes=part['bytes'],sha256=part['sha256']));cursor+=part['bytes']
    groups=b''.join(struct.pack('<36sIQ',named(n),0,nbytes) for n,nbytes in [('default',0),(group+'_a',maximum),(group+'_b',maximum)])
    device=struct.pack('<QIIQ36sI',ALIGN//512,ALIGN,0,size,named('super'),0)
    tables=bytes(partitions+extents)+groups+device
    header=bytearray(256);struct.pack_into('<IHHI',header,0,0x414c5030,10,2,256)
    struct.pack_into('<I',header,44,len(tables));header[48:80]=hashlib.sha256(tables).digest()
    offset=0
    for position,table,width in [(80,partitions,52),(92,extents,24),(104,groups,48),(116,device,64)]:
        struct.pack_into('<III',header,position,offset,len(table)//width,width);offset+=len(table)
    struct.pack_into('<I',header,128,1) # VIRTUAL_AB_DEVICE
    header[12:44]=hashlib.sha256(header).digest()
    metadata=bytes(header)+tables
    if len(metadata)>MAX_METADATA:raise ValueError('super metadata overflow')
    geometry=bytearray(struct.pack('<II32sIII',0x616c4467,52,bytes(32),MAX_METADATA,SLOTS,4096))
    geometry[8:40]=hashlib.sha256(geometry).digest()
    geometry=bytes(geometry).ljust(4096,b'\0')
    prefix=bytes(4096)+geometry*2+metadata.ljust(MAX_METADATA,b'\0')*(SLOTS*2)
    if len(prefix)>ALIGN:raise ValueError('super metadata overlaps data')
    return prefix,ranges

def build(directory,output,spec,check=lambda:None):
    prefix,ranges=layout(spec);output=Path(output)
    with output.open('xb') as target:
        target.truncate(spec['bytes']);target.write(prefix)
        for entry in ranges:
            path=Path(directory)/(entry['name']+'.img')
            if path.is_symlink() or path.stat().st_size!=entry['bytes']:raise ValueError('logical image size mismatch')
            target.seek(entry['offset']);h=hashlib.sha256()
            with path.open('rb') as source:
                while data:=source.read(4*1024**2):check();h.update(data);target.write(data)
            if h.hexdigest()!=entry['sha256']:raise ValueError('logical image hash mismatch: '+entry['name'])
    return [dict(offset=0,bytes=len(prefix),sha256=hashlib.sha256(prefix).hexdigest()),
            *[{k:v for k,v in r.items() if k!='name'} for r in ranges]]

def verify(path,spec,check=lambda:None):
    """Verify generated metadata, every logical image and all zero-filled gaps."""
    prefix,ranges=layout(spec);path=Path(path)
    if path.is_symlink() or path.stat().st_size!=spec['bytes']:raise ValueError('super image size mismatch')
    whole=hashlib.sha256()
    with path.open('rb') as f:
        head=f.read(len(prefix))
        if head!=prefix:raise ValueError('super metadata differs from the confirmed layout')
        whole.update(head);cursor=len(prefix)
        for entry in [*ranges,dict(offset=spec['bytes'],bytes=0)]:
            left=entry['offset']-cursor
            while left:
                check();b=f.read(min(left,4*1024**2))
                if not b or b.strip(b'\0'):raise ValueError('super unused area is not zero-filled')
                whole.update(b);left-=len(b)
            left=entry['bytes'];part=hashlib.sha256()
            while left:
                check();b=f.read(min(left,4*1024**2))
                if not b:raise ValueError('truncated super image')
                whole.update(b);part.update(b);left-=len(b)
            if entry['bytes'] and part.hexdigest()!=entry['sha256']:raise ValueError('super logical image differs from payload: '+entry['name'])
            cursor=entry['offset']+entry['bytes']
    return whole.hexdigest()
