# SPDX-License-Identifier: MIT
"""Bounded full-OTA payload import. Never execute an updater or postinstall hook.

Wire schema: AOSP system/update_engine/update_metadata.proto.
Supported data operations are REPLACE, REPLACE_BZ and REPLACE_XZ only.
"""
import base64,bz2,hashlib,lzma,re,struct,zipfile
from pathlib import Path

def varint(data,pos):
    value=0
    for shift in range(0,70,7):
        if pos>=len(data):raise ValueError('truncated protobuf integer')
        byte=data[pos];pos+=1;value|=(byte&127)<<shift
        if byte<128:
            if value>=2**64:raise ValueError('protobuf integer overflow')
            return value,pos
    raise ValueError('invalid protobuf integer')

def fields(data):
    pos=0;result={}
    while pos<len(data):
        tag,pos=varint(data,pos);number,wire=tag>>3,tag&7
        if number==0:raise ValueError('invalid protobuf field')
        if wire==0:value,pos=varint(data,pos)
        else:
            if wire==2:size,pos=varint(data,pos)
            elif wire in (1,5):size=8 if wire==1 else 4
            else:raise ValueError('unsupported protobuf wire type')
            if pos+size>len(data):raise ValueError('truncated protobuf field')
            value=data[pos:pos+size];pos+=size
        result.setdefault(number,[]).append(value)
    return result

def one(mapping,key,default=None):
    values=mapping.get(key,[default])
    if len(values)!=1:raise ValueError('duplicate singular payload field')
    return values[0]

def metadata(path):
    path=Path(path)
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)):raise ValueError('OTA ZIP contains duplicate entries')
        info=archive.getinfo('payload.bin')
        if info.compress_type!=zipfile.ZIP_STORED or info.flag_bits&1 or not 24<=info.file_size<=16*1024**3:raise ValueError('unsupported payload ZIP entry')
        def text(name):
            entry=archive.getinfo(name)
            if entry.file_size>65536:raise ValueError('OTA text metadata too large')
            value={}
            for line in archive.read(name).decode('utf-8').splitlines():
                if '=' not in line:continue
                k,v=line.split('=',1)
                if k in value:raise ValueError('duplicate OTA metadata key')
                value[k]=v.strip()
            return value
        ota=text('META-INF/com/android/metadata');properties=text('payload_properties.txt')
    with path.open('rb') as stream:
        stream.seek(info.header_offset);local=stream.read(30)
        if local[:4]!=b'PK\x03\x04':raise ValueError('invalid ZIP local header')
        n,e=struct.unpack_from('<HH',local,26);start=info.header_offset+30+n+e
        if start+info.file_size>path.stat().st_size:raise ValueError('payload exceeds ZIP bounds')
        stream.seek(start);head=stream.read(24)
        if head[:4]!=b'CrAU' or struct.unpack_from('>Q',head,4)[0]!=2:raise ValueError('only v2 full payload is supported')
        size,signature=struct.unpack_from('>QI',head,12)
        if not 0<size<=16*1024**2 or signature>1024**2 or 24+size+signature>info.file_size:raise ValueError('invalid payload metadata size')
        raw=stream.read(size);manifest=fields(raw)
    if int(properties['FILE_SIZE'])!=info.file_size or int(properties['METADATA_SIZE'])!=24+size:raise ValueError('payload properties size mismatch')
    if base64.b64decode(properties['METADATA_HASH'],validate=True)!=hashlib.sha256(head+raw).digest():raise ValueError('payload metadata hash mismatch')
    if ota.get('pre-device')!='liuqin' or ota.get('ota-type')!='AB':raise ValueError('OTA is not a liuqin A/B package')
    if one(manifest,3,4096)!=4096 or one(manifest,12,0)!=0 or one(manifest,16,0):raise ValueError('incremental or partial OTA is not supported')
    if not 1<=len(manifest.get(13,[]))<=256:raise ValueError('invalid payload partition count')
    partitions={};data_offset=start+24+size+signature
    for value in manifest.get(13,[]):
        p=fields(value);name=one(p,1).decode('ascii')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,47}',name) or name in partitions or 6 in p:raise ValueError('duplicate partition or old-image-dependent OTA')
        if one(p,2,0) and not one(p,9,0):raise ValueError('required postinstall is unsupported')
        new=fields(one(p,7));length=one(new,1);digest=one(new,2)
        if type(length) is not int or not 0<length<=16*1024**3 or length%4096 or len(digest)!=32:raise ValueError('invalid partition image identity')
        ops=[];coverage=[]
        for value in p.get(8,[]):
            op=fields(value);kind=one(op,1);offset=one(op,2);size=one(op,3);checksum=one(op,8)
            if kind not in (0,1,8) or 4 in op or 5 in op or 9 in op:raise ValueError('unsupported/source-dependent payload operation')
            if type(offset) is not int or type(size) is not int or not 0<size<=64*1024**2 or offset<0 or data_offset+offset+size>start+info.file_size or len(checksum)!=32:raise ValueError('payload data range invalid')
            extents=[]
            for value in op.get(6,[]):
                ex=fields(value);begin=one(ex,1)*4096;count=one(ex,2)*4096
                if begin<0 or count<=0 or begin+count>length:raise ValueError('payload extent out of bounds')
                extents.append((begin,count));coverage.append((begin,begin+count))
            output_bytes=sum(n for _,n in extents)
            if not 0<output_bytes<=64*1024**2:raise ValueError('payload operation output too large')
            ops.append(dict(kind=kind,offset=data_offset+offset,bytes=size,sha256=checksum,extents=extents,output_bytes=output_bytes))
        end=0
        for begin,limit in sorted(coverage):
            if begin!=end:raise ValueError('payload extents overlap or leave holes')
            end=limit
        if end!=length:raise ValueError('payload does not cover the complete partition')
        partitions[name]=dict(bytes=length,sha256=digest.hex(),operations=ops,optional_postinstall=bool(one(p,2,0)))
    dynamic=fields(one(manifest,15));groups=[]
    for group in dynamic.get(1,[]):
        g=fields(group);groups.append(dict(name=one(g,1).decode(),bytes=one(g,2),partitions=[v.decode() for v in g.get(3,[])]))
    return dict(ota=ota,partitions=partitions,groups=groups,snapshot=one(dynamic,2,0),payload_start=start,
        payload_bytes=info.file_size,payload_sha256=base64.b64decode(properties['FILE_HASH'],validate=True).hex(),metadata_sha256=hashlib.sha256(head+raw).hexdigest())

