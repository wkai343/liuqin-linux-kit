# SPDX-License-Identifier: MIT
"""Host-only build primitives: verified inputs, immutable cache and ARM64 chroot."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parents[2]
ARM64_MAGIC = bytes.fromhex('7f454c460201010000000000000000000200b700')
# Match QEMU's AArch64 rule: ignore OSABI and allow ET_EXEC/ET_DYN.
ARM64_MASK = bytes.fromhex('ffffffffffffff00fffffffffffffffffeffffff')


def arm64_registration(name, interpreter):
    """binfmt_misc stops decoding at raw NUL; use textual hex for every byte."""
    if not re.fullmatch(r'[A-Za-z0-9._+-]+', name):
        raise ValueError('ARM64 执行规则名称无效。')
    if not os.path.isabs(interpreter) or any(char in interpreter for char in (':', '\n', '\0')):
        raise ValueError('QEMU 解释器路径无效。')
    encoded = lambda value: ''.join('\\x%02x' % byte for byte in value).encode('ascii')
    return b':' + name.encode('ascii') + b':M::' + encoded(ARM64_MAGIC) + b':' + encoded(ARM64_MASK) + b':' + os.fsencode(interpreter) + b':F'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tree_identity(root):
    """Include modes, links and working-tree content, rather than only Git HEAD."""
    root = Path(root)
    records = []
    if not root.exists(): raise ValueError('输入目录不存在：' + str(root))
    for path in sorted(root.rglob('*')):
        if '.git' in path.relative_to(root).parts or '__pycache__' in path.parts: continue
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if path.is_symlink(): value = ['link', os.readlink(path)]
        elif path.is_file(): value = ['file', sha(path)]
        elif path.is_dir(): value = ['dir']
        else: raise ValueError('构建输入含特殊文件：' + str(path))
        records.append([name, stat.S_IMODE(info.st_mode), *value])
    return key(records)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def download(url, path, expected):
    path = Path(path)
    if not re.fullmatch('[0-9a-f]{64}', expected) or not url.startswith('https://'):
        raise ValueError('下载需要 HTTPS 来源和固定 SHA256。')
    if path.exists():
        if not path.is_file() or path.is_symlink() or sha(path) != expected:
            raise ValueError('缓存文件校验失败，保留文件并停止：' + str(path))
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ('.download-' + uuid.uuid4().hex)
    try:
        with urllib.request.urlopen(url, timeout=60) as source, temporary.open('xb') as output:
            while block := source.read(1024 * 1024): output.write(block)
            output.flush(); os.fsync(output.fileno())
        if sha(temporary) != expected: raise ValueError('下载内容与固定 SHA256 不一致。')
        os.link(temporary, path)
    finally: temporary.unlink(missing_ok=True)
    return path


class Cache:
    def __init__(self, root, run):
        self.root, self.run = Path(root), run

    def stage(self, name, inputs, build, artifacts):
        identity = key(inputs)
        destination = self.root / (name + '-' + identity[:20])
        receipt = destination / 'build-receipt.json'
        if destination.exists():
            if not receipt.is_file(): raise ValueError('构建缓存缺少完成记录：' + str(destination))
            record = json.loads(receipt.read_text())
            if record.get('inputs') != identity or set(record.get('artifacts', {})) != set(artifacts):
                raise ValueError('构建缓存身份或产物列表不一致。')
            for name, expected in record['artifacts'].items():
                path = destination / name
                actual = {'tree': tree_identity(path)} if path.is_dir() and not path.is_symlink() else {'file': sha(path)} if path.is_file() and not path.is_symlink() else None
                if actual != expected:
                    raise ValueError('构建缓存损坏，保留文件并停止：' + str(path))
            print('复用：' + destination.name, flush=True)
            return destination
        self.root.mkdir(parents=True, exist_ok=True)
        # The leaf gets a fresh path; failed work is retained outside published caches.
        attempt = self.root / ('.attempt-' + name + '-' + uuid.uuid4().hex)
        attempt.mkdir()
        build(attempt)
        hashes = {}
        for name in artifacts:
            path = attempt / name
            if path.is_dir() and not path.is_symlink(): hashes[name] = {'tree': tree_identity(path)}
            elif path.is_file() and not path.is_symlink(): hashes[name] = {'file': sha(path)}
            else: raise ValueError('构建未生成完整产物：' + str(path))
        record = dict(inputs=identity, artifacts=hashes)
        receipt = attempt / 'build-receipt.json'
        receipt.write_text(json.dumps(record, indent=2) + '\n')
        os.rename(attempt, destination)
        return destination


@contextlib.contextmanager
def arm64_execution():
    """Own a temporary binfmt entry; retain all existing host registrations."""
    if os.uname().machine == 'aarch64':
        yield; return
    interpreter = shutil.which('qemu-aarch64')
    if not interpreter: raise ValueError('缺少 Devbox 提供的静态 qemu-aarch64。')
    interpreter = str(Path(interpreter).resolve())
    if len(os.fsencode(interpreter)) > 127: raise ValueError('QEMU 解释器路径超过 binfmt 支持长度。')
    from elftools.elf.elffile import ELFFile
    with open(interpreter, 'rb') as stream:
        if any(segment['p_type'] == 'PT_INTERP' for segment in ELFFile(stream).iter_segments()):
            raise ValueError('跨架构构建要求静态 QEMU。')
    registry = Path('/proc/sys/fs/binfmt_misc')
    if not (registry / 'register').exists():
        subprocess.run(['mount', '-t', 'binfmt_misc', 'binfmt_misc', str(registry)], check=True)
    name = 'liuqin-' + uuid.uuid4().hex[:16]
    registration = arm64_registration(name, interpreter)
    entry = registry / name
    try:
        (registry / 'register').write_bytes(registration)
        fields = dict(line.split(' ', 1) for line in entry.read_text().splitlines() if ' ' in line)
        if fields.get('magic') != ARM64_MAGIC.hex() or fields.get('mask') != ARM64_MASK.hex() or fields.get('interpreter') != interpreter or fields.get('offset') != '0' or fields.get('flags:') != 'F':
            raise ValueError('ARM64 执行规则回读不完整，已停止并清理临时规则。')
        yield
    finally:
        if entry.exists(): entry.write_text('-1')


@contextlib.contextmanager
def mounted_root(root):
    """Run only in a private mount namespace; no host block devices are exposed."""
    root = Path(root).resolve()
    mounted = []
    created_policy = False
    changed_resolver = False
    try:
        for name in ('proc', 'dev', 'dev/pts', 'run', 'sys', 'tmp'):
            (root / name).mkdir(parents=True, exist_ok=True)
        for source, name, filesystem, options in (
            ('tmpfs', 'dev', 'tmpfs', 'mode=755,nosuid'),
            ('proc', 'proc', 'proc', 'ro,nosuid,nodev,noexec'),
            ('tmpfs', 'run', 'tmpfs', 'mode=755,nosuid,nodev'),
        ):
            subprocess.run(['mount', '-t', filesystem, '-o', options, source, str(root / name)], check=True)
            mounted.append(root / name)
        (root / 'dev/pts').mkdir(exist_ok=True)
        subprocess.run(['mount','-t','devpts','-o','newinstance,ptmxmode=0666,mode=0620',
                        'devpts',str(root/'dev/pts')],check=True)
        mounted.append(root/'dev/pts')
        for name, major, minor, mode in (('null',1,3,0o666),('zero',1,5,0o666),('random',1,8,0o666),('urandom',1,9,0o666),('tty',5,0,0o666)):
            try:
                os.mknod(root / 'dev' / name, stat.S_IFCHR | mode, os.makedev(major, minor))
            except PermissionError:
                # A user namespace cannot create device nodes. Bind only these
                # five standard character devices, never the host /dev tree.
                target = root / 'dev' / name
                target.touch()
                subprocess.run(['mount','--bind','/dev/'+name,str(target)],check=True)
                mounted.append(target)
        os.symlink('/proc/self/fd', root / 'dev/fd')
        os.symlink('pts/ptmx',root/'dev/ptmx')
        for number,name in enumerate(('stdin','stdout','stderr')):
            os.symlink('/proc/self/fd/'+str(number),root/'dev'/name)
        policy = root / 'usr/sbin/policy-rc.d'
        if policy.exists() or policy.is_symlink(): raise ValueError('基础目录已有服务启动策略，未覆盖。')
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text('#!/bin/sh\nexit 101\n'); policy.chmod(0o755)
        created_policy = True
        resolver = root / 'etc/resolv.conf'
        if resolver.is_symlink(): resolver.unlink()
        resolver.write_bytes(Path('/etc/resolv.conf').read_bytes())
        changed_resolver = True
        with arm64_execution(): yield
    finally:
        failures = []
        for mount in reversed(mounted):
            if subprocess.run(['umount', str(mount)]).returncode: failures.append(str(mount))
        if created_policy: (root / 'usr/sbin/policy-rc.d').unlink(missing_ok=True)
        # Host DNS configuration and runtime state are not image content.
        resolver = root / 'etc/resolv.conf'
        if changed_resolver:
            if resolver.exists() or resolver.is_symlink(): resolver.unlink()
            resolver.symlink_to('../run/systemd/resolve/stub-resolv.conf')
        if failures: raise RuntimeError('构建挂载未能正常卸载：' + ', '.join(failures))
