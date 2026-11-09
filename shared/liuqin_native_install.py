# SPDX-License-Identifier: MIT
"""Scoped RAM authorization for native installer candidates; no block writes here."""
import hashlib,json,os,re,stat,uuid
from pathlib import Path
from liuqin_partition_names import is_root_name,validate_name

MARKER='etc/liuqin-native-recovery.json'
SESSION='run/liuqin-native-install-session.json'
CAPABILITIES=['backup','partition-edit','android-install','linux-install','boot-bind']
FIXED_USAGE='fixed-recovery-installer'
READ_ACTIONS={'partition-inspect','partition-resume-check','inspect','android-check','export-boot'}
# The Android guard reads storage but belongs to the authorized installation
# session: it must verify the selected ROM against the confirmed plan.
WRITE_ACTIONS={'partition-edit','partition-resume','android-direct','guard','stage','install','commit','linux-stage','linux-guard','linux-finish','linux-commit'}
ANDROID={'super','vendor_boot_a','dtbo_a','vbmeta_a','vbmeta_system_a'}
TOKEN='LIUQIN_NATIVE_INSTALL_TOKEN'

def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(v):return hashlib.sha256(encoded(v)).hexdigest()
def capable(m):
    role = m.get('usage') == 'temporary-boot-only' or (
        m.get('usage') == FIXED_USAGE and m.get('fixed_installer_version') == 1)
    return (m.get('installer_version') == 1 and m.get('inspection_only') is False
            and m.get('installer_capabilities') == CAPABILITIES and role)
def load(path):
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_size>1024**2:raise RuntimeError('原生安装控制文件无效。')
    return json.loads(path.read_text())
def marker(root=Path('/')):
    p=root/MARKER
    return load(p) if p.exists() else None
def session(root=Path('/')):
    p=root/SESSION
    return load(p) if p.exists() else None
def store(value,root=Path('/')):
    p=root/SESSION;p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.'+uuid.uuid4().hex)
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as f:f.write(encoded(value));f.flush();os.fsync(f.fileno())
    os.replace(tmp,p)
def identity(p):
    validate_name(p['name']);uuid.UUID(p['partuuid'])
    if not re.fullmatch('sd[a-z]',p['parent']) or any(type(p.get(k)) is not int or p[k]<0 or p[k]%4096 for k in ('start','bytes')) or not p['bytes']:raise ValueError('写入目标范围无效。')
    return {k:p[k] for k in ('name','parent','partuuid','start','bytes')}
def allowed_name(name,full_rom=False):
    from liuqin_ota import FIRMWARE
    extra={n+'_a' for n in FIRMWARE}|{'recovery_a'} if full_rom else set()
    return name in ANDROID|{'userdata','metadata','linux_boot','boot_a'}|extra or is_root_name(name)

