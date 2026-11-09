#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Native installer control and boot_a writer. All authority is RAM/session bound."""
import fcntl,hashlib,importlib.util,json,os,re,shutil,stat,sys,tempfile,urllib.parse,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import liuqin_native_install as gate

BOOT_CAPACITY = 192 * 1024**2
CHUNK = 4 * 1024**2

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def write_boot(request,hardware,fetch=urllib.request.urlopen):
    s=gate.require(request.get('token',''));os.environ[gate.TOKEN]=s['token']
    if s['scope'].get('kind') in ('switch','maintenance'):raise RuntimeError('切换引导会话仅接受已确认的切换计划。')
    parts=hardware.inventory();p=parts['boot_a'];gate.authorize_target(p)
    android_default=s['scope'].get('kind')!='combined' or s['scope'].get('default_target')=='userdata'
    if android_default and s['scope'].get('android_boot_sha256') and request.get('sha256')!=s['scope']['android_boot_sha256']:raise RuntimeError('新 boot 不属于已确认的 Android OTA。')
    if p['bytes']!=192*1024**2 or request.get('bytes')!=p['bytes'] or request.get('previous_sha256')!=s['scope']['previous_boot_sha256']:raise RuntimeError('boot_a 尺寸或旧引导身份不匹配。')
    if not re.fullmatch('[a-f0-9]{64}',request.get('sha256','')):raise ValueError('新引导校验值无效。')
    url=urllib.parse.urlsplit(request['url'])
    if url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password or url.fragment or not re.fullmatch('/[a-f0-9]{32}/boot',url.path):raise ValueError('引导来源不是当前 ADB 环回传输端点。')
    hardware.sealed(list(parts.values()))
    if hardware.digest(p)!=request['previous_sha256']:raise RuntimeError('写入前 boot_a 已改变。')
    with tempfile.TemporaryDirectory(prefix='native-boot-',dir='/run') as d:
        path=Path(d)/'boot.img';h=hashlib.sha256();left=p['bytes']
        with fetch(request['url'],timeout=60) as source,path.open('xb') as target:
            while left:
                data=source.read(min(left,4*1024**2))
                if not data:raise RuntimeError('引导传输中断。')
                target.write(data);h.update(data);left-=len(data)
            if source.read(1):raise RuntimeError('引导材料超过目标容量。')
        if h.hexdigest()!=request['sha256']:raise RuntimeError('引导传输校验失败。')
        with path.open('rb') as source:
            if source.read(8)!=b'ANDROID!':raise RuntimeError('不是 Android boot 镜像。')
        if hardware.digest(p)!=request['previous_sha256']:raise RuntimeError('传输期间旧引导已变化。')
        with hardware.writable(p,{'boot_a'}) as fd,path.open('rb') as source:
            offset=0
            while data:=source.read(4*1024**2):hardware.write(fd,offset,data);offset+=len(data)
        hardware.sealed(list(hardware.inventory().values()))
        if hardware.digest(p)!=request['sha256']:raise RuntimeError('boot_a 完整回读校验失败；保持 Rec。')
    return dict(boot_written=True,boot_readback_verified=True,sha256=request['sha256'],active_slot_changed=False)


def _system_generation(system):
    from liuqin_multiboot import identifier
    generation = identifier(system['target']['boot_generation'] if system['type'] == 'linux' else system['generation'])
    if system['image'] != f"systems/{system['id']}/images/{generation}.img":
        raise RuntimeError('系统引导镜像与登记的版本不一致。')
    return generation


def _padded_image(path, expected, capacity, destination=None):
    """Check the stored image and derive exactly the bytes for the boot partition."""
    size = path.stat().st_size
    if not 8 <= size <= capacity:
        raise RuntimeError('系统引导镜像尺寸不符合 boot_a。')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        if source.read(8) != b'ANDROID!':
            raise RuntimeError('系统引导镜像头无效。')
        source.seek(0)
        remaining = size
        while remaining:
            data = source.read(min(remaining, CHUNK))
            if not data:
                raise RuntimeError('系统引导镜像被截断。')
            digest.update(data)
            if destination is not None:
                destination.write(data)
            remaining -= len(data)
        if source.read(1) or digest.hexdigest() != expected:
            raise RuntimeError('系统引导镜像校验失败。')
    if destination is not None:
        destination.truncate(capacity)
    zeroes = bytes(CHUNK)
    for offset in range(size, capacity, CHUNK):
        digest.update(zeroes[:min(CHUNK, capacity-offset)])
    return digest.hexdigest()