def extract(path,destination,info,admitted,names,check=lambda:None):
    if set(info['partitions'])!=set(admitted):raise ValueError('payload partition set does not match the admitted ROM')
    for name,record in admitted.items():
        actual=info['partitions'][name]
        if any(actual[k]!=record[k] for k in ('bytes','sha256')):raise ValueError('payload image does not match the admitted ROM: '+name)
    with Path(path).open('rb') as source:
        source.seek(info['payload_start']);left=info['payload_bytes'];digest=hashlib.sha256()
        while left:
            check();data=source.read(min(left,4*1024**2))
            if not data:raise ValueError('truncated payload')
            digest.update(data);left-=len(data)
        if digest.hexdigest()!=info['payload_sha256']:raise ValueError('payload hash mismatch')
        for name in names:
            entry=info['partitions'][name];out=Path(destination)/(name+'.img');digest=hashlib.sha256()
            with out.open('xb') as target:
                target.truncate(entry['bytes'])
                for op in entry['operations']:
                    check();source.seek(op['offset']);packed=source.read(op['bytes'])
                    if hashlib.sha256(packed).digest()!=op['sha256']:raise ValueError('payload operation hash mismatch')
                    if op['kind']==0:data=packed
                    else:
                        dec=bz2.BZ2Decompressor() if op['kind']==1 else lzma.LZMADecompressor(memlimit=256*1024**2)
                        data=dec.decompress(packed,max_length=op['output_bytes']+1)
                        if not dec.eof or dec.unused_data:raise ValueError('payload compressed stream invalid or too large')
                    if len(data)>op['output_bytes']:raise ValueError('payload output exceeds extent')
                    data=data.ljust(op['output_bytes'],b'\0');cursor=0
                    for offset,length in op['extents']:
                        target.seek(offset);target.write(data[cursor:cursor+length]);cursor+=length
            with out.open('rb') as target:
                while data:=target.read(4*1024**2):check();digest.update(data)
            if digest.hexdigest()!=entry['sha256']:raise ValueError('extracted image hash mismatch: '+name)
