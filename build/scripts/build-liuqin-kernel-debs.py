#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build coinstallable kernel packages and an opt-in boot updater, without device access."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'shared'))
from liuqin_kernel import build_contract, parse_contract, digest, extract_modules
from liuqin_artifacts import boot_interface

TOOLS_VERSION = '0.1.3'


def put(root, name, content, mode=0o644):
    path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)


def control(root, name, version, depends, description, predepends=''):
    put(root, 'DEBIAN/control', f'Package: {name}\nVersion: {version}\nArchitecture: arm64\n'
        'Maintainer: Liuqin Linux Kit contributors\nSection: kernel\nPriority: optional\n'
        + (f'Depends: {depends}\n' if depends else '')
        + (f'Pre-Depends: {predepends}\n' if predepends else '')
        + f'Description: {description}\n')
    for document in ('LICENSE', 'NOTICE', 'CREDITS.md'):
        put(root, f'usr/share/doc/{name}/{document}', (PROJECT / document).read_bytes())


def build(kernel, modules, boot_image, output, version, kernel_source=None, package_format='deb'):
    kernel, modules, boot_image, output = map(lambda p: Path(p).resolve(), (kernel, modules, boot_image, output))
    if output.exists(): raise ValueError('输出已存在，请使用新目录。')
    if not re.fullmatch(r'[0-9][a-zA-Z0-9.+:~\-]*', version): raise ValueError('无效 Debian 包版本。')
    content = build_contract(kernel, modules)
    source = Path(kernel_source or json.loads((kernel / 'build-inputs.json').read_text())['source']).resolve()
    recorded = json.loads((kernel / 'build-info.json').read_text())
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != recorded['commit'] or subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain']):
        raise ValueError('对应内核源码提交不一致或有未提交修改。')
    if not (source / 'COPYING').is_file() or not (source / 'LICENSES').is_dir():
        raise ValueError('内核源码缺少原始 COPYING/LICENSES。')
    identity = parse_contract(content); release = identity['release']
    if not re.fullmatch(r'[0-9][a-z0-9.+-]*liuqin[a-z0-9.+-]*', release):
        raise ValueError('软件包的 kernel release 需要包含 liuqin，且只使用小写字母、数字、点、加号或连字符。')
    boot = boot_interface(boot_image)
    if boot['profile'] != b'native-updatable\n' or boot['contract'] != content:
        raise ValueError('boot.img 与内核/模块不是同一组内核产物。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.kernel-debs-', dir=output.parent) as directory:
        work = Path(directory); packages = work / 'packages'; packages.mkdir()
        tools = work / 'tools'
        control(tools, 'liuqin-boot-tools', TOOLS_VERSION, 'python3 (>= 3.11), python3-zstandard (>= 0.23)',
                'Liuqin versioned kernel boot update and recovery tools')
        put(tools, 'usr/lib/liuqin/boot-update.py', (PROJECT / 'device/boot/boot-update.py').read_bytes(), 0o755)
        put(tools, 'etc/liuqin-root-interface', b'liuqin-root-v2\n')
        for name in ('liuqin_kernel.py', 'liuqin_artifacts.py', 'liuqin_filesystems.py'):
            put(tools, 'usr/lib/liuqin/' + name, (PROJECT / 'shared' / name).read_bytes())
        put(tools, 'usr/sbin/liuqin-boot-update', '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/liuqin/boot-update.py "$@"\n', 0o755)
        put(tools, 'etc/kernel/postinst.d/zz-liuqin-boot', '#!/bin/sh\nset -eu\nexec /usr/sbin/liuqin-boot-update hook "$1" >&2\n', 0o755)
        put(tools, 'etc/apt/apt.conf.d/99-liuqin-kernels',
            'APT::NeverAutoRemove { "^liuqin-kernel-image-.*$"; "^liuqin-kernel-modules-.*$"; };\n')
        put(tools, 'usr/lib/systemd/system/liuqin-kernel-confirm.service',
            '[Unit]\nDescription=Confirm the running Liuqin kernel update\n'
            'After=local-fs.target liuqin-gnome-storage-guard.service\n'
            'Requires=liuqin-gnome-storage-guard.service\n'
            'ConditionPathExists=/etc/liuqin/boot-updates.json\n'
            '[Service]\nType=oneshot\nExecStart=/usr/sbin/liuqin-boot-update confirm\n'
            '[Install]\nWantedBy=multi-user.target\n')
        link = tools / 'etc/systemd/system/multi-user.target.wants/liuqin-kernel-confirm.service'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to('/usr/lib/systemd/system/liuqin-kernel-confirm.service')
        put(tools, 'DEBIAN/conffiles', '/etc/apt/apt.conf.d/99-liuqin-kernels\n')

        image_name, module_name = 'liuqin-kernel-image-' + release, 'liuqin-kernel-modules-' + release
        image, module, meta = work / 'image', work / 'modules', work / 'meta'
        control(module, module_name, version, 'kmod', 'Liuqin kernel modules for ' + release,
                f'liuqin-boot-tools (>= {TOOLS_VERSION})')
        module.mkdir(parents=True, exist_ok=True)
        manifest = extract_modules(modules, module, content)
        put(module, 'usr/share/liuqin/kernels/' + release + '/modules.manifest', manifest)
        put(module, 'usr/share/liuqin/kernels/' + release + '/modules.contract', content)
        control(image, image_name, version, f'{module_name} (= {version})', 'Liuqin boot image for ' + release,
                f'liuqin-boot-tools (>= {TOOLS_VERSION})')
        for root, name in ((image, image_name), (module, module_name)):
            put(root, f'usr/share/doc/{name}/kernel-COPYING', (source / 'COPYING').read_bytes())
            shutil.copytree(source / 'LICENSES', root / f'usr/share/doc/{name}/kernel-LICENSES')
            put(root, f'usr/share/doc/{name}/kernel-source.txt',
                'Kernel source commit: ' + commit + '\nCorresponding configuration: kernel-config\n'
                'Redistribution must include or provide the corresponding kernel source and changes.\n')
            put(root, f'usr/share/doc/{name}/kernel-config', (kernel / '.config').read_bytes())
        put(image, 'usr/lib/liuqin/boot-images/' + release + '/boot.img', boot_image.read_bytes())
        packaged_boot = image / 'usr/lib/liuqin/boot-images' / release / 'boot.img'
        packaged_interface = boot_interface(packaged_boot)
        if packaged_interface['profile'] != b'native-updatable\n' or packaged_interface['contract'] != content:
            raise ValueError('boot 镜像在打包期间发生变化。')
        put(image, 'usr/share/liuqin/kernels/' + release + '/boot.contract', content)
        put(image, 'usr/share/liuqin/kernels/' + release + '/build-info.json', json.dumps(recorded, indent=2) + '\n')
        for root in (image, module):
            put(root, 'DEBIAN/preinst', '#!/bin/sh\nset -eu\n'
                f'/usr/sbin/liuqin-boot-update check-version {release} {identity["image_sha256"]} {identity["modules_sha256"]} >&2\n', 0o755)
            put(root, 'DEBIAN/prerm', '#!/bin/sh\nset -eu\ncase "$1" in remove|deconfigure)\n'
                f' /usr/sbin/liuqin-boot-update can-remove {release} >&2 ;;\nesac\n', 0o755)
        put(image, 'DEBIAN/postinst', '#!/bin/sh\nset -eu\n[ "$1" = configure ] || exit 0\n'
            f'run-parts --exit-on-error --regex="^zz-liuqin-boot$" --arg={release} --arg=/usr/lib/liuqin/boot-images/{release}/boot.img /etc/kernel/postinst.d\n', 0o755)
        # Keep the legacy liuqin-kernel payload package installed during migration.
        control(meta, 'liuqin-kernel-updates', version, f'{image_name} (= {version})',
                'Recommended coinstallable Liuqin kernel')
        names = ((tools, 'liuqin-boot-tools', TOOLS_VERSION), (module, module_name, version),
                 (image, image_name, version), (meta, 'liuqin-kernel-updates', version))
        for root, name, package_version in names:
            if package_format=='deb':
                subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(root),
                                str(packages / f'{name}_{package_version}_arm64.deb')], check=True)
            elif package_format=='rpm':
                sys.path.insert(0,str(PROJECT/'build/lib'))
                from rpm_package import build as rpm
                shutil.rmtree(root/'DEBIAN')
                if root==tools:
                    shutil.rmtree(root/'etc/apt')
                    shutil.rmtree(root/'etc/kernel')
                    dependencies=['python3 >= 3.11','python3-zstandard >= 0.23'];pre=[];posttrans=''
                elif root==module:
                    dependencies=['kmod'];pre=['liuqin-boot-tools >= '+TOOLS_VERSION];posttrans=''
                elif root==image:
                    dependencies=[module_name+' = '+version+'-1.liuqin'];pre=['liuqin-boot-tools >= '+TOOLS_VERSION]
                    posttrans='/usr/sbin/liuqin-boot-update hook '+release
                else:
                    dependencies=[image_name+' = '+version+'-1.liuqin'];pre=[];posttrans=''
                rpm(root,name,package_version,packages,requires=dependencies,prerequires=pre,
                    license='GPL-2.0-only' if root in (image,module) else 'MIT',
                    summary='Liuqin versioned kernel package',posttrans=posttrans)
            else:raise ValueError('需要 deb 或 rpm 包格式。')
        record = dict(status='OFFLINE_BUILT_NOT_DEVICE_TESTED', release=release,
                      source=recorded['commit'],
                      image_sha256=identity['image_sha256'], modules_sha256=identity['modules_sha256'],
                      boot_sha256=digest(packaged_boot))
        (packages / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
        (packages / 'SHA256SUMS').write_text(''.join(digest(path) + '  ' + path.name + '\n' for path in sorted(packages.iterdir()) if path.is_file()))
        os.rename(packages, output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-out', type=Path, required=True)
    parser.add_argument('--modules-dir', type=Path, required=True)
    parser.add_argument('--boot-image', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--format', choices=('deb','rpm'), default='deb')
    parser.add_argument('--kernel-source', type=Path, help='defaults to source recorded in kernel build-inputs.json')
    args = parser.parse_args()
    print(build(args.kernel_out, args.modules_dir, args.boot_image, args.out, args.version, args.kernel_source,args.format))


if __name__ == '__main__':
    main()
