# SPDX-License-Identifier: MIT
"""Device-level contract for full OTA imports, independent of ROM names.

Firmware is measured, never flashed. Generic v1 also requires an unchanged
DTBO so the already-running native Recovery keeps the same boot environment.
"""
import hashlib,json,re,struct
from liuqin_super import layout as super_layout
VERSION=1
CORE={'boot','vendor_boot','dtbo','vbmeta','vbmeta_system'}
LOGICAL={'system','system_ext','product','vendor','odm','vendor_dlkm','odm_dlkm','system_dlkm','mi_ext'}
FIRMWARE={'abl','aop','aop_config','bluetooth','cpucp','devcfg','dsp','featenabler','hyp','imagefv','keymaster','modem','qupfw','shrm','tz','uefi','uefisecapp','vm-bootsys','xbl','xbl_config','xbl_ramdump'}
ESSENTIAL={'abl','xbl','xbl_config','tz','hyp','keymaster'}
SUPER_BYTES=9126805504
INSTALL=['super.img','vendor_boot.img','dtbo.img','vbmeta_system.img','vbmeta.img']
HEX=re.compile('[a-f0-9]{64}')
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(v):return hashlib.sha256(encoded(v)).hexdigest()
def generic(p):return isinstance(p,dict) and p.get('generic_ota_version')==VERSION

def record(r):
    if (not isinstance(r,dict) or type(r.get('bytes')) is not int or not 0<r['bytes']<=16*1024**3 or r['bytes']%4096 or
        not isinstance(r.get('sha256'),str) or not HEX.fullmatch(r['sha256'])):raise ValueError('OTA 镜像身份无效。')

def validate(p):
    from liuqin_android import format_settings
    if (not generic(p) or p.get('schema_version')!=2 or p.get('device')!='liuqin' or p.get('slot')!='a' or
        p.get('firmware_policy')!='preserve-existing' or p.get('shared_firmware')!=[] or p.get('anti_version') is not None or
        p.get('install_images')!=INSTALL or set(p.get('images',{}))!=set(INSTALL)|{'boot.img'} or
        set(p.get('material_images',[]))!=set(p['images']) or len(p['material_images'])!=6):raise ValueError('OTA 写入范围不符合 liuqin 设备规则。')
    a=p.get('archive',{})
    if a.get('format')!='payload-zip-v2' or not HEX.fullmatch(a.get('sha256','')) or type(a.get('bytes')) is not int or not 0<a['bytes']<=16*1024**3 or p.get('id')!='ota-'+a['sha256'][:32] or p.get('rom')!='OTA-'+a['sha256'][:16]:raise ValueError('OTA 来源身份无效。')
    ota=p.get('ota',{});parts=ota.get('partitions',{});names=set(parts)
    if not CORE<=names or not {'system','vendor','product'}<=names or names-(CORE|LOGICAL|FIRMWARE|{'recovery'}):raise ValueError('OTA 缺少系统镜像或包含未支持的分区。')
    if (type(ota.get('sdk')) is not int or not 33<=ota['sdk']<=37 or ota.get('device')!='liuqin' or
        not HEX.fullmatch(ota.get('payload_sha256','')) or not HEX.fullmatch(ota.get('metadata_sha256','')) or
        not isinstance(ota.get('fingerprint'),str) or not 0<len(ota['fingerprint'])<=1024):raise ValueError('OTA 平台元数据无效或版本超出当前执行器范围。')
    for r in parts.values():record(r)
    if any(parts[n]['bytes']>512*1024**2 for n in names&FIRMWARE):raise ValueError('固件镜像超出核验上限。')
    if set(ota.get('ignored_partitions',[]))!=names-(CORE|LOGICAL):raise ValueError('OTA 排除分区清单被修改。')
    if not set(ota.get('optional_postinstall',[]))<=names:raise ValueError('OTA postinstall 清单无效。')
    for name,r in p['images'].items():
        record(r);role=name[:-4]
        if r.get('partition')!=(role if role=='super' else role+'_a'):raise ValueError('OTA 目标分区被修改。')
        if role!='super' and {k:r[k] for k in ('bytes','sha256')}!=parts[role]:raise ValueError('OTA 镜像与 payload 声明不一致。')
    if parts['boot']['bytes']!=192*1024**2 or parts['vendor_boot']['bytes']!=96*1024**2 or parts['dtbo']['bytes']>24*1024**2 or any(parts[n]['bytes']>131072 for n in ('vbmeta','vbmeta_system')):raise ValueError('启动镜像不符合 liuqin 分区容量。')
    spec=p.get('super',{}).get('layout',{})
    if (spec.get('bytes')!=SUPER_BYTES or spec.get('group')!='qti_dynamic_partitions' or spec.get('group_bytes')!=9122611200 or
        [r['name'] for r in spec.get('partitions',[])]!=sorted(names&LOGICAL) or
        any({k:r[k] for k in ('bytes','sha256')}!=parts[r['name']] for r in spec['partitions'])):raise ValueError('OTA 动态分区布局被修改或不受支持。')
    prefix,ranges=super_layout(spec);expected=[dict(offset=0,bytes=len(prefix),sha256=hashlib.sha256(prefix).hexdigest()),*[{k:v for k,v in r.items() if k!='name'} for r in ranges]]
    if p['super'].get('verification_ranges')!=expected or p['super'].get('expanded_bytes')!=SUPER_BYTES or p['images']['super.img']['bytes']!=SUPER_BYTES or p['super'].get('source_sha256')!=p['images']['super.img']['sha256']:raise ValueError('OTA super 组装描述不一致。')
    for name in ('userdata','metadata'):
        if p.get(name)!=format_settings(name):raise ValueError('OTA 数据格式化策略被修改。')
    contract=p.get('boot_contract',{})
    if contract.get('data_filesystems')!={'/data':'f2fs','/metadata':'ext4'} or contract.get('boot_header') not in (3,4) or contract.get('vendor_header') not in (3,4):raise ValueError('OTA 启动或 data 格式不受支持。')
    for n in ('vbmeta','vbmeta_system'):
        v=contract.get(n,{})
        if v.get('flags') not in (0,1,2,3) or type(v.get('rollback_index')) is not int or not 0<=v['rollback_index']<2**64 or not HEX.fullmatch(v.get('metadata_sha256','')):raise ValueError('OTA AVB 观测不完整。')
    return p

