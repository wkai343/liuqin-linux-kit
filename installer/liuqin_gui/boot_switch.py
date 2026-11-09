# SPDX-License-Identifier: MIT
"""Prepare and confirm a boot-only switch of an installed system."""

import hashlib
import os
from pathlib import Path
import shutil
import uuid

from liuqin_native_install import identity, scope, validate_scope
from liuqin_partitions import snapshot_id

from . import host, partitioning
from .engine import InstallerError, canonical, digest_file
from .native_installer import Session


def available(engine):
    if engine.simulation:
        return True
    manifest = engine.manifest.get('native_recovery_manifest', {})
    return (manifest.get('boot_switch_version') == 1
            and getattr(engine.transport, 'installer_enabled', False)
            and engine.device.get('native_install_allowed') is True
            and not engine.device.get('native_ota_only'))


def prepare(engine, payload):
    from .workflow import connection_payload

    engine.guard_operation()
    connection = connection_payload(engine, payload)
    state = partitioning.connect(engine, connection, 'wizard')
    if not available(engine):
        raise InstallerError('请临时启动支持默认系统切换的新版 Recovery。')
    observed_id = snapshot_id(state)
    if observed_id != payload.get('snapshot_id'):
        raise InstallerError('平板分区或系统登记已经改变，请重新读取分区。')
    registry = state.get('registry') or {}
    system = next((item for item in registry.get('systems', [])
                   if item['id'] == payload.get('system_id')), None)
    if not system or system['id'] == registry.get('default_id'):
        raise InstallerError('请选择一个已安装的非默认系统。')

    request = dict(serial=connection['serial'], boot_id=engine.transport.boot_id,
                   system_id=system['id'], transaction=str(uuid.uuid4()))
    if engine.manifest.get('native_recovery_manifest',{}).get('fixed_installer_version')==1:
        request['fixed_recovery_sha256']=engine.manifest['files'][engine.manifest['runtime_file']]
    plan = engine.transport.native_rpc('switch-preview', request)['plan']
    parts = [identity(state['inventory'][name]) for name in ('boot_a', 'linux_boot')]
    if (any(plan.get(key) != value for key, value in request.items())
            or plan.get('parts') != parts or plan.get('target_name') != system['name']):
        raise InstallerError('Recovery 返回的切换目标与已读取的系统不匹配。')

    directory = Path(engine.receipt['run_dir']) / 'boot-switch'
    directory.mkdir(mode=0o700)
    if shutil.disk_usage(directory).free < engine.device['boot_bytes'] + 16*1024**2:
        raise InstallerError('电脑空间不足以保存本次旧引导快照。')
    previous = engine.transport.snapshot_boot(directory)
    backup = directory / 'boot_a.img'
    if previous != plan['previous_boot_sha256'] or backup.stat().st_size != engine.device['boot_bytes']:
        raise InstallerError('当前引导快照与切换方案不一致。')
    with backup.open('r+b') as stream:
        os.fsync(stream.fileno())
    if not host.IS_WINDOWS:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    snapshot = dict(kind='switch', switch_plan=plan, snapshot_id=observed_id,
                    previous_boot_path=str(backup), backup_dir=str(directory))
    if not engine.simulation:
        fingerprint = engine.receipt['firmware_fingerprint']
        snapshot['native_firmware_sha256'] = hashlib.sha256(canonical(fingerprint)).hexdigest()
    validate_scope(scope(snapshot))
    engine.workflow_snapshot = snapshot
    engine.plan_id = hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(plan_id=engine.plan_id, backup_dir=str(directory), workflow=snapshot)
    engine.save_receipt()
    engine.check_cancel()
    engine.set_state('prepared')
    engine.emit('wizard_plan', plan=dict(plan_id=engine.plan_id, serial=connection['serial'],
        kind='switch', target=plan['target_name'], previous_name=plan['previous_name'],
        boot_store_retention=engine.manifest.get('native_recovery_manifest',{}).get('boot_store_retention_version')==1,
        backup_dir=str(directory), set_default=True, experimental=True))


def install(engine, snapshot):
    """Execute the plan after the common wizard confirmation checks."""
    plan = snapshot['switch_plan']
    transport = engine.transport
    if not available(engine):
        raise InstallerError('当前 Recovery 不支持默认系统切换。')
    if transport.boot_id != plan['boot_id']:
        raise InstallerError('Recovery 会话发生变化，请重新准备切换。')
    if shutil.disk_usage(snapshot['backup_dir']).free < 16*1024**2:
        raise InstallerError('电脑空间不足以保存切换执行记录。')
    if digest_file(snapshot['previous_boot_path']) != plan['previous_boot_sha256']:
        raise InstallerError('电脑上的旧引导快照校验失败。')
    if transport.current_boot_digest() != plan['previous_boot_sha256']:
        raise InstallerError('当前默认引导发生变化，请重新准备切换。')
    if snapshot_id(transport.android_rpc('partition-inspect', {})) != snapshot['snapshot_id']:
        raise InstallerError('分区或系统登记在确认后改变。')

    engine.committing = True
    engine.set_state('installing')
    session = None
    try:
        if not engine.simulation:
            session = Session(engine, snapshot)
        if session:
            session.start()
        engine.phase('flash_boot')
        engine.receipt.update(write_dispatched=True, boot_flash_started=True,
                              boot_finalization_pending=True,
                              boot_expected_sha256=plan['target_boot_sha256'])
        engine.save_receipt()
        result = transport.native_rpc('switch-apply',
                                     dict(token=session.token if session else '', plan=plan))
        if (result.get('boot_written') is not True or result.get('boot_readback_verified') is not True
                or result.get('default_id') != plan['system_id']):
            raise InstallerError('Recovery 未确认默认引导和登记已提交。')
        engine.receipt.update(boot_written=True, boot_readback_verified=True,
                              boot_finalization_pending=False, default_id=plan['system_id'])
        engine.save_receipt()
        if session:
            session.finish(True)
        engine.receipt.update(completed=True, boot_switch_completed=True,
                              default_name=plan['target_name'], recovery_retained=True)
        engine.save_receipt()
        engine.set_state('wizard_complete')
        engine.emit('wizard_complete', receipt=engine.receipt)
    except Exception:
        engine.receipt['failed'] = True
        if engine.receipt.get('write_dispatched'):
            engine.receipt.update(recovery_required=True, recovery_retained=True)
        engine.save_receipt()
        raise
    finally:
        if session and session.active:
            try:
                session.finish(False)
            except Exception as error:
                engine.receipt['native_session_cleanup_error'] = str(error)
                engine.save_receipt()
        engine.committing = False
        engine.clean_scratch()
        engine.release_device()
