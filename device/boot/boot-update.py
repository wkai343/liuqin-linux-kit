#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Transactional liuqin boot_a updater. Production accepts no device/path override."""
import argparse
from array import array
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import uuid

from liuqin_artifacts import boot_interface, verify_storage_policy
from liuqin_kernel import digest, parse_contract, verify_installed, module_records, RELEASE
from liuqin_boot_target import parse as parse_target
from liuqin_partition_names import root_name
from liuqin_boot_update_store import StoreUpdate, checksum, encoded

BLKROSET, BLKROGET, BLKGETSIZE64, BLKFLSBUF = 0x125d, 0x125e, 0x80081272, 0x1261


def ro(fd):
    value = array('i', [0]); fcntl.ioctl(fd, BLKROGET, value, True)
    return value[0]


def set_ro(fd, value):
    fcntl.ioctl(fd, BLKROSET, array('i', [value]))
    if ro(fd) != value:
        raise RuntimeError('分区只读状态切换失败。')


def full_hash(path, size):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(1024 * 1024):
            value.update(data); size -= len(data)
    if size < 0:
        raise RuntimeError('镜像超过 boot 分区大小。')
    while size:
        count = min(size, 1024 * 1024); value.update(bytes(count)); size -= count
    return value.hexdigest()


def hardware_key(identity):
    # Linux disk names/device numbers may change on a later kernel boot.
    key = dict(serial=identity['serial'], slot=identity['slot'],
                boot={key: identity['boot'].get(key) for key in ('uuid', 'start', 'size')},
                root={key: identity['root'].get(key) for key in ('uuid', 'start', 'size')})
    if 'store' in identity:
        key.update(store={k: identity['store'][k] for k in ('uuid', 'start', 'size', 'fsuuid')},
                   system_id=identity['system_id'], filesystem=identity['filesystem'], fsuuid=identity['fsuuid'])
    return key


def root_binding(root, regular):
    marker = root/'etc/liuqin-boot-target'
    system_id = root/'etc/liuqin-system-id'
    if not marker.exists() and not marker.is_symlink():
        if system_id.exists() and regular(system_id).read_bytes() != b'unbound\n':
            raise RuntimeError('系统身份缺少配套的根分区绑定。')
        return None
    data = regular(marker).read_bytes()
    identity = regular(system_id).read_bytes()
    # Published roots also carry these templates when installed on userdata.
    if data == identity == b'unbound\n': return None
    target = parse_target(data)
    if identity != (target['os_id']+'\n').encode():
        raise RuntimeError('系统身份与根分区绑定不一致。')
    return target