def scope(snapshot):
    """Only the full GUI workflow may authorize native writes."""
    if snapshot['kind'] == 'maintenance':
        plan = snapshot['maintenance_plan']
        return dict(kind='maintenance', parts=plan['parts'], maintenance_plan_sha256=digest(plan),
                    operation=plan['operation'], previous_boot_sha256=plan['previous_boot_sha256'])
    if snapshot['kind'] == 'switch':
        plan = snapshot['switch_plan']
        return dict(kind='switch', parts=plan['parts'], switch_plan_sha256=digest(plan),
                    previous_boot_sha256=plan['previous_boot_sha256'])
    full_rom=snapshot.get('full_rom',False) is True
    rom_targets=set(snapshot.get('rom_targets',[])) if full_rom else set()
    edit=snapshot['edit'];current=edit['source']['inventory'];projected=edit['projected']['inventory'];names=set()
    combined=snapshot['kind']=='combined'
    jobs=snapshot.get('installations',[])
    linux_installs={j['target']:j['source']['archive_sha256'] for j in jobs if j['kind']=='linux'} if combined else {}
    android=snapshot['kind']=='android' or (combined and any(j['kind']=='android' for j in jobs))
    default=snapshot.get('default_target',snapshot['target'] if snapshot['request'].get('set_default') else '')
    for row in edit['formats']:
        names.add(row['partition']['name'])
        if row['role']=='android':names.add('metadata')
    for row in edit['partitions']:
        if row['role'] in ('android','store'):names.add(row['name'])
    if ('linux_boot' in projected or 'linux_boot' in current) and (edit['changes'] or snapshot['kind'] in ('linux','android','combined')):names.add('linux_boot')
    if snapshot['kind']=='linux':names.add(snapshot['target'])
    names.update(linux_installs)
    if android:names.update((rom_targets if full_rom else ANDROID)|{'userdata','metadata'})
    fixed_recovery=snapshot.get('fixed_recovery',{}).get('plan')
    if fixed_recovery:names.add('recovery_a')
    if default and snapshot['kind'] in ('linux','android','combined'):names.add('boot_a')
    parts=[]
    for name in sorted(names):
        if not allowed_name(name,full_rom):raise ValueError('原生安装禁止修改此分区：'+name)
        p=projected.get(name,current.get(name))
        if p is None:raise ValueError('安装目标缺少分区身份：'+name)
        parts.append(identity(p))
        # Partition editing journals to the old store before deleting/recreating
        # it; both exact identities belong to that explicitly confirmed edit.
        if name=='linux_boot' and edit['changes'] and name in current and identity(current[name])!=identity(p):parts.append(identity(current[name]))
    for row in edit.get('deletes',[]):
        if row['name'] not in ('userdata','linux_boot') and not is_root_name(row['name']):raise ValueError('禁止删除底层固件分区。')
    if snapshot['recovery_mode']!='off':raise ValueError('原生安装流程保持当前 Rec；不同时切换或刷写 Recovery。')
    result = dict(kind=snapshot['kind'],parts=parts,full_rom=full_rom,android_boot_write=android and default=='userdata',rom_targets=sorted(rom_targets),rom_backups=snapshot.get('rom_backups',{}),gpt_sha256=digest(edit) if edit['changes'] else None,
        android_install=android,linux_installs=linux_installs,default_target=default,
        root_name=snapshot['target'] if snapshot['kind']=='linux' else None,
        root_archive_sha256=snapshot['source']['archive_sha256'] if snapshot['kind']=='linux' else None,
        previous_boot_sha256=snapshot['previous_boot_sha256'],
        android_profile_sha256=snapshot.get('android_profile_sha256'),android_boot_sha256=snapshot.get('android_boot_sha256'))
    if fixed_recovery:result['fixed_recovery']=fixed_recovery
    return result