def _android_switch_evidence(agent, root, system, generation, state):
    from liuqin_android import ANDROID_PARTITIONS, load_profile, write_partitions
    from liuqin_ota import FIRMWARE
    from liuqin_rom_admission import catalog

    inventory = state['inventory']
    if (system.get('slot') != 'a' or state.get('userdata_blank') or state.get('userdata_linux_signature') or
            state.get('pending_transactions')):
        raise RuntimeError('Android 数据分区或安装事务状态不适合切换引导。')
    for name in ('userdata', 'metadata'):
        if name not in inventory or gate.identity(system[name]) != gate.identity(inventory[name]):
            raise RuntimeError('Android 数据分区身份已变化：'+name)

    journal = root/'transactions'/('android-'+generation)
    if not all((journal/name).is_file() for name in ('journal.json', 'rom-installed.json', 'complete.json')):
        raise RuntimeError('Android 缺少完整重装记录，无法核验现有 ROM。')
    record = agent.read_json(journal/'journal.json')
    installed = agent.read_json(journal/'rom-installed.json')
    completion = agent.read_json(journal/'complete.json')
    images = installed.get('images')
    if (record.get('mode') != 'reinstall' or record.get('transaction') != generation or
            record.get('rom') != system.get('rom') or installed.get('transaction') != generation or
            completion.get('status') != 'complete' or
            not re.fullmatch('[a-f0-9]{64}', completion.get('registry_sha256', '')) or
            not re.fullmatch('[a-f0-9]{64}', completion.get('boot_sha256', '')) or
            not isinstance(images, dict) or record.get('targets') != sorted(images) or
            not ANDROID_PARTITIONS <= images.keys() or
            images.keys() - (ANDROID_PARTITIONS | {name+'_a' for name in FIRMWARE} | {'recovery_a'})):
        raise RuntimeError('Android ROM 的完成记录无效。')
    for name in ('userdata', 'metadata'):
        if gate.identity(record[name]) != gate.identity(inventory[name]):
            raise RuntimeError('Android 数据分区与安装事务不匹配：'+name)

    shared = []
    if re.fullmatch(r'OTA-[0-9a-f]{16}', system.get('rom', '')):
        version = record.get('generic_ota_version')
        firmware_names = {name+'_a' for name in FIRMWARE}
        if version is None:
            # Generic v1 wrote only the core system images. Older complete
            # v2 records can be identified by their additional firmware writes.
            if not images.keys() & firmware_names:
                raise RuntimeError('旧 Android 安装记录缺少底层固件核验依据，请重新登记匹配的完整安装记录。')
        else:
            if type(version) is not int or version not in (1,2):
                raise RuntimeError('Android 安装记录的执行器版本无效。')
            shared = record.get('shared_firmware')
            if not isinstance(shared,list) or len(shared)>len(FIRMWARE):
                raise RuntimeError('Android 配套固件记录无效。')
            seen = set()
            for entry in shared:
                if (not isinstance(entry,dict) or set(entry)!={'partition','bytes','sha256'} or
                        not isinstance(entry['partition'],str) or
                        entry['partition'] not in firmware_names or entry['partition'] in seen or
                        type(entry['bytes']) is not int or not 0<entry['bytes']<=512*1024**2 or
                        not isinstance(entry['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',entry['sha256'])):
                    raise RuntimeError('Android 配套固件记录无效。')
                seen.add(entry['partition'])
            if version==1:
                from liuqin_ota_v1 import ESSENTIAL
                if not {name+'_a' for name in ESSENTIAL}<=seen:
                    raise RuntimeError('旧 Android 安装记录缺少必要的底层固件核验依据。')
    else:
        profiles = [load_profile(entry['id']) for entry in catalog()]
        matches = [p for p in profiles if p['rom']==system['rom'] and p['images']['boot.img']['sha256']==system['boot_sha256']]
        if len(matches) != 1 or set(images) != write_partitions(matches[0]):
            raise RuntimeError('Android 版本与已知安装记录不匹配。')
        profile = matches[0]
        shared = [profile['images'][name] for name in profile['shared_firmware']]
    return images, shared


def switch_state(hardware, agent):
    from liuqin_partitions import discover

    state = agent.PartitionOperations(hardware).inspect()
    if state.get('error'):
        raise RuntimeError('读取分区或引导存储失败：'+state['error'])
    if state.get('registry') is not None and not discover(state, hardware.serial)['can_create']:
        raise RuntimeError('无法核对当前设备的多系统分区与引导存储。')
    return state


def require_store_ready(hardware):
    """Resume a partially collected history before another registry mutation."""
    store=hardware.inventory().get('linux_boot')
    if store:
        with hardware.mount_store(store) as root:
            if (root/'.retention/active.json').exists():
                raise RuntimeError('上次引导清理尚未结束，请先在引导存储管理中继续清理，再安装或切换系统。')


def _local_fixed_recovery(hardware, part, observed):
    """Build-bound evidence shared by tablet and computer switch requests."""
    from liuqin_recovery_preserve import CAPACITY

    manifest = gate.marker() or {}
    if (not gate.capable(manifest) or manifest.get('tablet_boot_switch_version') != 1
            or part['bytes'] != CAPACITY):
        return None
    if manifest['usage'] == gate.FIXED_USAGE:
        from liuqin_universal_recovery import inspect_bytes

        checked = inspect_bytes(hardware.read(part, 0, CAPACITY))
        if checked['manifest'] == manifest and checked['image_sha256'] == observed:
            return observed
    else:
        witness = manifest.get('fixed_recovery_witness') or {}
        if (witness.get('serial') == hardware.serial and witness.get('partition') == 'recovery_a'
                and witness.get('image_sha256') == observed):
            return observed
    return None


def _prepare_switch(request, hardware, agent, *, local=False):
    from liuqin_multiboot import identifier
    from liuqin_partitions import snapshot_id

    system_id = identifier(request.get('system_id'))
    transaction = identifier(request.get('transaction'))
    state = switch_state(hardware, agent)
    if not state.get('registry'):
        raise RuntimeError('无法核对当前设备的多系统分区与引导存储。')
    registry = state['registry']
    target = next((item for item in registry['systems'] if item['id']==system_id),None)
    if target is None:
        raise RuntimeError('目标系统尚未安装或登记。')
    previous = next((item for item in registry['systems'] if item['id']==registry.get('default_id')),None)
    if local and previous is None:
        raise RuntimeError('请先在电脑安装器中登记当前默认系统，才能在平板切换。')
    if previous == target:
        raise RuntimeError('该系统已经是默认引导。')
    generation = _system_generation(target)
    parts = state['inventory']
    boot, store = parts['boot_a'], parts['linux_boot']
    if boot['bytes'] != BOOT_CAPACITY:
        raise RuntimeError('当前设备的 boot_a 容量不符合要求。')
    from liuqin_android import BOOT_STORE_CHUNK, sparse_boot_bytes
    chunks=(hardware.read(boot,offset,min(BOOT_STORE_CHUNK,boot['bytes']-offset))
            for offset in range(0,boot['bytes'],BOOT_STORE_CHUNK))
    if state['free_bytes'] < sparse_boot_bytes(chunks)+16*1024**2:
        raise RuntimeError('引导存储空间不足，无法保存本次切换备份。')
    if shutil.disk_usage(hardware.work).free < 2*boot['bytes']+16*1024**2:
        raise RuntimeError('RAM 空间不足，无法安全暂存本次切换镜像。')

    images, shared = {}, []
    with hardware.mount_store(store) as root:
        agent.preserve_files(root,state['files'])
        for journal in (root/'transactions').glob('switch-*'):
            if not ((journal/'complete.json').is_file() or (journal/'rolled-back.json').is_file()):
                raise RuntimeError('存在未结束的引导切换记录，保持 Recovery 以便检查。')
        if (root/'transactions'/('switch-'+transaction)).exists():
            raise RuntimeError('该切换计划已经执行。')
        target_sha = _padded_image(root/target['image'],target['boot_sha256'],boot['bytes'])
        previous_sha = hardware.digest(boot)
        if previous:
            if (registry['default_generation']!=_system_generation(previous) or
                    previous_sha!=_padded_image(root/previous['image'],previous['boot_sha256'],boot['bytes'])):
                raise RuntimeError('当前 boot_a 与登记的默认系统不匹配，先核对引导状态。')
        elif registry.get('default_generation') is not None:
            raise RuntimeError('当前默认引导登记不完整。')
        if target['type']=='linux':
            journal = root/'transactions'/('linux-'+generation)
            installed = journal/'root-installed.json'
            if installed.is_file():
                complete=agent.read_json(journal/'complete.json')
                if (not re.fullmatch('[a-f0-9]{64}',complete.get('registry_sha256','')) or
                        not re.fullmatch('[a-f0-9]{64}',complete.get('boot_sha256',''))):
                    raise RuntimeError('Linux 安装登记尚未提交。')
            else:
                # The legacy first-install path stored per-system manifests.
                installed = root/'systems'/system_id/'manifest.json'
            if agent.read_json(installed)!=target:
                raise RuntimeError('Linux 安装记录与登记不一致。')
        else:
            images, shared = _android_switch_evidence(agent,root,target,generation,state)

    if target['type']=='linux':
        hardware.verify_linux(parts[target['partition']['name']],target)
    fixed_recovery=None if local else request.get('fixed_recovery_sha256')
    if fixed_recovery is not None:
        marker=gate.marker() or {}
        if (not isinstance(fixed_recovery,str) or not re.fullmatch('[a-f0-9]{64}',fixed_recovery) or
                not gate.capable(marker)):
            raise RuntimeError('固定 Recovery 的切换校验依据无效。')
        if marker.get('usage')!=gate.FIXED_USAGE:
            # A GUI apply replays the witness resolved during preview. It is
            # a hint, not authority: remeasure and reauthenticate it here.
            recovery=parts.get('recovery_a')
            if recovery is None:
                raise RuntimeError('固定 Recovery 的切换校验依据无效。')
            observed=hardware.digest(recovery)
            if (fixed_recovery!=observed or
                    _local_fixed_recovery(hardware,recovery,observed)!=fixed_recovery):
                raise RuntimeError('固定 Recovery 的切换校验依据无效。')
    for name, digest in images.items():
        if name not in parts or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
            raise RuntimeError('Android ROM 内容与安装记录不一致：'+name)
        observed = hardware.digest(parts[name])
        if name == 'recovery_a' and fixed_recovery is None and observed != digest:
            fixed_recovery = _local_fixed_recovery(hardware, parts[name], observed)
        if observed != digest and not (name == 'recovery_a' and observed == fixed_recovery):
            raise RuntimeError('Android ROM 内容与安装记录不一致：'+name)
    for entry in shared:
        name=entry['partition']
        if name not in parts or hardware.digest(parts[name],0,entry['bytes'])!=entry['sha256']:
            raise RuntimeError('Android 配套固件已变化：'+name)

    plan = dict(schema=1, transaction=transaction, system_id=system_id,
                boot_id=request['boot_id'], serial=hardware.serial,
                target_name=target['name'], previous_name=previous['name'] if previous else '未登记的引导',
                previous_boot_sha256=previous_sha, target_boot_sha256=target_sha,
                parts=[gate.identity(boot),gate.identity(store)], layout_sha256=snapshot_id(state))
    if local:
        plan.update(target_partition=_system_partition(target),
                    previous_partition=_system_partition(previous))
    if fixed_recovery is not None:plan['fixed_recovery_sha256']=fixed_recovery
    return plan, state, target, generation


def _system_partition(system):
    return system['partition']['name'] if system['type'] == 'linux' else 'userdata'


def preview_switch(request, hardware, agent, probe, *, local=False):
    if (request.get('serial') != probe['serial'] or request.get('boot_id') != probe['boot_id'] or
            not probe.get('sealed') or probe.get('phase') in ('installing','write-failed')):
        raise RuntimeError('设备会话或存储保护状态已变化。')
    return dict(plan=_prepare_switch(request,hardware,agent,local=local)[0])


def _fsync_dir(path):
    fd = os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_switch_boot(hardware, parts, path, expected):
    part = parts['boot_a']
    hardware.sealed(list(parts.values()))
    with hardware.writable(part,{'boot_a'}) as fd, path.open('rb') as source:
        for offset in range(0,part['bytes'],CHUNK):
            size=min(CHUNK,part['bytes']-offset)
            data=source.read(size)
            if len(data)!=size:
                raise RuntimeError('暂存引导镜像读取不完整。')
            hardware.write(fd,offset,data)
    hardware.sealed(list(parts.values()))
    if hardware.digest(part)!=expected:
        raise RuntimeError('切换引导的 boot_a 回读校验失败。')


def apply_switch(request, hardware, agent, probe, *, local=False):
    from liuqin_partitions import stable_partition

    lease = gate.require(request.get('token',''))
    plan = request['plan']
    scope = lease['scope']
    if (scope.get('kind')!='switch' or scope.get('switch_plan_sha256')!=gate.digest(plan) or
            scope.get('parts')!=plan['parts'] or scope.get('previous_boot_sha256')!=plan['previous_boot_sha256'] or
            plan['serial']!=probe['serial'] or plan['boot_id']!=probe['boot_id'] or lease.get('write_started')):
        raise RuntimeError('默认引导切换不匹配本次确认或已开始执行。')
    confirmed,state,target,generation = _prepare_switch(plan,hardware,agent,local=local)
    if plan!=confirmed:
        raise RuntimeError('确认后默认引导或系统登记已变化。')
    os.environ[gate.TOKEN] = lease['token']
    parts = state['inventory']
    boot, store = parts['boot_a'], parts['linux_boot']
    journal_name = 'switch-'+plan['transaction']
    candidate_registry = gate.encoded(dict(state['registry'],default_id=target['id'],default_generation=generation))

    with tempfile.TemporaryDirectory(prefix='switch-boot-',dir=hardware.work) as staging:
        candidate, backup = Path(staging)/'candidate.img', Path(staging)/'previous.img'
        with hardware.mount_store(store) as root, candidate.open('xb') as output:
            old_registry = (root/'registry.json').read_bytes()
            old_sums = (root/'SHA256SUMS').read_bytes()
            new_sums = b''.join(line for line in old_sums.splitlines(keepends=True) if not line.rstrip(b'\r\n').endswith(b'  registry.json'))
            if _padded_image(root/target['image'],target['boot_sha256'],boot['bytes'],output)!=plan['target_boot_sha256']:
                raise RuntimeError('目标引导在准备切换期间发生变化。')
        with backup.open('xb') as output:
            for offset in range(0,boot['bytes'],CHUNK):
                output.write(hardware.read(boot,offset,min(CHUNK,boot['bytes']-offset)))
        if agent.sha(backup)!=plan['previous_boot_sha256']:
            raise RuntimeError('当前引导在备份期间发生变化。')
        with hardware.mount_store(store,True) as root:
            agent.preserve_files(root,state['files'])
            journal = root/'transactions'/journal_name
            journal.mkdir(mode=0o700,parents=True)
            from liuqin_android import BOOT_STORE_CHUNK
            with backup.open('rb') as source:
                agent.write_sparse_boot(journal/'previous-boot.img',
                    iter(lambda:source.read(BOOT_STORE_CHUNK),b''),boot['bytes'],plan['previous_boot_sha256'])
            agent.atomic(journal/'previous-registry.json',old_registry)
            if old_sums!=new_sums:
                agent.atomic(journal/'previous-SHA256SUMS',old_sums)
            agent.atomic(journal/'plan.json',gate.encoded(plan))
            _fsync_dir(journal.parent)
            _fsync_dir(root)

        if hardware.digest(boot)!=plan['previous_boot_sha256']:
            raise RuntimeError('切换写入前 boot_a 已变化。')
        try:
            _write_switch_boot(hardware,parts,candidate,plan['target_boot_sha256'])
            with hardware.mount_store(store,True) as root:
                agent.preserve_files(root,state['files'])
                if old_sums!=new_sums:
                    agent.atomic(root/'SHA256SUMS',new_sums)
                agent.atomic(root/'registry.json',candidate_registry)
                if (root/'registry.json').read_bytes()!=candidate_registry:
                    raise RuntimeError('默认系统登记回读校验失败。')
                agent.atomic(root/'transactions'/journal_name/'complete.json',gate.encoded(dict(
                    default_id=target['id'],boot_sha256=plan['target_boot_sha256'],
                    registry_sha256=hashlib.sha256(candidate_registry).hexdigest())))
                agent.preserve_files(root,state['files'],mutable=('registry.json','SHA256SUMS'))
                # Host switches have a durable host backup. Local switches
                # require a verified old image already recorded in linux_boot.
                # previous-registry.json locates it after the commit.
                journal = root/'transactions'/journal_name
                (journal/'previous-boot.img').unlink()
                _fsync_dir(journal)
            hardware.sealed(list(parts.values()))
        except Exception as error:
            try:
                current=hardware.inventory()
                if ({n:stable_partition(p) for n,p in current.items()} !=
                        {n:stable_partition(p) for n,p in parts.items()} or
                        agent.sha(backup)!=plan['previous_boot_sha256']):
                    raise RuntimeError('原分区身份或旧引导快照发生变化。')
                hardware.sealed(list(current.values()))
                with hardware.mount_store(store) as root:
                    if ((root/'registry.json').read_bytes() not in (old_registry,candidate_registry) or
                            (root/'SHA256SUMS').read_bytes() not in (old_sums,new_sums)):
                        raise RuntimeError('引导存储出现未知状态。')
                    agent.preserve_files(root,state['files'],mutable=('registry.json','SHA256SUMS'))
                if hardware.digest(boot)!=plan['previous_boot_sha256']:
                    _write_switch_boot(hardware,parts,backup,plan['previous_boot_sha256'])
                with hardware.mount_store(store,True) as root:
                    if (root/'SHA256SUMS').read_bytes()!=old_sums:
                        agent.atomic(root/'SHA256SUMS',old_sums)
                    if (root/'registry.json').read_bytes()!=old_registry:
                        agent.atomic(root/'registry.json',old_registry)
                    agent.preserve_files(root,state['files'])
                    journal = root/'transactions'/journal_name
                    if (journal/'complete.json').exists():
                        (journal/'complete.json').unlink()
                    agent.atomic(journal/'rolled-back.json',gate.encoded(dict(boot_sha256=plan['previous_boot_sha256'])))
                hardware.sealed(list(parts.values()))
            except Exception as rollback_error:
                raise RuntimeError(f'切换引导失败，请保留 Recovery 和本次切换备份。{error}；恢复检查失败：{rollback_error}') from error
            raise RuntimeError(f'切换引导失败，旧 boot_a 和默认登记已恢复。请保留 Recovery 检查记录。{error}') from error
    return dict(boot_written=True,boot_readback_verified=True,default_id=target['id'])


def main():
    if os.geteuid()!=0 or os.uname().machine!='aarch64' or len(sys.argv)!=4:raise RuntimeError('仅支持原生 ARM64 Rec。')
    action,path,expected=sys.argv[1:];path=Path(path)
    if not str(path).startswith('/run/liuqin-native-host/') or '..' in path.parts or not re.fullmatch('[a-f0-9]{64}',expected):raise RuntimeError('安装控制请求必须在 RAM 中。')
    request=gate.load(path)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise RuntimeError('安装控制请求校验失败。')
    status=module('native_status',Path(__file__).with_name('native-status.py'))
    fd=os.open('/run/liuqin-android-operation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+b') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):raise RuntimeError('操作锁无效。')
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);probe=status.collect()
        if action=='begin':
            requested_scope=request.get('scope',{})
            if (requested_scope.get('kind')!='maintenance' and
                    any(p.get('name')=='linux_boot' for p in requested_scope.get('parts',[]))):
                agent=module('android_device',Path(__file__).with_name('android-device.py'))
                require_store_ready(agent.Hardware(probe['boot_id'],probe['serial']))
            profile=request.get('android_profile',{})
            if profile.get('generic_ota_version'):
                from liuqin_ota import require_device
                hardware=module('android_device',Path(__file__).with_name('android-device.py')).Hardware(probe['boot_id'],probe['serial'])
                require_device(profile,hardware,check_data=not bool(request.get('scope',{}).get('gpt_sha256')))
                if request.get('scope',{}).get('full_rom'):
                    gate.validate_scope(request['scope'])
                    for name,backup in request['scope']['rom_backups'].items():
                        part=hardware.inventory()[name]
                        if gate.identity(part)!=backup['identity'] or hardware.digest(part)!=backup['sha256']:raise RuntimeError('确认后分区或备份发生变化：'+name)
                    request['rom_backups_verified']=True
            fixed=request.get('scope',{}).get('fixed_recovery')
            if fixed:
                if profile.get('generic_ota_version')!=2 or gate.marker().get('fixed_recovery_restore_version')!=1:
                    raise RuntimeError('固定 Rec 保护需要新版完整 OTA 执行器。')
                from liuqin_recovery_preserve import validate,check_chain
                validate(fixed)
                check_chain(hardware,fixed,'before')
                if hardware.digest(hardware.inventory()['recovery_a'])!=fixed['previous_sha256']:
                    raise RuntimeError('确认后 Recovery 备份已不匹配。')
                request['fixed_recovery_backup_verified']=True
            result=gate.begin(request,probe)
        elif action=='finish':
            if request.get('success') is True:
                lease=gate.require(request.get('token',''))
                if lease['scope'].get('fixed_recovery'):
                    os.environ[gate.TOKEN]=lease['token']
                    from liuqin_recovery_preserve import verify
                    hardware=module('android_device',Path(__file__).with_name('android-device.py')).Hardware(probe['boot_id'],probe['serial'])
                    verify(hardware)
            retention=None
            if request.get('success') is True:
                agent=module('android_device',Path(__file__).with_name('android-device.py'))
                hardware=agent.Hardware(probe['boot_id'],probe['serial'])
                backend=module('native_store',Path(__file__).with_name('native-store.py'))
                retention=backend.auto_cleanup(request,hardware,agent,sys.modules[__name__],probe)
            result=gate.finish(request,probe)
            if retention is not None:result['retention']=retention
            if result['state']=='complete':
                lease=gate.session()
                if lease.get('scope',{}).get('fixed_recovery'):
                    from liuqin_recovery_preserve import directory
                    shutil.rmtree(directory(lease),ignore_errors=True)
        elif action=='recovery-stage':
            from liuqin_recovery_preserve import stage
            hardware=module('android_device',Path(__file__).with_name('android-device.py')).Hardware(probe['boot_id'],probe['serial'])
            result=stage(request,hardware)
        elif action=='record-fastboot':
            m=gate.marker()
            if (not gate.capable(m) or not probe['sealed'] or not probe['unlocked'] or probe['slot']!='a' or probe['device']!='liuqin' or
                any(request.get(k)!=probe[k] for k in ('boot_id','serial','build_id')) or type(request.get('anti_version')) is not int or not 0<=request['anti_version']<10**8):raise RuntimeError('Fastboot 观测与当前会话不匹配。')
            p=Path('/run/liuqin-native-fastboot.json');fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as f:f.write(gate.encoded(request))
            result=dict(recorded=True)
        elif action in ('switch-preview','switch-apply'):
            m=gate.marker()
            if (not m or not gate.capable(m) or m.get('boot_switch_version')!=1 or
                    not probe.get('sealed') or probe.get('slot')!='a' or probe.get('unlocked') is not True or
                    probe.get('device')!='liuqin' or probe.get('build_id')!=m['build_id']):
                raise RuntimeError('当前原生 Recovery 不具备切换默认引导的条件。')
            agent=module('android_device',Path(__file__).with_name('android-device.py'))
            hardware=agent.Hardware(probe['boot_id'],probe['serial'])
            operation=preview_switch if action=='switch-preview' else apply_switch
            result=operation(request,hardware,agent,probe)
        elif action in ('store-inspect','store-preview','store-apply'):
            m=gate.marker()
            if (not m or not gate.capable(m) or m.get('boot_store_version')!=1 or
                    not probe.get('sealed') or probe.get('slot')!='a' or probe.get('unlocked') is not True or
                    probe.get('device')!='liuqin' or probe.get('build_id')!=m['build_id']):
                raise RuntimeError('当前 Rec 不支持引导存储管理，请临时启动新版 Rec。')
            agent=module('android_device',Path(__file__).with_name('android-device.py'))
            hardware=agent.Hardware(probe['boot_id'],probe['serial'])
            backend=module('native_store',Path(__file__).with_name('native-store.py'))
            native=sys.modules.get(__name__)
            if native is None:raise RuntimeError('缺少引导维护执行器。')
            if action=='store-inspect':result=backend.inspect(hardware,agent,native,probe)
            else:result=(backend.preview if action=='store-preview' else backend.apply)(request,hardware,agent,native,probe)
        elif action=='boot-write':
            s=gate.require(request.get('token',''));hardware=module('android_device',Path(__file__).with_name('android-device.py')).Hardware(probe['boot_id'],probe['serial'])
            result=write_boot(request,hardware)
        else:raise ValueError('未知原生安装控制操作。')
    print(json.dumps(result),flush=True)
if __name__=='__main__':
    gate.signal_handlers()
    try:main()
    except BaseException:gate.interrupted();raise
