#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded Android writes in the RAM installer. The CLI has no device/path override."""
from __future__ import annotations
from array import array
import base64
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liuqin_android import (ANDROID_PARTITIONS, FORMAT_PARTITIONS, CHUNK_BYTES, canonical,
    check_registry_transition, format_settings, linux_userdata_signature, load_profile, sha, validate_images, write_partitions,
    BOOT_STORE_CHUNK, sparse_boot_bytes, sparse_boot_chunks)
from liuqin_partition_names import is_root_name, validate_names

BLKROSET, BLKROGET, BLKGETSIZE64 = 0x125d, 0x125e, 0x80081272


def ro(fd):
    value = array('i', [0]); fcntl.ioctl(fd, BLKROGET, value, True); return value[0]


def set_ro(fd, value):
    fcntl.ioctl(fd, BLKROSET, array('i', [value]))
    if ro(fd) != value: raise RuntimeError('分区只读保护切换失败。')


def clear_btrfs_superblocks(fd, size):
    # Reusing a UUID can leave mkfs holding the old filesystem open. Clear its
    # superblocks before mkfs scans it, retaining the block-device busy check.
    exclusive = os.open('/proc/self/fd/'+str(fd), os.O_RDWR | os.O_EXCL | os.O_CLOEXEC)
    try:
        for offset in (64*1024, 64*1024**2, 256*1024**3):
            if offset+4096 <= size:
                if os.pwrite(exclusive, bytes(4096), offset) != 4096:
                    raise RuntimeError('清理旧 Btrfs 超级块时写入不完整。')
        os.fsync(exclusive)
    finally: os.close(exclusive)


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise ValueError('无效的系统登记文件。')
    return json.loads(path.read_text())


def atomic(path, content):
    pending = path.with_name('.'+path.name+'.pending-'+uuid.uuid4().hex)
    with pending.open('xb') as out:
        os.chmod(pending, 0o600); out.write(content); out.flush(); os.fsync(out.fileno())
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def write_sparse_boot(path, chunks, size, digest):
    with path.open('xb') as out:
        os.fchmod(out.fileno(), 0o600)
        for offset, data in sparse_boot_chunks(chunks):
            out.seek(offset)
            out.write(data)
        out.truncate(size)
        out.flush()
        os.fsync(out.fileno())
    if sha(path) != digest:
        raise RuntimeError('引导镜像或回退快照回读校验失败。')