def validate_scope(s):
    if not isinstance(s,dict) or s.get('kind') not in ('linux','android','partitions','combined','switch','maintenance') or not isinstance(s.get('parts'),list) or len(s['parts'])>128:raise ValueError('原生安装范围无效。')
    if s['kind'] == 'maintenance':
        operation = s.get('operation')
        names = ['boot_a','linux_boot'] if operation == 'restore-switch' else ['linux_boot']
        if (set(s) != {'kind','parts','operation','maintenance_plan_sha256','previous_boot_sha256'} or
                operation not in ('cleanup','restore-switch','finish-switch','finish-install') or
                [p['name'] for p in s['parts']] != names or any(identity(p) != p for p in s['parts']) or
                any(not isinstance(s.get(k),str) or not re.fullmatch('[a-f0-9]{64}',s[k])
                    for k in ('maintenance_plan_sha256','previous_boot_sha256'))):
            raise ValueError('引导存储维护范围无效。')
        return s
    if s['kind'] == 'switch':
        if (set(s) != {'kind', 'parts', 'switch_plan_sha256', 'previous_boot_sha256'}
                or [p['name'] for p in s['parts']] != ['boot_a', 'linux_boot']
                or any(identity(p) != p for p in s['parts'])
                or any(not isinstance(s[key], str) or not re.fullmatch('[a-f0-9]{64}', s[key])
                       for key in ('switch_plan_sha256', 'previous_boot_sha256'))):
            raise ValueError('默认引导切换范围无效。')
        return s
    full_rom=s.get('full_rom') is True
    fixed_recovery=s.get('fixed_recovery')
    if fixed_recovery is not None:
        from liuqin_recovery_preserve import validate
        validate(fixed_recovery)
        if not full_rom or fixed_recovery['partition'] not in s['parts']:
            raise ValueError('固定 Rec 保护必须绑定完整 Android 安装范围。')
    if full_rom and (not (s['kind']=='android' or (s['kind']=='combined' and s.get('android_install') is True)) or type(s.get('android_boot_write')) is not bool or not isinstance(s.get('rom_targets'),list) or not s.get('android_profile_sha256')):raise ValueError('完整 ROM 会话缺少材料身份。')
    if full_rom and (len(set(s['rom_targets']))!=len(s['rom_targets']) or any(not allowed_name(n,True) or n in ('userdata','metadata','linux_boot') or is_root_name(n) for n in s['rom_targets'])):raise ValueError('完整 ROM 写入目标越界。')
    names=set();identities=set();stores=0
    for p in s['parts']:
        key=digest(p)
        if (not allowed_name(p['name'],full_rom) or (p['name'] in names and p['name']!='linux_boot') or key in identities or identity(p)!=p):raise ValueError('安装范围重复或包含受保护分区。')
        if p['name']=='linux_boot':
            stores+=1
            if stores>2 or (stores==2 and not s.get('gpt_sha256')):raise ValueError('引导存储身份不属于明确的分区编辑。')
        if full_rom and p['name'] not in ANDROID|{'userdata','metadata','linux_boot','boot_a'} and not is_root_name(p['name']) and p['name'] not in s['rom_targets'] and not (fixed_recovery and p['name']=='recovery_a'):raise ValueError('固件分区未在 ROM 清单中声明。')
        identities.add(key)
        names.add(p['name'])
    for name in ('previous_boot_sha256','gpt_sha256','root_archive_sha256','android_profile_sha256','android_boot_sha256'):
        value=s.get(name)
        if value is not None and not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('安装范围摘要无效。')
    if s['kind']=='linux' and (s.get('root_name') not in names or not is_root_name(s['root_name']) or not s.get('root_archive_sha256')):raise ValueError('缺少 Linux 安装目标。')
    if s['kind']=='combined':
        roots=s.get('linux_installs');android=s.get('android_install');default=s.get('default_target')
        if (type(android) is not bool or not isinstance(roots,dict) or not roots or len(roots)+int(android)<2 or
            any(not is_root_name(n) or n not in names or not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h) for n,h in roots.items()) or
            (android and not s.get('android_profile_sha256')) or (not android and s.get('android_profile_sha256')) or
            default not in set(roots)|({'userdata'} if android else set())|{''} or (default and 'boot_a' not in names) or
            s.get('android_boot_write')!=(android and default=='userdata')):raise ValueError('联合计划的系统目标、归档或默认引导无效。')
        if {n for n in names if is_root_name(n)}-set(roots) and not s.get('gpt_sha256'):raise ValueError('联合计划包含未选择的 rootfs。')
    if full_rom:
        if not set(s['rom_targets'])<=names or set(s.get('rom_backups',{}))!=set(s['rom_targets']):raise ValueError('完整 ROM 缺少逐分区备份。')
        for n,b in s['rom_backups'].items():
            if b.get('identity') not in s['parts'] or b['identity']['name']!=n or not re.fullmatch('[a-f0-9]{64}',b.get('sha256','')):raise ValueError('ROM 分区备份身份无效。')
    return s