class Device:
    """Running liuqin, slot A, single root or a registered native-root."""
    def identity(self):
        if os.uname().machine != 'aarch64':
            raise RuntimeError('仅能在真实 liuqin ARM64 系统上更新引导。')
        compatible = Path('/sys/firmware/devicetree/base/compatible').read_bytes().split(b'\0')
        model = Path('/sys/firmware/devicetree/base/model').read_bytes().rstrip(b'\0')
        if b'xiaomi,liuqin' not in compatible and not (
                compatible == [b'qcom,cape-mtp', b'qcom,cape', b'qcom,mtp', b''] and
                model == b'Liuqin based on Qualcomm Technologies, Inc SM8475'):
            raise RuntimeError('仅能在真实 liuqin ARM64 系统上更新引导。')
        if not os.path.samestat(Path('/').stat(), Path('/proc/1/root').stat()):
            raise RuntimeError('禁止在 chroot 中写入引导。')
        words = Path('/proc/cmdline').read_text().split()
        serials = {w.split('=', 1)[1] for w in words if w.startswith('androidboot.serialno=')}
        slots = {w.split('=', 1)[1] for w in words if w.startswith(('androidboot.slot_suffix=', 'slot_suffix='))}
        if len(serials) != 1 or slots != {'_a'}:
            raise RuntimeError('需要明确的设备序列号和 A 槽。')
        serial = serials.pop()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', serial):
            raise RuntimeError('无效设备序列号。')
        target = root_binding(Path('/'), self.trusted)
        root_label = root_name(target['os_id']) if target else 'userdata'
        wanted = {'boot_a', root_label} | ({'linux_boot'} if target else set())
        entries = {}
        for path in Path('/sys/class/block').glob('*'):
            fields = dict(row.split('=', 1) for row in (path / 'uevent').read_text().splitlines() if '=' in row)
            name = fields.get('PARTNAME')
            if name in wanted:
                if name in entries or not fields.get('PARTUUID'):
                    raise RuntimeError('分区身份缺失或重复。')
                entries[name] = dict(name=name, node=path.name, parent=path.resolve().parent.name,
                    uuid=fields['PARTUUID'], dev=(path / 'dev').read_text().strip(),
                    start=int((path / 'start').read_text()), size=int((path / 'size').read_text()) * 512)
        if set(entries) != wanted:
            raise RuntimeError('缺少 boot_a、根分区或 linux_boot。')
        boot, root = entries['boot_a'], entries[root_label]
        if boot['parent'] == root['parent'] or boot['size'] != 192 * 1024 * 1024:
            raise RuntimeError('boot 分区布局不符合已支持的 liuqin 布局。')
        controllers = []
        for part in entries.values():
            node = (Path('/sys/class/block') / part['parent']).resolve()
            controllers.append(next((str(parent) for parent in node.parents
                if (parent / 'driver').is_symlink() and (parent / 'driver').resolve().name == 'ufshcd-qcom'), None))
        if not all(controllers) or len(set(controllers)) != 1:
            raise RuntimeError('目标分区不属于同一内置 Qualcomm UFS 控制器。')
        mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
        roots = [row for row in mounts if row[4] == '/']
        if (len(roots) != 1 or roots[0][3] != '/native-root' or
                roots[0][roots[0].index('-') + 1] not in ('ext4', 'btrfs') or 'rw' not in roots[0][5].split(',')):
            raise RuntimeError('根挂载必须是对应分区的 ext4/Btrfs native-root；构建目录和 chroot 禁止写入。')
        filesystem = roots[0][roots[0].index('-') + 1]
        if filesystem == 'ext4':
            if roots[0][2] != root['dev']: raise RuntimeError('根挂载设备不匹配。')
        else:
            # Btrfs mountinfo has a virtual major/minor. Pin its mount source
            # and require exactly the one registered underlying block device.
            source = roots[0][roots[0].index('-') + 2]
            fsuuid = str(uuid.UUID(self.blkid(root, 'UUID')))
            devices = list((Path('/sys/fs/btrfs')/fsuuid/'devices').glob('*'))
            if (Path(source).resolve() != Path('/dev/'+root['node']) or len(devices) != 1 or
                    (devices[0]/'dev').read_text().strip() != root['dev']):
                raise RuntimeError('仅支持绑定到单一内部 UFS 分区的 Btrfs 根系统。')
        parent_dev = (Path('/sys/class/block') / boot['parent'] / 'dev').read_text().strip()
        if any(row[2] in (boot['dev'], parent_dev) for row in mounts):
            raise RuntimeError('boot 分区或父设备已挂载。')
        result = dict(serial=serial, slot='a', boot=boot, root=root, parent_dev=parent_dev)
        if target:
            fsuuid = self.blkid(root, 'UUID')
            if (root['uuid'] != target['root_partuuid'] or fsuuid != target['root_fsuuid'] or
                    filesystem != target['root_fstype']):
                raise RuntimeError('根文件系统身份与持久绑定不一致。')
            store = entries['linux_boot']
            if (store['parent'] != root['parent'] or store['uuid'] == root['uuid'] or
                    self.blkid(store, 'TYPE') != 'ext4'):
                raise RuntimeError('linux_boot 不属于当前根系统的内部 UFS。')
            store['fsuuid'] = str(uuid.UUID(self.blkid(store, 'UUID')))
            result.update(store=store, system_id=target['os_id'], filesystem=filesystem, fsuuid=fsuuid)
        writable = {root['node'], root['parent']}
        for path in Path('/sys/class/block').glob('sd*'):
            if str(path.resolve()).startswith(controllers[0] + '/'):
                if (path/'ro').read_text().strip() != ('0' if path.name in writable else '1'):
                    raise RuntimeError('内部 UFS 只读保护状态不符合当前根系统。')
        return result

    @staticmethod
    def trusted(path):
        path = Path(path)
        if path.resolve() != path or not path.is_file():
            raise RuntimeError('系统身份文件不能是链接。')
        st = path.stat()
        if st.st_uid != os.geteuid() or st.st_mode & 0o022 or st.st_nlink != 1:
            raise RuntimeError('系统身份文件权限不安全。')
        return path

    @staticmethod
    def blkid(part, field):
        return subprocess.check_output(['blkid', '-p', '-s', field, '-o', 'value',
                                        '/dev/' + part['node']], text=True).strip()

    @contextlib.contextmanager
    def mount_store(self, expected, writable=False):
        if self.identity() != expected:
            raise RuntimeError('引导存储身份发生变化。')
        part = expected['store']
        if any(row.split()[2] == part['dev'] for row in Path('/proc/self/mountinfo').read_text().splitlines()):
            raise RuntimeError('linux_boot 已被其他操作挂载。')
        with self.node(part['node'], part['dev']) as fd:
            if ro(fd) != 1 or struct.unpack('Q', fcntl.ioctl(fd, BLKGETSIZE64, bytes(8)))[0] != part['size']:
                raise RuntimeError('引导存储未封存或容量变化。')
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            directory = None
            mounted = False
            try:
                directory = Path(tempfile.mkdtemp(prefix='liuqin-boot-store-', dir='/run'))
                if writable: set_ro(fd, 0)
                options = 'rw,nodev,nosuid,noexec' if writable else 'ro,noload,nodev,nosuid,noexec'
                subprocess.run(['mount', '-t', 'ext4', '-o', options,
                                '/dev/' + part['node'], str(directory)], check=True)
                mounted = True
                yield directory
            finally:
                try:
                    if mounted:
                        subprocess.run(['umount', str(directory)], check=True)
                        mounted = False
                finally:
                    try:
                        if writable: set_ro(fd, 1)
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                        # Never recursively remove a mountpoint: a failed
                        # umount must leave both the mount and its files alone.
                        if directory is not None and not mounted and not os.path.ismount(directory):
                            directory.rmdir()

    @contextlib.contextmanager
    def node(self, name, dev, writable=False):
        fd = os.open('/dev/' + name, (os.O_RDWR | os.O_EXCL if writable else os.O_RDONLY) | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}' != dev:
                raise RuntimeError('设备节点身份发生变化。')
            yield fd
        finally:
            os.close(fd)

    def read(self, output, expected):
        if self.identity() != expected:
            raise RuntimeError('设备或布局发生变化。')
        with self.node(expected['boot']['node'], expected['boot']['dev']) as fd, Path(output).open('xb') as target:
            remaining = expected['boot']['size']
            while remaining:
                block = os.read(fd, min(remaining, 1024 * 1024))
                if not block: raise RuntimeError('boot 回读不完整。')
                target.write(block); remaining -= len(block)
            target.flush(); os.fsync(target.fileno())

    def write(self, source, expected):
        if self.identity() != expected:
            raise RuntimeError('设备或布局发生变化；未写入。')
        boot = expected['boot']
        with self.node(boot['parent'], expected['parent_dev']) as parent, self.node(boot['node'], boot['dev']) as control:
            if ro(parent) != 1 or ro(control) != 1:
                raise RuntimeError('boot 或父设备未处于只读状态。')
            size = struct.unpack('Q', fcntl.ioctl(control, BLKGETSIZE64, bytes(8)))[0]
            if size != boot['size'] or Path(source).stat().st_size > size:
                raise RuntimeError('boot 容量发生变化。')
            for child in Path('/sys/class/block').glob(boot['parent'] + '*'):
                if (child / 'partition').exists():
                    with self.node(child.name, (child / 'dev').read_text().strip()) as fd:
                        if ro(fd) != 1: raise RuntimeError('同一启动 LUN 中存在其他可写分区。')
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            try:
                set_ro(parent, 0); set_ro(control, 0)
                with self.node(boot['node'], boot['dev'], writable=True) as fd, Path(source).open('rb') as image:
                    remaining = size
                    while remaining:
                        data = image.read(min(remaining, 1024 * 1024)) or bytes(min(remaining, 1024 * 1024))
                        view = memoryview(data)
                        while view:
                            count = os.write(fd, view)
                            if count <= 0: raise RuntimeError('boot 写入不完整。')
                            view = view[count:]
                        remaining -= len(data)
                    os.fsync(fd)
                fcntl.ioctl(control, BLKFLSBUF)
            finally:
                try:
                    try: set_ro(control, 1)
                    finally: set_ro(parent, 1)
                finally: signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def running_release(self):
        return os.uname().release


