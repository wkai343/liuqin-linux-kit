# SPDX-License-Identifier: MIT
"""Multi-root prepare/commit orchestration, sharing the single-root transport."""
from __future__ import annotations
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import uuid

from . import artifacts
from .engine import InstallerError, canonical, digest_file, cli
from liuqin_boot_target import bind, encode, validate_helper
from liuqin_kernel import parse_contract
from liuqin_multiboot import normalize, plan, layout_payload, verify_layout


def inspect_system(row, primary_bundle, check_cancel):
    bundle = Path(row['bundle'] or primary_bundle).expanduser().resolve()
    boot = Path(row['boot_image']).expanduser().resolve() if row['boot_image'] else bundle/'boot.img'
    archive = bundle/'rootfs.tar.gz'
    for path in (boot, archive):
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise InstallerError('系统安装材料缺失或无效：' + str(path))
    boot_hash = digest_file(boot, check=check_cancel)
    archive_hash = digest_file(archive, check=check_cancel)
    interface = artifacts.boot_interface(boot)
    if interface.get('multiboot_version') != 1 or interface.get('boot_target'):
        raise InstallerError(row['name']+' 需要支持多系统的未绑定 boot.img，请重新构建安装包。')
    validate_helper(interface)
    root = artifacts.inspect_root(archive, artifacts.contract_hashes(interface['contract']),
                                  check_cancel, profile=interface['profile'].decode().strip(),
                                  storage_policy_version=interface.get('storage_policy_version',0))
    if root.get('root_multiboot_version') != 1:
        raise InstallerError(row['name']+' 的 rootfs 缺少引导绑定模板，请重新构建根归档。')
    if row['filesystem'] not in interface['filesystems'] or row['filesystem'] not in root['root_filesystems']:
        raise InstallerError(row['name']+' 的 boot/rootfs 不支持所选文件系统。')
    capacity = row.get('size_bytes', row['size_gib']*1024**3)
    if root['expanded_bytes'] + 512*1024**2 > capacity:
        raise InstallerError(row['name']+' 的 rootfs 分区不足以容纳解压后的系统。')
    return dict(bundle=str(bundle), boot_image=str(boot), archive=str(archive),
                boot_sha256=boot_hash, archive_sha256=archive_hash, archive_bytes=archive.stat().st_size,
                expanded_bytes=root['expanded_bytes'], contract_sha256=hashlib.sha256(interface['contract']).hexdigest(),
                kernel_release=parse_contract(interface['contract'])['release'])


def _archive(path, records):
    with tarfile.open(path, 'w') as stream:
        for name, content in sorted(records.items()):
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.size = content.stat().st_size if isinstance(content, Path) else len(content)
            source = content.open('rb') if isinstance(content, Path) else io.BytesIO(content)
            with source: stream.addfile(entry, source)


