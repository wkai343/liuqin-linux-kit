# SPDX-License-Identifier: MIT
"""Full-package reinstall in the existing A slot. No firmware equality policy.

Only partitions declared by the liuqin payload can enter the confirmed write
scope. GPT layout edits and Linux data retain their separate explicit plans.
"""
import hashlib,re,struct
import liuqin_ota_v1 as legacy
from liuqin_ota_v1 import CORE,LOGICAL,FIRMWARE,SUPER_BYTES,HEX,record,encoded,digest
from liuqin_super import layout as super_layout
VERSION=2
CRITICAL=('keymaster','tz','hyp','devcfg','aop_config','aop','cpucp','uefisecapp','imagefv','uefi','xbl_ramdump','xbl_config','xbl','abl')
INSTALL=legacy.INSTALL

def generic(p):return isinstance(p,dict) and p.get('generic_ota_version') in (1,2)
def full(p):return isinstance(p,dict) and p.get('generic_ota_version')==2

def install_order(parts):
    names=set(parts)
    # All system data is verified before updating the boot firmware. boot_a is
    # committed separately, last; the in-RAM Recovery remains running on errors.
    order=['super','vendor_boot','dtbo']
    order+=sorted((names&FIRMWARE)-set(CRITICAL))
    if 'recovery' in names:order.append('recovery')
    order+=['vbmeta_system','vbmeta']
    order+=[n for n in CRITICAL if n in names]
    return [n+'.img' for n in order]

def validate(p):
    if not full(p):return legacy.validate(p)
    from liuqin_android import format_settings
    a=p.get('archive',{});ota=p.get('ota',{});parts=ota.get('partitions',{});names=set(parts)
    if (p.get('schema_version')!=3 or p.get('device')!='liuqin' or p.get('slot')!='a' or p.get('firmware_policy')!='rom-package' or
        p.get('shared_firmware')!=[] or p.get('anti_version') is not None):raise ValueError('完整 ROM 的设备或执行方式无效。')
    if (a.get('format')!='payload-zip-v2' or not HEX.fullmatch(a.get('sha256','')) or type(a.get('bytes')) is not int or not 0<a['bytes']<=16*1024**3 or
        p.get('id')!='ota2-'+a['sha256'][:32] or p.get('rom')!='OTA-'+a['sha256'][:16]):raise ValueError('完整 ROM 来源身份无效。')
    if not CORE<=names or not {'system','vendor','product'}<=names or names-(CORE|LOGICAL|FIRMWARE|{'recovery'}):raise ValueError('ROM 缺少必要镜像或试图写入非 liuqin 系统分区。')
    if (type(ota.get('sdk')) is not int or not 1<=ota['sdk']<=10000 or ota.get('device')!='liuqin' or
        not HEX.fullmatch(ota.get('payload_sha256','')) or not HEX.fullmatch(ota.get('metadata_sha256','')) or
        not isinstance(ota.get('fingerprint'),str) or not 0<len(ota['fingerprint'])<=1024):raise ValueError('ROM 元数据无效。')
    for r in parts.values():record(r)
    if any(parts[n]['bytes']>512*1024**2 for n in names&FIRMWARE):raise ValueError('固件镜像超出设备容量范围。')
    targets=names-LOGICAL|{'super'}
    if (p.get('install_images')!=install_order(parts) or set(p.get('images',{}))!={n+'.img' for n in targets} or
        set(p.get('material_images',[]))!=set(p['images']) or len(p['material_images'])!=len(targets) or ota.get('ignored_partitions')!=[]):raise ValueError('ROM 声明的镜像与刷写清单不一致。')
    if not set(ota.get('optional_postinstall',[]))<=names:raise ValueError('postinstall 清单无效。')
    for filename,r in p['images'].items():
        record(r);role=filename[:-4]
        if r.get('partition')!=('super' if role=='super' else role+'_a'):raise ValueError('ROM 目标分区不符合 A 槽刷写计划。')
        if role not in ('super','boot') and {k:r[k] for k in ('bytes','sha256')}!=parts[role]:raise ValueError('镜像内容身份不符合 payload。')
    if p['images']['boot.img']['bytes']!=192*1024**2 or parts['boot']['bytes']>192*1024**2 or parts['vendor_boot']['bytes']>96*1024**2 or parts['dtbo']['bytes']>24*1024**2 or any(parts[n]['bytes']>131072 for n in ('vbmeta','vbmeta_system')):raise ValueError('启动镜像超过 liuqin 分区容量。')
    spec=p.get('super',{}).get('layout',{})
    if (spec.get('bytes')!=SUPER_BYTES or not re.fullmatch('[A-Za-z0-9_]{1,33}',spec.get('group','')) or type(spec.get('group_bytes')) is not int or
        not 0<spec['group_bytes']<=SUPER_BYTES or [r['name'] for r in spec.get('partitions',[])]!=sorted(names&LOGICAL) or
        any({k:r[k] for k in ('bytes','sha256')}!=parts[r['name']] for r in spec['partitions'])):raise ValueError('动态分区布局超出设备范围。')
    prefix,ranges=super_layout(spec);expected=[dict(offset=0,bytes=len(prefix),sha256=hashlib.sha256(prefix).hexdigest()),*[{k:v for k,v in r.items() if k!='name'} for r in ranges]]
    if p['super'].get('verification_ranges')!=expected or p['super'].get('expanded_bytes')!=SUPER_BYTES or p['images']['super.img']['bytes']!=SUPER_BYTES or p['super'].get('source_sha256')!=p['images']['super.img']['sha256']:raise ValueError('super 组装描述不一致。')
    c=p.get('boot_contract',{});fs=c.get('data_filesystems',{})
    if fs.get('/data') not in ('f2fs','ext4') or fs.get('/metadata')!='ext4' or set(fs)!={'/data','/metadata'} or c.get('boot_header') not in (3,4) or c.get('vendor_header') not in (3,4):raise ValueError('未实现此启动或数据文件系统格式。')
    for name in ('userdata','metadata'):
        if p.get(name)!=format_settings(name,fs['/data'] if name=='userdata' else 'ext4'):raise ValueError('数据格式化参数不一致。')
    for name in ('vbmeta','vbmeta_system'):
        v=c.get(name,{})
        if v.get('flags') not in (0,1,2,3) or type(v.get('rollback_index')) is not int or not 0<=v['rollback_index']<2**64 or not HEX.fullmatch(v.get('metadata_sha256','')):raise ValueError('AVB 元数据无效。')
    if 'recovery' in parts:
        r=c.get('recovery',{})
        if parts['recovery']['bytes']>100*1024**2 or r.get('device')!='liuqin' or r.get('kernel_bytes')!=0 or r.get('header_version') not in (3,4) or r.get('sha256')!=parts['recovery']['sha256']:raise ValueError('ROM Recovery 不是 liuqin 的无内核独立 Recovery 镜像。')
    return p

