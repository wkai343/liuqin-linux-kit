#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Inspect persistent transactions; reconcile commits and retire duplicate backups.

The caller owns the operation lock. Every write also needs a confirmed, boot-bound
maintenance lease. No GPT, rootfs, ROM or firmware write is performed here.
"""
import copy
import hashlib
import os
from pathlib import Path
import re
import tempfile

import liuqin_native_install as gate
from liuqin_multiboot import identifier
from liuqin_partitions import snapshot_id

MARGIN = 4 * 1024**2
TRANSACTION = re.compile(r'(android|linux|switch|edit|partition)-([0-9a-f-]{36})\Z')
IMAGE = re.compile(r'systems/([0-9a-f-]{36})/images/([0-9a-f-]{36})\.img\Z')


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def image_path(value):
    match = IMAGE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise RuntimeError('引导镜像路径不符合系统登记。')
    identifier(match[1]); identifier(match[2])
    return value


def _terminal(agent, journal):
    if (journal/'rolled-back.json').is_file():
        value = agent.read_json(journal/'rolled-back.json')
        if re.fullmatch('[a-f0-9]{64}', value.get('boot_sha256', '')):
            return 'rolled-back'
    if (journal/'complete.json').is_file():
        value = agent.read_json(journal/'complete.json')
        if (re.fullmatch('[a-f0-9]{64}', value.get('registry_sha256', '')) and
                re.fullmatch('[a-f0-9]{64}', value.get('boot_sha256', ''))):
            return 'complete'
    return 'pending'


def _transactions(root, agent):
    rows = []
    for journal in sorted((root/'transactions').glob('*')):
        if journal.is_symlink() or not journal.is_dir():
            raise RuntimeError('事务目录包含链接或未知文件。')
        match = TRANSACTION.fullmatch(journal.name)
        if not match:
            try:
                identifier(journal.name)
            except (TypeError,ValueError):
                raise RuntimeError('引导存储存在未知事务目录，先导出日志检查。')
            rows.append(dict(name=journal.name,kind='legacy',id=journal.name,status='record'))
            continue
        identifier(match[2])
        status = _terminal(agent, journal)
        # Partition transactions have different terminal receipts. They are
        # listed, but are never assumed to be installation/boot transactions.
        if match[1] in ('edit', 'partition'):
            status = 'record'
        rows.append(dict(name=journal.name, kind=match[1], id=match[2], status=status))
    return rows


def _classify_superseded(root, rows, state, hardware, agent, native):
    """Recognize failed attempts replaced by a verified later installation.

    Nothing is deleted or marked resolved on disk. Incomplete historical
    Linux attempts otherwise make every later recovery appear ambiguous.
    """
    registry = state['registry']
    verified = set()
    fields = ('name','partuuid','start','bytes')
    deleted = set()
    for edit_row in rows:
        if edit_row['kind'] != 'edit':
            continue
        try:
            journal = root/'transactions'/edit_row['name']
            if agent.read_json(journal/'complete.json').get('status') != 'complete':
                continue
            edit = agent.read_json(journal/'plan.json')
            if edit.get('transaction') != edit_row['id']:
                continue
            from liuqin_partition_editor import validate_edit
            validate_edit(edit,hardware.serial)
            for part in edit['deletes']:
                deleted.add(tuple(part[k] for k in fields))
        except (OSError,ValueError,KeyError,RuntimeError):
            continue
    for row in rows:
        if row['status'] != 'pending':
            continue
        if row['kind']=='android' and row['id'] in registry.get('resolved_android_transactions',[]):
            current = next((s for s in registry['systems'] if s.get('type')=='android'),None)
            if (current and current.get('generation') != row['id'] and
                    _terminal(agent,root/'transactions'/('android-'+current['generation'])) == 'complete'):
                row['status']='superseded'
            continue
        if row['kind'] != 'linux':
            continue
        try:
            old = agent.read_json(root/'transactions'/row['name']/'journal.json')
            original = old['system']
            if old.get('transaction') != row['id'] or original['target']['boot_generation'] != row['id']:
                continue
            # Planned root records intentionally omit transient device/parent
            # handles. Compare the persistent GPT fields, not an ioctl target.
            old_part = tuple(original['partition'][k] for k in fields)
            live_ids = {p['partuuid'] for p in state['inventory'].values()}
            registered_ids = {s['id'] for s in registry['systems']} | {s['id'] for s in registry.get('rootfs_slots',[])}
            if (old_part in deleted and original['id'] not in registered_ids and old_part[1] not in live_ids):
                row.update(status='superseded',message='原目标分区已被后续已完成的分区操作明确删除；保留旧失败记录。')
                continue
            current = next(s for s in registry['systems'] if s['id']==original['id'] and s['type']=='linux')
            generation = native._system_generation(current)
            if (generation == row['id'] or
                    tuple(current['partition'][k] for k in fields) != old_part or
                    tuple(state['inventory'][current['partition']['name']][k] for k in fields) != old_part):
                continue
            journal = root/'transactions'/('linux-'+generation)
            if _terminal(agent,journal) != 'complete' or agent.read_json(journal/'root-installed.json') != current:
                continue
            if current['id'] not in verified:
                hardware.verify_linux(state['inventory'][current['partition']['name']],current)
                verified.add(current['id'])
            row['status']='superseded'
        except (OSError,ValueError,KeyError,StopIteration,RuntimeError):
            continue


def _sums_without_registry(data):
    return b''.join(line for line in data.splitlines(keepends=True)
                    if not line.rstrip(b'\r\n').endswith(b'  registry.json'))


def _validate_default(native, hardware, agent, root, registry, state, fixed_sha=None):
    system = next((s for s in registry['systems'] if s['id'] == registry.get('default_id')), None)
    if system is None or registry.get('default_generation') != native._system_generation(system):
        raise RuntimeError('当前默认系统登记不完整。')
    path = root/image_path(system['image'])
    digest = native._padded_image(path, system['boot_sha256'], state['inventory']['boot_a']['bytes'])
    if system['type'] == 'linux':
        hardware.verify_linux(state['inventory'][system['partition']['name']], system)
    elif system['type'] == 'android':
        checked_state = dict(state, registry=registry)
        # Terminal Android journals are not pending, regardless of old indexes.
        images, shared = native._android_switch_evidence(agent, root, system,
                                                         native._system_generation(system), checked_state)
        for name, expected in images.items():
            observed = hardware.digest(state['inventory'][name])
            # An Android installation can predate a separately qualified
            # fixed Recovery. A Linux-target switch has no recovery override
            # in its original plan, so consult this build's exact-device
            # witness just as the tablet switch path does. A supplied older
            # override still has to match, and all other ROM hashes remain
            # mandatory.
            override = (name == 'recovery_a' and observed != expected and
                        (fixed_sha is None or observed == fixed_sha) and
                        native._local_fixed_recovery(hardware, state['inventory'][name], observed) == observed)
            if observed != expected and not override:
                raise RuntimeError('原默认 Android 的 ROM 内容已变化：'+name)
        for entry in shared:
            if hardware.digest(state['inventory'][entry['partition']], 0, entry['bytes']) != entry['sha256']:
                raise RuntimeError('原默认 Android 的共享固件已变化。')
    else:
        raise RuntimeError('默认系统类型无效。')
    return digest


def _switch_recovery(root, row, state, observed, native, hardware, agent, direction):
    journal = root/'transactions'/row['name']
    original = agent.read_json(journal/'plan.json')
    if (original.get('serial') != hardware.serial or original.get('transaction') != row['id'] or
            original.get('parts') != [gate.identity(state['inventory'][n]) for n in ('boot_a', 'linux_boot')]):
        raise RuntimeError('中断引导记录不属于当前设备或分区。')
    old_raw = (journal/'previous-registry.json').read_bytes()
    old = agent.read_json(journal/'previous-registry.json')
    target = next((s for s in old['systems'] if s['id'] == original['system_id']), None)
    if not target:
        raise RuntimeError('中断记录缺少目标系统。')
    candidate = dict(old, default_id=target['id'], default_generation=native._system_generation(target))
    candidate_raw = gate.encoded(candidate)
    sums = (root/'SHA256SUMS').read_bytes()
    old_sums = ((journal/'previous-SHA256SUMS').read_bytes()
                if (journal/'previous-SHA256SUMS').is_file() else sums)
    if ((root/'registry.json').read_bytes() not in (old_raw, candidate_raw) or
            sums not in (old_sums, _sums_without_registry(old_sums))):
        raise RuntimeError('中断后系统登记出现未知变化，不能自动恢复。')
    # Reconstruct the exact original snapshot. Later installs, partition edits
    # and changed historical files make this comparison fail.
    source = copy.deepcopy(state)
    source['registry'] = old
    source['files'] = {n:entry for n,entry in state['files'].items()
                       if not n.startswith('transactions/'+row['name']+'/')}
    source['files']['registry.json'] = dict(bytes=len(old_raw), sha256=checksum(old_raw))
    source['files']['SHA256SUMS'] = dict(bytes=len(old_sums), sha256=checksum(old_sums))
    if snapshot_id(source) != original['layout_sha256']:
        raise RuntimeError('中断后分区或引导材料已变化，不能重放旧引导计划。')
    backup = journal/'previous-boot.img'
    if backup.stat().st_size != state['inventory']['boot_a']['bytes'] or agent.sha(backup) != original['previous_boot_sha256']:
        raise RuntimeError('原引导快照不完整或校验失败。')
    expected = _validate_default(native, hardware, agent, root, old, state,
                                 original.get('fixed_recovery_sha256'))
    if expected != original['previous_boot_sha256']:
        raise RuntimeError('原默认系统与回退快照不匹配。')
    target_sha = native._padded_image(root/image_path(target['image']), target['boot_sha256'],
                                      state['inventory']['boot_a']['bytes'])
    if target_sha != original['target_boot_sha256']:
        raise RuntimeError('目标引导与中断计划不匹配。')
    if direction == 'finish-switch':
        if observed != target_sha:
            raise RuntimeError('目标 boot_a 尚未完整写入；请恢复原引导。')
        _validate_default(native, hardware, agent, root, candidate, state,
                          original.get('fixed_recovery_sha256'))
        return dict(registry=candidate_raw, sums=_sums_without_registry(old_sums),
                    boot_sha256=observed, marker='complete.json', boot_source=None)
    return dict(registry=old_raw, sums=old_sums, boot_sha256=expected,
                marker='rolled-back.json', boot_source=str(backup))


def _installation_commit(root, row, state, observed, hardware, agent):
    """Resume only the final registration, after a durable verified OS receipt."""
    journal = root/'transactions'/row['name']
    intent = agent.read_json(journal/'commit-intent.json')
    plan = intent['plan']
    if (intent.get('schema') != 1 or plan.get('transaction') != row['id'] or
            intent.get('serial') != hardware.serial):
        raise RuntimeError('安装提交记录不属于当前设备。')
    old_raw = (journal/'previous-registry.json').read_bytes()
    if checksum(old_raw) != plan['registry_sha256'] or agent.read_json(journal/'previous-registry.json') != plan['registry']:
        raise RuntimeError('原系统登记快照无效。')
    old_sums = (journal/'previous-SHA256SUMS').read_bytes()
    if (dict(bytes=len(old_sums),sha256=checksum(old_sums)) != plan['store_files'].get('SHA256SUMS')):
        raise RuntimeError('原引导校验清单无效。')
    agent.preserve_files(root, plan['store_files'], mutable=('registry.json','SHA256SUMS'))
    from liuqin_partitions import stable_partition
    expected_inventory = copy.deepcopy(plan['inventory'])
    candidate = copy.deepcopy(plan['candidate_registry'])
    if row['kind'] == 'linux':
        from liuqin_managed import validate_linux_plan
        validate_linux_plan(plan, hardware.serial)
        system = plan['system']
        expected_inventory[system['partition']['name']].update(
            filesystem=system['target']['root_fstype'], fsuuid=system['target']['root_fsuuid'])
        if agent.read_json(journal/'root-installed.json') != system:
            raise RuntimeError('Linux 根文件系统尚未完成校验。')
        hardware.verify_linux(state['inventory'][system['partition']['name']], system)
        if observed == plan['previous_boot_sha256'] and (not plan['set_default'] or observed != plan['padded_sha256']):
            candidate.update(default_id=plan['registry'].get('default_id'),
                             default_generation=plan['registry'].get('default_generation'))
        elif not plan['set_default'] or observed != plan['padded_sha256']:
            raise RuntimeError('当前引导不匹配旧引导或已确认的 Linux 引导。')
    elif row['kind'] == 'android':
        profile = intent['profile']
        operation = agent.Operations(hardware, profile)
        operation.validate_plan(plan)
        if plan['mode'] != 'reinstall':
            raise RuntimeError('此旧式 Android 登记不支持自动提交。')
        from liuqin_ota import require_device
        from liuqin_android import validate_images, check_registry_transition
        from liuqin_rom_admission import require_install
        require_install(profile)
        require_device(profile, hardware)
        # Minor device numbers are refreshed by each Recovery kernel epoch.
        # Validate images against the original inventory and then compare the
        # complete stable partition identities with the live inventory below.
        validate_images(plan['images'],profile,plan['inventory'])
        for name in profile['shared_firmware']:
            entry=profile['images'][name]
            if hardware.digest(state['inventory'][entry['partition']],0,entry['bytes']) != entry['sha256']:
                raise RuntimeError('Android 配套共享固件已变化：'+entry['partition'])
        completion = agent.read_json(journal/'rom-installed.json')
        if completion.get('transaction') != row['id']:
            raise RuntimeError('Android 安装回读记录无效。')
        images = completion['images']
        expected_images = {i['partition']['name']:i['whole_sha256'] for i in plan['images']}
        # Fixed Recovery overrides must already be described in the durable
        # intent, never inferred from an arbitrary image found on the device.
        fixed = intent.get('fixed_recovery')
        if fixed:
            from liuqin_recovery_preserve import check_chain, validate
            validate(fixed); check_chain(hardware, fixed, 'after')
            if hardware.digest(state['inventory']['recovery_a']) != fixed['image_sha256']:
                raise RuntimeError('固定 Recovery 安装回读不匹配。')
            expected_images['recovery_a'] = fixed['image_sha256']
        if images != expected_images:
            raise RuntimeError('Android ROM 回读清单不匹配安装计划。')
        for name, digest in images.items():
            if hardware.digest(state['inventory'][name]) != digest:
                raise RuntimeError('Android ROM 内容已变化：'+name)
        for name in ('userdata','metadata'):
            if checksum(hardware.read(state['inventory'][name],0,4096)) != completion[name+'_header']:
                raise RuntimeError('Android 数据初始化状态发生变化。')
        system = next(s for s in candidate['systems'] if s['type'] == 'android')
        check_registry_transition(plan['registry'],candidate,plan['android_id'],plan['set_default'])
        if (system.get('rom') != profile['rom'] or system.get('slot') != 'a' or
                system.get('generation') != row['id'] or
                system.get('boot_sha256') != profile['images']['boot.img']['sha256'] or
                any(gate.identity(system[n]) != gate.identity(state['inventory'][n]) for n in ('userdata','metadata'))):
            raise RuntimeError('Android 候选登记不匹配本次 ROM 或数据分区。')
        if observed == plan['previous_boot_sha256'] and (not plan['set_default'] or observed != system['boot_sha256']):
            candidate['default_id'] = plan['registry'].get('default_id')
            candidate['default_generation'] = (row['id'] if candidate['default_id'] == system['id']
                                               else plan['registry'].get('default_generation'))
        elif not plan['set_default'] or observed != system['boot_sha256']:
            raise RuntimeError('当前引导不匹配 Android 安装计划。')
    else:
        raise RuntimeError('不支持自动提交此类事务。')
    if ({n:stable_partition(p) for n,p in state['inventory'].items()} !=
            {n:stable_partition(p) for n,p in expected_inventory.items()}):
        raise RuntimeError('安装提交前分区身份或文件系统已变化。')
    allowed = {system['image']}
    extra = set(state['files']) - set(plan['store_files']) - allowed
    if any(not n.startswith('transactions/'+row['name']+'/') for n in extra):
        raise RuntimeError('安装中断后出现了其他引导存储写入，不能重放旧登记。')
    image = root/image_path(system['image'])
    if agent.sha(image) != system['boot_sha256']:
        raise RuntimeError('安装的系统引导镜像校验失败。')
    candidate_raw = gate.encoded(candidate)
    if ((root/'registry.json').read_bytes() not in (old_raw,candidate_raw) or
            (root/'SHA256SUMS').read_bytes() not in (old_sums,_sums_without_registry(old_sums))):
        raise RuntimeError('安装后的登记已变化，不能重复提交旧事务。')
    # Staging can have failed before all recovery materials reached storage.
    if agent.sha(journal/'previous-boot.img') != plan['previous_boot_sha256']:
        raise RuntimeError('缺少完整旧引导快照。')
    return dict(registry=candidate_raw, sums=_sums_without_registry(old_sums),
                boot_sha256=observed, marker='complete.json', boot_source=None)


def _duplicates(root, rows, registry, state, native, agent):
    """Retire only terminal snapshots with a verified, persistent equivalent.

    System images and every transaction's JSON evidence remain in place. A
    small reference receipt replaces each redundant previous-boot.img.
    """
    images = []
    for name, entry in state['files'].items():
        if IMAGE.fullmatch(name):
            image_path(name)
            try:
                padded = native._padded_image(root/name, entry['sha256'], state['inventory']['boot_a']['bytes'])
            except (ValueError,RuntimeError):
                continue
            images.append((name,padded))
    candidates = []
    for row in rows:
        if row['status'] not in ('complete','rolled-back'):
            continue
        name = 'transactions/'+row['name']+'/previous-boot.img'
        entry = state['files'].get(name)
        if not entry or entry['bytes'] != state['inventory']['boot_a']['bytes']:
            continue
        source = next((image for image,digest in images if digest == entry['sha256']), None)
        if source:
            candidates.append(dict(path=name, source=source, sha256=entry['sha256'],
                                   allocated_bytes=(root/name).stat().st_blocks*512))
    return candidates


def inspect(hardware, agent, native, probe):
    state = agent.PartitionOperations(hardware).inspect()
    if state.get('error'):
        raise RuntimeError('读取引导存储失败：'+state['error'])
    if not state.get('registry'):
        return dict(available=False, message='当前没有 linux_boot 引导存储。', transactions=[],
                    reclaimable_bytes=0,cleanup_available=False,cleanup_pending=False)
    from liuqin_partitions import discover
    summary = discover(state,hardware.serial)
    if not summary['can_create']:
        raise RuntimeError('无法核对引导存储的设备身份与管理布局：'+summary.get('reason',''))
    hardware.sealed(list(state['inventory'].values()))
    observed = hardware.digest(state['inventory']['boot_a'])
    with hardware.mount_store(state['inventory']['linux_boot']) as root:
        agent.preserve_files(root,state['files'])
        rows = _transactions(root,agent)
        _classify_superseded(root,rows,state,hardware,agent,native)
        pending = [r for r in rows if r['status'] == 'pending']
        duplicates = _duplicates(root,rows,state['registry'],state,native,agent)
        for row in rows:
            row['actions'] = []
            if row['status'] != 'pending':
                continue
            try:
                others = [r for r in pending if r != row]
                if others:
                    raise RuntimeError('还有其他未结束的安装或切换记录，先核对完整执行报告。')
                if row['kind'] == 'switch':
                    _switch_recovery(root,row,state,observed,native,hardware,agent,'restore-switch')
                    row['actions'].append(dict(operation='restore-switch',label='恢复原默认引导'))
                    try:
                        _switch_recovery(root,row,state,observed,native,hardware,agent,'finish-switch')
                        row['actions'].append(dict(operation='finish-switch',label='完成目标引导登记'))
                    except (OSError,ValueError,KeyError,RuntimeError):
                        pass
                elif row['kind'] in ('android','linux'):
                    _installation_commit(root,row,state,observed,hardware,agent)
                    row['actions'].append(dict(operation='finish-install',label='完成系统登记'))
                else:
                    continue
                row['message'] = '恢复材料与设备当前状态匹配，可预览操作。'
            except (OSError,ValueError,KeyError,StopIteration,RuntimeError) as error:
                row['message'] = ('安装未完成，保留现场；需要原执行报告进一步检查。'+str(error))
        logical = sum(v['bytes'] for v in state['files'].values())
        allocated = sum((root/name).stat().st_blocks*512 for name in state['files'])
        registry = state['registry']
        current = next((s for s in registry['systems'] if s['id']==registry.get('default_id')),None)
        match = False
        if current:
            try:
                match = native._padded_image(root/image_path(current['image']),current['boot_sha256'],
                                             state['inventory']['boot_a']['bytes']) == observed
            except (OSError,ValueError,KeyError,RuntimeError):
                pass
        retention = None
        retention_error = ''
        cleanup_pending = (root/'.retention/active.json').is_file()
        if (gate.marker() or {}).get('boot_store_retention_version') == 1:
            try:
                if not match:
                    raise RuntimeError('当前默认引导与登记不一致。')
                from liuqin_boot_retention import build
                retention = build(root,state,rows,agent.read_json,native._system_generation,
                                  lambda path,digest:native._padded_image(path,digest,state['inventory']['boot_a']['bytes']))
            except (OSError,ValueError,KeyError,RuntimeError) as error:
                retention_error = str(error)
    return dict(available=True, snapshot_id=snapshot_id(state), boot_sha256=observed,
                serial=hardware.serial, boot_id=probe['boot_id'], total_bytes=state['inventory']['linux_boot']['bytes'],
                free_bytes=state['free_bytes'], logical_bytes=logical, allocated_bytes=allocated,
                default_name=current['name'] if current else '未登记', default_matches=match,
                transactions=rows, pending_count=len(pending), duplicates=duplicates,
                retention=retention,retention_error=retention_error,
                cleanup_pending=cleanup_pending,
                cleanup_available=bool(not pending and match and not retention_error and
                    (retention['units'] if retention is not None else duplicates)),
                reclaimable_bytes=(retention['reclaimable_bytes'] if retention is not None else
                    max(0,sum(r['allocated_bytes'] for r in duplicates)-4096*len(duplicates))))


def preview(request, hardware, agent, native, probe):
    operation = request.get('operation')
    if operation not in ('cleanup','restore-switch','finish-switch','finish-install'):
        raise ValueError('未知引导存储维护操作。')
    report = inspect(hardware,agent,native,probe)
    if (not report['available'] or report['snapshot_id'] != request.get('snapshot_id') or
            request.get('boot_id') != probe['boot_id'] or request.get('serial') != hardware.serial):
        raise RuntimeError('引导存储或启动会话已变化，请重新检查。')
    if operation == 'cleanup':
        if report['pending_count']:
            raise RuntimeError('存在未结束的安装或切换记录，暂不清理恢复材料。')
        if not report['default_matches']:
            raise RuntimeError('当前默认引导与系统登记不一致，先恢复引导状态。')
        if report.get('retention_error'):
            raise RuntimeError(report['retention_error'])
        if report.get('retention') is not None:
            if not report['retention']['units']:
                raise RuntimeError('没有需要回收的过期引导版本。')
        elif not report['duplicates']:
            raise RuntimeError('没有可回收的重复引导快照。')
    else:
        row = next((r for r in report['transactions'] if r['name']==request.get('transaction')),None)
        if not row or operation not in {a['operation'] for a in row['actions']}:
            raise RuntimeError('该中断记录当前不能执行所选恢复操作。')
    state = agent.PartitionOperations(hardware).inspect()
    parts = ['linux_boot'] if operation != 'restore-switch' else ['boot_a','linux_boot']
    plan = dict(schema=1, operation=operation, transaction=request.get('transaction',''),
                serial=hardware.serial, boot_id=probe['boot_id'], snapshot_id=report['snapshot_id'],
                previous_boot_sha256=report['boot_sha256'],
                parts=[gate.identity(state['inventory'][n]) for n in parts],
                duplicates=report['duplicates'] if operation=='cleanup' else [])
    if operation == 'cleanup':
        plan['reclaimable_bytes'] = report['reclaimable_bytes']
        if report.get('retention') is not None:
            plan['retention'] = report['retention']
    return dict(plan=plan)


def apply(request, hardware, agent, native, probe):
    lease = gate.require(request.get('token',''))
    plan = request['plan']
    if (lease['scope'] != gate.scope(dict(kind='maintenance',maintenance_plan=plan)) or
            lease.get('write_started')):
        raise RuntimeError('引导维护不匹配本次确认，或已开始执行。')
    confirmed = preview(dict(plan),hardware,agent,native,probe)['plan']
    if confirmed != plan:
        raise RuntimeError('确认后引导维护方案已变化。')
    os.environ[gate.TOKEN] = lease['token']
    state = agent.PartitionOperations(hardware).inspect()
    store = state['inventory']['linux_boot']
    if state['free_bytes'] < MARGIN:
        raise RuntimeError('引导存储至少需要 4 MiB 空间来保存维护记录。')
    if plan['operation'] == 'cleanup':
        if plan.get('retention') is not None:
            from liuqin_boot_retention import apply as collect
            with hardware.mount_store(store,True) as root:
                result = collect(root,plan['retention'],agent.atomic,agent.read_json)
            hardware.sealed(list(hardware.inventory().values()))
            return dict(result,operation='cleanup')
        mutable = [r['path'] for r in plan['duplicates']]
        with hardware.mount_store(store) as root:
            old_sums = (root/'SHA256SUMS').read_bytes()
            # Drop checksum entries for retired snapshots before unlinking.
            # The equivalent source is authenticated again on each application.
            new_sums = b''.join(line for line in old_sums.splitlines(keepends=True)
                                if not any(line.rstrip(b'\r\n').endswith(b'  '+name.encode()) for name in mutable))
        with hardware.mount_store(store,True) as root:
            agent.preserve_files(root,state['files'])
            for item in plan['duplicates']:
                receipt = dict(schema=1,source=item['source'],sha256=item['sha256'],
                               bytes=state['inventory']['boot_a']['bytes'],zero_padding=True)
                agent.atomic((root/item['path']).with_name('previous-boot-reference.json'),gate.encoded(receipt))
            if old_sums != new_sums:
                agent.atomic(root/'SHA256SUMS',new_sums)
            for item in plan['duplicates']:
                (root/item['path']).unlink()
                native._fsync_dir((root/item['path']).parent)
            agent.preserve_files(root,state['files'],mutable=(*mutable,'SHA256SUMS'))
        hardware.sealed(list(hardware.inventory().values()))
        return dict(completed=True, operation='cleanup', retired_snapshots=len(mutable),
                    reclaimed_bytes=plan['reclaimable_bytes'],boot_written=False)

    with tempfile.TemporaryDirectory(prefix='recover-boot-',dir=hardware.work) as directory:
        staged = Path(directory)/'previous.img'
        with hardware.mount_store(store) as root:
            rows = _transactions(root,agent)
            row = next(r for r in rows if r['name']==plan['transaction'])
            if row['kind'] == 'switch':
                change = _switch_recovery(root,row,state,plan['previous_boot_sha256'],native,hardware,agent,plan['operation'])
            else:
                change = _installation_commit(root,row,state,plan['previous_boot_sha256'],hardware,agent)
            # Keep all recovery materials until registry+boot+terminal receipt
            # reach stable storage. Lost USB replies can be inspected afresh.
            source = change['boot_source']
            if source:
                if os.statvfs(hardware.work).f_bavail*os.statvfs(hardware.work).f_frsize < state['inventory']['boot_a']['bytes']+MARGIN:
                    raise RuntimeError('RAM 空间不足以完整暂存旧引导。')
                with Path(source).open('rb') as inp,staged.open('xb') as out:
                    while data:=inp.read(native.CHUNK):
                        out.write(data)
                if agent.sha(staged) != change['boot_sha256']:
                    raise RuntimeError('回退引导暂存校验失败。')
        # The read-only store mount ends before opening either writer.
        boot_written = bool(source and plan['previous_boot_sha256'] != change['boot_sha256'])
        if boot_written:
            native._write_switch_boot(hardware,state['inventory'],staged,change['boot_sha256'])
    with hardware.mount_store(store,True) as root:
        agent.preserve_files(root,state['files'])
        agent.atomic(root/'SHA256SUMS',change['sums'])
        agent.atomic(root/'registry.json',change['registry'])
        if (hardware.digest(state['inventory']['boot_a']) != change['boot_sha256'] or
                (root/'registry.json').read_bytes() != change['registry']):
            raise RuntimeError('中断恢复回读不一致，保留 Recovery 和恢复材料。')
        terminal = dict(status='complete' if change['marker']=='complete.json' else 'rolled-back',
                        boot_sha256=change['boot_sha256'],registry_sha256=checksum(change['registry']),
                        recovered_by='boot-store-v1')
        agent.atomic(root/'transactions'/row['name']/change['marker'],gate.encoded(terminal))
        agent.preserve_files(root,state['files'],mutable=('registry.json','SHA256SUMS'))
    hardware.sealed(list(hardware.inventory().values()))
    return dict(completed=True,operation=plan['operation'],boot_written=boot_written,
                boot_readback_verified=True,boot_sha256=change['boot_sha256'])


def auto_cleanup(request,hardware,agent,native,probe,session_root=Path('/')):
    """Run once after the whole confirmed workflow committed, before sealing.

    A collection failure is separate from installation success. Durable GC
    evidence is left for a later confirmed cleanup; it never triggers reinstall.
    """
    if not request.get('success') or (gate.marker(session_root) or {}).get('boot_store_retention_version') != 1:
        return dict(status='skipped')
    lease = gate.require(request.get('token',''),session_root)
    scope = lease['scope']
    if (scope['kind'] not in ('linux','android','combined','partitions','switch') or
            not any(p['name']=='linux_boot' for p in scope['parts'])):
        return dict(status='skipped')
    os.environ[gate.TOKEN] = lease['token']
    try:
        report = inspect(hardware,agent,native,probe)
        if not report['available']:
            return dict(status='skipped')
        if report.get('retention_error'):
            return dict(status='deferred',message=report['retention_error'])
        plan = report.get('retention')
        if plan is None or not plan['units']:
            return dict(status='current')
        parts = hardware.inventory()
        if gate.identity(parts['linux_boot']) not in scope['parts']:
            raise RuntimeError('引导存储不在本次已确认的写入范围。')
        from liuqin_boot_retention import apply as collect
        with hardware.mount_store(parts['linux_boot'],True) as root:
            result = collect(root,plan,agent.atomic,agent.read_json)
        hardware.sealed(list(hardware.inventory().values()))
        return dict(result,status='complete')
    except Exception as error:
        hardware.sealed(list(hardware.inventory().values()))
        return dict(status='deferred',message=str(error))
