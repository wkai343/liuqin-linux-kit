# SPDX-License-Identifier: MIT
"""Versioned kernel identity, independent of mutable distribution files."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

HEADER = b'LIUQIN_ROOT_INTERFACE_V2\n'
ROOT_MARKER = b'liuqin-root-v2\n'
RELEASE = r'[A-Za-z0-9][A-Za-z0-9._+-]{0,254}'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def parse_contract(content):
    match = re.fullmatch(rb'LIUQIN_ROOT_INTERFACE_V2\nkernel_release=(' + RELEASE.encode() +
                        rb')\nimage_sha256=([0-9a-f]{64})\nmodules_sha256=([0-9a-f]{64})\n', content)
    if not match:
        raise ValueError('无效的内核启动接口。')
    release, image, modules = (value.decode('ascii') for value in match.groups())
    if release in ('.', '..'):
        raise ValueError('无效的内核版本。')
    return dict(release=release, image_sha256=image, modules_sha256=modules)


def module_records(content, release):
    records = {}
    prefix = 'usr/lib/modules/' + release + '/'
    for row in content.decode('ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (\S+)', row)
        if not match:
            raise ValueError('无效的模块清单。')
        sha, name = match.groups()
        if (not name.startswith(prefix) or '..' in PurePosixPath(name).parts or
                str(PurePosixPath(name)) != name or name in records):
            raise ValueError('模块清单路径无效：' + name)
        records[name] = sha
    if prefix + 'modules.dep' not in records:
        raise ValueError('模块清单缺少 modules.dep。')
    return records


def build_contract(kernel, modules):
    kernel, modules = Path(kernel), Path(modules)
    release = (kernel / 'include/config/kernel.release').read_text().strip()
    info = json.loads((kernel / 'build-info.json').read_text())
    actual = dict(commit=info['commit'], release=release,
                  image_sha256=digest(kernel / 'arch/arm64/boot/Image'),
                  config_sha256=digest(kernel / '.config'),
                  archive_sha256=digest(modules / 'modules.tar'),
                  manifest_sha256=digest(modules / 'modules.manifest'))
    if (json.loads((modules / 'kernel.build.json').read_text()) != actual or
            info['config_sha256'] != actual['config_sha256'] or
            (modules / 'kernel.release').read_text().strip() != release or
            (modules / 'kernel.commit').read_text().strip() != info['commit']):
        raise ValueError('内核、配置与模块构建记录不一致。')
    content = (HEADER + f'kernel_release={release}\nimage_sha256={actual["image_sha256"]}\n'
               f'modules_sha256={actual["manifest_sha256"]}\n'.encode())
    parse_contract(content)
    records = module_records((modules / 'modules.manifest').read_bytes(), release)
    found, seen = {}, set()
    with tarfile.open(modules / 'modules.tar') as archive:
        for item in archive:
            name = item.name
            if name in seen or str(PurePosixPath(name)) != name or '..' in PurePosixPath(name).parts:
                raise ValueError('模块归档包含重复或不安全路径。')
            seen.add(name)
            if item.isdir():
                if name not in ('usr/lib/modules', 'usr/lib/modules/' + release) and not name.startswith('usr/lib/modules/' + release + '/'):
                    raise ValueError('模块目录超出版本范围。')
                continue
            if not item.isfile() or name not in records or item.mode != 0o644:
                raise ValueError('模块归档包含无效文件：' + name)
            found[name] = hashlib.file_digest(archive.extractfile(item), 'sha256').hexdigest()
    if found != records:
        raise ValueError('模块内容与清单不一致。')
    return content


def extract_modules(modules, destination, contract):
    """Extract only authenticated regular module files, validating during extraction."""
    modules, destination = Path(modules), Path(destination).resolve()
    identity = parse_contract(contract)
    manifest = (modules / 'modules.manifest').read_bytes()
    if hashlib.sha256(manifest).hexdigest() != identity['modules_sha256']:
        raise ValueError('模块清单在构建期间发生变化。')
    records = module_records(manifest, identity['release'])
    prefix = 'usr/lib/modules/' + identity['release']
    found, seen = {}, set()
    with tarfile.open(modules / 'modules.tar') as archive:
        for item in archive:
            name = item.name
            if name in seen or str(PurePosixPath(name)) != name or '..' in PurePosixPath(name).parts:
                raise ValueError('模块归档路径无效。')
            seen.add(name)
            if item.isdir():
                if name not in ('usr/lib/modules', prefix) and not name.startswith(prefix + '/'):
                    raise ValueError('模块目录超出版本范围。')
                continue
            if not item.isfile() or name not in records or item.mode != 0o644:
                raise ValueError('模块归档包含未声明文件或链接。')
            path = destination / name
            if path.resolve() != path:
                raise ValueError('模块目标路径包含符号链接。')
            path.parent.mkdir(parents=True, exist_ok=True)
            value = hashlib.sha256()
            with archive.extractfile(item) as source, path.open('xb') as output:
                while data := source.read(1024 * 1024):
                    output.write(data); value.update(data)
            path.chmod(0o644)
            found[name] = value.hexdigest()
    if found != records:
        raise ValueError('模块内容在构建期间发生变化。')
    return manifest


def verify_installed(root, content):
    """Check an installed kernel without executing any code in the target root."""
    root = Path(root).resolve()
    info = parse_contract(content)
    base = 'usr/share/liuqin/kernels/' + info['release']
    def regular(name):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError('内核文件不是独立普通文件：' + name)
        if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
            raise ValueError('内核文件所有权或写入权限无效：' + name)
        return path
    if regular('etc/liuqin-root-interface').read_bytes() != ROOT_MARKER:
        raise ValueError('根系统的启动接口不受支持。')
    if regular('etc/liuqin-native-root').read_bytes() != b'liuqin-native-root-v1\n':
        raise ValueError('根目录身份无效。')
    guard = 'usr/local/sbin/liuqin-gnome-storage-guard'
    directory = root / 'usr/local/sbin'
    if directory.is_symlink() and os.readlink(directory) == 'bin':
        guard = 'usr/local/bin/liuqin-gnome-storage-guard'
    for name, mode in ((guard, 0o755),
                       ('etc/systemd/system/liuqin-gnome-storage-guard.service', 0o644),
                       ('usr/lib/systemd/systemd', 0o755)):
        if regular(name).stat().st_mode & 0o777 != mode:
            raise ValueError('启动组件权限无效：' + name)
    link = root / 'etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service'
    if not link.is_symlink() or os.readlink(link) != '../liuqin-gnome-storage-guard.service':
        raise ValueError('存储保护服务未启用。')
    if regular(base + '/boot.contract').read_bytes() != content:
        raise ValueError('已安装内核身份不匹配。')
    manifest = regular(base + '/modules.manifest')
    if digest(manifest) != info['modules_sha256']:
        raise ValueError('已安装模块清单不匹配。')
    for name, expected in module_records(manifest.read_bytes(), info['release']).items():
        if digest(regular(name)) != expected:
            raise ValueError('已安装模块损坏或不匹配：' + name)
    return info
