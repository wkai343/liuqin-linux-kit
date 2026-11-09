#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Devbox build entry points. All commands operate on host files, never a tablet."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from usb_identity import DEFAULTS as USB_DEFAULTS, settings as usb_settings

EXAMPLE = PROJECT / 'build/devbox/build.example.json'
DEFAULTS = {
    **USB_DEFAULTS,
    'kernel_source': 'sources/linux-liuqin-7.3.3',
    'kernel_fragments': ['build/kernel/configs/custom-zram.config'],
    'kernel_localversion': '-liuqin-custom',
    'kernel_reclaim_mte': False,
    'jobs': 12,
    'package_version': '0.3.19',
}
CONFIG_KEYS = set(DEFAULTS) | {'rootfs_config', 'kernel_source_lock', 'kernel_config', 'rom_images'}
BUILD_TARGETS = {'system': 'rootfs', 'kernel': 'kernel', 'modules': 'modules',
                 'boot': 'boot', 'packages': 'kernel-debs'}


def command_help():
    print('''常用命令（在仓库根目录运行）：
  devbox run init               创建 build.json，已有配置保持不变
  devbox run doctor             检查构建环境
  devbox run fetch-rom          下载并校验原厂资料
  devbox run plan               预览当前配置与构建计划
  devbox run build              构建配套 boot + rootfs
  devbox run outputs            查看已完成的配套产物路径
  devbox run test               运行离线回归
  devbox run help               查看本说明

单独构建：devbox run build -- --target kernel（也可选 modules、boot、packages）
配置预览：devbox run build -- --target kernel --configure-only
镜像导出：devbox run build -- --export-dir /path/to/new-output
其他参数：--config build.custom.json；--refresh 重新装配系统软件。
完整系统构建需要 rootfs_config；各目标自动补齐依赖。
原生 Rec 独立构建，见 docs/NATIVE-RECOVERY.md。
''')

def show_outputs():
    """Inventory completed image caches without builds, configuration or large-file hashing."""
    sys.path.insert(0, str(PROJECT / 'shared'))
    from liuqin_kernel import parse_contract

    cache = PROJECT / 'out/build/from-source'
    groups = {}
    specifications = (
        ('boot', 'image/boot-liuqin-native.img', 'image/native-root.contract'),
        ('rootfs', 'packed/rootfs.tar.gz', 'packed/native-root.contract'),
    )
    for directory in sorted(cache.iterdir()) if cache.is_dir() else ():
        if directory.name.startswith('.') or directory.is_symlink() or not directory.is_dir():
            continue
        receipt = directory / 'build-receipt.json'
        if receipt.is_symlink() or not receipt.is_file():
            continue
        try:
            artifacts = json.loads(receipt.read_text()).get('artifacts')
            if not isinstance(artifacts, dict):
                raise ValueError('无效产物列表')
            timestamp = receipt.stat().st_mtime_ns
            for label, filename, contract_name in specifications:
                path = directory / filename
                if filename not in artifacts or not path.is_file() or path.is_symlink():
                    continue
                if not path.resolve().is_relative_to(directory.resolve()):
                    continue
                contract = directory / contract_name
                if contract.is_symlink() or not contract.resolve().is_relative_to(directory.resolve()):
                    raise ValueError('内核标识路径无效')
                content = contract.read_bytes()
                if artifacts.get(contract_name) != {'file': hashlib.sha256(content).hexdigest()}:
                    raise ValueError('内核标识与完成记录不一致')
                identity = parse_contract(content)
                group = groups.setdefault(content, dict(release=identity['release'], timestamp=timestamp, files=[]))
                group['timestamp'] = max(group['timestamp'], timestamp)
                group['files'].append((label, path))
        except (OSError, ValueError, AttributeError) as error:
            print('跳过无法读取的缓存记录：' + str(receipt) + '：' + str(error), file=sys.stderr)
    if not groups:
        print('尚无已完成的镜像产物；先运行 devbox run build。')
        return
    print('已完成的缓存产物（按内核/模块标识配对，不代表当前配置；未重新校验大型镜像）：')
    for content, group in sorted(groups.items(), key=lambda item: item[1]['timestamp'], reverse=True):
        print('\n配套组 ' + hashlib.sha256(content).hexdigest()[:12] + '，内核 ' + group['release'])
        for label, path in sorted(group['files']):
            print('  ' + label + ': ' + str(path))