class Updater:
    def __init__(self, root=Path('/'), device=None):
        self.root = Path(root).resolve()
        self.device = device or Device()
        self.state = self.root / 'var/lib/liuqin/boot-updates'
        self.enabled = self.root / 'etc/liuqin/boot-updates.json'

    def regular(self, path):
        path = Path(path)
        if path.resolve() != path or not path.is_file() or path.is_symlink():
            raise RuntimeError('需要普通文件，禁止符号链接：' + str(path))
        info = path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022 or info.st_nlink != 1:
            raise RuntimeError('文件所有权或写入权限不安全：' + str(path))
        return path

    def directory(self, path):
        path = Path(path)
        if path.resolve() != path:
            raise RuntimeError('状态目录禁止符号链接。')
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
            raise RuntimeError('状态目录所有权或写入权限不安全。')

    def save(self, path, value):
        self.directory(path.parent)
        temporary = path.parent / ('.' + uuid.uuid4().hex)
        try:
            with temporary.open('x') as stream:
                os.chmod(temporary, 0o600)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self):
        self.directory(self.state)
        fd = os.open(self.state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        shared = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime = self.root / 'run'
            runtime.mkdir(mode=0o755, exist_ok=True)
            st = runtime.stat()
            if (runtime.resolve() != runtime or st.st_uid != os.geteuid() or
                    (st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX)):
                raise RuntimeError('运行目录权限不安全。')
            shared = os.open(runtime / 'liuqin-android-operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            st = os.fstat(shared)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022 or st.st_nlink != 1:
                raise RuntimeError('共享引导操作锁权限不安全。')
            fcntl.flock(shared, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            if shared is not None: os.close(shared)
            os.close(fd)

    def store(self, identity):
        return StoreUpdate(self, identity) if 'store' in identity else None

    @staticmethod
    def padded_hash(image, identity):
        return full_hash(image, identity['boot']['size'])

    def current(self):
        path = self.state / 'current.json'
        if not path.exists(): return None
        value = json.loads(self.regular(path).read_text())
        if (value.get('schema') != 1 or not re.fullmatch(r'[0-9a-f]{32}', value.get('transaction', '')) or
                value.get('state') not in ('prepared', 'writing', 'restoring', 'verified', 'confirmed', 'failed', 'rolled-back', 'rollback-failed') or
                not all(re.fullmatch(r'[0-9a-f]{64}', value.get(key, '')) for key in ('old_sha256', 'new_sha256'))):
            raise RuntimeError('更新记录无效，禁止写入。')
        return value

    def check_root_filesystem(self, info):
        marker = self.root / 'etc/liuqin-root-filesystem'
        if marker.exists() or marker.is_symlink():
            filesystem = self.regular(marker).read_text().strip()
            if filesystem not in info['filesystems']:
                raise RuntimeError('候选内核不支持当前根文件系统：' + filesystem)

    def candidate(self, release):
        if not re.fullmatch(RELEASE, release): raise RuntimeError('无效内核版本。')
        path = self.regular(self.root / 'usr/lib/liuqin/boot-images' / release / 'boot.img')
        info = boot_interface(path)
        verify_storage_policy(self.root,info.get('storage_policy_version',0))
        self.check_root_filesystem(info)
        if info['profile'] != b'native-updatable\n':
            raise RuntimeError('仅支持当前启动接口的镜像。')
        identity = verify_installed(self.root, info['contract'])
        if identity['release'] != release: raise RuntimeError('内核包版本不匹配。')
        return path, info['contract']

    def check_enabled(self, identity):
        config = json.loads(self.regular(self.enabled).read_text())
        if config.get('schema') != 1 or hardware_key(config['identity']) != hardware_key(identity):
            raise RuntimeError('更新配置与当前设备或布局不匹配。')

    def verify_root_for_boot(self, info):
        verify_storage_policy(self.root,info.get('storage_policy_version',0))
        self.check_root_filesystem(info)
        actual = root_binding(self.root, self.regular)
        binding = info.get('boot_target')
        if actual:
            if not binding or any(binding[k] != v for k, v in actual.items() if k != 'boot_generation'):
                raise RuntimeError('引导镜像绑定了其他根系统。')
        elif binding:
            raise RuntimeError('单根系统不能接收多系统绑定镜像。')
        return verify_installed(self.root, info['contract'])['release']

    def inspect_live(self, identity, output):
        self.device.read(output, identity)
        info = boot_interface(output)
        release = self.verify_root_for_boot(info)
        if release != self.device.running_release():
            raise RuntimeError('运行中的内核与 boot_a 不一致；请先完成上次更新并重启。')
        return release

    def enable(self):
        with self.lock():
            identity = self.device.identity()
            probe = self.state / ('probe-' + uuid.uuid4().hex)
            try:
                self.inspect_live(identity, probe)
                store = self.store(identity)
                if store:
                    with self.device.mount_store(identity) as root:
                        _, current = store.inspect(root)
                        if self.padded_hash(root/current['image'], identity) != digest(probe):
                            raise RuntimeError('boot_a 与登记的默认引导不一致。')
            finally: probe.unlink(missing_ok=True)
            self.save(self.enabled, dict(schema=1, identity=identity))
        return {'enabled': True}

    def remove_enabled(self):
        self.directory(self.enabled.parent)
        self.enabled.unlink(missing_ok=True)
        fd = os.open(self.enabled.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def disable(self):
        with self.lock(): self.remove_enabled()
        return {'enabled': False}

    def apply(self, release):
        with self.lock():
            identity = self.device.identity()
            self.check_enabled(identity)
            image, contract = self.candidate(release)
            store = self.store(identity)
            if not store and boot_interface(image).get('boot_target'):
                raise RuntimeError('单根更新需要未绑定的发布镜像。')
            previous = self.current()
            if previous and hardware_key(previous['identity']) != hardware_key(identity):
                raise RuntimeError('更新记录属于其他设备或布局。')
            if previous and previous['state'] in ('prepared', 'writing', 'restoring', 'failed', 'rollback-failed'):
                raise RuntimeError('存在中断的更新；先运行 recover，不重新备份可能损坏的 boot。')
            expected = full_hash(image, identity['boot']['size'])
            if previous and previous['state'] == 'verified':
                same = previous.get('source_sha256') == digest(image) if store else previous['new_sha256'] == expected
                if previous['release'] == release and same:
                    if store: store.verify(previous)
                    self.verify_write(self.state / previous['transaction'], previous['new_sha256'], identity)
                    return {'state': 'verified', 'message': '已写入，等待重启确认。'}
                raise RuntimeError('先重启并确认上次内核更新。')
            if shutil.disk_usage(self.state).free < identity['boot']['size'] * 3 + 32 * 1024 * 1024:
                raise RuntimeError('没有足够空间保存引导备份和回读。')
            transaction = uuid.uuid4().hex
            directory = self.state / transaction; self.directory(directory)
            snapshot = directory / 'candidate.img'
            backup = directory / 'previous.img'
            old_release = self.inspect_live(identity, backup)
            old_digest = digest(backup)
            source_sha = digest(image)
            store_plan = None
            if store:
                store_plan = store.prepare(image, directory, str(uuid.UUID(transaction)), old_digest)
                if source_sha != digest(image): raise RuntimeError('镜像在绑定期间发生变化。')
            else:
                shutil.copyfile(image, snapshot)
                if digest(snapshot) != source_sha: raise RuntimeError('镜像在暂存期间发生变化。')
            frozen = boot_interface(snapshot)
            if frozen['profile'] != b'native-updatable\n' or frozen['contract'] != contract:
                raise RuntimeError('暂存镜像的内核身份发生变化。')
            with snapshot.open('rb') as stream: os.fsync(stream.fileno())
            expected = full_hash(snapshot, identity['boot']['size'])
            if old_digest == expected:
                shutil.rmtree(directory)
                return {'state': 'unchanged', 'release': release}
            record = dict(schema=1, transaction=transaction, identity=identity, release=release,
                          old_release=old_release, old_sha256=old_digest, new_sha256=expected,
                          contract_sha256=hashlib.sha256(contract).hexdigest(), source_sha256=source_sha, state='prepared')
            if store_plan:
                record.update(store_update=store_plan, store_plan_sha256=checksum(encoded(store_plan)))
            self.save(directory / 'receipt.json', record)
            self.save(self.state / 'current.json', record)
            try:
                record['state'] = 'writing'; self.save(self.state / 'current.json', record)
                verify_installed(self.root, contract)
                if store: store.stage(record, directory)
                self.device.write(snapshot, identity)
                self.verify_write(directory, expected, identity)
                if store:
                    store.commit(record)
                    store.verify(record)
                record['state'] = 'verified'
            except BaseException:
                record['state'] = 'failed'; self.save(self.state / 'current.json', record)
                try:
                    self.restore(record)
                    record['state'] = 'rolled-back'
                except BaseException:
                    record['state'] = 'rollback-failed'
                self.save(self.state / 'current.json', record)
                self.save(directory / 'receipt.json', record)
                raise
            self.save(self.state / 'current.json', record)
            self.save(directory / 'receipt.json', record)
            return record

    def verify_write(self, directory, expected, identity):
        path = directory / ('readback-' + uuid.uuid4().hex)
        try:
            self.device.read(path, identity)
            if digest(path) != expected: raise RuntimeError('boot 整分区回读不一致。')
        finally: path.unlink(missing_ok=True)

    def restore(self, record):
        transaction = record['transaction']
        if not re.fullmatch(r'[0-9a-f]{32}', transaction): raise RuntimeError('无效更新记录。')
        identity = self.device.identity()
        if hardware_key(identity) != hardware_key(record['identity']): raise RuntimeError('恢复记录不属于当前设备布局。')
        directory = self.state / transaction
        backup = self.regular(directory / 'previous.img')
        if backup.stat().st_size != record['identity']['boot']['size'] or digest(backup) != record['old_sha256']:
            raise RuntimeError('旧引导备份损坏，禁止恢复。')
        boot = boot_interface(backup)
        self.verify_root_for_boot(boot)
        store = self.store(identity)
        if store:
            if 'store_update' not in record: raise RuntimeError('多系统恢复缺少对应的登记备份。')
            store.can_restore(record)
            if record['state'] in ('verified', 'confirmed', 'rolled-back'):
                probe = directory/('restore-check-' + uuid.uuid4().hex)
                try:
                    self.device.read(probe, identity)
                    if digest(probe) not in (record['old_sha256'], record['new_sha256']):
                        raise RuntimeError('boot_a 已被其他操作修改，拒绝覆盖。')
                finally: probe.unlink(missing_ok=True)
        # This must be durable before the first rollback write too. Otherwise
        # a power loss during rollback leaves a "confirmed" receipt next to a
        # partial boot and cannot be distinguished from an unrelated overwrite.
        record['state'] = 'restoring'
        self.save(self.state / 'current.json', record)
        self.device.write(backup, identity)
        self.verify_write(directory, record['old_sha256'], identity)
        if store: store.restore(record)

    def rollback(self):
        with self.lock():
            identity = self.device.identity()
            if self.enabled.exists(): self.check_enabled(identity)
            record = self.current()
            if not record: raise RuntimeError('没有可恢复的更新记录。')
            try:
                self.restore(record)
            except BaseException:
                if record['state'] == 'restoring':
                    record['state'] = 'rollback-failed'
                    self.save(self.state / 'current.json', record)
                    self.save(self.state / record['transaction'] / 'receipt.json', record)
                raise
            if record.get('migration'): self.remove_enabled()
            record['state'] = 'rolled-back'
            self.save(self.state / 'current.json', record)
            self.save(self.state / record['transaction'] / 'receipt.json', record)
            return record

    def confirm(self):
        with self.lock():
            record = self.current()
            if not record or record['state'] != 'verified': return {'state': 'nothing-pending'}
            identity = self.device.identity(); self.check_enabled(identity)
            if hardware_key(identity) != hardware_key(record['identity']) or self.device.running_release() != record['release']:
                raise RuntimeError('尚未启动到待确认的内核。')
            _, contract = self.candidate(record['release'])
            if hashlib.sha256(contract).hexdigest() != record['contract_sha256']:
                raise RuntimeError('待确认内核的配套模块身份已改变。')
            self.verify_write(self.state / record['transaction'], record['new_sha256'], identity)
            store = self.store(identity)
            if store: store.verify(record)
            record['state'] = 'confirmed'
            self.save(self.state / 'current.json', record)
            self.save(self.state / record['transaction'] / 'receipt.json', record)
            return record

    def check_version(self, release, image_sha, modules_sha):
        if not re.fullmatch(RELEASE, release): raise RuntimeError('无效内核版本。')
        if not all(re.fullmatch(r'[0-9a-f]{64}', value) for value in (image_sha, modules_sha)):
            raise RuntimeError('无效内核构建身份。')
        found = False
        for name in ('boot.contract', 'modules.contract'):
            path = self.root / 'usr/share/liuqin/kernels' / release / name
            if path.exists():
                found = True
                info = parse_contract(self.regular(path).read_bytes())
                if info != dict(release=release, image_sha256=image_sha, modules_sha256=modules_sha):
                    raise RuntimeError('相同内核 release 已对应另一组内核或模块；请使用新版本后缀。')
        if not found and (self.root / 'usr/lib/modules' / release).exists():
            raise RuntimeError('旧模块目录缺少版本化身份，拒绝覆盖；请使用新的内核 release。')

    def plan(self, release):
        with self.lock():
            image, contract = self.candidate(release)
            identity = self.device.identity()
            with tempfile.TemporaryDirectory(prefix='plan-', dir=self.state) as temporary:
                directory = Path(temporary); probe = directory / 'previous.img'
                running = self.inspect_live(identity, probe)
                result = dict(device=identity, running_release=running, target_release=release,
                              old_sha256=digest(probe), enabled=self.enabled.exists(), root_contract=parse_contract(contract))
                store = self.store(identity)
                if store:
                    plan = store.prepare(image, directory, str(uuid.uuid4()), digest(probe))
                    result.update(system_id=identity['system_id'], generation=plan['generation'],
                                  writes=['boot_a', 'linux_boot', '/etc/liuqin-boot-target'])
                    image = directory/'candidate.img'
                elif boot_interface(image).get('boot_target'):
                    raise RuntimeError('单根更新需要未绑定的发布镜像。')
                result['new_sha256'] = self.padded_hash(image, identity)
                if store and result['new_sha256'] == result['old_sha256']:
                    result['writes'] = []
                return result

    def can_remove(self, release):
        protected = {self.device.running_release()}
        record = self.current()
        if record: protected.update((record['release'], record['old_release']))
        if root_binding(self.root, self.regular):
            with self.lock():
                identity = self.device.identity()
                if not self.store(identity): raise RuntimeError('绑定系统缺少引导存储。')
                protected.update(self.store(identity).protected_releases())
        if release in protected: raise RuntimeError('不能移除正在运行或恢复记录仍需要的内核/模块。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'check', 'plan', 'enable', 'disable', 'apply', 'hook', 'confirm', 'rollback', 'recover', 'check-version', 'can-remove'))
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    if os.geteuid() != 0: parser.error('请以 root 运行。')
    os.umask(0o077)
    def interrupted(_number, _frame):
        raise InterruptedError('更新被中断。')
    for number in (signal.SIGHUP, signal.SIGTERM): signal.signal(number, interrupted)
    updater = Updater()
    action, fields = args.action, args.arguments
    counts = {'check': 1, 'plan': 1, 'apply': 1, 'hook': 1, 'check-version': 3, 'can-remove': 1}
    if len(fields) != counts.get(action, 0): parser.error('参数数量不正确。')
    if fields and not re.fullmatch(RELEASE, fields[0]): parser.error('无效内核版本。')
    if action == 'status': result = dict(enabled=updater.enabled.exists(), transaction=updater.current())
    elif action == 'check':
        image, contract = updater.candidate(fields[0]); result = dict(image=str(image), **parse_contract(contract))
    elif action == 'enable': result = updater.enable()
    elif action == 'disable': result = updater.disable()
    elif action == 'plan': result = updater.plan(fields[0])
    elif action in ('apply', 'hook'):
        if action == 'hook' and (not updater.enabled.exists() or not (updater.root / 'usr/lib/liuqin/boot-images' / fields[0] / 'boot.img').is_file()):
            result = {'state': 'disabled-or-unrelated', 'message': '仅安装内核文件；引导更新尚未启用或不属于 liuqin。'}
        else: result = updater.apply(fields[0])
    elif action == 'confirm': result = updater.confirm() if updater.enabled.exists() else {'state': 'disabled'}
    elif action in ('rollback', 'recover'): result = updater.rollback()
    elif action == 'check-version': result = updater.check_version(*fields) or {'state': 'compatible'}
    else: result = updater.can_remove(fields[0]) or {'state': 'removable'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('引导更新停止：' + str(error), file=sys.stderr)
        sys.exit(1)
