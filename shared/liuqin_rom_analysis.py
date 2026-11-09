# SPDX-License-Identifier: MIT
"""Read local ROMs for a non-executable preview. Never run archive programs."""
import base64
import bz2
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tarfile
import zipfile
from liuqin_payload import fields, one
from liuqin_universal_recovery import avb_measurement

CATALOG=Path(__file__).with_name('liuqin-rom-preview-profiles.json')
MAX_IMAGE=16*1024**3
CORE=('boot','vendor_boot','dtbo','vbmeta','vbmeta_system')
ALIASES={'NON-HLOS.bin':'modem','BTFM.bin':'bluetooth','dspso.bin':'dsp'}

def sha(data):return hashlib.sha256(data).hexdigest()
def stamp(path):
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode):raise ValueError('ROM 材料必须是普通文件，不能是链接或设备：'+str(path))
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

def profiles():
    c=json.loads(CATALOG.read_text())
    if c.get('schema')!=1 or c.get('device')!='liuqin' or c.get('preview_only') is not True:raise ValueError('ROM 预览目录无效。')
    if not {'miui14','hyperos206','miku26101322'}<={p['id'] for p in c['profiles']} or len({p['id'] for p in c['profiles']})!=len(c['profiles']):raise ValueError('ROM 预览条目不完整。')
    return c['profiles']

def role(name):
    name=PurePosixPath(name).name
    if name in ALIASES:return ALIASES[name]
    if not name.endswith(('.img','.bin','.elf','.mbn')):return None
    name=name.rsplit('.',1)[0].removesuffix('.payload').removesuffix('_a')
    return name if re.fullmatch('[a-z][a-z0-9_-]{0,47}',name) else None

def digest_stream(stream,size,check,prefix_limit=65536):
    if not 0<size<=MAX_IMAGE:raise ValueError('ROM 镜像大小超过分析上限。')
    h=hashlib.sha256();prefix=b'';left=size
    while left:
        check();b=stream.read(min(left,4*1024**2))
        if not b:raise ValueError('ROM 镜像被截断。')
        h.update(b);prefix=(prefix+b)[:prefix_limit];left-=len(b)
    return dict(bytes=size,sha256=h.hexdigest()),prefix

def add(records,prefixes,name,stream,size,check):
    if name in records:raise ValueError('ROM 存在重名镜像：'+name)
    if name in ('vbmeta','vbmeta_system') and size>131072:raise ValueError('AVB 镜像超过目标分区容量。')
    records[name],prefix=digest_stream(stream,size,check,131072 if name in ('vbmeta','vbmeta_system') else 65536)
    if name in ('vbmeta','vbmeta_system'):prefixes[name]=prefix