def run(argv, *, env=None, root=False):
    environment = dict(os.environ, **(env or {}))
    if root and os.geteuid() != 0:
        mode=environment.get('LIUQIN_ROOTFS_MODE','rootless')
        if mode not in ('rootless','sudo'):raise ValueError('LIUQIN_ROOTFS_MODE 只支持 rootless 或 sudo。')
        tool=shutil.which('podman' if mode=='rootless' else 'sudo')
        if not tool:
            raise RuntimeError('rootless 构建需要 Podman、subuid/subgid 和 newuidmap/newgidmap；不自动请求 sudo。'
                               if mode=='rootless' else '显式 sudo 模式需要 sudo。')
        keys = ('PATH', 'LC_ALL', 'CROSS_COMPILE', 'SLOT_SUCCESS_GCC', 'OBJCOPY', 'DEPMOD',
                'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'PKG_CONFIG_PATH')
        keys = dict.fromkeys((*keys, *(env or {}).keys()))
        assignments = [key + '=' + environment[key] for key in keys if key in environment]
        if mode=='rootless':
            argv=[tool,'unshare','--',shutil.which('env'),*assignments,sys.executable,
                  str(PROJECT/'build/lib/rootless_build.py'),'--',*map(str,argv)]
            print('rootfs 阶段使用用户命名空间：保留完整 UID/GID、ACL 和能力，无需 sudo。',flush=True)
        else:
            argv=[tool,'--',shutil.which('env'),*assignments,*map(str,argv)]
            print('rootfs 阶段使用显式 sudo 模式。',flush=True)
    subprocess.run(list(map(str, argv)), cwd=PROJECT, env=environment, check=True)


def config(path):
    if not path.is_file():
        raise RuntimeError('先运行 devbox run init，再填写 build.json 中的源码和素材路径。')
    overrides = json.loads(path.read_text())
    if not isinstance(overrides, dict):
        raise RuntimeError('构建配置必须是 JSON 对象。')
    unknown = set(overrides) - CONFIG_KEYS
    if unknown:
        raise ValueError('不支持的构建配置项：' + ', '.join(sorted(unknown)))
    if not isinstance(overrides.get('rootfs_config'), str) or not overrides['rootfs_config']:
        raise ValueError('配置需要 rootfs_config 指定系统配方；用 devbox run init -- --config build.new.json 创建新配置。')
    data = copy.deepcopy(DEFAULTS)
    data.update(overrides)
    data.update(usb_settings(data))
    if type(data['jobs']) is not int or data['jobs'] < 1:
        raise RuntimeError('jobs 必须是正整数。')
    if type(data['kernel_reclaim_mte']) is not bool:
        raise RuntimeError('kernel_reclaim_mte 必须是 true 或 false。')
    for key in ('kernel_fragments',):
        if not isinstance(data[key], list) or any(not isinstance(value, str) or not value for value in data[key]):
            raise RuntimeError(key + ' 必须是配置路径列表；不添加片段时使用 []。')
    if data.get('kernel_source_lock') is not None and (
            not isinstance(data['kernel_source_lock'],str) or not data['kernel_source_lock']):
        raise RuntimeError('kernel_source_lock 必须是内核来源记录的路径。')
    return data


def doctor():
    tools = ('make', 'gcc', 'bc', 'bison', 'flex', 'perl', 'pkg-config', 'git', 'curl',
             'tar', 'gzip', 'xz', 'zstd', 'cpio', 'rsync', 'dtc', 'fdtoverlay', 'readelf',
             'depmod', 'mkfs.ext4', 'e2fsck', 'getcap', 'sgdisk', 'unsquashfs', '7z', 'qemu-aarch64', 'dpkg-deb')
    root_mode=os.environ.get('LIUQIN_ROOTFS_MODE','rootless')
    if root_mode not in ('rootless','sudo'):raise ValueError('未知 rootfs 构建模式。')
    if os.geteuid()!=0:
        tools+=('podman','unshare','newuidmap','newgidmap') if root_mode=='rootless' else ('sudo','unshare')
    cross = os.environ.get('CROSS_COMPILE', 'aarch64-unknown-linux-musl-')
    missing = [name for name in (*tools, cross + 'gcc', cross + 'objcopy') if not shutil.which(name)]
    if missing:
        raise RuntimeError('缺少工具，请先进入 Devbox：' + ', '.join(missing))
    from elftools.elf.elffile import ELFFile
    probe = PROJECT / 'out/build/devbox/doctor'
    probe.mkdir(parents=True, exist_ok=True)
    hello = probe / 'hello.c'
    hello.write_text('#include <stdio.h>\nint main(void) { puts("ARM64 static probe: PASS"); return 0; }\n')
    binary = probe / 'hello-arm64'
    run([cross + 'gcc', '-static', '-Os', str(hello), '-o', str(binary)])
    with binary.open('rb') as stream:
        elf = ELFFile(stream)
        if elf['e_machine'] != 'EM_AARCH64' or any(s['p_type'] == 'PT_INTERP' for s in elf.iter_segments()):
            raise RuntimeError('交叉工具链未产生独立的 ARM64 静态程序。')
    run(['qemu-aarch64', str(binary)])
    file_handle = probe / 'file-handle-arm64'
    run([cross + 'gcc', '-static', '-O2',
         str(PROJECT / 'build/devbox/qemu-file-handle-probe.c'), '-o', str(file_handle)])
    run(['qemu-aarch64', str(file_handle)])
    for name, source in (
        ('slot-success', 'device/boot/liuqin-mark-slot-successful.c'),
        ('storage-seal', 'device/boot/liuqin-storage-seal.c'),
        ('charger-exit', 'device/charger-mode/liuqin-charger-mode-exit.c'),
        ('charger-power-key', 'device/charger-mode/liuqin-charger-mode-power-key.c'),
    ):
        sources = [source]
        if name == 'storage-seal':
            sources.append('device/boot/liuqin-storage-ready.c')
        run([cross + 'gcc', '-static', '-Os', *sources, '-o', str(probe / name)])
        # Never execute helpers that can access real partitions.
    native = probe / 'native.c'
    native.write_text('#include <openssl/crypto.h>\n#include <libelf.h>\nint main(void) { return elf_version(EV_CURRENT) == EV_NONE || !OpenSSL_version(0); }\n')
    flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'openssl', 'libelf'], text=True)
    import shlex
    run(['gcc', str(native), '-o', str(probe / 'native'), *shlex.split(flags)])
    run([str(probe / 'native')])
    print('PASS：宿主构建依赖、ARM64 静态链接、QEMU 和 Python ELF 解析可用。')
    if root_mode=='rootless':
        print('rootfs 默认使用 Podman 用户命名空间，无需 sudo；需完整 subuid/subgid 映射和支持私有 binfmt 的 Linux 6.7+。')
    else:
        print('当前显式选择 sudo 模式，密码要求由宿主已有策略决定。')