def prepare(engine, payload):
    engine.guard_operation()
    partition_only = payload.get('layout') == 'partition-reset'
    config = normalize(dict(payload.get('partition_config', {}), partition_only=partition_only))
    serial = engine.require_serial(payload)
    if not payload.get('backup_dir'):
        raise InstallerError('请先创建或选择设备备份。')
    engine.begin_run(payload, 'partition' if partition_only else 'install')
    if engine.manifest.get('installer_multiboot_version') != 1:
        raise InstallerError('installer.img 尚不支持多系统，请重新构建安装环境；未访问设备。')
    engine.multi_config = config
    engine.receipt.update(layout=payload['layout'], boot_action=config['boot_action'])
    record = engine.load_backup(payload['backup_dir'], serial)
    engine.receipt['backup_dir'] = record['path']
    engine.save_receipt()
    sources = []
    for row in config['roots']:
        if partition_only:
            continue
        engine.check_cancel()
        engine.emit('notice', message='检查系统安装材料：'+row['name'])
        source = inspect_system(row, engine.bundle, engine.check_cancel)
        if row['filesystem'] not in engine.manifest['filesystems']:
            raise InstallerError('主安装环境不支持 '+row['filesystem'])
        sources.append(source)
    disk = engine.connect_ram(require_archive=False)
    for source in sources:
        if source['archive_bytes'] + 512*1024**2 >= engine.device['available_memory_bytes']:
            raise InstallerError('RAM 空间不足以安全暂存 '+source['bundle']+' 的归档。')
    planned = plan(disk, config, str(uuid.uuid4()))
    engine.phase('verify_backup')
    compatibility = engine.verify_current_backup(record)
    engine.backup_record = record
    engine.multi_info = engine.transport.multiboot_info()
    source_payload = layout_payload(planned, engine.multi_info)
    engine.multi_layout_payload = source_payload
    directory = Path(engine.receipt['run_dir'])/'multiboot'
    directory.mkdir(mode=0o700)
    required = sum(Path(s['boot_image']).stat().st_size*2 + engine.device['boot_bytes'] for s in sources) + 2*engine.device['boot_bytes']
    if shutil.disk_usage(directory).free < required + 512*1024**2:
        raise InstallerError('电脑运行目录空间不足以保存各系统引导与回退材料。')
    previous_directory = directory/'previous'
    previous_directory.mkdir(mode=0o700)
    previous_hash = engine.transport.snapshot_boot(previous_directory)
    engine.multi_previous_boot = previous_directory/'boot_a.img'
    if engine.multi_previous_boot.stat().st_size != engine.device['boot_bytes']:
        raise InstallerError('当前 boot_a 快照尺寸不正确。')
    systems = []
    records = {}
    for system, source in zip(planned['systems'], sources):
        engine.check_cancel()
        dest = directory/system['id']
        dest.mkdir(mode=0o700)
        boot = dest/'boot.img'
        engine.phase('bind_boot', detail=system['name'])
        digest = bind(Path(source['boot_image']), boot, system['target'], engine.device['boot_bytes'])
        padded, padded_digest = cli.pad_boot_image(boot, digest, engine.device['boot_bytes'], dest)
        system.update(source=source, boot_image=str(boot), boot_sha256=digest,
                      padded_image=str(padded), padded_sha256=padded_digest)
        metadata = dict(id=system['id'], name=system['name'], type='linux', status='installed-unverified',
                        partition=system['partition'], target=system['target'], boot_sha256=digest,
                        source_boot_sha256=source['boot_sha256'], kernel_release=source['kernel_release'],
                        image='systems/'+system['id']+'/images/'+planned['generation']+'.img')
        systems.append(metadata)
        records[metadata['image']] = boot
        records['systems/'+system['id']+'/manifest.json'] = canonical(metadata)
    registry = dict(schema_version=1, serial=serial, layout_generation=planned['generation'],
                    default_id=None, default_generation=None, systems=systems,
                    userdata_state='requires-android-recovery-format' if config['userdata_gib'] else 'absent',
                    boot_transaction=dict(status='staged', requested_default=config['default_id'] or None,
                        previous_slot=engine.device['current-slot'], previous_boot_sha256=previous_hash))
    if partition_only:
        registry['rootfs_slots'] = [dict(id=s['id'], name=s['name'], partition=s['partition'],
            target=s['target'], status='reserved') for s in planned['systems']]
        registry['partition_layout'] = copy.deepcopy(planned['partitions'])
        registry['userdata_state'] = 'reserved' if config['userdata_gib'] else 'absent'
    records['registry.json'] = canonical(registry)
    records['transactions/'+planned['generation']+'/previous-boot.img'] = engine.multi_previous_boot
    records['transactions/'+planned['generation']+'/journal.json'] = canonical(registry['boot_transaction'])
    records['SHA256SUMS'] = ''.join(
        (digest_file(content) if isinstance(content, Path) else hashlib.sha256(content).hexdigest()) + '  ' + name + '\n'
        for name, content in sorted(records.items())).encode()
    archive = directory/'boot-store.tar'
    _archive(archive, records)
    if archive.stat().st_size+512*1024**2 >= engine.device['available_memory_bytes']:
        raise InstallerError('RAM 空间不足以暂存引导存储，请减少同时安装的系统数量。')
    if archive.stat().st_size+256*1024**2 > planned['store']['bytes']:
        raise InstallerError('引导存储分区空间不足。')
    engine.multi_plan, engine.multi_registry, engine.multi_archive = planned, registry, archive
    snapshot = dict(serial=serial, session_id=engine.session_id, boot_id=engine.transport.boot_id,
                    report=engine.report_text, info=engine.multi_info, manifest=engine.manifest_digest,
                    backup_id=record['backup_id'], current_persist_sha256=compatibility['current_persist_sha256'],
                    plan=copy.deepcopy(planned), registry=copy.deepcopy(registry), previous_boot_sha256=previous_hash, archive_sha256=digest_file(archive), layout_payload_sha256=hashlib.sha256(source_payload).hexdigest())
    engine.plan_snapshot = snapshot
    engine.plan_id = hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(plan_id=engine.plan_id, backup_id=record['backup_id'], backup_verified=True,
                          backup_reused=True, backup_compatibility=compatibility,
                          multiboot_plan=planned, device=engine.device)
    engine.save_receipt()
    engine.check_cancel()
    engine.set_state('prepared')
    engine.emit('plan_ready', plan=dict(plan_id=engine.plan_id, serial=serial, layout=payload['layout'],
                partitions=planned['partitions'], systems=systems, free_bytes=planned['free_bytes'],
                rootfs_slots=registry.get('rootfs_slots', []),
                root_bytes=sum(s['partition']['bytes'] for s in planned['systems']),
                userdata_bytes=config['userdata_gib']*1024**3, separate_home=False,
                boot_action=config['boot_action'], default_id=config['default_id'],
                default_name=next((s['name'] for s in systems if s['id']==config['default_id']), ''),
                changes_partition_boundaries=True, boot_slots=['a'] if config['boot_action']=='set-default' else [],
                active_slot='a' if config['boot_action']=='set-default' else None,
                erases_personal_data=True, backup_dir=record['path'], run_dir=engine.receipt['run_dir'],
                backup_verified=True, backup_reused=True, backup_id=record['backup_id'], experimental=True,
                warnings=compatibility['warnings']+['首次创建多系统会清空原 userdata 中的全部文件。',
                    *(['Android 空间仅预留，之后选择安装 Android，由安装器初始化。'] if config['userdata_gib'] else []),
                    *(['本次只创建分区与引导存储，不安装系统、不修改默认 boot。'] if partition_only else []),
                    *(['完成后停在 Fastboot；原默认系统可能已无法启动。'] if config['boot_action']=='install-only' else [])], calibration_usable=True))