def blockers(p):
    validate(p);problems=[]
    missing=ESSENTIAL-set(p['ota']['partitions'])
    if missing:problems.append('此 OTA 未提供完整的底层固件比对依据：'+', '.join(sorted(missing))+'。不能据 ROM 名称猜测所需固件。')
    if p['boot_contract']['vbmeta']['flags']!=3:problems.append('通用执行器 v1 尚未验收启用 AVB 校验的未知 ROM 启动链；不会修改 ROM 自带的 AVB flags。')
    return problems

def requirements(p):
    validate(p)
    return [dict(partition=n+'_a',**p['ota']['partitions'][n]) for n in sorted((set(p['ota']['partitions'])&FIRMWARE)|{'dtbo'})]

def assess(p,inventory,measure,read):
    """Callbacks only read verified block identities; usable on host or device."""
    problems=blockers(p);checks=[]
    for r in p['images'].values():
        part=inventory.get(r['partition'])
        if not part or part['bytes']<r['bytes'] or (r['partition']=='super' and part['bytes']!=SUPER_BYTES):problems.append(r['partition']+'：安装目标缺失或容量不匹配。')
    for name,minimum in (('userdata',16*1024**3),('metadata',16*1024**2)):
        if name not in inventory or inventory[name]['bytes']<minimum:problems.append(name+'：缺少足够容量的 Android 数据分区，请先单独完成分区配置。')
    for r in requirements(p):
        part=inventory.get(r['partition']);actual=None
        if not part or part['bytes']<r['bytes']:reason='缺少分区或容量不足'
        else:
            actual=measure(part,r['bytes']);reason='' if actual==r['sha256'] else '与 ROM 所需内容不同'
        checks.append(dict(**r,actual_sha256=actual,matched=not reason))
        if reason:problems.append(r['partition']+'：'+reason+'；保持现状，禁止自动更新底层固件/DTBO。')
    for name in ('vbmeta','vbmeta_system'):
        part=inventory.get(name+'_a')
        raw=read(part,256) if part else b''
        if len(raw)!=256 or raw[:4]!=b'AVB0':problems.append('无法确认当前 '+name+' 回滚索引。');continue
        current=struct.unpack_from('>Q',raw,112)[0]
        if p['boot_contract'][name]['rollback_index']<current:problems.append(name+' 的目标回滚索引低于当前镜像。')
    return dict(compatible=not problems,blockers=problems,firmware_checks=checks,dtbo_unchanged=next(r['matched'] for r in checks if r['partition']=='dtbo_a'),
        automatic_firmware_update=False,hardware_install_verified=False,
        limitations=['未读取 TEE 保存的全部回滚计数。','软件检查不能替代真实安装、首次启动和失败恢复验收。'])

def require_device(p,hardware):
    if not generic(p):return
    inventory=hardware.inventory();hardware.sealed(list(inventory.values()))
    result=assess(p,inventory,lambda part,n:hardware.digest(part,0,n),lambda part,n:hardware.read(part,0,n))
    if not result['compatible']:raise RuntimeError('OTA 设备条件不满足：'+'；'.join(result['blockers']))
    return result
