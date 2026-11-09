# SPDX-License-Identifier: MIT
"""Install one Linux into an existing reserved partition, without GPT writes."""
from __future__ import annotations
import copy
import hashlib
from pathlib import Path
import shutil
import uuid
from .engine import InstallerError, canonical, digest_file, cli
from .multiboot import inspect_system
from liuqin_android import managed_layout
from liuqin_boot_target import bind
from liuqin_managed import next_linux_registry


def connect(engine, payload, operation):
    engine.begin_run(payload, operation)
    if engine.manifest.get('installer_android_version') != 1 or engine.manifest.get('installer_multiboot_version') != 1:
        raise InstallerError('需要支持分步安装的新版 installer.img。')
    engine.android_profile = engine.android_profile_for_run()
    engine.android_profile_sha = hashlib.sha256(canonical(engine.android_profile)).hexdigest()
    disk = engine.connect_ram(require_archive=False)
    scan = engine.transport.android_rpc('inspect',dict(mode='layout'))
    layout = managed_layout(disk,scan['inventory'],scan,engine.device['serial'],require_android=False)
    return scan, layout


def scan_layout(engine, payload):
    engine.guard_operation()
    scan, layout = connect(engine,payload,'scan-layout')
    engine.emit('managed_layout',slots=layout['slots'],has_userdata=layout['userdata'] is not None,
                systems=scan['registry']['systems'],serial=engine.device['serial'])
    engine.transport.cancel_cleanup();engine.clean_scratch();engine.release_device()
    engine.set_state('layout_scanned')


