# SPDX-License-Identifier: MIT
"""Build/inspect a v2 embedded-DTBO candidate and assess its prerequisites.

Assessments never grant boot/flash permission and never modify vbmeta flags.
"""
import hashlib,json,re,struct,zlib
from pathlib import Path
from liuqin_fixed_recovery import LIMIT,align,avb_info,encoded,sha
from liuqin_artifacts import bounded_initramfs,cpio_files
from liuqin_native_recovery import MARKER,validate as validate_native
from liuqin_native_install import FIXED_USAGE, capable

USAGE='universal-recovery-candidate'
CATALOG=Path(__file__).with_name('liuqin-universal-recovery-profiles.json')
HEX=re.compile('[a-f0-9]{64}')

def avb_measurement(data,partition_sha256=None):
    """Normalize only flags; signatures, keys and descriptors remain identity data."""
    if len(data)<256 or data[:4]!=b'AVB0':raise ValueError('invalid vbmeta header')
    major,minor=struct.unpack_from('>II',data,4);auth,aux=struct.unpack_from('>QQ',data,12);n=256+auth+aux
    flags=struct.unpack_from('>I',data,120)[0]
    if major!=1 or minor>3 or auth%64 or aux%64 or n>65536 or n>len(data) or flags&~3:raise ValueError('unsupported vbmeta structure or flags')
    raw=data[:n];normal=bytearray(raw);normal[120:124]=bytes(4)
    value=dict(status='complete',metadata_bytes=n,metadata_sha256=sha(raw),normalized_flags_sha256=sha(normal),flags=flags)
    if partition_sha256 is not None:value['partition_sha256']=partition_sha256
    return value

def validate_policy(p):
    if (not isinstance(p,dict) or p.get('schema')!=1 or p.get('device')!='liuqin' or p.get('boot_header_version')!=2 or
        p.get('partition_bytes')!=LIMIT or p.get('partition_name')!='recovery' or p.get('unlocked_required') is not True or
        type(p.get('inspection_only')) is not bool or p.get('automatic_avb_changes') is not False or
        p.get('validation')!='OFFLINE_CANDIDATE_NOT_HARDWARE_BOOT_TESTED' or
        p.get('required_layout')!={'boot':201326592,'recovery':LIMIT}):raise ValueError('unsupported universal Recovery policy')
    def hash_ok(v):return isinstance(v,str) and HEX.fullmatch(v)
    if any(not hash_ok(p.get(k)) for k in ('kernel_sha256','dtb_sha256')):raise ValueError('kernel/DTB identity missing')
    d=p.get('embedded_dtbo',{})
    if (not isinstance(d,dict) or type(d.get('bytes')) is not int or not 32<=d['bytes']<=24*1024**2 or
        not hash_ok(d.get('sha256')) or not hash_ok(d.get('liuqin_entry_sha256')) or
        type(d.get('entry_count')) is not int or not 0<d['entry_count']<=512 or
        type(d.get('liuqin_index')) is not int or not 0<=d['liuqin_index']<d['entry_count']):raise ValueError('invalid embedded DTBO policy')
    if type(p.get('recovery_rollback_index')) is not int or not 0<=p['recovery_rollback_index']<2**64:raise ValueError('invalid recovery rollback value')
    ids=set();abls=set();roms=set()
    if not isinstance(p.get('baselines'),list) or not 1<=len(p['baselines'])<=16:raise ValueError('invalid ABL baseline list')
    for b in p['baselines']:
        if (not isinstance(b,dict) or not isinstance(b.get('id'),str) or not b['id'] or b['id'] in ids or
            b.get('abl_partition_bytes')!=8388608 or not hash_ok(b.get('abl_partition_sha256')) or
            b['abl_partition_sha256'] in abls or not hash_ok(b.get('abl_pe_sha256')) or
            type(b.get('requires_verification_disabled')) is not bool):raise ValueError('invalid/ambiguous ABL policy')
        ids.add(b['id']);abls.add(b['abl_partition_sha256'])
    if not isinstance(p.get('roms'),list) or not 1<=len(p['roms'])<=64:raise ValueError('invalid ROM policy list')
    ids=set()
    for r in p['roms']:
        a=r.get('vbmeta',{})
        if (not isinstance(r.get('id'),str) or not r['id'] or r['id'] in ids or
            not hash_ok(a.get('metadata_sha256')) or not hash_ok(a.get('normalized_flags_sha256')) or
            a['normalized_flags_sha256'] in roms or not hash_ok(r.get('vbmeta_system_metadata_sha256')) or
            type(a.get('metadata_bytes')) is not int or not 256<=a['metadata_bytes']<=65536 or
            a.get('observed_flags') not in (0,1,2,3)):raise ValueError('invalid/ambiguous ROM metadata policy')
        ids.add(r['id']);roms.add(a['normalized_flags_sha256'])
    return p

