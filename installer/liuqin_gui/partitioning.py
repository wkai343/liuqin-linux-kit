# SPDX-License-Identifier: MIT
"""Live GPT discovery and append-only partition transactions."""
import copy
import hashlib
from pathlib import Path
import uuid
from .engine import InstallerError,canonical,digest_file
from liuqin_partitions import discover,plan_append,snapshot_id,validate_append


def connect(engine,payload,operation):
    engine.begin_run(payload,operation)
    if engine.manifest.get('installer_android_version')!=1 or engine.manifest.get('installer_multiboot_version')!=1:
        raise InstallerError('需要支持实际分区读取的新版 installer.img。')
    engine.android_profile=engine.android_profile_for_run()
    engine.android_profile_sha=hashlib.sha256(canonical(engine.android_profile)).hexdigest()
    engine.connect_ram(require_archive=False)
    state=engine.transport.android_rpc('partition-inspect',{})
    if state['report']!=engine.report_text:raise InstallerError('读取期间 GPT 发生变化。')
    return state


def scan(engine,payload):
    engine.guard_operation();state=connect(engine,payload,'scan-partitions')
    summary=discover(state,engine.device['serial'])
    engine.partition_discovery=copy.deepcopy(state)
    engine.emit('partition_inventory',inventory=summary)
    engine.transport.cancel_cleanup();engine.clean_scratch();engine.release_device()
    engine.set_state('partitions_scanned')


def prepare(engine,payload):
    engine.guard_operation()
    if not payload.get('partition_snapshot_id'):raise InstallerError('请先读取平板实际分区。')
    if not payload.get('backup_dir'):raise InstallerError('请选择与实际布局相符的设备备份。')
    state=connect(engine,payload,'partition-add')
    if snapshot_id(state)!=payload['partition_snapshot_id']:
        raise InstallerError('平板分区或系统登记在读取后改变，请重新读取实际分区。')
    planned=plan_append(state,payload.get('partition_config',{}),engine.device['serial'],str(uuid.uuid4()))
    record=engine.load_backup(payload['backup_dir'],engine.device['serial'])
    compatibility=engine.verify_current_backup(record)
    directory=Path(engine.receipt['run_dir'])/'partitions';directory.mkdir(mode=0o700)
    previous=engine.transport.snapshot_boot(directory)
    snapshot=dict(plan=copy.deepcopy(planned),manifest=engine.manifest_digest,boot_id=engine.transport.boot_id,
        backup_id=record['backup_id'],persist_sha256=compatibility['current_persist_sha256'],previous_boot_sha256=previous)
    engine.append_plan=planned;engine.plan_snapshot=snapshot
    engine.plan_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    (directory/'plan.json').write_bytes(canonical(snapshot))
    engine.receipt.update(plan_id=engine.plan_id,backup_dir=record['path'],backup_id=record['backup_id'],
        boot_action='install-only',backup_verified=True,device=engine.device,append_plan=planned)
    engine.save_receipt();engine.check_cancel();engine.set_state('prepared')
    engine.emit('plan_ready',plan=dict(plan_id=engine.plan_id,serial=engine.device['serial'],layout='partition-only',
        partitions=planned['partitions'],rootfs_slots=planned['candidate_registry']['rootfs_slots'],systems=[],
        protected=discover(state,engine.device['serial'])['partitions'],changes_partition_boundaries=True,
        preserves_existing_partitions=True,free_bytes=planned['free_bytes'],
        root_bytes=sum(s['partition']['bytes'] for s in planned['candidate_registry']['rootfs_slots']),
        userdata_bytes=next((p['bytes'] for p in planned['candidate_registry']['partition_layout'] if p['name']=='userdata'),0),
        boot_action='install-only',backup_verified=True,backup_dir=record['path'],experimental=True,
        warnings=compatibility['warnings']+['仅在未分配空间新增上列分区；已有 userdata、rootfs 和默认引导保持不变。']))


def install(engine,payload):
    consent=payload.get('confirmation',{})
    if (payload.get('session_id')!=engine.session_id or payload.get('plan_id')!=engine.plan_id or
        consent.get('serial')!=engine.device['serial'] or
        any(consent.get(k) is not True for k in ('create_partitions','preserve_existing_partitions','allow_experimental'))):
        raise InstallerError('请确认仅追加分区并保留全部已有分区。')
    snapshot=engine.plan_snapshot;planned=engine.append_plan
    if hashlib.sha256(canonical(snapshot)).hexdigest()!=engine.plan_id or planned!=snapshot['plan']:
        raise InstallerError('已确认的追加分区方案发生变化。')
    validate_append(planned,engine.device['serial'])
    engine.committing=True;engine.set_state('installing');engine.validate_installer(str(engine.bundle))
    if engine.manifest_digest!=snapshot['manifest'] or engine.transport.boot_id!=snapshot['boot_id']:
        raise InstallerError('安装环境或 RAM 会话发生变化。')
    engine.transport.preflight(require_archive=False)
    record=engine.load_backup(engine.receipt['backup_dir'],engine.device['serial'])
    compatibility=engine.verify_current_backup(record)
    if record['backup_id']!=snapshot['backup_id'] or compatibility['current_persist_sha256']!=snapshot['persist_sha256']:
        raise InstallerError('备份或校准信息发生变化。')
    if engine.transport.current_boot_digest()!=snapshot['previous_boot_sha256']:
        raise InstallerError('当前默认引导发生变化，未追加分区。')
    engine.receipt.update(confirmation=dict(consent),write_dispatched=True);engine.save_receipt()
    result=engine.transport.android_rpc('partition-apply',dict(plan=planned,preserve_existing=True))
    if result.get('partitions_created') is not True or result.get('existing_partitions_preserved') is not True:
        raise InstallerError('平板未确认追加分区完成。')
    if engine.transport.current_boot_digest()!=snapshot['previous_boot_sha256']:
        raise InstallerError('追加分区后 boot_a 校验不符。')
    after=engine.transport.android_rpc('partition-inspect',{})
    summary=discover(after,engine.device['serial'])
    if not summary['can_create'] or after['registry']!=planned['candidate_registry']:
        raise InstallerError('追加后分区与登记回读不符。')
    engine.receipt.update(layout_written=True,boot_store_written=True,existing_partitions_preserved=True,
        installation_commands_completed=True)
    engine.save_receipt();engine.transport.cancel_cleanup();engine.set_state('partition_complete')
    engine.emit('installation_written',receipt=engine.receipt,first_boot_verified=False)
    engine.committing=False;engine.clean_scratch();engine.release_device()
