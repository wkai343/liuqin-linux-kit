# SPDX-License-Identifier: MIT
"""Default-system kernel updates sharing the Recovery store/retention format.

The caller holds both updater and Recovery operation locks. Block access stays
in Device; this module writes only the running root binding and linux_boot files.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from liuqin_artifacts import boot_interface
from liuqin_boot_target import bind, encode, parse
from liuqin_boot_retention import encoded, safe_path, fsync_dir, sha
from liuqin_kernel import RELEASE, parse_contract


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    """Durable replacement, retaining the root binding's SELinux label/mode."""
    if path.resolve() != path:
        raise RuntimeError('引导登记路径包含链接。')
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent); parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700); fsync_dir(directory.parent)
    if path.resolve() != path or path.parent.resolve() != path.parent:
        raise RuntimeError('引导登记路径包含链接。')
    old = path.stat() if path.exists() else None
    if old and (not stat.S_ISREG(old.st_mode) or old.st_nlink != 1):
        raise RuntimeError('引导登记不是独立普通文件。')
    temporary = path.with_name('.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush()
            if old:
                os.fchmod(stream.fileno(), stat.S_IMODE(old.st_mode))
                os.fchown(stream.fileno(), old.st_uid, old.st_gid)
                for name in os.listxattr(path): os.setxattr(temporary, name, os.getxattr(path, name))
            os.fsync(stream.fileno())
        os.replace(temporary, path); fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class StoreUpdate:
    def __init__(self, updater, identity):
        self.updater, self.identity = updater, identity
        self.target_path = updater.root / 'etc/liuqin-boot-target'

    def read(self, root, name):
        return self.updater.regular(safe_path(root, name)).read_bytes()

    def json(self, root, name):
        data = self.read(root, name)
        if len(data) > 4 * 1024**2: raise RuntimeError('引导登记文件过大。')
        return json.loads(data)

    def files(self, root):
        result = {}
        for path in sorted(root.rglob('*')):
            if path.is_symlink(): raise RuntimeError('引导存储包含链接。')
            if path.is_file():
                self.updater.regular(path)
                if path.stat().st_nlink != 1: raise RuntimeError('引导存储包含硬链接。')
                result[path.relative_to(root).as_posix()] = dict(bytes=path.stat().st_size, sha256=sha(path))
        return result

    @staticmethod
    def generation(system):
        generation = system['target']['boot_generation'] if system['type'] == 'linux' else system['generation']
        if str(uuid.UUID(generation)) != generation or str(uuid.UUID(system['id'])) != system['id']:
            raise RuntimeError('系统版本身份无效。')
        if system['image'] != f"systems/{system['id']}/images/{generation}.img":
            raise RuntimeError('引导镜像路径与系统版本不一致。')
        return generation

    def inspect(self, root):
        if (root/'.retention/active.json').exists():
            raise RuntimeError('引导存储清理尚未完成；先在 Recovery 中恢复。')
        registry = self.json(root, 'registry.json')
        if (registry.get('schema_version') != 1 or registry.get('serial') != self.identity['serial'] or
                not 1 <= len(registry.get('systems', [])) <= 17):
            raise RuntimeError('引导登记不属于当前设备。')
        systems = registry['systems']
        if len({s['id'] for s in systems}) != len(systems): raise RuntimeError('系统登记重复。')
        target = parse(self.updater.regular(self.target_path).read_bytes())
        current = next((s for s in systems if s['id'] == self.identity['system_id']), None)
        if (not current or current['type'] != 'linux' or current['target'] != target or
                registry.get('default_id') != current['id'] or
                registry.get('default_generation') != self.generation(current)):
            raise RuntimeError('只能更新当前已登记的默认 Linux；不能从临时启动的其他系统覆盖默认引导。')
        part = current['partition']; actual = self.identity['root']
        if any(part[k] != v for k, v in dict(name=actual['name'], partuuid=actual['uuid'],
                start=actual['start'] * 512, bytes=actual['size'], fsuuid=self.identity['fsuuid'],
                filesystem=self.identity['filesystem']).items()):
            raise RuntimeError('默认系统的分区登记与硬件不一致。')
        stores = [p for p in registry.get('partition_layout', []) if p.get('name') == 'linux_boot']
        actual = self.identity['store']
        if len(stores) != 1 or any(stores[0][k] != v for k, v in dict(partuuid=actual['uuid'],
                start=actual['start'] * 512, bytes=actual['size'], fsuuid=actual['fsuuid']).items()):
            raise RuntimeError('linux_boot 登记与硬件不一致。')
        slots = [s for s in registry.get('rootfs_slots', []) if s['id'] == current['id']]
        if len(slots) != 1 or slots[0]['target'] != target or slots[0]['partition'] != part:
            raise RuntimeError('根分区槽位登记不一致。')
        for system in systems:
            self.generation(system)
            if sha(self.updater.regular(safe_path(root, system['image']))) != system['boot_sha256']:
                raise RuntimeError('已登记的引导镜像校验失败。')
        manifest = 'systems/' + current['id'] + '/manifest.json'
        if self.json(root, manifest) != current or boot_interface(root/current['image'])['boot_target'] != target:
            raise RuntimeError('默认系统的镜像、清单与根绑定不一致。')
        journal = 'transactions/linux-' + self.generation(current)
        if (root/journal/'root-installed.json').exists():
            complete = self.json(root, journal+'/complete.json')
            if (self.json(root, journal+'/root-installed.json') != current or
                    any(not re.fullmatch('[a-f0-9]{64}', complete.get(k, ''))
                        for k in ('registry_sha256', 'boot_sha256'))):
                raise RuntimeError('当前系统安装事务与登记不一致。')
        for directory in (root/'transactions').iterdir():
            if directory.is_symlink() or not directory.is_dir(): raise RuntimeError('事务目录无效。')
            if directory.name.startswith(('linux-', 'android-', 'switch-')):
                terminal = False
                for marker in ('complete.json', 'rolled-back.json'):
                    if not (directory/marker).exists(): continue
                    record = self.json(directory, marker)
                    fields = ('boot_sha256',) if marker == 'rolled-back.json' else ('boot_sha256', 'registry_sha256')
                    terminal |= all(re.fullmatch('[a-f0-9]{64}', record.get(k, '')) for k in fields)
                if not terminal: raise RuntimeError('引导存储存在未完成事务；先恢复该事务。')
        for directory in (root/'kernel-updates').glob('*'):
            if not re.fullmatch('[a-f0-9]{64}', self.json(directory, 'complete.json').get('registry_sha256', '')):
                raise RuntimeError('引导存储存在未完成的内核更新。')
        return registry, current

    def prepare(self, image, directory, generation, old_sha256):
        with self.updater.device.mount_store(self.identity) as root:
            old, previous = self.inspect(root)
            if shutil.disk_usage(root).free < image.stat().st_size + 16 * 1024**2:
                raise RuntimeError('linux_boot 空间不足，不能保留旧镜像并暂存新版本。')
            if self.updater.padded_hash(root/previous['image'], self.identity) != old_sha256:
                raise RuntimeError('boot_a 与登记的默认镜像不一致。')
            originals = {name: self.read(root, name).hex() for name in
                         ('registry.json', 'systems/' + previous['id'] + '/manifest.json', 'SHA256SUMS')}
            before = self.files(root)
        snapshot = directory/'candidate.img'
        # Compare the actual bound bytes before inventing a new generation.
        # Reinstalling an unchanged package must keep the previous-kernel slot.
        bind(image, snapshot, previous['target'], self.identity['boot']['size'])
        if self.updater.padded_hash(snapshot, self.identity) == old_sha256:
            generation = self.generation(previous)
        else:
            snapshot.unlink()
            bind(image, snapshot, dict(previous['target'], boot_generation=generation), self.identity['boot']['size'])
        target = dict(previous['target'], boot_generation=generation)
        with snapshot.open('rb') as stream: os.fsync(stream.fileno())
        system = copy.deepcopy(previous)
        system.update(target=target, image=f"systems/{previous['id']}/images/{generation}.img",
                      boot_sha256=sha(snapshot), source_boot_sha256=sha(image),
                      kernel_release=parse_contract(boot_interface(image)['contract'])['release'])
        new = copy.deepcopy(old)
        new['default_generation'] = generation
        new['systems'] = [system if s['id'] == system['id'] else s for s in new['systems']]
        for slot in new['rootfs_slots']:
            if slot['id'] == system['id']: slot['target'] = target
        replacements = {'registry.json': encoded(new), 'systems/'+system['id']+'/manifest.json': encoded(system)}
        sums = bytes.fromhex(originals['SHA256SUMS']); lines = []; seen = set()
        for line in sums.splitlines():
            match = re.fullmatch(rb'([a-f0-9]{64})  (.+)', line)
            if not match: raise RuntimeError('引导存储校验清单无效。')
            name = match[2].decode()
            if name in seen or name not in before or before[name]['sha256'] != match[1].decode():
                raise RuntimeError('引导存储校验清单与文件不一致。')
            seen.add(name)
            value = checksum(replacements[name]) if name in replacements else before[name]['sha256']
            lines.append(value + '  ' + name + '\n')
        if sums and system['image'] not in seen: lines.append(system['boot_sha256'] + '  ' + system['image'] + '\n')
        replacements['SHA256SUMS'] = ''.join(lines).encode()
        return dict(generation=generation, old_registry=old, new_registry=new, new_system=system,
                    previous_boot_sha256=old_sha256, before=before, originals=originals,
                    old_target=encode(previous['target']).hex(), new_target=encode(target).hex(),
                    replacements={k:v.hex() for k,v in replacements.items()})

    def materials(self, record):
        plan = record['store_update']
        if checksum(encoded(plan)) != record['store_plan_sha256']:
            raise RuntimeError('引导更新计划损坏。')
        generation = plan['generation']; system = plan['new_system']
        if (str(uuid.UUID(record['transaction'])) != generation or self.generation(system) != generation or
                system['id'] != self.identity['system_id'] or not re.fullmatch(RELEASE, system['kernel_release'])):
            raise RuntimeError('引导更新计划身份不一致。')
        prefix = 'kernel-updates/' + generation
        journal = 'transactions/linux-' + generation
        previous = next(s for s in plan['old_registry']['systems'] if s['id'] == system['id'])
        files = {prefix+'/plan.json': encoded(plan), prefix+'/previous-registry.json': bytes.fromhex(plan['originals']['registry.json']),
                 prefix+'/previous-root-target': bytes.fromhex(plan['old_target']),
                 prefix+'/previous-manifest.json': encoded(previous),
                 prefix+'/previous-boot-reference.json': encoded(dict(schema=1, source=previous['image'],
                    bytes=self.identity['boot']['size'], sha256=record['old_sha256'], zero_padding=True)),
                 journal+'/upgrade.json': encoded(dict(operation='kernel-upgrade', source=prefix,
                    previous_generation=previous['target']['boot_generation'], rootfs_reformatted=False))}
        receipt = dict(registry_sha256=checksum(encoded(plan['new_registry'])), boot_sha256=record['new_sha256'])
        completed = {journal+'/root-installed.json': encoded(system),
                     journal+'/complete.json': encoded(dict(receipt, operation='kernel-upgrade')),
                     prefix+'/complete.json': encoded(dict(receipt, kernel_release=record['release'], rootfs_reformatted=False))}
        return plan, files, completed

    def unchanged(self, root, plan, transitional=False):
        """Refuse rollback over an unrelated subsequent default switch/edit."""
        for name, entry in plan['before'].items():
            data = self.read(root, name)
            if transitional and name in plan['replacements']:
                if data in (bytes.fromhex(plan['originals'][name]), bytes.fromhex(plan['replacements'][name])): continue
            if len(data) != entry['bytes'] or checksum(data) != entry['sha256']:
                raise RuntimeError('引导存储在更新后发生其他修改，拒绝覆盖：' + name)
        target = self.updater.regular(self.target_path).read_bytes()
        accepted = [bytes.fromhex(plan['old_target'])]
        if transitional: accepted.append(bytes.fromhex(plan['new_target']))
        if target not in accepted: raise RuntimeError('根分区绑定已被其他操作修改。')
        if (root/'.retention/active.json').exists(): raise RuntimeError('引导清理正在进行。')

    def stage(self, record, directory):
        plan, files, _ = self.materials(record)
        with self.updater.device.mount_store(self.identity, writable=True) as root:
            self.unchanged(root, plan)
            names = [*files, plan['new_system']['image']]
            if any(safe_path(root, n).exists() for n in names): raise RuntimeError('更新版本已存在，拒绝覆盖。')
            for name, data in files.items(): atomic(safe_path(root, name), data)
            image = self.updater.regular(directory/'candidate.img')
            if sha(image) != plan['new_system']['boot_sha256']: raise RuntimeError('暂存引导镜像已改变。')
            atomic(safe_path(root, plan['new_system']['image']), image.read_bytes())

    def commit(self, record):
        plan, files, completed = self.materials(record)
        with self.updater.device.mount_store(self.identity, writable=True) as root:
            self.unchanged(root, plan)
            for name, data in files.items():
                if self.read(root, name) != data: raise RuntimeError('更新材料已改变。')
            if sha(root/plan['new_system']['image']) != plan['new_system']['boot_sha256']:
                raise RuntimeError('已存储的新镜像校验失败。')
            atomic(self.target_path, bytes.fromhex(plan['new_target']))
            for name, data in plan['replacements'].items(): atomic(safe_path(root, name), bytes.fromhex(data))
            for name, data in completed.items(): atomic(safe_path(root, name), data)

    def verify(self, record):
        plan, files, completed = self.materials(record)
        with self.updater.device.mount_store(self.identity) as root:
            registry, system = self.inspect(root)
            if registry != plan['new_registry'] or system != plan['new_system']:
                raise RuntimeError('已写入引导与当前默认登记不一致。')
            for name, data in {**files, **completed}.items():
                if self.read(root, name) != data: raise RuntimeError('更新事务记录不完整。')
            if self.read(root, 'SHA256SUMS') != bytes.fromhex(plan['replacements']['SHA256SUMS']):
                raise RuntimeError('更新校验清单不一致。')

    def can_restore(self, record):
        plan, _, _ = self.materials(record)
        with self.updater.device.mount_store(self.identity) as root:
            self.unchanged(root, plan, transitional=True)

    def restore(self, record):
        plan, _, _ = self.materials(record)
        with self.updater.device.mount_store(self.identity, writable=True) as root:
            self.unchanged(root, plan, transitional=True)
            atomic(self.target_path, bytes.fromhex(plan['old_target']))
            for name, data in plan['originals'].items(): atomic(safe_path(root, name), bytes.fromhex(data))
            receipt = encoded(dict(registry_sha256=checksum(bytes.fromhex(plan['originals']['registry.json'])),
                                   boot_sha256=record['old_sha256'], operation='kernel-upgrade-rollback'))
            atomic(root/'transactions'/('linux-'+plan['generation'])/'rolled-back.json', receipt)
            atomic(root/'kernel-updates'/plan['generation']/'complete.json', receipt)
        with self.updater.device.mount_store(self.identity) as root:
            self.unchanged(root, plan)

    def protected_releases(self):
        with self.updater.device.mount_store(self.identity) as root:
            _, current = self.inspect(root)
            protected = {current['kernel_release']}
            previous = root/'kernel-updates'/self.generation(current)/'previous-registry.json'
            if previous.exists():
                old = self.json(previous.parent, previous.name)
                protected.update(s['kernel_release'] for s in old['systems'] if s['id'] == current['id'])
            return protected
