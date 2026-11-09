# SPDX-License-Identifier: MIT
"""Review boot-store maintenance and recovery through the normal wizard consent."""
import hashlib
import os
from pathlib import Path
import shutil

from liuqin_native_install import scope, validate_scope
from .engine import InstallerError, canonical, digest_file
from .native_installer import Session
from . import host, partitioning

LABELS = {'cleanup':'清理过期引导版本','restore-switch':'恢复原默认引导',
          'finish-switch':'完成目标引导登记','finish-install':'完成系统登记'}


def _connect(engine,payload):
    from .workflow import connection_payload
    if engine.state == 'failed':
        engine.clean_scratch(); engine.release_device(); engine.state='idle'
    engine.guard_operation()
    connection = connection_payload(engine,payload)
    partitioning.connect(engine,connection,'boot-store')
    if (not getattr(engine.transport,'installer_enabled',False) or
            engine.manifest.get('native_recovery_manifest',{}).get('boot_store_version') != 1):
        raise InstallerError('当前 Rec 不支持引导存储管理。请从 Fastboot 临时启动安装包中的新版 Rec；固定 Rec 可另行升级。')
    return connection


def inspect(engine,payload):
    try:
        _connect(engine,payload)
        report = engine.transport.native_rpc('store-inspect',{})
        engine.receipt['boot_store_report'] = report
        engine.save_receipt()
        engine.emit('boot_store_report',report=report)
        engine.set_state('wizard_scanned')
    finally:
        if engine.transport:
            engine.transport.cancel_cleanup()
        engine.clean_scratch(); engine.release_device()


def prepare(engine,payload):
    _connect(engine,payload)
    operation = payload.get('operation')
    if operation not in LABELS:
        raise InstallerError('未知引导存储操作。')
    request = dict(serial=engine.device['serial'],boot_id=engine.transport.boot_id,
                   snapshot_id=payload.get('snapshot_id'),operation=operation,
                   transaction=payload.get('transaction',''))
    plan = engine.transport.native_rpc('store-preview',request)['plan']
    if any(plan.get(k) != v for k,v in request.items()):
        raise InstallerError('Recovery 返回的引导维护计划与所选操作不匹配。')
    directory = Path(engine.receipt['run_dir'])/'boot-store'
    directory.mkdir(mode=0o700)
    if shutil.disk_usage(directory).free < engine.device['boot_bytes']+16*1024**2:
        raise InstallerError('电脑空间不足以保存本次引导维护快照。')
    previous = engine.transport.snapshot_boot(directory)
    if previous != plan['previous_boot_sha256']:
        raise InstallerError('引导在准备期间发生变化。')
    snapshot = dict(kind='maintenance',maintenance_plan=plan,backup_dir=str(directory),
                    previous_boot_path=str(directory/'boot_a.img'),
                    native_firmware_sha256=hashlib.sha256(canonical(engine.receipt['firmware_fingerprint'])).hexdigest())
    validate_scope(scope(snapshot))
    (directory/'plan.json').write_bytes(canonical(plan))
    for name in ('boot_a.img','plan.json'):
        with (directory/name).open('r+b') as stream:
            os.fsync(stream.fileno())
    if not host.IS_WINDOWS:
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    engine.workflow_snapshot = snapshot
    engine.plan_id = hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(plan_id=engine.plan_id,workflow=snapshot,backup_dir=str(directory))
    engine.save_receipt(); engine.check_cancel(); engine.set_state('prepared')
    message = ('保留全部现用系统及最近一个可核验的 Linux 内核回退版本，清理过期镜像与已结束的恢复快照；存在未完成事务时暂停清理。'
               if operation=='cleanup' else '重新校验设备、安装回读记录与恢复材料，仅完成下列引导或登记操作。')
    if operation=='cleanup' and 'retention' not in plan:
        message='当前旧版 Rec 仅回收有持久等价镜像的重复快照，历史系统版本继续保留。'
    engine.emit('wizard_plan',plan=dict(plan_id=engine.plan_id,serial=engine.device['serial'],
        kind='maintenance',target=LABELS[operation],maintenance_message=message,
        reclaimable_bytes=plan.get('reclaimable_bytes',0),maintenance_files=plan.get('retention',{}).get('units',plan.get('duplicates',[])),
        transaction=plan['transaction'],backup_dir=str(directory),experimental=True))


def install(engine,snapshot):
    plan = snapshot['maintenance_plan']
    if (engine.transport.boot_id != plan['boot_id'] or
            digest_file(snapshot['previous_boot_path']) != plan['previous_boot_sha256'] or
            engine.transport.current_boot_digest() != plan['previous_boot_sha256']):
        raise InstallerError('引导、备份或 Recovery 会话发生变化，请重新检查。')
    engine.committing=True; engine.set_state('installing')
    session=None
    try:
        session=Session(engine,snapshot); session.start()
        engine.phase('revalidate',detail=LABELS[plan['operation']])
        engine.receipt.update(write_dispatched=True,recovery_retained=True)
        engine.save_receipt()
        result=engine.transport.native_rpc('store-apply',dict(token=session.token,plan=plan))
        if result.get('completed') is not True or result.get('operation') != plan['operation']:
            raise InstallerError('Rec 尚未确认引导维护完成，请重新检查当前状态。')
        engine.receipt.update(maintenance_result=result)
        engine.save_receipt(); session.finish(True)
        engine.receipt.update(completed=True,maintenance_completed=True)
        engine.save_receipt(); engine.set_state('wizard_complete')
        engine.emit('wizard_complete',receipt=engine.receipt)
    except Exception:
        engine.receipt['failed']=True
        if engine.receipt.get('write_dispatched'):
            engine.receipt.update(recovery_required=True,recovery_retained=True)
        engine.save_receipt()
        raise
    finally:
        if session and session.active:
            try:
                session.finish(False)
            except Exception as error:
                engine.receipt['native_session_cleanup_error']=str(error);engine.save_receipt()
        engine.committing=False;engine.clean_scratch();engine.release_device()