def begin(request,probe,root=Path('/')):
    m=marker(root)
    if not m or not capable(m) or not probe.get('sealed') or probe.get('slot')!='a' or probe.get('unlocked') is not True or probe.get('device')!='liuqin':raise RuntimeError('安装候选或只读设备身份不符合要求。')
    if probe['build_id']!=m['build_id'] or request.get('build_id')!=m['build_id'] or request.get('boot_id')!=probe['boot_id'] or request.get('serial')!=probe['serial']:raise RuntimeError('安装确认不属于当前 Rec 会话。')
    old=session(root)
    recovering = (request.get('scope',{}).get('kind') == 'maintenance' and
                  (request.get('scope',{}).get('operation') != 'cleanup' or
                   (old or {}).get('scope',{}).get('kind') == 'maintenance') and
                  m.get('boot_store_version') == 1 and old and old.get('state') == 'failed')
    if old and old.get('state')!='complete' and not recovering:raise RuntimeError('前一次安装尚未安全结束；保留 Rec 并检查记录。')
    if not re.fullmatch('[a-f0-9]{32}',request.get('token','')) or not re.fullmatch('[a-f0-9]{64}',request.get('plan_id','')):raise ValueError('安装确认标识无效。')
    s=validate_scope(request['scope'])
    if s['kind']=='maintenance' and m.get('boot_store_version') != 1:
        raise ValueError('当前 Rec 不支持引导存储维护。')
    if s.get('fixed_recovery'):
        if m.get('fixed_recovery_restore_version')!=1:
            raise ValueError('当前 Rec 不支持固定 Recovery 自动保护。')
        if request.get('fixed_recovery_backup_verified') is not True:
            raise ValueError('尚未核验固定 Recovery 的完整备份。')
    if s['kind']=='combined' and m.get('combined_install_version')!=1:raise ValueError('当前 Rec 不支持联合安装计划。')
    if s['kind']=='switch' and m.get('boot_switch_version')!=1:raise ValueError('当前 Rec 不支持切换默认系统。')
    if (s['kind']=='android' or (s['kind']=='combined' and s['android_install'])) and m.get('generic_ota_version',0)>=1:
        from liuqin_rom_admission import require_install
        profile=request.get('android_profile',{})
        require_install(profile)
        if profile.get('generic_ota_version',0)>m.get('generic_ota_version',0):raise ValueError('ROM 执行描述需要更新的 Rec。')
        if (profile.get('generic_ota_version')==2)!=(s.get('full_rom') is True):raise ValueError('完整 ROM 必须绑定完整的固件范围与备份，不能沿用旧系统写入范围。')
        if not profile.get('generic_ota_version'):
            from liuqin_android import load_profile
            if digest(profile)!=digest(load_profile(profile.get('id','stock'))):raise ValueError('固定 ROM 适配与 Rec 中的版本不同。')
        if s.get('android_profile_sha256')!=digest(profile) or s.get('android_boot_sha256')!=profile['images']['boot.img']['sha256']:raise ValueError('Android 安装描述或引导不属于确认范围。')
        if profile.get('generic_ota_version')==1 and s.get('gpt_sha256'):raise ValueError('通用 OTA 不与 GPT 编辑混合执行。')
    if s.get('full_rom'):
        from liuqin_ota import full,write_names
        profile=request.get('android_profile',{})
        if m.get('generic_ota_version',0)<2 or not full(profile) or set(s['rom_targets'])!=write_names(profile)|({'boot_a'} if s['android_boot_write'] else set()):raise ValueError('完整 ROM 清单与 Rec 能力不匹配。')
        if request.get('rom_backups_verified') is not True:raise ValueError('尚未核验完整 ROM 备份。')
    value=dict(schema=1,state='active',token=request['token'],plan_id=request['plan_id'],scope=s,scope_sha256=digest(s),build_id=m['build_id'],boot_id=probe['boot_id'],serial=probe['serial'])
    if recovering:
        value['superseded_session'] = {key:old.get(key) for key in
            ('state','plan_id','scope_sha256','build_id','boot_id','serial','write_started')}
    store(value,root);return dict(state='active',plan_id=value['plan_id'])