def install(engine, payload):
    consent = payload.get('confirmation', {})
    if (payload.get('session_id')!=engine.session_id or payload.get('plan_id')!=engine.plan_id or
        consent.get('serial')!=engine.device['serial'] or
        any(consent.get(key) is not True for key in ('personal_data_backed_up','erase_userdata','repartition_userdata','allow_experimental'))):
        raise InstallerError('必须确认当前设备、分区重建及全部 userdata 数据清除，并接受实验功能。')
    if hashlib.sha256(canonical(engine.plan_snapshot)).hexdigest()!=engine.plan_id or engine.multi_plan!=engine.plan_snapshot['plan']:
        raise InstallerError('确认后的多系统方案发生变化。')
    if (engine.manifest_digest != engine.plan_snapshot['manifest'] or
        engine.report_text != engine.plan_snapshot['report'] or engine.multi_info != engine.plan_snapshot['info'] or
        engine.multi_registry != engine.plan_snapshot['registry'] or
        hashlib.sha256(engine.multi_layout_payload).hexdigest() != engine.plan_snapshot['layout_payload_sha256']):
        raise InstallerError('确认后的写入计划或设备身份快照发生变化。')
    if normalize(engine.multi_config)!=engine.multi_plan['configuration']:
        raise InstallerError('确认后的分区配置发生变化。')
    engine.committing=True
    engine.set_state('installing')
    engine.phase('revalidate')
    original_manifest=engine.manifest_digest
    (engine.validate_installer if engine.multi_config.get('partition_only') else engine.validate_bundle)(str(engine.bundle))
    if engine.manifest_digest!=original_manifest: raise InstallerError('确认后主安装包发生变化。')
    engine.transport.preflight(require_archive=False)
    if engine.transport.boot_id!=engine.plan_snapshot['boot_id'] or engine.transport.report()!=engine.report_text or engine.transport.multiboot_info()!=engine.multi_info:
        raise InstallerError('确认后的 RAM 会话或分区表发生变化，未写入。')
    record=engine.load_backup(engine.receipt['backup_dir'],engine.device['serial'])
    compatibility=engine.verify_current_backup(record)
    if record['backup_id']!=engine.plan_snapshot['backup_id'] or compatibility['current_persist_sha256']!=engine.plan_snapshot['current_persist_sha256']:
        raise InstallerError('确认后的设备备份或校准发生变化。')
    if digest_file(engine.multi_previous_boot)!=engine.plan_snapshot['previous_boot_sha256']:
        raise InstallerError('本次引导回退快照发生变化，未写入。')
    if engine.transport.current_boot_digest()!=engine.plan_snapshot['previous_boot_sha256']:
        raise InstallerError('确认后 boot_a 发生变化，未写入。')
    for system in engine.multi_plan['systems']:
        if engine.multi_config.get('partition_only'): continue
        source=system['source']
        for path,digest in ((source['boot_image'],source['boot_sha256']), (source['archive'],source['archive_sha256']),
                            (system['boot_image'],system['boot_sha256']), (system['padded_image'],system['padded_sha256'])):
            if digest_file(Path(path))!=digest: raise InstallerError('确认后安装材料发生变化：'+str(path))
    if digest_file(engine.multi_archive)!=engine.plan_snapshot['archive_sha256']:
        raise InstallerError('确认后的引导存储材料发生变化。')
    engine.receipt.update(confirmation=dict(consent),write_dispatched=True)
    engine.save_receipt()
    engine.phase('write_layout')
    engine.transport.multiboot_apply(engine.multi_plan,engine.multi_layout_payload,engine.report_text,engine.multi_info)
    verify_layout(cli.layout.parse_print(engine.transport.report()),engine.multi_plan)
    engine.receipt['layout_written']=True;engine.save_receipt()
    engine.transport.multiboot_store('initialize',engine.multi_plan['store'])
    for system in engine.multi_plan['systems']:
        if engine.multi_config.get('partition_only'): continue
        engine.emit('notice',message='安装 '+system['name'])
        engine.transport.install_root(Path(system['boot_image']),system=system)
        engine.receipt.setdefault('installed_systems',[]).append(system['id']);engine.save_receipt()
    engine.receipt['root_installed']=not engine.multi_config.get('partition_only', False);engine.save_receipt()
    engine.phase('store_boots')
    engine.transport.multiboot_store('stage',engine.multi_plan['store'],engine.multi_archive,
                                     engine.plan_snapshot['archive_sha256'])
    engine.receipt['boot_store_written']=True;engine.save_receipt()
    if engine.multi_config['boot_action']=='set-default':
        system=next(s for s in engine.multi_plan['systems'] if s['id']==engine.multi_config['default_id'])
        registry=copy.deepcopy(engine.multi_registry)
        registry.update(default_id=system['id'],default_generation=engine.multi_plan['generation'])
        registry['boot_transaction'].update(status='committed',boot_a_sha256=system['padded_sha256'])
        engine.transport.multiboot_default(system,engine.multi_plan['store'],canonical(registry),record)
        engine.receipt['default_id']=system['id']
    else:
        engine.phase('return_fastboot');engine.transport.cancel_cleanup()
    engine.receipt['installation_commands_completed']=True;engine.save_receipt()
    engine.set_state('partition_complete' if engine.multi_config.get('partition_only') else 'awaiting_first_boot')
    engine.emit('installation_written',receipt=engine.receipt,first_boot_verified=False)
    engine.committing=False;engine.clean_scratch();engine.release_device()