class Hardware:
    sys_root = Path('/sys/class/block')
    def __init__(self, boot_id, serial):
        recovery_session=Path('/etc/liuqin-recovery-session.json')
        recovery=json.loads(recovery_session.read_text()) if recovery_session.is_file() else None
        device_matches=(recovery.get('device')=='liuqin' and recovery.get('serial')==serial and
            recovery.get('boot_id')==boot_id and recovery.get('slot')=='a' and
            Path('/run/maintenance-ready').read_text().strip()==boot_id) if recovery else (
            b'xiaomi,liuqin' in Path('/sys/firmware/devicetree/base/compatible').read_bytes().split(b'\0'))
        if not recovery and Path('/etc/liuqin-native-recovery.json').is_file():
            from liuqin_board_identity import identify
            native=json.loads(Path('/etc/liuqin-native-recovery.json').read_text())
            from liuqin_native_install import capable
            device_matches=(native.get('interface')=='liuqin-native-recovery-v1' and
                (native.get('inspection_only') is True or capable(native)) and identify()['device']=='liuqin')
        if (os.geteuid() != 0 or os.uname().machine != 'aarch64' or
            not device_matches or
            Path('/etc/liuqin-installer').read_text().strip() != 'liuqin' or
            Path('/etc/liuqin-android-installer').read_text().strip() != '1' or
            Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot_id):
            raise RuntimeError('不是已验证的 liuqin RAM 安装会话。')
        words = Path('/proc/cmdline').read_text().split()
        serials = {w.split('=',1)[1] for w in words if w.startswith('androidboot.serialno=')}
        slots = {w.split('=',1)[1] for w in words if w.startswith(('androidboot.slot_suffix=', 'slot_suffix='))}
        if recovery:
            # Android 12+ may move these values into bootconfig/properties.
            # The gateway validated the actual Recovery identity before creating
            # this private RAM file; conflicting command-line values still fail.
            if serials and serials!={serial} or slots and slots!={'_a'}:raise RuntimeError('Recovery 身份与内核启动参数冲突。')
            serials={recovery['serial']};slots={'_'+recovery['slot']}
        if serials != {serial} or slots != {'_a'}:
            raise RuntimeError('Android 操作要求当前设备身份明确且处于 A 槽。')
        self.serial = serial
        self.work = Path('/run/liuqin-android')
        self.work.mkdir(mode=0o700, exist_ok=True)

    def inventory(self, allow_unknown_store=False):
        from liuqin_block_identity import partition_fields
        result = {}; controllers = set(); identifiers = set();gpt_cache={}
        for path in self.sys_root.glob('sd*'):
            if not (path/'partition').is_file(): continue
            real = path.resolve()
            controller = next((str(p) for p in real.parents if (p/'driver').is_symlink()
                and (p/'driver').resolve().name == 'ufshcd-qcom'), None)
            if not controller: continue
            fields = partition_fields(path,gpt_cache)
            name = fields.get('PARTNAME'); partuuid = fields.get('PARTUUID', '').lower()
            if not name: continue
            if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_-]*', name) or not re.fullmatch(r'sd[a-z][0-9]+', path.name):
                raise RuntimeError('分区名称或设备节点无效：'+repr(name)+' / '+path.name)
            if str(uuid.UUID(partuuid)) != partuuid or name in result or partuuid in identifiers:
                raise RuntimeError('分区身份缺失或重复。')
            identifiers.add(partuuid); controllers.add(controller)
            parent = real.parent.name
            if not re.fullmatch(r'sd[a-z]', parent) or int((path.parent/parent/'queue/logical_block_size').read_text()) != 4096:
                raise RuntimeError('不支持的 UFS 扇区大小。')
            result[name] = dict(name=name, node=path.name, dev=(path/'dev').read_text().strip(),
                parent=parent, parent_dev=(path.parent/parent/'dev').read_text().strip(),
                number=int((path/'partition').read_text()), partuuid=partuuid,
                start=int((path/'start').read_text())*512, bytes=int((path/'size').read_text())*512)
        if len(controllers) != 1: raise RuntimeError('分区不属于同一个内部 UFS 控制器。')
        for parent in {p['parent'] for p in result.values()}:
            previous = 0
            for part in sorted((p for p in result.values() if p['parent']==parent), key=lambda p:p['start']):
                if part['start'] < previous or part['bytes'] <= 0 or part['bytes'] % 4096:
                    raise RuntimeError('内核分区范围重叠或不完整。')
                previous = part['start']+part['bytes']
        for part in result.values():
            if part['name']=='linux_boot' or is_root_name(part['name']):
                header = self.read(part, 0, min(65608, part['bytes']))
                if header[1080:1082] == b'\x53\xef':
                    part.update(filesystem='ext4', fsuuid=str(uuid.UUID(bytes=header[1128:1144])))
                    if part['name'] == 'linux_boot' and (struct.unpack_from('<H', header, 1082)[0] != 1 or struct.unpack_from('<I', header, 1120)[0] & 4):
                        if not allow_unknown_store: raise RuntimeError('引导存储需要离线文件系统恢复，未执行写入。')
                        part['filesystem']='unsafe-ext4'
                elif header[65600:65608] == b'_BHRfS_M':
                    part.update(filesystem='btrfs', fsuuid=str(uuid.UUID(bytes=header[65568:65584])))
                elif part['name'] == 'linux_boot' and not allow_unknown_store: raise RuntimeError('引导存储文件系统无法识别。')
                else: part.update(filesystem='unformatted', fsuuid=None)
        return result

    def gpt(self):
        from liuqin_layout import parse_print
        report=subprocess.run(['/usr/sbin/sgdisk','-p','/dev/sda'],check=True,capture_output=True,text=True).stdout
        details={str(p.number):subprocess.run(['/usr/sbin/sgdisk','-i',str(p.number),'/dev/sda'],
            check=True,capture_output=True,text=True).stdout for p in parse_print(report).partitions}
        return report,details

    def append_gpt(self, plan):
        from liuqin_native_install import authorize_gpt
        authorize_gpt(plan)
        from liuqin_partitions import partition_arguments
        # Validate independently before opening the disk or changing RO flags.
        validate_names(plan['source']['inventory'])
        validate_names(p['name'] for p in plan['partitions'])
        validate_names(self.inventory(allow_unknown_store=True))
        before=plan['source']['inventory'];self.sealed(list(before.values()))
        parent=next(p for p in before.values() if p['parent']=='sda')
        with self.node('sda',parent['parent_dev']) as fd:
            original_error=None
            try:
                # Probe while the disk is still read-only. Busy Recovery clients
                # must be detected before changing the on-disk GPT.
                fcntl.ioctl(fd,0x125f)
                self.refresh_partition_nodes()
                self.seal_data_partitions()
                from liuqin_partitions import stable_partition
                refreshed=self.inventory(allow_unknown_store=True)
                if set(refreshed)!=set(before) or any(stable_partition(refreshed[n])!=stable_partition(p) for n,p in before.items()):
                    raise RuntimeError('预检刷新后分区身份改变，未写入 GPT。')
                from liuqin_native_install import mark_write
                mark_write();set_ro(fd,0)
                arguments=[*['--delete='+str(p['number']) for p in plan.get('deletes',[])],*partition_arguments(plan)]
                subprocess.run(['/usr/sbin/sgdisk',*arguments,'/proc/self/fd/'+str(fd)],
                    pass_fds=(fd,),check=True,capture_output=True)
                print('liuqin-partition-progress '+json.dumps(dict(stage='gpt_written')),flush=True)
                os.sync();fcntl.ioctl(fd,0x125f) # BLKRRPART; failure stops before any formatting.
                print('liuqin-partition-progress '+json.dumps(dict(stage='kernel_refreshed')),flush=True)
            except BaseException as error:
                original_error=error;raise
            finally:
                os.sync();set_ro(fd,1)
                try:self.refresh_partition_nodes();self.seal_data_partitions()
                except Exception as cleanup:
                    if original_error is None:raise
                    original_error.add_note('分区写入已停止；刷新/只读恢复另报：'+str(cleanup))

    def refresh_partition_nodes(self):
        if Path('/etc/liuqin-recovery-session.json').is_file():
            subprocess.run(['/usr/bin/python3','-I','-B','/usr/lib/liuqin/recovery-session.py','nodes-after-gpt'],check=True)
        else:subprocess.run(['/bin/busybox','mdev','-s'],check=True,capture_output=True)

    def seal_data_partitions(self):
        for entry in self.sys_root.glob('sda*/dev'):
            node=entry.parent.name
            if node!='sda' and not re.fullmatch(r'sda[0-9]+',node):continue
            with self.node(node,entry.read_text().strip()) as child:set_ro(child,1)

    def initialize_new_partition(self, part, planned):
        if part['name']=='linux_boot' and planned['role']=='store':
            with self.writable(part,{'linux_boot'}) as fd:
                subprocess.run(['/usr/sbin/mkfs.ext4','-F','-L','LQ_BOOT','-U',planned['fsuuid'],'-m','0',
                    '-O','^metadata_csum_seed,^orphan_file','/proc/self/fd/'+str(fd)],pass_fds=(fd,),check=True,capture_output=True)
        elif part['name']=='userdata' and planned['role']=='android':
            with self.writable(part,{'userdata'}) as fd:self.write(fd,0,bytes(16*1024**2))
        else:raise ValueError('只允许初始化本次新建的 userdata 或引导存储。')

    def format_root(self, part, filesystem, fsuuid):
        if not is_root_name(part['name']) or filesystem not in ('ext4','btrfs'):
            raise ValueError('不是受管理的 rootfs 格式化目标。')
        if str(uuid.UUID(fsuuid))!=fsuuid:raise ValueError('文件系统 UUID 无效。')
        with self.writable(part,{part['name']}) as fd:
            if filesystem=='btrfs': clear_btrfs_superblocks(fd, part['bytes'])
            args=['/usr/sbin/mkfs.ext4','-F','-U',fsuuid,'-m','0'] if filesystem=='ext4' else ['/usr/sbin/mkfs.btrfs','-f','-K','-U',fsuuid,'-d','single','-m','dup','-s','4096']
            subprocess.run([*args,'/proc/self/fd/'+str(fd)],pass_fds=(fd,),check=True,capture_output=True)

    @contextlib.contextmanager
    def node(self, node, dev, writable=False):
        path = Path('/dev')/node
        fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISBLK(metadata.st_mode) or f'{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}' != dev:
                raise RuntimeError('块设备路径身份变化。')
            yield fd
        finally: os.close(fd)

    def idle(self, part):
        devices = {part['dev'], part['parent_dev']}
        if any(line.split()[2] in devices for line in Path('/proc/self/mountinfo').read_text().splitlines()):
            raise RuntimeError('目标分区或整个磁盘已挂载：'+part['name'])

    def sealed(self, parts):
        for parent, number in {(part['parent'],part['parent_dev']) for part in parts}:
            with self.node(parent,number) as fd:
                if ro(fd) != 1: raise RuntimeError('UFS 父磁盘不再只读。')
        for part in parts:
            self.idle(part)
            with self.node(part['node'], part['dev']) as fd:
                if ro(fd) != 1: raise RuntimeError('分区不再只读：'+part['name'])
                if struct.unpack('<Q', fcntl.ioctl(fd, BLKGETSIZE64, bytes(8)))[0] != part['bytes']:
                    raise RuntimeError('分区大小发生变化。')

    def read(self, part, offset, length):
        if offset < 0 or length < 0 or offset+length > part['bytes']: raise ValueError('读取越界。')
        with self.node(part['node'], part['dev']) as fd:
            data = os.pread(fd, length, offset)
        if len(data) != length: raise RuntimeError('分区读取不完整。')
        return data

    def digest(self, part, offset=0, length=None):
        length = part['bytes']-offset if length is None else length
        if offset < 0 or length < 0 or offset+length > part['bytes']: raise ValueError('校验范围越界。')
        digest = hashlib.sha256()
        with self.node(part['node'], part['dev']) as fd:
            while length:
                data = os.pread(fd, min(length, 4*1024**2), offset)
                if not data: raise RuntimeError('分区回读不完整。')
                digest.update(data); offset += len(data); length -= len(data)
        return digest.hexdigest()

    @contextlib.contextmanager
    def writable(self, part, allowed):
        from liuqin_native_install import authorize_target
        authorize_target(part)
        if part['name'] not in allowed: raise RuntimeError('拒绝打开白名单以外的分区。')
        self.sealed([part])
        with self.node(part['parent'], part['parent_dev']) as parent, self.node(part['node'], part['dev']) as fd:
            if ro(parent) != 1: raise RuntimeError('父磁盘不再只读。')
            try:
                from liuqin_native_install import mark_write
                mark_write()
                set_ro(parent, 0); set_ro(fd, 0)
                with self.node(part['node'], part['dev'], True) as output: yield output
            finally:
                os.sync(); set_ro(fd, 1); set_ro(parent, 1)

    def write(self, fd, offset, data):
        while data:
            count = os.pwrite(fd, data, offset)
            if count <= 0: raise RuntimeError('分区写入不完整。')
            offset += count; data = data[count:]
        os.fsync(fd)

    @contextlib.contextmanager
    def mount_store(self, part, writable=False):
        directory = Path(tempfile.mkdtemp(prefix='store-', dir=self.work))
        def mounted():
            subprocess.run(['mount','-t','ext4','-o', 'rw,nosuid,nodev,noexec' if writable else 'ro,noload,nosuid,nodev,noexec',
                            '/dev/'+part['node'],str(directory)],check=True,capture_output=True)
        try:
            if writable:
                with self.writable(part, {'linux_boot'}):
                    mounted()
                    try: yield directory
                    finally: subprocess.run(['umount',str(directory)],check=True,capture_output=True)
            else:
                self.sealed([part]); mounted()
                try: yield directory
                finally: subprocess.run(['umount',str(directory)],check=True,capture_output=True)
        finally:
            # Do not recurse into a mount whose unmount failed.
            if not os.path.ismount(directory): directory.rmdir()

    def verify_linux(self, part, system):
        from liuqin_boot_target import encode
        self.sealed([part])
        directory = Path(tempfile.mkdtemp(prefix='verify-root-',dir=self.work))
        filesystem = system['target']['root_fstype']
        options = 'ro,noload,nosuid,nodev,noexec' if filesystem == 'ext4' else 'ro,rescue=nologreplay,subvolid=5,nosuid,nodev,noexec'
        try:
            subprocess.run(['mount','-t',filesystem,'-o',options,'/dev/'+part['node'],str(directory)],check=True,capture_output=True)
            try:
                root = directory/'native-root'
                for name, expected in (('liuqin-system-id',(system['id']+'\n').encode()),
                                       ('liuqin-boot-target',encode(system['target']))):
                    path = root/'etc'/name
                    if path.is_symlink() or path.read_bytes() != expected:
                        raise RuntimeError('已安装 Linux 的根绑定不匹配。')
            finally: subprocess.run(['umount',str(directory)],check=True,capture_output=True)
        finally:
            if not os.path.ismount(directory): directory.rmdir()

    def check_format_tools(self):
        for name in ('/usr/sbin/mkfs.f2fs', '/usr/sbin/mkfs.ext4'):
            result = subprocess.run([name, '-V'], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError('Android 格式化工具不可用，未开始 ROM 写入：'+name)

    def format(self, part, settings):
        if settings != format_settings(part['name'],settings.get('filesystem')): raise ValueError('格式化参数不匹配固定策略。')
        with self.writable(part, FORMAT_PARTITIONS) as fd:
            executable = '/usr/sbin/mkfs.f2fs' if settings['filesystem']=='f2fs' else '/usr/sbin/mkfs.ext4'
            result = subprocess.run([executable,*settings['mkfs_args'],'-U',str(uuid.uuid4()),'/proc/self/fd/'+str(fd)],
                                    pass_fds=(fd,),capture_output=True,text=True)
            if result.returncode: raise RuntimeError('Android 数据初始化失败：'+result.stderr[-1000:])
        header = self.read(part,0,4096)
        if settings['filesystem']=='f2fs' and header[1024:1028] != b'\x10\x20\xf5\xf2':
            raise RuntimeError('userdata F2FS 超级块回读失败。')
        if settings['filesystem']=='ext4' and header[1080:1082] != b'\x53\xef':
            raise RuntimeError('metadata ext4 超级块回读失败。')


def files_snapshot(root):
    files = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode): continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('引导存储包含链接或特殊文件。')
        if len(files) >= 4096: raise RuntimeError('引导存储文件过多。')
        files[path.relative_to(root).as_posix()] = dict(bytes=info.st_size,sha256=sha(path))
    return files