def validate_dtbo(data,policy):
    d=policy['embedded_dtbo']
    if len(data)!=d['bytes'] or sha(data)!=d['sha256']:raise ValueError('embedded DTBO digest/size mismatch')
    magic,total,hs,es,count,offset,page,version=struct.unpack_from('>8I',data)
    if (magic!=0xd7b7ab1e or total!=len(data) or hs!=32 or es!=32 or count!=d['entry_count'] or
        offset<hs or offset+count*es>total or page!=4096 or version!=0):raise ValueError('embedded DTBO table bounds invalid')
    ranges=[]
    for i in range(count):
        size,start=struct.unpack_from('>II',data,offset+i*es)
        if start<offset+count*es or size<40 or start+size>len(data):raise ValueError('DTBO entry exceeds table')
        blob=data[start:start+size]
        if blob[:4]!=b'\xd0\x0d\xfe\xed' or struct.unpack_from('>I',blob,4)[0]!=size:raise ValueError('DTBO entry is not a complete FDT')
        ranges.append((start,start+size))
        if i==d['liuqin_index'] and sha(blob)!=d['liuqin_entry_sha256']:raise ValueError('wrong liuqin overlay')
    previous=offset+count*es
    for start,end in sorted(ranges):
        if start<previous:raise ValueError('overlapping DTBO entries')
        previous=end

def pack_v2(kernel,ramdisk,dtb,dtbo,source_header):
    if len(source_header)!=4096:raise ValueError('expected source header page')
    h=bytearray(source_header);r=4096+align(len(kernel));o=r+align(len(ramdisk))
    struct.pack_into('<I',h,8,len(kernel));struct.pack_into('<I',h,16,len(ramdisk));struct.pack_into('<I',h,24,0)
    struct.pack_into('<II',h,36,4096,2);struct.pack_into('<IQII',h,1632,len(dtbo),o,1660,len(dtb))
    digest=hashlib.sha1()
    for part in (kernel,ramdisk,b'',dtbo,dtb):digest.update(part);digest.update(struct.pack('<I',len(part)))
    h[576:608]=digest.digest()+bytes(12)
    return bytes(h)+kernel+bytes(-len(kernel)%4096)+ramdisk+bytes(-len(ramdisk)%4096)+dtbo+bytes(-len(dtbo)%4096)+dtb+bytes(-len(dtb)%4096)

def inspect(path):
    return inspect_bytes(Path(path).read_bytes())


def inspect_bytes(data):
    avb=avb_info(data);u=lambda n:struct.unpack_from('<I',data,n)[0]
    if data[:8]!=b'ANDROID!' or (u(36),u(40),u(24),u(1644))!=(4096,2,0,1660):raise ValueError('not a v2 embedded-DTBO candidate')
    ks,rs,osize,ds=u(8),u(16),u(1632),u(1648);r=4096+align(ks);o=r+align(rs);d=o+align(osize)
    if (not ks or not rs or not osize or not ds or struct.unpack_from('<Q',data,1636)[0]!=o or
        d+align(ds)!=avb['original_bytes']):raise ValueError('boot, DTBO and AVB boundaries differ')
    kb,ram,dtbo,dtb=data[4096:4096+ks],data[r:r+rs],data[o:o+osize],data[d:d+ds]
    dec=zlib.decompressobj(31);kernel=dec.decompress(kb,128*1024**2+1)
    if not dec.eof or dec.unused_data or dec.unconsumed_tail or len(kernel)>128*1024**2:raise ValueError('invalid standalone gzip kernel')
    legacy=hashlib.sha1()
    for part in (kb,ram,b'',dtbo,dtb):legacy.update(part);legacy.update(struct.pack('<I',len(part)))
    if data[576:608]!=legacy.digest()+bytes(12):raise ValueError('legacy v2 image ID mismatch')
    files=cpio_files(bounded_initramfs(ram,256*1024**2));mode,raw=files.get(MARKER,(0,b''))
    if mode&0o170000!=0o100000:raise ValueError('universal identity missing')
    m=json.loads(raw);p=validate_policy(m.get('universal_policy'));identity=m.get('identity',{})
    usage=m.get('usage')
    if usage not in (USAGE,FIXED_USAGE) or m.get('universal_version')!=1 or m.get('fixed_boot_header')!=2:raise ValueError('wrong universal image role')
    if m.get('inspection_only') is not p['inspection_only'] or (usage==FIXED_USAGE)!=(not p['inspection_only']):raise ValueError('fixed Recovery role and policy disagree')
    if usage==FIXED_USAGE and (not capable(m) or m.get('native_adb_version')!=1):raise ValueError('fixed installer capabilities missing')
    validate_native(encoded(m),sha(kernel),dtb,files,usage=usage)
    if (sha(encoded(identity))!=m['build_id'] or identity.get('universal_policy')!=p or identity.get('payload_sha256')!=m['payload_sha256'] or
        identity.get('kernel_sha256')!=sha(kernel) or identity.get('dtb_sha256')!=sha(dtb) or
        identity.get('embedded_dtbo_sha256')!=sha(dtbo) or identity.get('avb_public_key_sha256')!=avb['public_key_sha256']):raise ValueError('universal build identity mismatch')
    if p['kernel_sha256']!=sha(kernel) or p['dtb_sha256']!=sha(dtb) or p['recovery_rollback_index']!=avb['rollback_index']:raise ValueError('universal components differ from policy')
    validate_dtbo(dtbo,p)
    if files.get('etc/liuqin-storage-mode',(0,b''))[1]!=b'readonly\n':raise ValueError('universal candidate must remain read-only')
    return dict(image_sha256=sha(data),image_bytes=len(data),manifest=m,avb=avb,dtbo_offset=o,dtb_offset=d,
        kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),embedded_dtbo_sha256=sha(dtbo),status='OFFLINE_CANDIDATE_NOT_HARDWARE_BOOT_TESTED')

