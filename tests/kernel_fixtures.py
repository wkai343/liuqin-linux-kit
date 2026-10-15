# SPDX-License-Identifier: MIT
"""Synthetic kernels/modules/images for offline tests; never usable on hardware."""
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tarfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'shared'))
sys.path.insert(0, str(PROJECT / 'installer'))
from liuqin_kernel import build_contract, digest
from liuqin_gui.demo import demo_image


def put(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content); path.chmod(mode)


def kernel_fixture(base, release):
    kernel, modules = base / 'kernel', base / 'modules'
    source = base / 'source'
    put(source / 'COPYING', b'SYNTHETIC KERNEL LICENSE FIXTURE ONLY\n')
    put(source / 'LICENSES/test-license', b'SYNTHETIC LICENSE FILE\n')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', 'COPYING', 'LICENSES'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Synthetic fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    image = b'SYNTHETIC KERNEL NOT BOOTABLE ' + release.encode()
    put(kernel / 'arch/arm64/boot/Image', image)
    put(kernel / '.config', b'CONFIG_SYNTHETIC_TEST=y\n')
    put(kernel / 'include/config/kernel.release', (release + '\n').encode())
    info = dict(commit=commit, config_sha256=digest(kernel / '.config'))
    put(kernel / 'build-inputs.json', json.dumps({'source': str(source)}).encode())
    put(kernel / 'build-info.json', json.dumps(info).encode())
    put(kernel / 'SHA256SUMS', ''.join(digest(kernel / name) + '  ' + name + '\n' for name in
        ('arch/arm64/boot/Image', '.config', 'build-info.json')).encode())
    prefix = 'usr/lib/modules/' + release + '/'
    contents = {prefix + 'modules.dep': b'kernel/demo.ko:\n', prefix + 'kernel/demo.ko': b'SYNTHETIC MODULE ' + release.encode()}
    modules.mkdir(parents=True)
    with tarfile.open(modules / 'modules.tar', 'w') as archive:
        for name, data in contents.items():
            entry = tarfile.TarInfo(name); entry.mode = 0o644; entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    put(modules / 'modules.manifest', ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in contents.items()).encode())
    put(modules / 'kernel.release', (release + '\n').encode()); put(modules / 'kernel.commit', (commit + '\n').encode())
    metadata = dict(commit=info['commit'], release=release, image_sha256=digest(kernel / 'arch/arm64/boot/Image'),
                    config_sha256=info['config_sha256'], archive_sha256=digest(modules / 'modules.tar'),
                    manifest_sha256=digest(modules / 'modules.manifest'))
    put(modules / 'kernel.build.json', json.dumps(metadata).encode())
    contract = build_contract(kernel, modules)
    boot = demo_image({'etc/liuqin-storage-mode': b'persistent\n',
                       'etc/liuqin-root-profile': b'native-updatable\n',
                       'etc/liuqin-native-root.contract': contract}, image)
    path = base / 'boot.img'; put(path, boot)
    return kernel, modules, path, contract, contents


def install_fixture(root, release, fixture):
    kernel, modules, boot, contract, contents = fixture
    for name, data in contents.items(): put(root / name, data)
    base = root / 'usr/share/liuqin/kernels' / release
    put(base / 'boot.contract', contract)
    put(base / 'modules.manifest', (modules / 'modules.manifest').read_bytes())
    put(root / 'usr/lib/liuqin/boot-images' / release / 'boot.img', boot.read_bytes())
    put(root / 'etc/liuqin-root-interface', b'liuqin-root-v2\n')
    put(root / 'etc/liuqin-native-root', b'liuqin-native-root-v1\n')
    elf = bytearray(20); elf[:6] = b'\x7fELF\x02\x01'; elf[18:20] = (183).to_bytes(2, 'little')
    put(root / 'usr/lib/systemd/systemd', bytes(elf), 0o755)
    put(root / 'etc/passwd', b'fastrpc:x:1:1::/:/bin/false\n'); put(root / 'etc/group', b'fastrpc:x:1:\n')
    put(root / 'usr/local/sbin/liuqin-gnome-storage-guard', b'#!/bin/sh\nexit 0\n', 0o755)
    put(root / 'etc/systemd/system/liuqin-gnome-storage-guard.service', b'[Service]\nType=oneshot\n')
    link = root / 'etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service'
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink(): link.symlink_to('../liuqin-gnome-storage-guard.service')


def archive_root(root, output):
    with tarfile.open(output, 'w:gz') as archive:
        for path in sorted(root.rglob('*')):
            entry = archive.gettarinfo(str(path), path.relative_to(root).as_posix())
            entry.uid = entry.gid = 0; entry.uname = entry.gname = ''
            if entry.isfile():
                with path.open('rb') as stream: archive.addfile(entry, stream)
            else: archive.addfile(entry)


def installer_fixture():
    return demo_image({'etc/liuqin-storage-mode': b'readonly\n', 'etc/liuqin-installer': b'liuqin\n',
                       'etc/liuqin-installer-interface': b'liuqin-installer-v2\n',
                       'etc/liuqin-installer-kernel.release': b'6.17-installer\n'}, b'SYNTHETIC INSTALLER')