def test():
    for name in ('project-identity.py', 'project-structure.py', 'devbox-build.py', 'source-build.py', 'device-overlays.py', 'stock-rom.py', 'fedora-build.py', 'fedora-stats.py', 'fedora-startup.py', 'nanosic-folio.py', 'selinux-packaging.py', 'first-boot-setup.py', 'backlight-default.py', 'boot-splash.py', 'splash-handoff.py', 'splash-logo.py', 'splash-orientation.py', 'plymouth-guard.py', 'display-clock.py', 'display-brightness-minimum.py', 'display-refresh-rates.py', 'display-policy.py', 'display-recovery.py', 'wlan-mac.py', 'rtc.py', 'mipps-startup.py', 'storage-scope.py', 'storage-seal.py', 'storage-ready.py', 'storage-paths.py', 'mkbootimg-fetch.py', 'initramfs-inputs.py', 'sensor-build-environment.py', 'gui-artifacts.py', 'filesystems.py', 'windows-installer.py', 'usb-installer-network.py', 'custom-kernel.py', 'kernel-common-fixes.py', 'rootless-build.py', 'kernel-updates.py', 'gui-engine.py', 'gui-worker.py', 'install-root-single.py', 'multiboot.py', 'multiboot-device.py', 'multiboot-store.py', 'android-installer.py', 'android-device.py', 'android-direct.py', 'android-rom.py', 'android-runtime.py', 'staged-install.py', 'managed-linux-device.py', 'partition-discovery.py', 'partition-device.py', 'partition-editor-device.py', 'workflow.py', 'recovery-installer.py'):
        run([shutil.which('python3'), 'tests/' + name])
    run([shutil.which('python3'), 'tests/fingerprint-gallery-client.py'])
    # The provider's private-file checks run as namespace root using fake TAs.
    # Polkit's JavaScript policy test runs separately in CI with Node available.
    for name, selection in (('fingerprint-native-provider.py', ['ProviderTests']),
                            ('fingerprint-enrol-preflight.py', []), ('fingerprint-gallery.py', [])):
        command = [shutil.which('python3'), 'tests/' + name, *selection]
        if os.geteuid() != 0:
            command = ['unshare', '-Ur', *command]
        run(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('help', 'init', 'doctor', 'plan', 'build', 'outputs', 'test',
                                           'show-config', 'check-inputs', 'prepare'),
                        help='构建使用 build --target；诊断使用 show-config、check-inputs、prepare')
    parser.add_argument('--target', choices=tuple(BUILD_TARGETS), help='build 的构建目标，默认 system（配套 boot/rootfs）')
    parser.add_argument('--config', type=Path, default=PROJECT / 'build.json')
    parser.add_argument('--configure-only', action='store_true')
    parser.add_argument('--refresh',action='store_true',help='重新装配官方系统软件包阶段（APT/DNF）')
    parser.add_argument('--export-dir',type=Path,help='构建后将本次 boot/rootfs 与校验文件导出到新目录')
    args = parser.parse_args()
    if args.target is not None and args.action != 'build':
        parser.error('--target 只用于 build')
    if args.action == 'build':
        args.action = BUILD_TARGETS[args.target or 'system']
    if args.export_dir is not None and args.action != 'rootfs':
        parser.error('--export-dir 只用于完整系统构建')
    if args.configure_only and args.action != 'kernel':
        parser.error('--configure-only 只用于 kernel')
    if args.action == 'doctor':
        doctor()
    elif args.action == 'help':
        command_help()
    elif args.action == 'outputs':
        show_outputs()
    elif args.action == 'test':
        test()
    elif args.action == 'init':
        if args.config.exists():
            if not args.config.is_file():
                raise RuntimeError('配置路径不是文件：' + str(args.config))
            config(args.config)
            print('配置已存在，已保留全部设置：' + str(args.config))
            option = '' if args.config.resolve() == (PROJECT / 'build.json').resolve() else ' -- --config ' + shlex.quote(str(args.config))
            print('无需重复初始化。接下来检查配置并运行 devbox run plan' + option + '；确认材料齐全后运行 devbox run build' + option + '。')
            return
        with args.config.open('xb') as target:
            target.write(EXAMPLE.read_bytes())
        print('已创建 ' + str(args.config) + '，请填写实际输入路径。')
    else:
        data = config(args.config)
        if args.action == 'show-config':
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            from source_flow import execute
            options = {'export_dir': args.export_dir} if args.export_dir is not None else {}
            execute(data,args.action,run,refresh=args.refresh,configure_only=args.configure_only,**options)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print('构建停止：' + str(error), file=sys.stderr)
        sys.exit(1)