def preserve_files(root, expected, mutable=()):
    for name, entry in expected.items():
        if name in mutable: continue
        path = root/name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != entry['bytes'] or sha(path) != entry['sha256']):
            raise RuntimeError('已有引导存储文件变化：'+name)


def fetch(url, length, digest):
    if not url.startswith('http://') or not 0 < length <= 192*1024**2:
        raise ValueError('传输请求无效。')
    memory = next((int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')), 0)
    if memory < length+128*1024**2: raise RuntimeError('RAM 空间不足以校验本次传输。')
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(length+1)
    if len(data)!=length or hashlib.sha256(data).hexdigest()!=digest:
        raise RuntimeError('传输数据校验失败，未写入本块。')
    return data


class Operations:
    def __init__(self, device, profile=None):
        self.device = device; self.profile = profile or load_profile()

    def inspect(self, mode):
        inventory = self.device.inventory()
        required = {'linux_boot','boot_a','boot_b','metadata'}
        if mode != 'layout':
            required |= write_partitions(self.profile) | FORMAT_PARTITIONS | {'modem_a','bluetooth_a','dsp_a'}
        if not required <= inventory.keys(): raise RuntimeError('缺少 Android 或多系统分区。')
        self.device.sealed(list(inventory.values()))
        for name in (() if mode == 'layout' else self.profile['shared_firmware']):
            image = self.profile['images'][name]; part = inventory[image['partition']]
            if self.device.digest(part,0,image['bytes']) != image['sha256']:
                raise RuntimeError('共享固件与受支持 ROM 不匹配，保留 Linux，停止安装：'+image['partition'])
        if mode == 'preserve': self.verify_rom(inventory, masked=True)
        elif mode == 'reinstall': self.device.check_format_tools()
        elif mode != 'layout': raise ValueError('Android 操作类型无效。')
        store = inventory['linux_boot']
        if store['filesystem']!='ext4': raise RuntimeError('引导存储不是 ext4。')
        with self.device.mount_store(store) as root:
            registry = read_json(root/'registry.json'); files = files_snapshot(root)
            space = os.statvfs(root); free_bytes = space.f_bavail*space.f_frsize
            transactions = [p.name.removeprefix('android-') for p in (root/'transactions').glob('android-*') if p.is_dir()]
        head = self.device.read(inventory['userdata'],0,min(65608,inventory['userdata']['bytes'])) if 'userdata' in inventory else b''
        return dict(inventory=inventory,free_bytes=free_bytes,registry=registry,registry_sha256=files['registry.json']['sha256'],files=files,
                    store_fsuuid=store['fsuuid'],userdata_blank=not any(head),
                    userdata_linux_signature=linux_userdata_signature(head),
                    pending_transactions=sorted(set(transactions)-set(registry.get('resolved_android_transactions',[]))))

    def direct_install(self,plan,base):
        from liuqin_rom_admission import require_install
        require_install(self.profile)
        from liuqin_ota import require_device
        require_device(self.profile,self.device)
        current=self.device.inventory()
        if current!=plan['inventory'] or 'linux_boot' in current or any(is_root_name(n) for n in current):
            raise RuntimeError('单 Android 安装不能接管多系统分区。')
        if 'userdata' not in current or 'metadata' not in current or current['userdata']['bytes']<16*1024**3:
            raise RuntimeError('缺少有效 Android 数据分区。')
        from liuqin_partitions import discover
        state=PartitionOperations(self.device,self.profile).inspect()
        if not discover(state,self.device.serial)['can_create']:raise RuntimeError('单 Android 安装需要已识别的原厂前缀布局。')
        validate_images(plan['images'],self.profile,current);self.device.sealed(list(current.values()));self.device.check_format_tools()
        if self.device.digest(current['boot_a'])!=plan['previous_boot_sha256']:raise RuntimeError('默认引导在确认后改变。')
        for filename in self.profile['shared_firmware']:
            entry=self.profile['images'][filename]
            if self.device.digest(current[entry['partition']],0,entry['bytes'])!=entry['sha256']:raise RuntimeError('共享固件不匹配。')
        protected=[p for name,p in current.items() if name not in write_partitions(self.profile)|FORMAT_PARTITIONS]
        import liuqin_recovery_preserve as fixed
        fixed_plan=fixed.ensure(self.device)
        for image in plan['images']:
            part=image['partition'];self.device.sealed(protected)
            if fixed_plan and part['name']=='recovery_a':
                continue
            with self.device.writable(part,write_partitions(self.profile)) as fd:
                for index,chunk in enumerate(image['chunks']):
                    data=fetch(base+'/images/'+part['name']+'/'+str(index),chunk['bytes'],chunk['sha256'])
                    self.device.write(fd,chunk['offset'],data)
                    print('liuqin-android-progress '+json.dumps(dict(partition=part['name'],offset=chunk['offset']+chunk['bytes'],bytes=part['bytes'])),flush=True)
            if self.device.digest(part)!=image['whole_sha256']:raise RuntimeError('Android 分区回读失败。')
        if self.profile.get('generic_ota_version'):self.verify_rom(current)
        fixed_result=fixed.verify(self.device) or {}
        if self.device.inventory()!=current:raise RuntimeError('分区身份发生变化，未初始化 Android 数据。')
        self.device.format(current['userdata'],self.profile['userdata']);self.device.format(current['metadata'],self.profile['metadata'])
        self.device.sealed(list(current.values()))
        return dict(rom_written=True,android_data_initialized=True,**fixed_result)

    def verify_rom(self, inventory, masked=False):
        from liuqin_recovery_preserve import active
        _,fixed=active(optional=True)
        for name in self.profile['install_images']:
            image = self.profile['images'][name]; part = inventory[image['partition']]
            if fixed and part['name']=='recovery_a':
                if self.device.digest(part)!=fixed['image_sha256']:
                    raise RuntimeError('固定 Recovery 与确认方案不匹配。')
                continue
            if name=='super.img':
                for segment in self.profile['super']['verification_ranges']:
                    if self.device.digest(part,segment['offset'],segment['bytes'])!=segment['sha256']:
                        raise RuntimeError('现有 Android super 与固定 ROM 不匹配。')
            elif self.device.digest(part,0,image['bytes'])!=image['sha256']:
                raise RuntimeError('现有 Android 镜像不匹配：'+name)

    def validate_plan(self, plan):
        if plan.get('schema_version') != 1 or plan.get('mode') not in ('preserve', 'reinstall') or type(plan.get('set_default')) is not bool:
            raise ValueError('Android 操作计划无效。')
        for key in ('transaction', 'android_id'):
            if str(uuid.UUID(plan[key])) != plan[key]: raise ValueError('事务身份无效。')
        for key in ('previous_boot_sha256', 'registry_sha256', 'candidate_registry_sha256'):
            if not re.fullmatch('[0-9a-f]{64}', plan.get(key, '')): raise ValueError('事务校验值无效。')
        if plan['registry'].get('serial') != self.device.serial: raise RuntimeError('事务设备身份不匹配。')
        if hashlib.sha256(canonical(plan['candidate_registry'])).hexdigest() != plan['candidate_registry_sha256']:
            raise RuntimeError('候选系统登记与已确认摘要不一致。')

    def guard(self, plan):
        from liuqin_rom_admission import require_install
        require_install(self.profile)
        from liuqin_ota import require_device
        require_device(self.profile,self.device)
        self.validate_plan(plan)
        current = self.device.inventory()
        if current != plan['inventory']: raise RuntimeError('设备分区身份或边界发生变化。')
        self.device.sealed(list(current.values()))
        head = self.device.read(current['userdata'],0,65608)
        if linux_userdata_signature(head):
            raise RuntimeError('userdata 含 Linux 文件系统，拒绝 Android 操作。')
        if plan['mode']=='preserve' and not any(head): raise RuntimeError('userdata 尚未初始化，不能登记为保留 Android。')
        for name in self.profile['shared_firmware']:
            image = self.profile['images'][name]
            if self.device.digest(current[image['partition']],0,image['bytes']) != image['sha256']:
                raise RuntimeError('共享固件在确认后变化。')
        if self.device.digest(current['boot_a']) != plan['previous_boot_sha256']:
            raise RuntimeError('默认引导在确认后发生变化。')
        with self.device.mount_store(current['linux_boot']) as root:
            if sha(root/'registry.json') != plan['registry_sha256'] or read_json(root/'registry.json') != plan['registry']: raise RuntimeError('系统登记在确认后变化。')
            present = files_snapshot(root)
            allowed_new = {'systems/'+plan['android_id']+'/images/'+plan['transaction']+'.img'}
            allowed_new |= {'transactions/android-'+plan['transaction']+'/'+name for name in ('previous-boot.img','previous-registry.json','previous-SHA256SUMS','commit-intent.json','journal.json','rom-installed.json')}
            if set(present)-set(plan['store_files'])-allowed_new: raise RuntimeError('引导存储出现了未确认的新文件。')
            preserve_files(root,plan['store_files'])
        return current

    def staged(self, plan, inventory):
        with self.device.mount_store(inventory['linux_boot']) as root:
            journal = root/'transactions'/('android-'+plan['transaction'])
            record = read_json(journal/'journal.json')
            image = root/'systems'/plan['android_id']/'images'/(plan['transaction']+'.img')
            if (record.get('transaction') != plan['transaction'] or record.get('mode') != plan['mode'] or
                record.get('rom') != self.profile['rom'] or record.get('userdata') != inventory['userdata'] or
                record.get('metadata') != inventory['metadata'] or
                sha(journal/'previous-registry.json') != plan['registry_sha256'] or
                sha(journal/'previous-boot.img') != plan['previous_boot_sha256'] or
                sha(image) != self.profile['images']['boot.img']['sha256']):
                raise RuntimeError('Android 事务或恢复材料尚未完整保存。')

    def stage(self, plan, base):
        from liuqin_rom_admission import require_install
        require_install(self.profile)
        from liuqin_ota import require_device
        require_device(self.profile,self.device)
        if plan.get('registration_required') is False: raise RuntimeError('此保留操作不允许写入引导存储。')
        inventory = self.guard(plan); store = inventory['linux_boot']
        transaction = str(uuid.UUID(plan['transaction'])); os_id = str(uuid.UUID(plan['android_id']))
        if os_id in {s['id'] for s in plan['registry']['systems'] if s['type']=='linux'}:
            raise RuntimeError('Android 身份与 Linux 冲突。')
        if self.profile.get('generic_ota_version') == 1:
            from liuqin_ota import requirements
            firmware_evidence = [entry for entry in requirements(self.profile) if entry['partition'] != 'dtbo_a']
        else:
            firmware_evidence = [{key:self.profile['images'][name][key]
                                  for key in ('partition','bytes','sha256')}
                                 for name in self.profile['shared_firmware']]
        boot = self.profile['images']['boot.img']
        data = fetch(base+'/boot',boot['bytes'],boot['sha256'])
        previous_part = inventory['boot_a']

        def image_chunks():
            for offset in range(0, len(data), BOOT_STORE_CHUNK):
                yield data[offset:offset+BOOT_STORE_CHUNK]

        def previous_chunks():
            for offset in range(0, previous_part['bytes'], BOOT_STORE_CHUNK):
                yield self.device.read(previous_part, offset, min(BOOT_STORE_CHUNK, previous_part['bytes']-offset))

        with self.device.mount_store(store,True) as root:
            if sha(root/'registry.json')!=plan['registry_sha256']: raise RuntimeError('引导登记发生变化。')
            preserve_files(root,plan['store_files'])
            space = os.statvfs(root)
            image_space = sparse_boot_bytes(image_chunks(), space.f_frsize)
            previous_space = sparse_boot_bytes(previous_chunks(), space.f_frsize)
            if space.f_bavail*space.f_frsize < image_space+previous_space+16*1024**2:
                raise RuntimeError('引导存储空间不足，不删除已有文件。')
            journal = root/'transactions'/('android-'+transaction)
            journal.parent.mkdir(mode=0o700,exist_ok=True)
            journal.mkdir(mode=0o700)
            image = root/'systems'/os_id/'images'/(transaction+'.img')
            image.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            write_sparse_boot(image, image_chunks(), boot['bytes'], boot['sha256'])
            write_sparse_boot(journal/'previous-boot.img', previous_chunks(),
                              previous_part['bytes'], plan['previous_boot_sha256'])
            atomic(journal/'previous-registry.json',(root/'registry.json').read_bytes())
            atomic(journal/'previous-SHA256SUMS',(root/'SHA256SUMS').read_bytes())
            from liuqin_native_install import require
            lease = require()
            intent = dict(schema=1,serial=self.device.serial,plan=plan,profile=self.profile)
            if lease and lease['scope'].get('fixed_recovery'):
                intent['fixed_recovery'] = lease['scope']['fixed_recovery']
            atomic(journal/'commit-intent.json',canonical(intent))
            atomic(journal/'journal.json',canonical(dict(transaction=transaction,mode=plan['mode'],status='prepared',
                rom=self.profile['rom'],targets=sorted(write_partitions(self.profile)) if plan['mode']=='reinstall' else [],
                userdata=inventory['userdata'],metadata=inventory['metadata'],
                generic_ota_version=self.profile.get('generic_ota_version',0),shared_firmware=firmware_evidence)))
            preserve_files(root,plan['store_files'])
        return dict(staged=True)

    def matches_full_rom_image(self, part, image, backup):
        from liuqin_native_install import identity

        image_bytes = image['source_bytes']
        if (not backup or backup['identity'] != identity(part)
                or backup['sha256'] != image['whole_sha256']
                or image['expanded_bytes'] != image_bytes):
            return False
        self.device.sealed([part])
        if self.device.digest(part) != image['whole_sha256']:
            return False
        # An exact match must also agree with the approved raw ROM image,
        # including the zero padding emitted by the ordinary write path.
        if image_bytes == part['bytes']:
            return image['whole_sha256'] == image['source_sha256']
        if self.device.digest(part, 0, image_bytes) != image['source_sha256']:
            return False
        for offset in range(image_bytes, part['bytes'], 4*1024**2):
            if self.device.read(part, offset, min(4*1024**2, part['bytes']-offset)).strip(b'\0'):
                return False
        return True

    def install(self, plan, base):
        from liuqin_rom_admission import require_install
        require_install(self.profile)
        from liuqin_ota import require_device
        require_device(self.profile,self.device)
        inventory = self.guard(plan)
        if plan['mode']!='reinstall': raise RuntimeError('保留模式不允许写入 Android 分区。')
        validate_images(plan['images'],self.profile,inventory)
        self.device.check_format_tools()
        self.staged(plan, inventory)
        protected = [p for name,p in inventory.items() if name not in write_partitions(self.profile) | FORMAT_PARTITIONS]
        backups = {}
        if self.profile.get('generic_ota_version') == 2:
            from liuqin_native_install import require
            session = require()
            if session and session['scope'].get('full_rom') is True:
                backups = session['scope']['rom_backups']
        import liuqin_recovery_preserve as fixed
        fixed_plan=fixed.ensure(self.device)
        for image in plan['images']:
            part=image['partition'];self.device.sealed(protected)
            if fixed_plan and part['name']=='recovery_a':
                continue
            if not self.matches_full_rom_image(part,image,backups.get(part['name'])):
                with self.device.writable(part,write_partitions(self.profile)) as fd:
                    for index,chunk in enumerate(image['chunks']):
                        print('liuqin-android-progress '+json.dumps(dict(partition=part['name'],offset=chunk['offset'],bytes=part['bytes'])),flush=True)
                        data=fetch(base+'/images/'+part['name']+'/'+str(index),chunk['bytes'],chunk['sha256'])
                        self.device.write(fd,chunk['offset'],data)
                if self.device.digest(part)!=image['whole_sha256']: raise RuntimeError('ROM 分区回读失败：'+part['name'])
            print('liuqin-android-progress '+json.dumps(dict(partition=part['name'],offset=part['bytes'],bytes=part['bytes'])),flush=True)
            self.device.sealed(protected)
        fixed_result=fixed.verify(self.device) or {}
        if self.device.inventory()!=inventory: raise RuntimeError('写入后分区布局变化，停止数据初始化。')
        head=self.device.read(inventory['userdata'],0,65608)
        if linux_userdata_signature(head):
            raise RuntimeError('userdata 含 Linux 文件系统，拒绝格式化。')
        self.device.format(inventory['userdata'],self.profile['userdata'])
        self.device.format(inventory['metadata'],self.profile['metadata'])
        self.device.sealed(list(inventory.values()))
        if self.device.inventory()!=inventory: raise RuntimeError('Android 初始化改变了受保护分区。')
        from liuqin_native_install import require
        completion = dict(transaction=plan['transaction'], images=fixed.effective_hashes(plan['images'],require()),
            userdata_header=hashlib.sha256(self.device.read(inventory['userdata'],0,4096)).hexdigest(),
            metadata_header=hashlib.sha256(self.device.read(inventory['metadata'],0,4096)).hexdigest())
        with self.device.mount_store(inventory['linux_boot'],True) as root:
            if sha(root/'registry.json') != plan['registry_sha256']: raise RuntimeError('Android 写入后系统登记发生变化。')
            preserve_files(root,plan['store_files'])
            atomic(root/'transactions'/('android-'+plan['transaction'])/'rom-installed.json',canonical(completion))
            preserve_files(root,plan['store_files'])
        return dict(rom_written=True,android_data_initialized=True,**fixed_result)

    def commit(self, plan, new_registry, expected_boot, restore_default=False):
        from liuqin_rom_admission import require_install
        require_install(self.profile)
        from liuqin_ota import require_device
        require_device(self.profile,self.device)
        self.validate_plan(plan)
        inventory=self.device.inventory()
        if inventory!=plan['inventory']: raise RuntimeError('提交前分区身份变化。')
        self.device.sealed(list(inventory.values()))
        self.staged(plan, inventory)
        if plan['mode'] == 'reinstall':
            with self.device.mount_store(inventory['linux_boot']) as root:
                completion = read_json(root/'transactions'/('android-'+plan['transaction'])/'rom-installed.json')
            from liuqin_native_install import require
            from liuqin_recovery_preserve import effective_hashes
            expected_completion = dict(transaction=plan['transaction'], images=effective_hashes(plan['images'],require()),
                userdata_header=hashlib.sha256(self.device.read(inventory['userdata'],0,4096)).hexdigest(),
                metadata_header=hashlib.sha256(self.device.read(inventory['metadata'],0,4096)).hexdigest())
            if completion != expected_completion: raise RuntimeError('Android ROM 和数据初始化尚未完成或发生变化。')
        if self.device.digest(inventory['boot_a'])!=expected_boot: raise RuntimeError('提交前默认引导不匹配。')
        wanted_boot = self.profile['images']['boot.img']['sha256'] if plan['set_default'] and not restore_default else plan['previous_boot_sha256']
        if expected_boot != wanted_boot: raise RuntimeError('提交引导不匹配已确认的操作。')
        expected_registry = json.loads(canonical(plan['candidate_registry']))
        if restore_default:
            expected_registry['default_id'] = plan['registry'].get('default_id')
            expected_registry['default_generation'] = (plan['transaction'] if plan['registry'].get('default_id')==plan['android_id'] else plan['registry'].get('default_generation'))
        if new_registry != expected_registry: raise RuntimeError('提交的系统登记不匹配已确认的候选版本。')
        check_registry_transition(plan['registry'],new_registry,plan['android_id'],False if restore_default else plan['set_default'])
        android = next(s for s in new_registry['systems'] if s['type']=='android')
        if android.get('userdata') != inventory['userdata'] or android.get('metadata') != inventory['metadata'] or android.get('rom') != self.profile['rom'] or android.get('slot') != 'a':
            raise RuntimeError('Android 登记与实际分区或 ROM 不匹配。')
        with self.device.mount_store(inventory['linux_boot'],True) as root:
            accepted = {plan['registry_sha256']} | ({plan['candidate_registry_sha256']} if restore_default else set())
            if sha(root/'registry.json') not in accepted: raise RuntimeError('提交前系统登记变化。')
            preserve_files(root,plan['store_files'],mutable=('registry.json','SHA256SUMS') if restore_default else ())
            image=new_registry['systems'][-1]
            if sha(root/image['image'])!=self.profile['images']['boot.img']['sha256']:
                raise RuntimeError('Android 引导存储回读失败。')
            # Only registry and its mutable checksum entry change. Every old
            # Linux image, manifest and transaction remains byte-identical.
            sums=root/'SHA256SUMS'
            content=sums.read_text()
            atomic(sums, ''.join(line+'\n' for line in content.splitlines() if not line.endswith('  registry.json')).encode())
            atomic(root/'registry.json',canonical(new_registry))
            preserve_files(root,plan['store_files'],mutable=('registry.json','SHA256SUMS'))
            journal=root/'transactions'/('android-'+plan['transaction'])
            atomic(journal/'complete.json',canonical(dict(status='complete',registry_sha256=sha(root/'registry.json'),boot_sha256=expected_boot)))
        return dict(committed=True,linux_preserved=True)

    def export_boot(self, scan):
        android=[s for s in scan['registry']['systems'] if s['type']=='android']
        if len(android)!=1: raise RuntimeError('未登记 Android 镜像，请选择 ROM。')
        record=android[0];path=record['image']
        if not re.fullmatch(r'systems/[0-9a-f-]{36}/images/[0-9a-f-]{36}\.img',path): raise RuntimeError('无效引导路径。')
        with self.device.mount_store(scan['inventory']['linux_boot']) as root:
            source=root/path
            if source.is_symlink() or source.stat().st_size!=self.profile['images']['boot.img']['bytes'] or sha(source)!=self.profile['images']['boot.img']['sha256']:
                raise RuntimeError('Android 引导不匹配固定版本。')
            return dict(boot_gzip_base64=base64.b64encode(gzip.compress(source.read_bytes(),mtime=0)).decode())


class LinuxOperations(Operations):
    """Append a Linux registration; existing OS files are immutable."""
    def check(self, plan, installed=False, committed=False):
        from liuqin_managed import validate_linux_plan
        from liuqin_partitions import stable_partition
        selected = validate_linux_plan(plan, self.device.serial)
        current = self.device.inventory()
        expected = json.loads(canonical(plan['inventory']))
        if installed:
            expected[selected['name']].update(filesystem=plan['system']['target']['root_fstype'],
                                              fsuuid=plan['system']['target']['root_fsuuid'])
        if {n:stable_partition(p) for n,p in current.items()} != {n:stable_partition(p) for n,p in expected.items()}:
            raise RuntimeError('Linux 安装的分区身份、边界或受保护文件系统变化。')
        self.device.sealed(list(current.values()))
        with self.device.mount_store(current['linux_boot']) as root:
            accepted = {plan['registry_sha256']}
            if committed: accepted.add(hashlib.sha256(canonical(plan['candidate_registry'])).hexdigest())
            if sha(root/'registry.json') not in accepted: raise RuntimeError('已有系统登记变化。')
            if read_json(root/'registry.json') not in ([plan['registry'],plan['candidate_registry']] if committed else [plan['registry']]):
                raise RuntimeError('原始系统登记与快照不符。')
            preserve_files(root, plan['store_files'], mutable=('registry.json','SHA256SUMS') if committed else ())
            allowed = {plan['system']['image']} | {'transactions/linux-'+plan['transaction']+'/'+name
                for name in ('previous-boot.img','previous-registry.json','previous-SHA256SUMS','commit-intent.json','journal.json','root-installed.json','complete.json')}
            if set(files_snapshot(root))-set(plan['store_files'])-allowed:
                raise RuntimeError('引导存储出现未确认的文件。')
        return current

    def stage(self, plan, base):
        current = self.check(plan)
        if self.device.digest(current['boot_a']) != plan['previous_boot_sha256']:
            raise RuntimeError('当前 boot_a 与快照不一致。')
        system = plan['system']
        data = fetch(base+'/boot', plan['boot_bytes'], system['boot_sha256'])
        previous_part = current['boot_a']
        def image_chunks():
            for offset in range(0,len(data),BOOT_STORE_CHUNK):yield data[offset:offset+BOOT_STORE_CHUNK]
        def previous_chunks():
            for offset in range(0,previous_part['bytes'],BOOT_STORE_CHUNK):
                yield self.device.read(previous_part,offset,min(BOOT_STORE_CHUNK,previous_part['bytes']-offset))
        with self.device.mount_store(current['linux_boot'], True) as root:
            preserve_files(root, plan['store_files'])
            space = os.statvfs(root)
            needed=sparse_boot_bytes(image_chunks(),space.f_frsize)+sparse_boot_bytes(previous_chunks(),space.f_frsize)
            if space.f_bavail*space.f_frsize < needed+16*1024**2:
                raise RuntimeError('引导存储空间不足，不删除已有文件。')
            journal = root/'transactions'/('linux-'+plan['transaction'])
            journal.parent.mkdir(mode=0o700, exist_ok=True); journal.mkdir(mode=0o700)
            image = root/system['image']; image.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_sparse_boot(image,image_chunks(),len(data),system['boot_sha256'])
            write_sparse_boot(journal/'previous-boot.img',previous_chunks(),previous_part['bytes'],plan['previous_boot_sha256'])
            atomic(journal/'previous-registry.json',(root/'registry.json').read_bytes())
            atomic(journal/'previous-SHA256SUMS',(root/'SHA256SUMS').read_bytes())
            atomic(journal/'commit-intent.json',canonical(dict(schema=1,serial=self.device.serial,plan=plan)))
            atomic(journal/'journal.json', canonical(plan))
            preserve_files(root,plan['store_files'])
        return dict(staged=True)

    def staged(self, plan, current):
        with self.device.mount_store(current['linux_boot']) as root:
            journal = root/'transactions'/('linux-'+plan['transaction'])
            if (read_json(journal/'journal.json') != plan or
                sha(journal/'previous-registry.json') != plan['registry_sha256'] or
                sha(journal/'previous-boot.img') != plan['previous_boot_sha256'] or
                sha(root/plan['system']['image']) != plan['system']['boot_sha256']):
                raise RuntimeError('Linux 引导和恢复材料尚未完整保存。')

    def guard(self, plan):
        current = self.check(plan)
        self.staged(plan, current)
        if self.device.digest(current['boot_a']) != plan['previous_boot_sha256']:
            raise RuntimeError('确认后默认引导变化。')
        return dict(guarded=True)

    def finish(self, plan):
        current = self.check(plan, installed=True)
        self.staged(plan, current)
        self.device.verify_linux(current[plan['system']['partition']['name']], plan['system'])
        with self.device.mount_store(current['linux_boot'],True) as root:
            preserve_files(root,plan['store_files'])
            atomic(root/'transactions'/('linux-'+plan['transaction'])/'root-installed.json', canonical(plan['system']))
            preserve_files(root,plan['store_files'])
        return dict(root_installed=True)

    def commit(self, plan, registry, expected_boot, restore_default=False):
        current = self.check(plan, installed=True, committed=restore_default)
        self.staged(plan,current)
        candidate = json.loads(canonical(plan['candidate_registry']))
        if restore_default:
            candidate.update(default_id=plan['registry'].get('default_id'), default_generation=plan['registry'].get('default_generation'))
        if registry != candidate: raise RuntimeError('Linux 提交不能修改已有系统登记。')
        wanted = plan['padded_sha256'] if plan['set_default'] and not restore_default else plan['previous_boot_sha256']
        if expected_boot != wanted or self.device.digest(current['boot_a']) != wanted:
            raise RuntimeError('Linux 默认引导尚未回读确认。')
        self.device.verify_linux(current[plan['system']['partition']['name']], plan['system'])
        with self.device.mount_store(current['linux_boot'],True) as root:
            journal = root/'transactions'/('linux-'+plan['transaction'])
            if read_json(journal/'root-installed.json') != plan['system']:
                raise RuntimeError('Linux 根系统尚未完整安装。')
            preserve_files(root,plan['store_files'],mutable=('registry.json','SHA256SUMS') if restore_default else ())
            sums = root/'SHA256SUMS'
            atomic(sums, ''.join(line+'\n' for line in sums.read_text().splitlines() if not line.endswith('  registry.json')).encode())
            atomic(root/'registry.json',canonical(registry))
            atomic(journal/'complete.json',canonical(dict(registry_sha256=sha(root/'registry.json'),boot_sha256=expected_boot)))
            preserve_files(root,plan['store_files'],mutable=('registry.json','SHA256SUMS'))
        return dict(committed=True)


class PartitionOperations(Operations):
    def inspect(self):
        report,details=self.device.gpt()
        result=dict(report=report,details=details,inventory={},registry=None,files={})
        try:
            inventory=self.device.inventory(allow_unknown_store=True)
            self.device.sealed(list(inventory.values()));result['inventory']=inventory
            if 'userdata' in inventory:
                header=self.device.read(inventory['userdata'],0,min(65608,inventory['userdata']['bytes']))
                result.update(userdata_blank=not any(header),userdata_linux_signature=linux_userdata_signature(header))
            if 'linux_boot' in inventory:
                result.update(super().inspect('layout'))
        except (ValueError,RuntimeError,OSError) as error:result['error']=str(error)
        return result

    def after_gpt(self,plan):
        from liuqin_layout import parse_print,check_geometry
        report,details=self.device.gpt();disk=parse_print(report);check_geometry(disk)
        before=plan['source'];old_disk=parse_print(before['report'])
        if (disk.total_sectors,disk.sector_size,disk.entry_count)!=(old_disk.total_sectors,old_disk.sector_size,old_disk.entry_count):
            raise RuntimeError('追加后磁盘几何改变。')
        if any(details.get(k)!=v for k,v in before['details'].items()):
            raise RuntimeError('已有分区的边界、GUID 或属性发生变化。')
        expected={(p.number,p.name,p.start,p.size) for p in old_disk.partitions}
        expected |= {(p['number'],p['name'],p['start'],p['bytes']) for p in plan['partitions']}
        if {(p.number,p.name,p.start,p.size) for p in disk.partitions}!=expected:
            raise RuntimeError('追加后的 GPT 与方案不符。')
        inventory=self.device.inventory(allow_unknown_store=True);old=before['inventory']
        if set(inventory)!=set(old)|{p['name'] for p in plan['partitions']}:
            raise RuntimeError('追加后的内核分区列表不符。')
        from liuqin_partitions import stable_partition
        if any(stable_partition(inventory[name])!=stable_partition(part) for name,part in old.items()):
            raise RuntimeError('已有分区的内核身份发生变化。')
        for planned in plan['partitions']:
            actual=inventory[planned['name']]
            if actual['parent']!='sda' or any(actual.get(k)!=planned[k] for k in ('number','partuuid','start','bytes')):
                raise RuntimeError('新分区内核身份或边界不符。')
        self.device.sealed(list(inventory.values()))
        return inventory

    def apply(self,plan):
        from liuqin_partitions import validate_append,snapshot_id
        validate_append(plan,self.device.serial)
        current=self.inspect()
        if snapshot_id(current)!=snapshot_id(plan['source']):raise RuntimeError('确认后设备布局或引导存储改变，未追加分区。')
        old=plan['source'];transaction=plan['transaction']
        if not plan['create_store']:
            with self.device.mount_store(old['inventory']['linux_boot'],True) as root:
                preserve_files(root,old['files'])
                journal=root/'transactions'/('partition-'+transaction)
                journal.parent.mkdir(mode=0o700,exist_ok=True);journal.mkdir(mode=0o700)
                atomic(journal/'plan.json',canonical(plan))
                atomic(journal/'previous-registry.json',(root/'registry.json').read_bytes())
                preserve_files(root,old['files'])
        # Only the GPT writer is allowed to open the parent disk. Its arguments
        # contain new entries; no deletion, resizing or existing-name changes.
        report,details=self.device.gpt()
        if report!=old['report'] or details!=old['details']:raise RuntimeError('GPT 在写入前改变。')
        if self.device.inventory(allow_unknown_store=True)!=old['inventory']:raise RuntimeError('内核分区身份改变。')
        self.device.append_gpt(plan)
        inventory=self.after_gpt(plan)
        for part in plan['partitions']:
            if part['role'] in ('store','android'):
                self.device.initialize_new_partition(inventory[part['name']],part)
        inventory=self.after_gpt(plan)
        store=inventory['linux_boot']
        if store.get('filesystem')!='ext4' or store.get('fsuuid')!=plan['store']['fsuuid']:
            raise RuntimeError('引导存储初始化未通过回读。')
        with self.device.mount_store(store,True) as root:
            if plan['create_store']:
                if (root/'registry.json').exists():raise RuntimeError('新引导存储已有登记，停止提交。')
                journal=root/'transactions'/('partition-'+transaction)
                journal.mkdir(mode=0o700,parents=True)
                atomic(journal/'plan.json',canonical(plan))
                sums=b''
            else:
                if sha(root/'registry.json')!=old['registry_sha256']:raise RuntimeError('提交前旧登记改变。')
                preserve_files(root,old['files'])
                journal=root/'transactions'/('partition-'+transaction)
                sums=''.join(line+'\n' for line in (root/'SHA256SUMS').read_text().splitlines() if not line.endswith('  registry.json')).encode()
            atomic(root/'SHA256SUMS',sums)
            atomic(root/'registry.json',canonical(plan['candidate_registry']))
            atomic(journal/'complete.json',canonical(dict(status='complete',registry_sha256=sha(root/'registry.json'))))
            if not plan['create_store']:preserve_files(root,old['files'],mutable=('registry.json','SHA256SUMS'))
        return dict(partitions_created=True,existing_partitions_preserved=True)


    def edit(self,plan):
        from liuqin_partition_editor import validate_edit
        from liuqin_partitions import snapshot_id
        validate_edit(plan,self.device.serial)
        for action in plan['formats']:
            if action['role']=='android':self.device.check_format_tools()
            elif isinstance(self.device,Hardware) and not os.access('/usr/sbin/mkfs.'+action['filesystem'],os.X_OK):
                raise RuntimeError('缺少目标文件系统格式化工具，未修改分区。')
        old=plan['source']
        if snapshot_id(self.inspect())!=snapshot_id(old):raise RuntimeError('实际布局在确认后改变。')
        transaction=plan['transaction'];old_store=old['inventory'].get('linux_boot')
        store_erased=any(p['name']=='linux_boot' for p in plan['deletes']) or any(p['role']=='store' for p in plan['formats'])
        if old_store:
            with self.device.mount_store(old_store,True) as root:
                preserve_files(root,old['files'])
                journal=root/'transactions'/('edit-'+transaction);journal.mkdir(mode=0o700,parents=True)
                atomic(journal/'plan.json',canonical(plan))
                atomic(journal/'previous-registry.json',(root/'registry.json').read_bytes())
        if plan['deletes'] or plan['partitions']:self.device.append_gpt(plan)
        return self.finish_edit(plan)

    def finish_edit(self,plan,resume=False,check_only=False):
        # Resume only initialization of an exact, already-written fresh Linux
        # layout. Never replay GPT deletion or existing-partition formatting.
        from liuqin_partition_editor import validate_edit
        validate_edit(plan,self.device.serial)
        old=plan['source'];transaction=plan['transaction'];old_store=old['inventory'].get('linux_boot')
        store_erased=any(p['name']=='linux_boot' for p in plan['deletes']) or any(p['role']=='store' for p in plan['formats'])
        if resume and (old_store or plan['formats'] or not plan['candidate_registry'] or plan['candidate_registry']['systems'] or
            any(p['role'] not in ('store','root') for p in plan['partitions'])):
            raise RuntimeError('只允许继续尚未初始化的新 Linux 分区布局。')
        deleted={p['name'] for p in plan['deletes']}
        expected_names=(set(old['inventory'])-deleted)|{p['name'] for p in plan['partitions']}
        def check(formatted=False):
            from liuqin_layout import parse_print,check_geometry
            report,details=self.device.gpt();disk=parse_print(report);check_geometry(disk)
            original=parse_print(old['report'])
            if (disk.total_sectors,disk.sector_size,disk.entry_count)!=(original.total_sectors,original.sector_size,original.entry_count):raise RuntimeError('磁盘几何改变。')
            expected={(p.number,p.name,p.start,p.size) for p in original.partitions if p.name not in deleted}
            expected|={(p['number'],p['name'],p['start'],p['bytes']) for p in plan['partitions']}
            if {(p.number,p.name,p.start,p.size) for p in disk.partitions}!=expected:raise RuntimeError('GPT 不符合编辑方案。')
            deleted_numbers={str(p['number']) for p in plan['deletes']}
            if any(details.get(k)!=v for k,v in old['details'].items() if k not in deleted_numbers):raise RuntimeError('保留分区属性改变。')
            current=self.device.inventory(allow_unknown_store=True)
            if set(current)!=expected_names:raise RuntimeError('内核分区列表不符。')
            for name,part in old['inventory'].items():
                if name in deleted:continue
                wanted=dict(part)
                if formatted:
                    for action in plan['formats']:
                        if action['partition']['name']==name and action['role']=='root':wanted.update(filesystem=action['filesystem'],fsuuid=action['fsuuid'])
                from liuqin_partitions import stable_partition
                if stable_partition(current[name])!=stable_partition(wanted):raise RuntimeError('分区身份或文件系统不符：'+name)
            for part in plan['partitions']:
                if current[part['name']]['parent']!='sda' or any(current[part['name']][k]!=part[k] for k in ('number','start','bytes','partuuid')):raise RuntimeError('新建分区身份不符。')
            self.device.sealed(list(current.values()));return current
        current=check()
        if resume and any(current[p['name']].get('filesystem')!='unformatted' for p in plan['partitions']):
            raise RuntimeError('新分区已有文件系统，不能自动重复初始化。')
        if check_only:
            if not resume:raise ValueError('仅支持续接初始化的只读检查。')
            return dict(resumable=True)
        for part in plan['partitions']:
            if part['role'] in ('store','android'):self.device.initialize_new_partition(current[part['name']],part)
        for action in plan['formats']:
            part=current[action['partition']['name']]
            if action['role']=='android':
                self.device.format(part,format_settings('userdata'))
                self.device.format(current['metadata'],format_settings('metadata'))
            elif action['role']=='store':self.device.initialize_new_partition(part,dict(role='store',fsuuid=action['fsuuid']))
            else:self.device.format_root(part,action['filesystem'],action['fsuuid'])
        current=check(True)
        if plan['candidate_registry'] is not None:
            wanted=plan['store']['fsuuid']
            if current['linux_boot'].get('filesystem')!='ext4' or current['linux_boot'].get('fsuuid')!=wanted:raise RuntimeError('引导存储身份不符，停止提交。')
            with self.device.mount_store(current['linux_boot'],True) as root:
                if old_store and not store_erased:
                    if read_json(root/'registry.json')!=old['registry']:raise RuntimeError('系统登记在编辑时改变。')
                    preserve_files(root,old['files'])
                    sums=''.join(line+'\n' for line in (root/'SHA256SUMS').read_text().splitlines() if not line.endswith('  registry.json')).encode()
                else:sums=b''
                journal=root/'transactions'/('edit-'+transaction);journal.mkdir(mode=0o700,parents=True,exist_ok=True)
                if not old_store or store_erased:atomic(journal/'plan.json',canonical(plan))
                atomic(root/'SHA256SUMS',sums);atomic(root/'registry.json',canonical(plan['candidate_registry']))
                atomic(journal/'complete.json',canonical(dict(status='complete')))
                if old_store and not store_erased:preserve_files(root,old['files'],mutable=('registry.json','SHA256SUMS'))
        return dict(edited=True)


def main():
    sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
    if len(sys.argv)==4 and sys.argv[1]=='check-fastboot-names':
        device=Hardware(sys.argv[2],sys.argv[3])
        validate_names(device.inventory(allow_unknown_store=True))
        print('FASTBOOT_NAMES_OK')
        return
    if len(sys.argv)!=5: raise ValueError('usage: BOOT_ID ACTION REQUEST SHA256')
    boot_id,action,path,digest=sys.argv[1:];path=Path(path)
    if (not str(path).startswith('/run/') or '..' in path.parts or path.is_symlink() or
        not path.is_file() or path.stat().st_size>8*1024**2 or sha(path)!=digest):
        raise ValueError('Android 请求必须是经过核验的 RAM 文件。')
    from liuqin_rom_admission import request_profile
    request=json.loads(path.read_text());profile=request_profile(action,request,__import__('liuqin_native_install').marker())
    lock_fd = os.open('/run/liuqin-android-operation.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+b') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode): raise RuntimeError('无效的操作锁。')
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error: raise RuntimeError('另一个 Android 操作正在运行。') from error
        operations=Operations(Hardware(boot_id,request['serial']),profile)
        from liuqin_native_install import authorize_action
        authorize_action(action,request)
        if action not in ('partition-inspect','partition-resume-check','inspect','android-check','export-boot'):
            validate_names(operations.device.inventory(allow_unknown_store=True))
        if action.startswith('partition-'):
            operations=PartitionOperations(operations.device,profile)
            if action=='partition-inspect':result=operations.inspect()
            elif action=='partition-resume-check':result=operations.finish_edit(request['plan'],resume=True,check_only=True)
            elif action=='partition-resume':
                if request.get('confirm_edits') is not True:raise ValueError('未确认续接初始化。')
                result=operations.finish_edit(request['plan'],resume=True)
            elif action=='partition-edit':
                if request.get('confirm_edits') is not True:raise ValueError('未确认分区编辑。')
                result=operations.edit(request['plan'])
            elif action=='partition-apply':
                if request.get('preserve_existing') is not True:raise ValueError('未确认保留已有分区。')
                result=operations.apply(request['plan'])
            else:raise ValueError('未知分区操作。')
        elif action.startswith('linux-'):
            operations=LinuxOperations(operations.device,profile)
            if action=='linux-stage': result=operations.stage(request['plan'],request['base'])
            elif action=='linux-guard': result=operations.guard(request['plan'])
            elif action=='linux-finish': result=operations.finish(request['plan'])
            elif action=='linux-commit': result=operations.commit(request['plan'],request['registry'],request['expected_boot'],request.get('restore_default') is True)
            else: raise ValueError('未知 Linux 操作。')
        elif action=='inspect':result=operations.inspect(request['mode'])
        elif action=='android-direct':
            if request.get('erase_android_data') is not True:raise ValueError('未确认清空 Android 数据。')
            result=operations.direct_install(request['plan'],request['base'])
        elif action=='android-check':
            inventory=operations.device.inventory();operations.device.sealed(list(inventory.values()));operations.device.check_format_tools()
            for name in profile['shared_firmware']:
                entry=profile['images'][name]
                if operations.device.digest(inventory[entry['partition']],0,entry['bytes'])!=entry['sha256']:raise RuntimeError('共享固件与 Android ROM 不匹配。')
            from liuqin_ota import require_device
            result=require_device(profile,operations.device,check_data=profile.get('generic_ota_version')!=2) or {'compatible':True}
        elif action=='guard':operations.guard(request['plan']);result={'guarded':True}
        elif action=='stage':result=operations.stage(request['plan'],request['base'])
        elif action=='install':
            if request.get('erase_android_data') is not True: raise ValueError('未确认清除 Android userdata 和 metadata。')
            result=operations.install(request['plan'],request['base'])
        elif action=='commit':result=operations.commit(request['plan'],request['registry'],request['expected_boot'],request.get('restore_default') is True)
        elif action=='export-boot':result=operations.export_boot(request['scan'])
        else:raise ValueError('未知 Android 操作。')
        print('liuqin-android-result '+json.dumps(result,separators=(',',':'),ensure_ascii=False),flush=True)

if __name__=='__main__':
    import liuqin_native_install as native_gate
    native_gate.signal_handlers()
    try:main()
    except Exception as error:
        native_gate.interrupted()
        print('liuqin-android-error '+str(error),file=sys.stderr);sys.exit(1)
    except BaseException:native_gate.interrupted();raise