def prepare(engine, payload):
    engine.guard_operation()
    if not payload.get('backup_dir'): raise InstallerError('请选择与当前分区布局相符的备份。')
    if type(payload.get('set_default')) is not bool: raise InstallerError('请选择是否设置 Linux 默认引导。')
    scan, layout = connect(engine,payload,'linux-managed')
    slot = next((s for s in layout['slots'] if s['id'] == payload.get('rootfs_id')),None)
    if not slot or slot['status'] != 'reserved': raise InstallerError('请读取已有分区并选择尚未安装系统的预留 rootfs。')
    record = engine.load_backup(payload['backup_dir'],engine.device['serial'])
    compatibility = engine.verify_current_backup(record)
    generation = payload.get('boot_generation') if getattr(engine,'workflow_committing',False) else None
    generation = generation or str(uuid.uuid4())
    system = dict(id=slot['id'],name=slot['name'],partition=copy.deepcopy(slot['partition']),
        target=dict(slot['target'],boot_generation=generation),filesystem=slot['target']['root_fstype'],
        size_gib=slot['partition']['bytes']//1024**3,size_bytes=slot['partition']['bytes'],bundle='',boot_image='')
    source = inspect_system(system,engine.bundle,engine.check_cancel)
    if source['archive_bytes']+512*1024**2 >= engine.device['available_memory_bytes']:
        raise InstallerError('RAM 空间不足以暂存 Linux 根归档。')
    directory = Path(engine.receipt['run_dir'])/'linux';directory.mkdir(mode=0o700)
    if shutil.disk_usage(directory).free < 4*engine.device['boot_bytes']+512*1024**2:
        raise InstallerError('电脑空间不足以保存绑定引导和当前 boot_a。')
    previous = directory/'previous';previous.mkdir(mode=0o700)
    old_boot = engine.transport.snapshot_boot(previous)
    engine.linux_previous_boot = previous/'boot_a.img'
    if engine.linux_previous_boot.stat().st_size != engine.device['boot_bytes']: raise InstallerError('boot_a 快照尺寸错误。')
    boot = directory/'boot.img';digest = bind(Path(source['boot_image']),boot,system['target'],engine.device['boot_bytes'])
    padded,padded_sha = cli.pad_boot_image(boot,digest,engine.device['boot_bytes'],directory)
    system.update(source=source,boot_image=str(boot),boot_sha256=digest,padded_image=str(padded),padded_sha256=padded_sha)
    metadata = dict(id=system['id'],name=system['name'],type='linux',status='installed-unverified',
        partition=system['partition'],target=system['target'],boot_sha256=digest,source_boot_sha256=source['boot_sha256'],
        kernel_release=source['kernel_release'],image='systems/'+system['id']+'/images/'+generation+'.img')
    candidate = next_linux_registry(scan['registry'],metadata,generation,payload['set_default'])
    needed=boot.stat().st_size+engine.device['boot_bytes']
    if engine.manifest.get('native_recovery_manifest',{}).get('sparse_boot_store_version')==1:
        from liuqin_android import sparse_boot_file_bytes
        needed=sparse_boot_file_bytes(boot)+sparse_boot_file_bytes(engine.linux_previous_boot)
    if scan['free_bytes'] < needed+16*1024**2:
        raise InstallerError('引导存储空间不足，不会删除已有文件。')
    plan = dict(schema_version=1,transaction=generation,set_default=payload['set_default'],system=metadata,
        registry=scan['registry'],registry_sha256=scan['registry_sha256'],store_files=scan['files'],
        inventory=scan['inventory'],candidate_registry=candidate,previous_boot_sha256=old_boot,
        boot_bytes=boot.stat().st_size,padded_sha256=padded_sha)
    snapshot = dict(plan=copy.deepcopy(plan),system=copy.deepcopy(system),report=engine.report_text,
        manifest=engine.manifest_digest,boot_id=engine.transport.boot_id,backup_id=record['backup_id'],
        persist_sha256=compatibility['current_persist_sha256'])
    engine.linux_plan=plan;engine.linux_system=system;engine.plan_snapshot=snapshot
    engine.android_boot=boot # Shared RPC serves only this verified boot image.
    engine.plan_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(plan_id=engine.plan_id,backup_dir=record['path'],backup_id=record['backup_id'],
        backup_verified=True,backup_compatibility=compatibility,boot_action='set-default' if plan['set_default'] else 'install-only',
        rootfs_id=system['id'],device=engine.device)
    engine.save_receipt();engine.check_cancel();engine.set_state('prepared')
    engine.emit('plan_ready',plan=dict(plan_id=engine.plan_id,serial=engine.device['serial'],layout='linux-managed',
        partitions=[system['partition']],systems=[metadata],root_bytes=system['partition']['bytes'],
        filesystem=system['filesystem'],changes_partition_boundaries=False,protected=[p for p in layout['protected']
            if p['name']!=system['partition']['name']]+([layout['userdata']] if layout['userdata'] else []),
        boot_action=engine.receipt['boot_action'],default_name=system['name'],experimental=True,
        backup_verified=True,backup_dir=record['path'],warnings=compatibility['warnings']+[
            '只格式化所选预留 rootfs，保留 Android、其他 rootfs 和已有引导文件。']))


def install(engine,payload):
    consent=payload.get('confirmation',{})
    if (payload.get('session_id')!=engine.session_id or payload.get('plan_id')!=engine.plan_id or
        consent.get('serial')!=engine.device['serial'] or
        any(consent.get(k) is not True for k in ('personal_data_backed_up','erase_rootfs','preserve_other_systems','allow_experimental'))):
        raise InstallerError('请确认只清空选定 rootfs，并保留其他系统与引导存储。')
    snapshot=engine.plan_snapshot;plan=engine.linux_plan;system=engine.linux_system
    if (hashlib.sha256(canonical(snapshot)).hexdigest()!=engine.plan_id or plan!=snapshot['plan'] or system!=snapshot['system']):
        raise InstallerError('确认后的 Linux 方案发生变化。')
    engine.committing=True;engine.set_state('installing')
    engine.validate_bundle(str(engine.bundle));engine.transport.preflight(require_archive=False)
    if (engine.manifest_digest!=snapshot['manifest'] or engine.transport.boot_id!=snapshot['boot_id'] or
        engine.transport.report()!=snapshot['report']): raise InstallerError('安装材料、RAM 会话或分区布局发生变化。')
    record=engine.load_backup(engine.receipt['backup_dir'],engine.device['serial'])
    compatibility=engine.verify_current_backup(record)
    if record['backup_id']!=snapshot['backup_id'] or compatibility['current_persist_sha256']!=snapshot['persist_sha256']:
        raise InstallerError('备份或校准信息变化。')
    for path,digest in ((system['source']['boot_image'],system['source']['boot_sha256']),
        (system['source']['archive'],system['source']['archive_sha256']),(system['boot_image'],system['boot_sha256']),
        (system['padded_image'],system['padded_sha256']),(engine.linux_previous_boot,plan['previous_boot_sha256'])):
        if digest_file(Path(path))!=digest: raise InstallerError('确认后的 Linux 或引导材料变化。')
    engine.receipt.update(confirmation=dict(consent),write_dispatched=True);engine.save_receipt()
    engine.transport.android_rpc('linux-stage',dict(plan=plan))
    engine.transport.android_rpc('linux-guard',dict(plan=plan))
    engine.transport.install_root(Path(system['boot_image']),system=system)
    engine.transport.android_rpc('linux-finish',dict(plan=plan))
    engine.receipt['root_installed']=True;engine.save_receipt()
    if plan['set_default']: set_default(engine)
    else:
        engine.transport.android_rpc('linux-commit',dict(plan=plan,registry=plan['candidate_registry'],expected_boot=plan['previous_boot_sha256']))
        engine.transport.cancel_cleanup()
    engine.receipt.update(installation_commands_completed=True,boot_store_written=True,other_systems_preserved=True)
    engine.save_receipt();engine.set_state('awaiting_first_boot')
    engine.emit('installation_written',receipt=engine.receipt,first_boot_verified=False)
    engine.committing=False;engine.clean_scratch();engine.release_device()


def boot_snapshot_matches(system, files, previous, previous_sha256):
    stored=files.get(system.get('image',''),{})
    size=stored.get('bytes',0)
    if type(size) is not int or size<=0 or stored.get('sha256')!=system.get('boot_sha256'):return False
    # Registry hashes describe the image; the snapshot also includes partition
    # padding. Verify both before considering the old system a rollback target.
    data=Path(previous).read_bytes()
    return (size<=len(data) and hashlib.sha256(data).hexdigest()==previous_sha256 and
        hashlib.sha256(data[:size]).hexdigest()==stored['sha256'] and not data[size:].strip(b'\0'))


def previous_default_survives(plan,previous):
    from liuqin_partitions import stable_partition
    system=next((s for s in plan['registry']['systems'] if s['id']==plan['registry'].get('default_id')),None)
    if not system or not boot_snapshot_matches(system,plan['store_files'],previous,plan['previous_boot_sha256']):return False
    if system.get('type')=='linux':
        part=system.get('partition',{});name=part.get('name');actual=plan['inventory'].get(name)
        target=system.get('target',{})
        return (bool(actual) and name!=plan['system']['partition']['name'] and
            all(actual.get(k)==part.get(k) for k in ('number','partuuid','start','bytes')) and
            actual.get('fsuuid')==target.get('root_fsuuid') and actual.get('filesystem')==target.get('root_fstype'))
    if system.get('type')=='android':
        return all(name in plan['inventory'] and name in system and stable_partition(plan['inventory'][name])==stable_partition(system[name]) for name in ('userdata','metadata'))
    return False


def set_default(engine):
    transport=engine.transport;plan=engine.linux_plan;system=engine.linux_system
    previous=engine.linux_previous_boot
    if transport.current_boot_digest()!=plan['previous_boot_sha256']: raise InstallerError('当前 boot_a 发生变化，未切换默认系统。')
    engine.receipt.update(boot_finalization_pending=True,boot_expected_sha256=plan['padded_sha256']);engine.save_receipt()
    if getattr(transport,'installer_enabled',False):
        transport.write_native_boot(system['padded_image'],plan['padded_sha256'],plan['previous_boot_sha256'])
        transport.android_rpc('linux-commit',dict(plan=plan,registry=plan['candidate_registry'],expected_boot=plan['padded_sha256']))
        engine.receipt.update(boot_finalization_pending=False,default_id=system['id'],recovery_retained=True);engine.save_receipt();return
    try:
        transport.return_fastboot();transport.check_device()
        engine.phase('flash_boot');engine.receipt['boot_flash_started']=True;engine.save_receipt()
        transport.fastboot('flash','boot_a',system['padded_image']);transport.fastboot('--set-active=a')
        engine.receipt.update(boot_written=True,active_slot_set=True);engine.save_receipt()
        engine.phase('verify_boot');transport.boot_ram()
        if transport.current_boot_digest()!=plan['padded_sha256']:raise InstallerError('Linux 默认引导回读失败。')
        engine.receipt.update(boot_readback_verified=True);engine.save_receipt()
        transport.android_rpc('linux-commit',dict(plan=plan,registry=plan['candidate_registry'],expected_boot=plan['padded_sha256']))
        engine.receipt.update(boot_readback_verified=True,boot_finalization_pending=False,default_id=system['id']);engine.save_receipt()
    except Exception as error:
        if engine.receipt.get('boot_flash_started'):
            if not previous_default_survives(plan,previous):
                engine.receipt.update(boot_error=str(error),boot_rollback_skipped='Previous default system was removed or its image is not verified intact')
                try:
                    if transport.ram_active and getattr(transport,'boot_id',None):transport.return_fastboot()
                except Exception as cleanup_error:engine.receipt['boot_cleanup_error']=str(cleanup_error)
                engine.save_receipt();raise
            engine.receipt['boot_rollback_attempted']=True;engine.save_receipt()
            try:
                if transport.ram_active:transport.return_fastboot()
                transport.fastboot('flash','boot_a',str(previous));transport.fastboot('--set-active=a');transport.boot_ram()
                if transport.current_boot_digest()!=plan['previous_boot_sha256']:raise InstallerError('恢复旧 boot_a 回读失败。')
                fallback=copy.deepcopy(plan['candidate_registry'])
                fallback.update(default_id=plan['registry'].get('default_id'),default_generation=plan['registry'].get('default_generation'))
                transport.android_rpc('linux-commit',dict(plan=plan,registry=fallback,expected_boot=plan['previous_boot_sha256'],restore_default=True))
                engine.receipt.update(boot_rollback_verified=True,boot_written=False,boot_readback_verified=False,active_slot_set=False,boot_finalization_pending=False)
                transport.return_fastboot()
            except Exception as error:engine.receipt['boot_rollback_error']=str(error)
            engine.save_receipt()
        raise
    transport.return_fastboot();transport.fastboot('reboot')
