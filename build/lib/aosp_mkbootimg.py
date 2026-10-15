# SPDX-License-Identifier: MIT
"""Verify Gitiles archive contents against the pinned AOSP Git source tree."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
COMMIT = '954bc3ead5e679005fddf3484d247f2557b3c2c9'
# https://android.googlesource.com/platform/system/tools/mkbootimg/+/COMMIT
TREE = 'f2e7356e539626ed5a3565d19f41687da9b1e387'
URL = 'https://android.googlesource.com/platform/system/tools/mkbootimg/+archive/' + COMMIT + '.tar.gz'
ENTRY_SHA256 = {
    'mkbootimg.py': '37d84b3d162e0bc62e36c1f4e1c63c85ea0caa9f29be023eb2f8efe006ad948c',
    'unpack_bootimg.py': '06b54dd9a07c5281778e29e234e76f6e3faee8bf0c904a5ef88fdee30eeed12e',
}
MAX_BYTES = 32 * 1024 * 1024


def git_object(kind, payload):
    return hashlib.sha1(kind + b' ' + str(len(payload)).encode() + b'\0' + payload).digest()


def source_tree(files):
    root = {}
    for name, (mode, payload) in files.items():
        node = root
        parts = name.split('/')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError('源码路径同时是文件和目录：' + name)
        if parts[-1] in node:
            raise ValueError('源码路径重复或冲突：' + name)
        node[parts[-1]] = (b'100755' if mode == 0o755 else b'100644', git_object(b'blob', payload))

    def tree(node):
        payload = b''
        # Git sorts directory names as if a trailing slash were present.
        for name, value in sorted(node.items(), key=lambda pair: pair[0].encode() + (b'/' if isinstance(pair[1], dict) else b'')):
            mode, digest = (b'40000', tree(value)) if isinstance(value, dict) else value
            payload += mode + b' ' + name.encode() + b'\0' + digest
        return git_object(b'tree', payload)

    return tree(root).hex()


def validate(files, directories, *, expected_tree=None, entry_hashes=None):
    expected_tree = TREE if expected_tree is None else expected_tree
    entry_hashes = ENTRY_SHA256 if entry_hashes is None else entry_hashes
    parents = {str(parent) for name in files for parent in Path(name).parents if str(parent) != '.'}
    if not directories <= parents:
        raise ValueError('源码包含额外或空目录。')
    if source_tree(files) != expected_tree:
        raise ValueError('源码内容与固定 AOSP 提交不一致；保留现有缓存，未使用。')
    for name, expected in entry_hashes.items():
        if name not in files or hashlib.sha256(files[name][1]).hexdigest() != expected:
            raise ValueError('AOSP 入口文件校验失败：' + name)
    return files


def archive_files(path, *, expected_tree=None, entry_hashes=None):
    files, directories, seen = {}, set(), set()
    total = 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = member.name.rstrip('/') if member.isdir() else member.name
            if '\\' in name or any(part in ('', '.', '..', '.git') for part in name.split('/')) or name in seen:
                raise ValueError('归档含不安全或重复路径：' + member.name)
            seen.add(name)
            if len(seen) > 1024:
                raise ValueError('AOSP 归档文件数量异常。')
            if member.isdir():
                directories.add(name)
                continue
            mode = stat.S_IMODE(member.mode)
            if not member.isfile() or mode not in (0o644, 0o755):
                raise ValueError('归档含链接、特殊文件或异常权限：' + name)
            total += member.size
            if member.size < 0 or total > MAX_BYTES:
                raise ValueError('AOSP 归档大小异常。')
            payload = archive.extractfile(member).read()
            if len(payload) != member.size:
                raise ValueError('AOSP 归档文件不完整：' + name)
            files[name] = (mode, payload)
    return validate(files, directories, expected_tree=expected_tree, entry_hashes=entry_hashes)


def directory_files(path, *, clean_bytecode=False):
    if path.is_symlink() or not path.is_dir():
        raise ValueError('AOSP 工具缓存不是普通目录：' + str(path))
    files, directories = {}, set()
    bytecode, cache_directories = [], []
    total = 0
    for entry in path.rglob('*'):
        name = entry.relative_to(path).as_posix()
        info = entry.lstat()
        if clean_bytecode and '__pycache__' in Path(name).parts:
            # Never execute cached bytecode, or hide arbitrary added files from
            # the source-tree check. Only remove CPython cache files belonging
            # to verified source modules, after the complete source validates.
            if entry.name == '__pycache__' and stat.S_ISDIR(info.st_mode):
                cache_directories.append(entry)
                continue
            match = re.fullmatch(r'(.+)\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc', entry.name)
            if entry.parent.name != '__pycache__' or not match or not stat.S_ISREG(info.st_mode):
                raise ValueError('AOSP Python 缓存含非字节码文件或链接：' + name)
            source = entry.parent.parent / (match[1] + '.py')
            bytecode.append((entry, source.relative_to(path).as_posix()))
            continue
        if stat.S_ISDIR(info.st_mode):
            directories.add(name)
        elif stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) in (0o644, 0o755):
            total += info.st_size
            if total > MAX_BYTES:
                raise ValueError('AOSP 工具缓存大小异常。')
            files[name] = (stat.S_IMODE(info.st_mode), entry.read_bytes())
        else:
            raise ValueError('AOSP 工具缓存含链接、特殊文件或异常权限：' + name)
    verified = validate(files, directories)
    for entry, source in bytecode:
        if source not in verified:
            raise ValueError('AOSP Python 缓存没有对应的固定源码：' + str(entry.relative_to(path)))
    for directory in cache_directories:
        parent = directory.parent.relative_to(path).as_posix()
        if not any(Path(name).parent.as_posix() == parent and name.endswith('.py') for name in verified):
            raise ValueError('AOSP Python 缓存目录没有对应的固定源码：' + str(directory.relative_to(path)))
    for entry, _ in bytecode:
        entry.unlink()
    for directory in cache_directories:
        directory.rmdir()
    if bytecode:
        print('已清理 AOSP Python 字节码缓存：' + str(len(bytecode)) + ' 个（源码校验通过）', flush=True)
    return verified


def prepare(project=PROJECT):
    downloads = project / 'inputs/downloads'
    target = project / 'inputs/aosp-mkbootimg'
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise ValueError('AOSP 工具缓存路径异常：' + str(target))
        if any(target.iterdir()):
            directory_files(target, clean_bytecode=True)
            print('复用：AOSP mkbootimg ' + COMMIT[:12] + '（源码校验通过）', flush=True)
            return target
    archive = downloads / ('aosp-mkbootimg-' + COMMIT + '.tar.gz')
    downloads.mkdir(parents=True, exist_ok=True)
    if archive.exists() or archive.is_symlink():
        if archive.is_symlink() or not archive.is_file():
            raise ValueError('AOSP 下载缓存路径异常：' + str(archive))
        files = archive_files(archive)
    else:
        with tempfile.TemporaryDirectory(prefix='.mkbootimg-download-', dir=downloads) as directory:
            temporary = Path(directory) / 'source.tar.gz'
            with urllib.request.urlopen(URL, timeout=60) as source, temporary.open('xb') as output:
                total = 0
                while block := source.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_BYTES:
                        raise ValueError('AOSP 下载大小异常。')
                    output.write(block)
            files = archive_files(temporary)
            # Never publish a failed download or replace another writer's cache.
            os.link(temporary, archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mkbootimg-extract-', dir=target.parent) as directory:
        temporary = Path(directory) / 'source'
        temporary.mkdir()
        for name, (mode, payload) in files.items():
            file = temporary / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(payload)
            file.chmod(mode)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        for name in ENTRY_SHA256:
            subprocess.run([sys.executable, str(temporary / name), '--help'], env=env, check=True, stdout=subprocess.DEVNULL)
        directory_files(temporary)
        if target.exists():
            target.rmdir()  # Only the empty directory left by the old fetcher.
        os.rename(temporary, target)
    print('已准备：AOSP mkbootimg ' + COMMIT[:12] + '（完整源码树及入口 SHA256 校验通过）', flush=True)
    return target


if __name__ == '__main__':
    try:
        prepare()
    except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('mkbootimg 准备停止：' + str(error), file=sys.stderr)
        sys.exit(1)
