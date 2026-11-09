# SPDX-License-Identifier: MIT
"""Pinned ROM admission, shared by the host and Recovery. No device access."""
import hashlib,json,re
from pathlib import Path
CATALOG=Path(__file__).with_name('liuqin-android-roms.json')
ANDROID_ACTIONS=frozenset(('guard','stage','install','commit','android-direct'))
READ_ACTIONS=frozenset(('partition-inspect','partition-resume-check','inspect','android-check','export-boot'))
def catalog():
    data=json.loads(CATALOG.read_text())
    if data.get('schema')!=1 or data.get('device')!='liuqin' or not isinstance(data.get('roms'),list) or not 1<=len(data['roms'])<=32:raise ValueError('ROM 准入目录无效。')
    ids=set();files=set()
    for r in data['roms']:
        if (not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',r.get('id','')) or r['id'] in ids or
            not re.fullmatch('liuqin-[a-z0-9-]+[.]json',r.get('profile','')) or r['profile'] in files or
            r.get('kind') not in ('stock-images','payload-zip-v2') or r.get('write_status') not in ('experimental','blocked') or
            r.get('firmware_policy')!='preserve-existing' or r.get('hardware_install_verified') is not False or
            not isinstance(r.get('label'),str) or not r['label'] or
            not isinstance(r.get('blockers'),list) or any(not isinstance(b,str) or not b for b in r['blockers']) or
            (r['write_status']=='blocked')!=bool(r['blockers'])):raise ValueError('ROM 准入条目无效。')
        ids.add(r['id']);files.add(r['profile'])
    return data['roms']
def policy(profile_id):
    matches=[r for r in catalog() if r['id']==profile_id]
    if len(matches)!=1:raise ValueError('未收录的 Android ROM 适配。')
    return matches[0]
def assessment(profile):
    from liuqin_ota import generic,blockers,validate
    if generic(profile):
        validate(profile);reasons=blockers(profile)
        return dict(profile_id=profile['id'],label=profile['rom'],status='blocked' if reasons else 'conditional',write_allowed=not reasons,hardware_install_verified=False,
            firmware_policy=profile['firmware_policy'],blockers=reasons,warning='完整 ROM 将按确认清单更新系统、启动和随包固件；用户数据会清空，Linux 分区和引导文件保留。真实刷写仍需验收。')
    r=policy(profile.get('id','stock'))
    return dict(profile_id=r['id'],label=r['label'],status=r['write_status'],write_allowed=r['write_status']=='experimental',
        hardware_install_verified=False,firmware_policy=r['firmware_policy'],blockers=list(r['blockers']),
        warning='安装候选仅完成离线验证；还需通过当前设备检查、备份及完整计划确认，不代表已完成真机安装验收。')
def require_install(profile):
    result=assessment(profile)
    if not result['write_allowed']:raise ValueError(result['label']+' 仅支持分析和材料校验，禁止写入：'+'；'.join(result['blockers']))
    return result
def request_profile(action,request,native_manifest=None):
    from liuqin_android import load_profile,canonical
    from liuqin_ota import validate
    if 'generated_profile' in request:
        if not native_manifest or native_manifest.get('generic_ota_version',0)<request['generated_profile'].get('generic_ota_version',999) or native_manifest.get('inspection_only') is not False:raise ValueError('当前 Rec 没有通用 OTA 执行能力。')
        profile=validate(request['generated_profile'])
        if request.get('profile_id')!=profile['id']:raise ValueError('OTA 执行描述身份不一致。')
    else:profile=load_profile(request.get('profile_id','stock'))
    if request.get('profile_sha256')!=hashlib.sha256(canonical(profile)).hexdigest():raise ValueError('电脑与 Rec 中的 ROM 适配版本不一致。')
    # An unadmitted ROM cannot accompany partition edits before a later Android
    # failure, or be smuggled through a different write action.
    if action not in READ_ACTIONS:
        require_install(profile)
        if 'generated_profile' in request:
            from liuqin_native_install import require
            session=require(request.get('native_token'))
            if not session or session['scope'].get('android_profile_sha256')!=request['profile_sha256']:raise ValueError('OTA 执行描述不属于已确认的安装会话。')
    return profile
