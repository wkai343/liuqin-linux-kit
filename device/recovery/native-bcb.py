# SPDX-License-Identifier: MIT
"""Consume only an unchanged, argument-free Recovery request on a guarded exit."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import struct
import uuid

DIRECTORY = 'run/liuqin-bcb'
BCB_BYTES = 2048
COMMAND_BYTES = 32
SECTOR_BYTES = 4096


def digest(data):
    return hashlib.sha256(data).hexdigest()


def request_kind(data):
    if len(data) < 65536:
        raise ValueError('misc 分区内容不完整。')
    bcb = data[:BCB_BYTES]
    command = bcb[:COMMAND_BYTES].split(bytes(1), 1)[0]
    if not command:
        return 'empty'
    if command != b'boot-recovery':
        return 'other-command'
    recovery = bcb[64:832].split(bytes(1), 1)[0]
    if any(bcb[32:64]) or recovery not in (b'', b'recovery\n') or any(bcb[832:BCB_BYTES]):
        return 'pending-recovery'
    virtual_ab = data[32768:32832]
    if any(virtual_ab) and (virtual_ab[0] not in (1, 2) or
            struct.unpack_from('<I', virtual_ab, 1)[0] != 0x56740ab0 or virtual_ab[5] not in (0, 1)):
        return 'pending-virtual-ab'
    return 'consumed-recovery'


def state_directory(root):
    directory = root / DIRECTORY
    if directory.is_symlink():
        raise RuntimeError('BCB 记录目录无效。')
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory


def save(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16384:
        raise RuntimeError('BCB 快照不是有效的普通文件。')
    return json.loads(path.read_text())


def partition(hardware):
    parts = hardware.inventory()
    hardware.sealed(list(parts.values()))
    part = parts.get('misc')
    if part is None or not 65536 <= part['bytes'] <= 16 * 1024**2:
        raise RuntimeError('misc 分区身份或容量无效。')
    return parts, part


def capture(hardware, value, root=Path('/')):
    """Called once at startup under the shared operation lock; block devices stay sealed."""
    from liuqin_native_install import identity
    directory = state_directory(root)
    snapshot = directory / 'snapshot.json'
    if snapshot.exists() or snapshot.is_symlink():
        previous = load(snapshot)
        if any(previous.get(key) != value[key] for key in ('boot_id', 'serial', 'build_id')):
            raise RuntimeError('BCB 快照属于其他启动会话。')
        return previous
    parts, part = partition(hardware)
    raw = hardware.read(part, 0, 65536)
    result = dict(schema=1, boot_id=value['boot_id'], serial=value['serial'], build_id=value['build_id'],
                  partition=identity(part), kind=request_kind(raw),
                  bcb=base64.b64encode(raw[:BCB_BYTES]).decode(),
                  bcb_sha256=digest(raw[:BCB_BYTES]))
    hardware.sealed(list(parts.values()))
    save(snapshot, (json.dumps(result, sort_keys=True) + '\n').encode())
    return result


def pending_install(hardware, agent, parts):
    store = parts.get('linux_boot')
    if store is None:
        return False
    with hardware.mount_store(store) as directory:
        registry = agent.read_json(directory / 'registry.json')
        resolved = set(registry.get('resolved_android_transactions', []))
        transactions = directory / 'transactions'
        if not transactions.exists():
            return False
        if transactions.is_symlink() or not transactions.is_dir():
            raise RuntimeError('安装事务目录无效。')
        # BCB belongs to Android Recovery. Live Linux/partition work is
        # guarded by the shared lock and the native installation session.
        for entry in transactions.glob('android-*'):
            if entry.is_symlink() or not entry.is_dir():
                return True
            if entry.name.removeprefix('android-') in resolved:
                continue
            if not any((entry / name).is_file() and not (entry / name).is_symlink()
                       for name in ('complete.json', 'rolled-back.json')):
                return True
    return False


def consume(hardware, agent, value, root=Path('/')):
    """The caller holds the shared lock and has checked that installation ended."""
    from liuqin_native_install import identity, session
    snapshot_path = root / DIRECTORY / 'snapshot.json'
    if not snapshot_path.is_file() or snapshot_path.is_symlink():
        return dict(status='preserved', reason='missing-startup-snapshot')
    snapshot = load(snapshot_path)
    if any(snapshot.get(key) != value[key] for key in ('boot_id', 'serial', 'build_id')):
        raise RuntimeError('BCB 快照属于其他启动会话。')
    lease = session(root)
    if lease and lease.get('state') != 'complete':
        return dict(status='preserved', reason='pending-install-session')
    parts, part = partition(hardware)
    before = hardware.read(part, 0, part['bytes'])
    kind = request_kind(before)
    if kind != 'consumed-recovery':
        return dict(status='empty' if kind == 'empty' else 'preserved', reason=kind)
    initial = base64.b64decode(snapshot['bcb'], validate=True)
    if len(initial) != BCB_BYTES or digest(initial) != snapshot.get('bcb_sha256'):
        raise RuntimeError('BCB 启动快照损坏。')
    if (snapshot.get('schema') != 1 or snapshot.get('partition') != identity(part)
            or snapshot.get('kind') != 'consumed-recovery' or before[:BCB_BYTES] != initial):
        return dict(status='preserved', reason='request-changed')
    if pending_install(hardware, agent, parts):
        return dict(status='preserved', reason='pending-install-transaction')
    hardware.sealed(list(parts.values()))
    expected = bytes(COMMAND_BYTES) + before[COMMAND_BYTES:]
    directory = state_directory(root)
    save(directory / 'misc-before.img', before)
    record = dict(schema=1, boot_id=value['boot_id'], serial=value['serial'], build_id=value['build_id'],
                  partition=identity(part), before_sha256=digest(before), after_sha256=digest(expected),
                  command_field_bytes=COMMAND_BYTES, physical_write_bytes=SECTOR_BYTES, status='prepared')
    receipt = directory / 'receipt.json'
    save(receipt, (json.dumps(record, sort_keys=True) + '\n').encode())

    # A process interruption must wait for rollback/readback and resealing.
    # Native-adb-shell forwards disconnects as SIGTERM rather than killing us.
    def interrupted(signum, frame):
        raise InterruptedError('BCB 清理被中断。')
    old_handlers = {signum: signal.signal(signum, interrupted)
                    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        with hardware.node(part['parent'], part['parent_dev']) as parent, hardware.node(part['node'], part['dev']) as fd:
            if agent.ro(parent) != 1 or agent.ro(fd) != 1:
                raise RuntimeError('misc 或其父磁盘未封存。')
            try:
                agent.set_ro(parent, 0)
                for sibling in parts.values():
                    if sibling['parent'] == part['parent']:
                        with hardware.node(sibling['node'], sibling['dev']) as other:
                            if agent.ro(other) != 1:
                                raise RuntimeError('同盘分区未封存。')
                agent.set_ro(fd, 0)
                with hardware.node(part['node'], part['dev'], writable=True) as output:
                    if os.pread(output, part['bytes'], 0) != before:
                        raise RuntimeError('BCB 写入前内容发生变化。')
                    try:
                        hardware.write(output, 0, expected[:SECTOR_BYTES])
                        if os.pread(output, part['bytes'], 0) != expected:
                            raise RuntimeError('BCB 清理回读不符。')
                    except BaseException:
                        for signum in old_handlers:
                            signal.signal(signum, signal.SIG_IGN)
                        record['status'] = 'failed'
                        hardware.write(output, 0, before[:SECTOR_BYTES])
                        record['rollback_verified'] = os.pread(output, part['bytes'], 0) == before
                        save(receipt, (json.dumps(record, sort_keys=True) + '\n').encode())
                        if not record['rollback_verified']:
                            raise RuntimeError('BCB 回退校验失败，请保留 Recovery。')
                        raise
            finally:
                os.sync()
                try:
                    agent.set_ro(fd, 1)
                finally:
                    agent.set_ro(parent, 1)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    hardware.sealed(list(parts.values()))
    if hardware.read(part, 0, part['bytes']) != expected:
        raise RuntimeError('misc 封存后回读不符。')
    record.update(status='cleared', bytes_after_command_identical=True, storage_sealed=True)
    save(receipt, (json.dumps(record, sort_keys=True) + '\n').encode())
    return record
