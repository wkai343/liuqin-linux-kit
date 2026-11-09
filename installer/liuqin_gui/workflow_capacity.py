# SPDX-License-Identifier: MIT
"""Budget all child tasks before the first write in a wizard plan."""
from pathlib import Path
import os
import shutil

from .engine import InstallerError, canonical


MIB = 1024**2
RUN_MARGIN = 16 * MIB
STORE_MARGIN = 16 * MIB


def _host_requirements(jobs, boot_bytes, runtime_bytes, backup_bytes, reuse_layout=False, layout_changed=False):
    # Backups, boot snapshots and bound boot images remain in their run
    # directories. Only the copied Recovery image is discarded between runs.
    state_used = RUN_MARGIN
    backup_used = 0
    required = dict(state=state_used, backup=0, shared=state_used)

    def reserve(state_extra, backup_extra):
        state = state_used + state_extra
        backup = backup_used + backup_extra
        required['state'] = max(required['state'], state)
        required['backup'] = max(required['backup'], backup)
        required['shared'] = max(required['shared'], state + backup)

    for index, job in enumerate(jobs):
        new_backup = not reuse_layout or (index == 0 and layout_changed)
        # A reused backup still opens a read-only RAM session. A changed GPT
        # needs one backup of the new layout before any system child starts.
        reserve(256 * MIB, 768 * MIB if new_backup else 0)
        reserve(runtime_bytes + RUN_MARGIN, backup_bytes if new_backup else 0)
        if new_backup:
            backup_used += backup_bytes
        state_used += RUN_MARGIN  # Backup run receipt and event log.
        if job['kind'] == 'linux':
            # Count the padded boot at its logical size even on sparse storage.
            written = 2 * boot_bytes + job.get('bound_boot_bytes', boot_bytes)
            prepare_minimum = 4 * boot_bytes + 512 * MIB
        else:
            written = boot_bytes + Path(job['source']['boot.img']).stat().st_size
            prepare_minimum = 2 * boot_bytes + 128 * MIB
        reserve(runtime_bytes + RUN_MARGIN + max(256 * MIB, prepare_minimum, written), 0)
        state_used += written + RUN_MARGIN
    return required


def _store_requirements(edit, jobs, boot_bytes):
    source = edit['source']
    projected = edit['projected']
    old_store = source['inventory'].get('linux_boot')
    new_store = projected['inventory'].get('linux_boot')
    reinitialized = bool(edit['create_store'] or any(p['role'] == 'store' for p in edit['formats']))

    # The partition writer journals the operation in the old filesystem even
    # if a later step reformats or deletes that filesystem.
    journal_bytes = len(canonical(edit)) + MIB if edit['changes'] else 0
    if old_store and journal_bytes:
        journal_bytes += source['files'].get('registry.json', {}).get('bytes', 0)
    before = journal_bytes if old_store else 0

    after = 0
    if new_store and journal_bytes:
        after += journal_bytes + len(canonical(projected.get('registry')))
        if not reinitialized:
            after += source['files'].get('SHA256SUMS', {}).get('bytes', 0)
    for job in jobs:
        # Each system keeps its boot image and a complete previous boot_a.
        # New Android runtimes store both files without allocating zero padding.
        image_bytes = job.get('bound_boot_bytes', boot_bytes) if job['kind'] == 'linux' else boot_bytes
        after += job.get('store_boot_bytes', image_bytes + boot_bytes) + STORE_MARGIN
    return dict(before=before, after=after, reinitialized=reinitialized)


def plan_capacity(engine, snapshot, backup_record):
    """Freeze extra disk space requirements after the initial backup exists."""
    jobs = [] if snapshot['direct_android'] else sorted(snapshot.get('installations', []), key=lambda job: job['set_default'])
    boot_bytes = engine.device['boot_bytes']
    runtime_bytes = Path(snapshot['connection']['installer_path']).stat().st_size
    # The verified backup records actual partition sizes, including smaller
    # demo artifacts. Allow GPT text growth and the backup's manifest files.
    backup_bytes = sum(backup_record['sizes'].values()) + MIB
    edit = snapshot['edit']
    reuse_layout = snapshot.get('backup_policy') == 'reuse-layout-v1'
    layout_changed = bool(edit.get('deletes') or edit.get('partitions'))
    capacity = dict(host=_host_requirements(jobs, boot_bytes, runtime_bytes, backup_bytes,
                                          reuse_layout, layout_changed),
                    store=_store_requirements(snapshot['edit'], jobs, boot_bytes))
    _check_capacity(engine, snapshot, snapshot['edit']['source'], capacity)
    return capacity


def _require_bytes(label, required, free):
    if type(free) is not int or free < 0:
        raise InstallerError(f'无法确认{label}的可用空间。')
    if free < required:
        raise InstallerError(f'{label}空间不足（需要 {(required + MIB - 1) // MIB} MiB，可用 {free // MIB} MiB）。')


def _same_volume(left, right):
    if left.stat().st_dev == right.stat().st_dev:
        return True
    # Btrfs subvolumes can have separate device IDs but share free space.
    if hasattr(os, 'statvfs'):
        left_fs = getattr(os.statvfs(left), 'f_fsid', 0)
        return bool(left_fs and left_fs == getattr(os.statvfs(right), 'f_fsid', 0))
    return False


def _check_capacity(engine, snapshot, live_state, capacity):
    if snapshot.get('fixed_recovery'):
        # Both images remain in RAM until the entire combined workflow ends.
        # Include the largest subsequent rootfs, not just the staging phase.
        staged=2*snapshot['fixed_recovery']['plan']['image_bytes']
        rootfs=max((j['source'].get('archive_bytes',0) for j in snapshot.get('installations',[]) if j['kind']=='linux'),default=0)
        _require_bytes('设备 RAM',staged+rootfs+512*MIB,engine.device.get('available_memory_bytes'))
    host = capacity['host']
    state_path = Path(engine.state_dir)
    try:
        state_free = shutil.disk_usage(state_path).free
        _require_bytes('电脑运行目录', host['state'], state_free)
        if host['backup']:
            backup_path = Path(snapshot['backup_dir'])
            backup_parent = Path(snapshot['request']['backup_parent']).expanduser().resolve(strict=True)
            if backup_path.parent != backup_parent or not _same_volume(backup_path, backup_parent):
                raise InstallerError('备份位置已改变，请重新准备安装计划。')
            backup_free = shutil.disk_usage(backup_path).free
            _require_bytes('电脑备份目录', host['backup'], backup_free)
            if _same_volume(state_path, backup_path):
                _require_bytes('运行目录与备份目录所在的共享卷', host['shared'], min(state_free, backup_free))
    except OSError as error:
        raise InstallerError('无法读取电脑运行目录或备份目录的可用空间。') from error

    store = capacity['store']
    if store['reinitialized']:
        if store['before']:
            _require_bytes('现有 linux_boot 的分区事务记录', store['before'], live_state.get('free_bytes'))
        # The device creates ext4 with -m 0. Reserve metadata and journal
        # overhead instead of treating raw partition size as usable bytes.
        size = snapshot['edit']['projected']['inventory']['linux_boot']['bytes']
        usable = max(0, size - max(128 * MIB, size // 10))
        _require_bytes('新初始化的 linux_boot', store['after'], usable)
    elif required := max(store['before'], store['after']):
        _require_bytes('现有 linux_boot', required, live_state.get('free_bytes'))


def check_capacity(engine, snapshot, live_state):
    """Recheck free space after confirmation and before any device mutation."""
    _check_capacity(engine, snapshot, live_state, snapshot['capacity'])
