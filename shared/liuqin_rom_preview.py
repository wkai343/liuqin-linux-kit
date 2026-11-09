# SPDX-License-Identifier: MIT
"""Project a ROM's A-slot effects without creating an executable install plan."""
import copy
import hashlib
import json
import re
from liuqin_firmware import validate_snapshot, fingerprint_id, ROLES, LAYOUT
from liuqin_universal_recovery import assess, load_catalog
from liuqin_partition_names import is_root_name

SYSTEM=('boot','vendor_boot','dtbo','vbmeta','vbmeta_system','super')
LOGICAL={'system','system_ext','product','vendor','odm','vendor_dlkm','odm_dlkm','system_dlkm','mi_ext'}

def validate_layout(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=512:raise ValueError('分区读取结果无效。')
    names=set();nodes=set()
    for r in rows:
        if (not isinstance(r,dict) or not isinstance(r.get('name'),str) or not r['name'] or len(r['name'])>128 or r['name'] in names or
            not re.fullmatch(r'sd[a-z][1-9][0-9]*',r.get('node','')) or r['node'] in nodes or
            not re.fullmatch(r'[0-9]+:[0-9]+',r.get('dev','')) or r.get('ro')!='1' or
            type(r.get('size')) is not int or not 0<r['size']<2**44):raise ValueError('分区身份重复、缺失或存储未保持只读。')
        names.add(r['name']);nodes.add(r['node'])
    return rows

def build(analysis,snapshot,layout,firmware_policy='preserve'):
    if firmware_policy not in ('preserve','rom'):raise ValueError('未知的底层固件预览策略。')
    validate_snapshot(snapshot);validate_layout(layout)
    if analysis['profile'].get('unadmitted'):return unadmitted_preview(analysis,snapshot,layout,firmware_policy)
    catalog=load_catalog();p=analysis['profile'];parts={r['name']:r for r in layout};images={r['name']:r for r in analysis['images']}
    after=copy.deepcopy(snapshot);after['active_slot']='a';slot=after['slots']['a'];measured=copy.deepcopy(slot.get('measured',slot.get('match',{})))
    assumptions=[];blockers=list(analysis['issues']);warnings=[]
    for name in analysis['missing']:blockers.append('缺少安装材料：'+name)
    for role,field in [('vbmeta','avb'),('vbmeta_system','avb_system')]:
        value=analysis['projected'][role];measured[role]=copy.deepcopy(value['measurement']);slot[field]=copy.deepcopy(value['avb'])
        if role not in images:assumptions.append(role+' 使用已收录 ROM 基线；所选材料中未实际提供。')
    measured['dtbo']=copy.deepcopy(analysis['projected']['dtbo']['measurement'])
    if 'dtbo' not in images:assumptions.append('DTBO 使用目标 ROM 基线；没有提供对应镜像。')
    abl_source='保留当前 A 槽 ABL'
    if firmware_policy=='rom' and p['rom_abl']:
        base=next(b for b in catalog['policy']['baselines'] if b['id']==p['rom_abl'])
        observed=images.get('abl')
        if observed and not observed['matched']:
            measured['abl']=dict(partition_bytes=observed['bytes'],hash_bytes=observed['bytes'],sha256=observed['sha256'])
        elif measured.get('abl',{}).get('partition_bytes')!=base['abl_partition_bytes']:
            measured.pop('abl',None)
            blockers.append('实际 A 槽 ABL 分区容量不符合目标底座；预览不假设重建分区表。')
        else:
            measured['abl']=dict(partition_bytes=base['abl_partition_bytes'],hash_bytes=base['abl_partition_bytes'],sha256=base['abl_partition_sha256'])
            if not observed:assumptions.append('ABL 使用目标固件基线；没有提供待写 ABL 镜像。')
        abl_source='采用 ROM 的 ABL（仅推演，不写入）'
        blockers.append('更新底层固件的完整组合、防回滚和恢复路径尚未验收。')
    elif firmware_policy=='rom' and not p['rom_abl']:
        if 'abl' in images:blockers.append('ROM 自带 ABL 尚无已收录的适配条件，不推定可替换当前 Bootloader。')
        else:warnings.append('此 ROM 未提供 ABL；安装后继续使用设备当前 ABL。')
    elif p['rom_abl']:
        warnings.append('本计划保留现有 ABL；安装目标 ROM 不等于换成该 ROM 的 Bootloader。')
    slot['measured']=measured
    if set(measured)==set(ROLES)|{'layout'} and set(measured['layout'])==set(LAYOUT):
        slot.update(status='complete',match=copy.deepcopy(measured),fingerprint_id=fingerprint_id(measured));slot.pop('errors',None)
    else:
        slot.update(status='incomplete',errors=['预览保留了不完整的设备测量。']);slot.pop('match',None);slot.pop('fingerprint_id',None)
    before_a=copy.deepcopy(snapshot);before_a['active_slot']='a'
    before=assess(before_a,catalog);rec=assess(after,catalog)
    rec['basis']='基于目标 ROM 基线的条件推演' if assumptions else '基于已分析材料与当前固件的条件推演'
    writes=[]
    for name in SYSTEM:
        target=name if name=='super' else name+'_a';r=images.get(name)
        availability='已核对' if r and r['matched'] else '未收录' if r else '待提供'
        if name=='super' and {'system','vendor','product'}<=set(images):availability='需由 OTA 动态分区组装，尚未生成写入镜像'
        required_bytes=p.get('super_bytes') if name=='super' else r['bytes'] if r else None
        if target not in parts:blockers.append('设备缺少目标分区：'+target)
        elif required_bytes and required_bytes>parts[target]['size']:blockers.append('镜像展开大小大于目标分区：'+target)
        writes.append(dict(partition=target,action='写入目标 Android',material=availability,bytes=r['bytes'] if r else None,required_bytes=required_bytes))
    firmware=[]
    for name in sorted(set(images)-set(SYSTEM)-LOGICAL-{'recovery','userdata','metadata','super_empty'}):
        firmware.append(dict(partition=name+'_a',action='保留' if firmware_policy=='preserve' else '待核对后更新',material='已核对' if images[name]['matched'] else '未收录'))
    if firmware_policy=='rom' and p['rom_abl'] and 'abl' not in images:firmware.append(dict(partition='abl_a',action='目标基线假设，缺少镜像',material='待提供'))
    if firmware_policy=='rom' and p['rom_abl'] and not analysis['reference_only']:
        missing_firmware=set(p['images'])-set(SYSTEM)-LOGICAL-{'recovery','userdata','metadata','super_empty'}-set(images)
        if missing_firmware:blockers.append('缺少 ROM 底层固件材料：'+'、'.join(sorted(missing_firmware)))
    for name in ('userdata','metadata'):
        if name not in parts:blockers.append('设备缺少 '+name+'；本预览不新建或修改分区表。')
    protected=[r['name'] for r in layout if is_root_name(r['name']) or r['name']=='linux_boot']
    if protected:warnings.append('已列出 Linux 分区保护范围；执行前仍须核验引导登记与备份，当前预览不证明系统可恢复。')
    warnings.extend(['ROM 自带 Recovery 不纳入写入计划，recovery_a/recovery_b 保留。',
        'super 是共享分区；保留 B 槽物理启动分区不保证 B 槽 Android 可启动。',
        '本轮不修改 AVB flags；按 ROM 自带的 vbmeta 推演结果。'])
    if any(not h['optional'] for h in analysis['source'].get('hooks',[])):blockers.append('ROM 要求 postinstall；本分析器不执行安装脚本，需要单独适配。')
    admission=None
    if p.get('install_profile_id'):
        from liuqin_android import load_profile
        from liuqin_rom_admission import assessment
        admission=assessment(load_profile(p['install_profile_id']));blockers.extend(admission['blockers'])
    blockers.extend(['当前预览不授予写入权限；安装必须另行通过准入、设备检查和备份确认。','Android 完整启动链、防回滚、数据清空与备份恢复尚未完成本流程验收。','v2 Recovery 固定冷启动及回退尚未完成真机验收。'])
    if not rec['requirements_satisfied']:blockers.append('安装后 Rec 条件：'+rec['reason'])
    result=dict(schema=1,preview_only=True,executable=False,serial=snapshot['serial'],boot_id=snapshot['boot_id'],target_slot='a',
        rom=dict(id=p['id'],label=p['label']),source=analysis['source'],reference_only=analysis['reference_only'],
        firmware_policy=firmware_policy,abl_source=abl_source,rec_before=before,rec_after=rec,assumptions=assumptions,
        writes=writes,formats=['userdata','metadata'],firmware=firmware,protected=protected,
        preserved=['GPT 分区表','recovery_a / recovery_b','B 槽物理启动分区',*protected],
        admission=admission,images=analysis['images'],warnings=warnings,blockers=list(dict.fromkeys(blockers)),
        material_status='仅基线推演' if analysis['reference_only'] else '材料不完整或有差异' if analysis['missing'] or analysis['issues'] else '材料身份已核对，仍不是可执行方案',
        device_fingerprint=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest())
    result['preview_id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def unadmitted_preview(analysis,snapshot,layout,firmware_policy):
    """Describe unknown full OTAs without inventing any post-install boot proof."""
    images={r['name']:r for r in analysis['images']};protected=[p['name'] for p in layout if is_root_name(p['name']) or p['name']=='linux_boot']
    blockers=['ROM 没有已收录的安装规则，禁止写入。','目标启动链、当前固件兼容性、数据格式及失败恢复路径待验证。',*analysis['issues']]
    blockers += ['缺少安装材料：'+n for n in analysis['missing']]
    if any(not h['optional'] for h in analysis['source'].get('hooks',[])):blockers.append('包含必须执行的 postinstall，当前安装器不执行 ROM 程序。')
    unknown=dict(status='unqualified-rom',requirements_satisfied=False,fixed_install_allowed=False,automatic_boot_allowed=False,basis='未知 ROM，不推定安装后的 Rec 可启动',reason='需要为这份 ROM 核验启动链与恢复路径。')
    value=dict(schema=1,preview_only=True,executable=False,serial=snapshot['serial'],boot_id=snapshot['boot_id'],target_slot='a',
        rom=dict(id=analysis['profile']['id'],label=analysis['profile']['label']),source=analysis['source'],reference_only=False,
        firmware_policy=firmware_policy,abl_source='底层固件保持不变；没有更新许可',rec_before=assess(snapshot,load_catalog()),rec_after=unknown,assumptions=[],
        writes=[dict(partition=n if n=='super' else n+'_a',action='待适配，不允许写入',material='已解析 payload 声明' if n in images else '需生成或提供') for n in SYSTEM],
        formats=['userdata','metadata'],firmware=[dict(partition=n+'_a',action='保留',material='待适配') for n in images if n not in set(SYSTEM)|LOGICAL|{'recovery'}],
        protected=protected,preserved=['GPT 分区表','底层固件','recovery_a / recovery_b','B 槽物理启动分区',*protected],
        images=analysis['images'],warnings=['文件完整性校验不等于发行者签名验证，也不代表机型固件兼容。'],blockers=list(dict.fromkeys(blockers)),
        material_status='已解析完整 OTA，未准入安装',device_fingerprint=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest())
    value['preview_id']=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest();return value