def ota(path,check):
    """Verify the full payload stream; only decode bounded AVB images in RAM."""
    with zipfile.ZipFile(path) as z:
        entries=z.infolist();names=[e.filename for e in entries]
        if len(names)>8192 or len(names)!=len(set(names)):raise ValueError('OTA 条目过多或重名。')
        entry=z.getinfo('payload.bin')
        if entry.compress_type!=zipfile.ZIP_STORED or entry.flag_bits&1 or not 24<=entry.file_size<=MAX_IMAGE:raise ValueError('不支持该 OTA payload 格式。')
        def text(name):
            e=z.getinfo(name)
            if e.file_size>65536 or e.flag_bits&1:raise ValueError('OTA 文本元数据无效。')
            result={}
            for line in z.read(name).decode().splitlines():
                if '=' not in line:continue
                k,v=line.split('=',1)
                if k in result:raise ValueError('OTA 元数据键重复。')
                result[k]=v.strip()
            return result
        props=text('payload_properties.txt');meta=text('META-INF/com/android/metadata')
    if meta.get('pre-device')!='liuqin' or meta.get('ota-type')!='AB':raise ValueError('这不是 liuqin A/B OTA。')
    records={};prefixes={};hooks=[];operations=set()
    with path.open('rb') as f:
        f.seek(entry.header_offset);local=f.read(30)
        if len(local)!=30 or local[:4]!=b'PK\x03\x04':raise ValueError('OTA 本地条目头无效。')
        n,e=struct.unpack_from('<HH',local,26);start=entry.header_offset+30+n+e
        if start+entry.file_size>path.stat().st_size:raise ValueError('OTA payload 超出文件边界。')
        f.seek(start);header=f.read(24)
        if len(header)!=24 or header[:4]!=b'CrAU' or struct.unpack_from('>Q',header,4)[0]!=2:raise ValueError('仅支持 v2 完整 OTA。')
        length,sig=struct.unpack_from('>QI',header,12)
        if not 0<length<=16*1024**2 or sig>1024**2 or 24+length+sig>entry.file_size:raise ValueError('OTA 元数据边界无效。')
        raw=f.read(length);m=fields(raw)
        if (int(props['METADATA_SIZE'])!=24+length or int(props['FILE_SIZE'])!=entry.file_size or
            base64.b64decode(props['METADATA_HASH'],validate=True)!=hashlib.sha256(header+raw).digest()):raise ValueError('OTA 元数据校验失败。')
        if one(m,3,4096)!=4096 or one(m,12,0)!=0 or one(m,16,0):raise ValueError('增量或部分 OTA 不能用于重装预览。')
        f.seek(start);payload,_=digest_stream(f,entry.file_size,check)
        if payload['sha256']!=base64.b64decode(props['FILE_HASH'],validate=True).hex():raise ValueError('OTA payload 完整校验失败。')
        parts=m.get(13,[])
        if not 1<=len(parts)<=256:raise ValueError('OTA 分区数量无效。')
        data_start=start+24+length+sig
        for encoded in parts:
            check();p=fields(encoded);name=one(p,1).decode('ascii');info=fields(one(p,7));size=one(info,1);digest=one(info,2)
            if (not re.fullmatch('[a-z][a-z0-9_-]{0,47}',name) or name in records or 6 in p or
                type(size) is not int or not 0<size<=MAX_IMAGE or size%4096 or len(digest)!=32):raise ValueError('OTA 分区身份无效。')
            records[name]=dict(bytes=size,sha256=digest.hex())
            if one(p,2,0):hooks.append(dict(partition=name,optional=bool(one(p,9,0))))
            decode=name in ('vbmeta','vbmeta_system')
            if decode and size>131072:raise ValueError('AVB 镜像超过目标分区容量。')
            body=bytearray(size) if decode else None;coverage=[];ops=p.get(8,[])
            if not 1<=len(ops)<=50000:raise ValueError('OTA 操作数量无效。')
            for encoded_op in ops:
                op=fields(encoded_op);kind=one(op,1)
                if kind not in (0,1,6,7,8):raise ValueError('OTA 含未支持的数据操作。')
                operations.add(kind)
                if 4 in op or 5 in op or 9 in op:raise ValueError('OTA 含依赖旧镜像的操作。')
                ranges=[]
                for v in op.get(6,[]):
                    ex=fields(v);a=one(ex,1)*4096;b=one(ex,2)*4096
                    if a<0 or b<=0 or a+b>size:raise ValueError('OTA 分区范围越界。')
                    ranges.append((a,b));coverage.append((a,a+b))
                total=sum(n for _,n in ranges)
                if not 0<total<=64*1024**2:raise ValueError('OTA 操作展开大小无效。')
                offset=one(op,2,0);count=one(op,3,0)
                if kind in (6,7):
                    if count:raise ValueError('OTA 清零操作携带异常数据。')
                    packed=None
                else:
                    if offset<0 or not 0<count<=64*1024**2 or data_start+offset+count>start+entry.file_size or len(one(op,8,b''))!=32:raise ValueError('OTA 数据范围无效。')
                    f.seek(data_start+offset);h=hashlib.sha256();left=count;chunks=[]
                    while left:
                        check();chunk=f.read(min(left,1024**2))
                        if not chunk:raise ValueError('OTA 数据块被截断。')
                        h.update(chunk);left-=len(chunk)
                        if decode:chunks.append(chunk)
                    if h.digest()!=one(op,8):raise ValueError('OTA 数据块校验失败。')
                    packed=b''.join(chunks) if decode else None
                if not decode:continue
                if kind==7:raise ValueError('AVB 元数据含 DISCARD，无法推定被丢弃区域的内容。')
                if kind==6:data=bytes(total)
                elif kind==0:data=packed
                elif kind in (1,8):
                    dec=bz2.BZ2Decompressor() if kind==1 else lzma.LZMADecompressor(memlimit=256*1024**2)
                    data=dec.decompress(packed,max_length=total+1)
                    if not dec.eof or dec.unused_data:raise ValueError('AVB 压缩块无效。')
                else:raise ValueError('AVB 元数据使用未支持的 OTA 操作。')
                if len(data)>total:raise ValueError('AVB 展开数据越界。')
                data=data.ljust(total,b'\0');cursor=0
                for a,b in ranges:body[a:a+b]=data[cursor:cursor+b];cursor+=b
            end=0
            for a,b in sorted(coverage):
                if a!=end:raise ValueError('OTA 分区范围重叠或缺失。')
                end=b
            if end!=size:raise ValueError('OTA 分区内容不完整。')
            if decode:
                if sha(body)!=digest.hex():raise ValueError('AVB 镜像解包校验失败。')
                prefixes[name]=bytes(body)
    return records,prefixes,dict(kind='payload-zip',payload_sha256=payload['sha256'],metadata_sha256=sha(header+raw),fingerprint=meta.get('post-build',''),hooks=hooks,operations=sorted(operations),verification='已校验完整 payload 与解包后的 AVB；其余镜像为 payload 声明，未执行安装脚本或验证发行者签名。')