def write_names(p):
    validate(p)
    return {p['images'][n]['partition'] for n in p['install_images']}

def blockers(p):
    if not full(p):return legacy.blockers(p)
    validate(p);return []

def requirements(p):
    validate(p)
    return [dict(partition=n+'_a',**p['ota']['partitions'][n]) for n in sorted((set(p['ota']['partitions'])&FIRMWARE)|{'dtbo'})]

def assess(p,inventory,measure,read,check_data=True):
    if not full(p):return legacy.assess(p,inventory,measure,read)
    validate(p);problems=[];checks=[];warnings=[]
    for r in p['images'].values():
        part=inventory.get(r['partition'])
        if not part or part['bytes']<r['bytes'] or (r['partition']=='super' and part['bytes']!=SUPER_BYTES):problems.append(r['partition']+'：目标缺失或容量不足。')
    for name,minimum in (('userdata',16*1024**3),('metadata',16*1024**2)):
        if check_data and (name not in inventory or inventory[name]['bytes']<minimum):problems.append(name+'：请先配置足够容量的数据分区。')
    for r in requirements(p):
        part=inventory.get(r['partition']);actual=measure(part,r['bytes']) if part and part['bytes']>=r['bytes'] else None
        checks.append(dict(**r,actual_sha256=actual,matched=actual==r['sha256'],action='保持内容' if actual==r['sha256'] else '写入 ROM 提供的版本'))
    for name in ('vbmeta','vbmeta_system'):
        part=inventory.get(name+'_a');raw=read(part,256) if part else b''
        if len(raw)==256 and raw[:4]==b'AVB0' and p['boot_contract'][name]['rollback_index']<struct.unpack_from('>Q',raw,112)[0]:
            warnings.append(name+'：目标镜像的回滚索引较低；镜像中的索引不是 TEE 实际计数，启动是否接受由设备安全链决定。')
    if any(not c['matched'] for c in checks):warnings.append('将更新固件/DTBO，现有 Linux 和原生 Rec 的后续启动兼容性可能改变；rootfs 与引导存储不会被覆盖。')
    return dict(compatible=not problems,blockers=problems,firmware_checks=checks,firmware_updates=[r['partition'] for r in checks if not r['matched']],
        dtbo_unchanged=next(r['matched'] for r in checks if r['partition']=='dtbo_a'),automatic_firmware_update=True,hardware_install_verified=False,
        recovery_from_rom='recovery' in p['ota']['partitions'],limitations=warnings+['完整包重装会更新声明的 A 槽资源；保留 B 槽不保证回退。','未验证发行者身份或全部硬件防回滚状态，真实刷写与首次启动仍未验收。'])

def require_device(p,hardware,check_data=True):
    if not generic(p):return
    inventory=hardware.inventory();hardware.sealed(list(inventory.values()))
    result=assess(p,inventory,lambda part,n:hardware.digest(part,0,n),lambda part,n:hardware.read(part,0,n),check_data=check_data)
    if not result['compatible']:raise RuntimeError('ROM 无法写入当前设备：'+'；'.join(result['blockers']))
    return result