def require(token=None,root=Path('/'),allow_failed=False):
    m=marker(root)
    if m is None:return None # Existing, separately validated legacy installer.
    if not capable(m):raise RuntimeError('此原生 Rec 是只读版，没有安装能力。')
    s=session(root);token=os.environ.get(TOKEN,'') if token is None else token
    if (not s or s.get('state') not in (('active','failed') if allow_failed else ('active',)) or not token or s.get('token')!=token or s.get('build_id')!=m['build_id'] or
        s.get('boot_id')!=(root/'proc/sys/kernel/random/boot_id').read_text().strip() or s.get('scope_sha256')!=digest(s.get('scope'))):raise RuntimeError('没有绑定当前启动会话的有效安装确认。')
    validate_scope(s['scope']);return s

def authorize_target(part,token=None,root=Path('/')):
    s=require(token,root)
    if s and identity(part) not in s['scope']['parts']:raise RuntimeError('目标分区不在已确认的安装范围内：'+part['name'])

def authorize_gpt(plan,root=Path('/')):
    s=require(root=root)
    if s and s['scope'].get('gpt_sha256')!=digest(plan):raise RuntimeError('GPT 方案不匹配已确认的完整计划。')

def mark_write(root=Path('/')):
    s=require(root=root)
    if s and not s.get('write_started'):s['write_started']=True;store(s,root)

def authorize_action(action,request,root=Path('/')):
    if marker(root) is None or action in READ_ACTIONS:return
    s=require(request.get('native_token',''),root)
    if action not in WRITE_ACTIONS:raise RuntimeError('原生 Rec 不支持此写入操作。')
    kind=s['scope']['kind']
    if kind == 'switch':raise RuntimeError('默认引导切换会话禁止安装或编辑分区。')
    if kind == 'maintenance':raise RuntimeError('引导维护会话禁止安装或编辑分区。')
    scope=s['scope']
    linux=kind=='linux' or (kind=='combined' and bool(scope['linux_installs']))
    android=kind=='android' or (kind=='combined' and scope['android_install'])
    if (action.startswith('linux-') and not linux) or (action in ('guard','stage','install','commit','android-direct') and not android):raise RuntimeError('操作与已确认系统类型不一致。')
    if kind=='combined' and action.startswith('linux-') and request.get('plan',{}).get('system',{}).get('partition',{}).get('name') not in scope['linux_installs']:raise RuntimeError('Linux 子计划未包含在确认范围内。')
    os.environ[TOKEN]=s['token']

def authorize_root(name,archive_sha256,root=Path('/')):
    s=require(root=root);scope=s['scope'] if s else {}
    expected=scope.get('linux_installs',{}).get(name) if scope.get('kind')=='combined' else scope.get('root_archive_sha256') if scope.get('kind')=='linux' and scope.get('root_name')==name else None
    if not expected or archive_sha256!=expected:raise RuntimeError('rootfs 不在已确认的安装范围。')
    return s

def finish(request,probe,root=Path('/')):
    s=require(request.get('token',''),root,allow_failed=True)
    if not s:raise RuntimeError('不是原生安装会话。')
    if s['state']=='failed':return dict(state='failed',sealed=probe.get('sealed') is True)
    if request.get('success') is True and s['scope'].get('fixed_recovery') and not s.get('fixed_recovery_verified'):
        raise RuntimeError('尚未完成固定 Recovery 及安装后启动链回读。')
    success=(request.get('success') is True or not s.get('write_started')) and probe.get('sealed') is True
    s['state']='complete' if success else 'failed';store(s,root)
    return dict(state=s['state'],sealed=probe.get('sealed') is True)

def interrupted(root=Path('/')):
    try:
        s=require(root=root)
        if s and s.get('write_started'):s['state']='failed';store(s,root)
    except (OSError,RuntimeError,ValueError):pass

def signal_handlers():
    import signal
    def stopped(number,frame):raise InterruptedError('安装控制连接已中断，保持 Recovery。')
    for name in ('SIGHUP','SIGTERM'):signal.signal(getattr(signal,name),stopped)