def load_catalog(path=CATALOG):
    c=json.loads(Path(path).read_text(encoding='utf-8'));validate_policy(c.get('policy'))
    a=c.get('artifact',{})
    if (c.get('schema')!=1 or a.get('validation')!='OFFLINE_VALIDATED_HARDWARE_BOOT_UNTESTED' or
        not all(isinstance(a.get(k),str) and HEX.fullmatch(a[k]) for k in ('image_sha256','build_id')) or
        a.get('usage')!=USAGE or not a.get('evidence')):raise ValueError('universal candidate catalog is not qualified')
    return c

def assess(snapshot,catalog):
    from liuqin_firmware import validate_snapshot
    validate_snapshot(snapshot);p=validate_policy(catalog['policy']);slot=snapshot['slots'][snapshot['active_slot']]
    result=dict(status='incomplete',requirements_satisfied=False,hardware_boot_verified=False,automatic_boot_allowed=False,fixed_install_allowed=False,
        automatic_avb_changes=False,profile_id='',baseline_id='',artifact=catalog['artifact'],reason='v2 通用候选：需要刷新固件指纹及 AVB 状态。')
    measured=slot.get('measured',slot.get('match',{}));abl=measured.get('abl',{})
    bases=[b for b in p['baselines'] if abl.get('partition_bytes')==b['abl_partition_bytes'] and abl.get('hash_bytes')==abl.get('partition_bytes') and abl.get('sha256')==b['abl_partition_sha256']]
    if len(bases)!=1:
        result.update(status='unknown-baseline',reason='v2 通用候选：当前 ABL 底座未适配，不会自动启动或修改固件。');return result
    base=bases[0];result['baseline_id']=base['id'];result['baseline_label']=base['label']
    avb=slot.get('avb',{})
    if avb.get('status')!='complete' or avb.get('partition_sha256')!=measured.get('vbmeta',{}).get('sha256'):return result
    roms=[r for r in p['roms'] if avb.get('normalized_flags_sha256')==r['vbmeta']['normalized_flags_sha256'] and avb.get('metadata_bytes')==r['vbmeta']['metadata_bytes']]
    if len(roms)!=1:
        result.update(status='unknown-metadata',reason='v2 通用候选：识别到 ABL，但当前 AVB 元数据组合未收录。');return result
    rom=roms[0];result.update(profile_id=rom['id'],rom_label=rom['label'],root_avb_flags=avb['flags'])
    if any(measured.get('layout',{}).get(k)!=v for k,v in p['required_layout'].items()):
        result.update(status='layout-mismatch',reason='v2 通用候选：启动或 Recovery 分区容量不符合适配条件。');return result
    disabled=bool(avb['flags']&2)
    if base['requires_verification_disabled'] and not disabled:
        result.update(status='requires-avb-mode',reason='v2 通用候选：旧 MIUI ABL 需要关闭镜像验证；当前条件未满足，安装器不会自动修改 AVB。');return result
    if not disabled:
        system=slot.get('avb_system',{})
        if (system.get('status')!='complete' or system.get('partition_sha256')!=measured.get('vbmeta_system',{}).get('sha256') or system.get('metadata_sha256')!=rom['vbmeta_system_metadata_sha256']):
            result.update(status='requires-system-metadata',reason='v2 通用候选：保留验证时，需要匹配的 vbmeta_system 元数据。');return result
    result.update(status='offline-ready',requirements_satisfied=True,reason='v2 通用候选：已识别 '+rom['label']+'，启动条件符合离线适配规则；尚未真机验收，不会自动启动或刷写。')
    return result