def analyze(path,check=lambda:None,catalog=None):
    if not path:raise ValueError('请选择本地 ROM 文件或镜像目录。')
    allow_unknown=catalog is None
    catalog=profiles() if catalog is None else catalog;source=Path(path).expanduser()
    if source.is_symlink():raise ValueError('ROM 来源不能是符号链接。')
    records={};prefixes={}
    if source.is_dir():
        directory=source/'images' if (source/'images').is_dir() else source
        if directory.is_symlink():raise ValueError('images 目录不能是链接。')
        paths=sorted(p for p in directory.iterdir() if role(p.name))
        if not paths or len(paths)>256:raise ValueError('ROM 镜像目录为空或条目过多。')
        before={p:stamp(p) for p in paths}
        for p in paths:
            with p.open('rb') as f:add(records,prefixes,role(p.name),f,before[p][2],check)
        if before!={p:stamp(p) for p in paths} or paths!=sorted(p for p in directory.iterdir() if role(p.name)):raise ValueError('分析期间 ROM 目录发生变化。')
        origin=dict(kind='directory',verification='已计算目录镜像完整哈希；未核验发行者签名。')
    else:
        before=stamp(source)
        with source.open('rb') as f:archive,_=digest_stream(f,before[2],check)
        if zipfile.is_zipfile(source):records,prefixes,origin=ota(source,check)
        else:
            with tarfile.open(source,'r|*') as tar:
                count=0
                for entry in tar:
                    check();count+=1
                    if count>8192:raise ValueError('ROM 归档条目过多。')
                    name=role(entry.name)
                    if not name:continue
                    if not entry.isfile() or entry.issparse() or '..' in PurePosixPath(entry.name).parts or PurePosixPath(entry.name).is_absolute():raise ValueError('ROM 镜像条目不是安全的普通文件。')
                    with tar.extractfile(entry) as f:add(records,prefixes,name,f,entry.size,check)
            origin=dict(kind='tar',verification='已读取归档镜像并计算完整哈希；未执行任何刷机脚本。')
        if stamp(source)!=before:raise ValueError('分析期间 ROM 文件发生变化。')
        origin.update(archive)
    hits=set()
    for p in catalog:
        if any(records.get(n) in p['images'].get(n,[]) for n in CORE if n in records):hits.add(p['id'])
        if 'vbmeta' in prefixes:
            a=avb_measurement(prefixes['vbmeta'])
            if a['normalized_flags_sha256']==p['projected']['vbmeta']['avb']['normalized_flags_sha256']:hits.add(p['id'])
    if not hits and allow_unknown and origin.get('kind')=='payload-zip':
        return dict(profile=dict(id='unadmitted-'+origin['sha256'][:16],label='未收录 ROM（完整 OTA）',unadmitted=True),
            source=dict(path=str(source.resolve()),**origin),images=[dict(name=n,**r,matched=False) for n,r in records.items()],
            observed_avb={n:avb_measurement(b) for n,b in prefixes.items()},projected={},issues=['没有此 ROM 的安装准入记录。'],
            missing=[n for n in CORE if n not in records],reference_only=False)
    if len(hits)!=1:raise ValueError('ROM 无法唯一识别，或混用了不同 ROM 的启动镜像。')
    p=next(p for p in catalog if p['id'] in hits);issues=[];rows=[]
    for name,item in records.items():
        matched=item in p['images'].get(name,[])
        if not matched:issues.append(name+'：内容未收录或与该 ROM 基线不同。')
        rows.append(dict(name=name,**item,matched=matched))
    if p.get('archive') and origin.get('kind')=='payload-zip' and any(origin[k]!=p['archive'][k] for k in ('bytes','sha256')):issues.append('ZIP 与已收录的 ROM 完整包哈希不同。')
    projected=json.loads(json.dumps(p['projected']))
    for name in ('vbmeta','vbmeta_system'):
        if name not in prefixes:continue
        a=avb_measurement(prefixes[name]);expected=projected[name]['avb']
        # A flag-only patch is observed, never performed. Unrecognized keys or
        # descriptors cannot be replaced with the expected catalog metadata.
        if a['normalized_flags_sha256']!=expected['normalized_flags_sha256']:raise ValueError('目标 ROM 的 AVB 元数据不属于已收录基线：'+name)
        if a['metadata_sha256']!=expected['metadata_sha256']:
            issues.append(name+' 的 AVB flags 已被修改；仅展示实际值，不授予安装资格。')
        # Model the actual target partition, including zero padding, rather
        # than confusing an 8 KiB source file with a 128 KiB physical partition.
        digest=sha(prefixes[name].ljust(131072,b'\0'))
        projected[name]['avb']=dict(a,partition_sha256=digest)
        projected[name]['measurement']=dict(partition_bytes=131072,hash_bytes=131072,sha256=digest)
    missing=[n for n in CORE if n not in records]
    if 'super' not in records and not {'system','vendor','product'}<=set(records):missing.append('super 或完整动态分区镜像')
    return dict(profile=p,source=dict(path=str(source.resolve()),**origin),images=rows,projected=projected,issues=issues,missing=missing,reference_only=False)

def reference(profile_id):
    p=next((p for p in profiles() if p['id']==profile_id),None)
    if p is None:raise ValueError('未收录的 ROM 基线。')
    return dict(profile=p,source=dict(kind='reference',path='',verification='仅使用已收录基线推演；没有提供 ROM 文件，不代表安装材料完整。'),
        images=[],projected=p['projected'],issues=[],missing=['完整 ROM 材料'],reference_only=True)
