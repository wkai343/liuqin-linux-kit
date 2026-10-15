#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Devbox build entry points. All commands operate on host files, never a tablet."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from usb_identity import DEFAULTS as USB_DEFAULTS, settings as usb_settings, environment as usb_environment

EXAMPLE = PROJECT / 'build/devbox/build.example.json'
BOARD_INPUTS = ('FIRMWARE_POOL', 'AUDIO_TOPOLOGY', 'WLAN_HSP2_TUPLE',
                'STOCK_OVERLAY_DIR', 'STOCK_BASE_DIR', 'BUSYBOX', 'MKBOOTIMG_DIR')
DEFAULTS = {
    **USB_DEFAULTS,
    'kernel_source': 'sources/linux-sm8450-liuqin',
    'root_source': 'out/build/root-custom-work',
    'kernel_fragments': ['build/kernel/configs/custom-zram.config'],
    'kernel_localversion': '-liuqin-custom',
    'kernel_reclaim_mte': False,
    'jobs': 12,
    'kernel_out': 'out/build/devbox/kernel',
    'modules_out': 'out/build/devbox/modules',
    'root_out': 'out/build/devbox/root',
    'boot_out': 'out/build/devbox/boot',
    'image_work': 'out/build/devbox/images',
    'bundle_out': 'out/releases/devbox',
    'kernel_debs_out': 'out/releases/kernel-debs',
    'package_version': '0.1.0',
    # The installer keeps its own source/configuration and output defaults.
    'installer_kernel_source': 'sources/linux-sm8450-liuqin',
    'installer_kernel_out': 'out/build/devbox/installer-kernel',
    'installer_kernel_fragments': [],
    'installer_kernel_localversion': '-liuqin-installer',
    'installer_work': 'out/build/devbox/installer',
    'installer_image': 'out/releases/installer/installer.img',
    'runtime_root': 'inputs/ubuntu-desktop-26.04-arm64/rootfs',
    'runtime_out': 'out/build/devbox/runtime',
    'inputs_file': 'out/image-inputs.local.json',
    'inputs': {
        'FIRMWARE_POOL': 'inputs/firmware-liuqin',
        'AUDIO_TOPOLOGY': 'out/prepared-inputs/topology/Xiaomi-Pad-6-Pro-tplg.bin',
        'WLAN_HSP2_TUPLE': 'out/wlan-source-check',
        'STOCK_OVERLAY_DIR': 'inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/analysis/dtbo',
        'STOCK_BASE_DIR': 'inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/analysis/vendor_boot/dtbs',
        'BUSYBOX': 'inputs/busybox-arm64/usr/bin/busybox',
        'MKBOOTIMG_DIR': 'inputs/aosp-mkbootimg',
    },
}
BOARD_ACTIONS = ('boot', 'all', 'check-inputs', 'installer', 'build-installer', 'show-config')


def run(argv, *, env=None, root=False):
    environment = dict(os.environ, **(env or {}))
    if root and os.geteuid() != 0:
        sudo = shutil.which('sudo')
        if not sudo:
            raise RuntimeError('此步骤需要 root 保留文件所有权和能力，请在有 sudo 的 Linux 主机执行。')
        # Keep the already-resolved tool environment through sudo's secure_path.
        # Only the packaging command is elevated; Devbox/Nix remains user-owned.
        keys = ('PATH', 'LC_ALL', 'CROSS_COMPILE', 'SLOT_SUCCESS_GCC', 'OBJCOPY', 'DEPMOD',
                'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'PKG_CONFIG_PATH')
        keys = dict.fromkeys((*keys, *(env or {}).keys()))
        assignments = [key + '=' + environment[key] for key in keys if key in environment]
        argv = [sudo, '--', shutil.which('env'), *assignments, *map(str, argv)]
        print('此构建阶段需要 root，用于装配根文件系统、隔离挂载并保留 UID/GID、ACL 与文件能力。', flush=True)
    subprocess.run(list(map(str, argv)), cwd=PROJECT, env=environment, check=True)


def resolve(value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT / path).resolve()


def config(path, *, read_inputs=True):
    if not path.is_file():
        raise RuntimeError('先运行 devbox run init-build，再填写 build.json 中的源码和素材路径。')
    overrides = json.loads(path.read_text())
    if not isinstance(overrides, dict):
        raise RuntimeError('构建配置必须是 JSON 对象。')
    if overrides.get('rootfs_config'):read_inputs=False
    inputs = overrides.get('inputs', {})
    if not isinstance(inputs, dict):
        raise RuntimeError('inputs 必须是路径对象。')
    data = copy.deepcopy(DEFAULTS)
    data.update({key: value for key, value in overrides.items() if key != 'inputs'})
    data.update(usb_settings(data))
    prepared = data['inputs_file']
    needs_prepared = any(key not in inputs for key in BOARD_INPUTS)
    if read_inputs and prepared is not None and (needs_prepared or 'inputs_file' in overrides):
        if not isinstance(prepared, str) or not prepared:
            raise RuntimeError('inputs_file 必须是路径，或用 null 停用自动读取。')
        prepared_path = resolve(prepared)
        if prepared_path.is_file():
            paths = json.loads(prepared_path.read_text())
            if not isinstance(paths, dict):
                raise RuntimeError('板级素材清单必须是 JSON 对象：' + str(prepared_path))
            data['inputs'].update({key: paths[key] for key in BOARD_INPUTS if key in paths})
        elif 'inputs_file' in overrides:
            raise RuntimeError('指定的板级素材清单不存在：' + str(prepared_path))
    # Explicit paths always take priority; a partial inputs override is enough.
    data['inputs'].update(inputs)
    if read_inputs and any(not isinstance(value, str) or not value for value in data['inputs'].values()):
        raise RuntimeError('板级素材路径必须是非空字符串。')
    if type(data['jobs']) is not int or data['jobs'] < 1:
        raise RuntimeError('jobs 必须是正整数。')
    if type(data['kernel_reclaim_mte']) is not bool:
        raise RuntimeError('kernel_reclaim_mte 必须是 true 或 false。')
    for key in ('kernel_fragments', 'installer_kernel_fragments'):
        if not isinstance(data[key], list) or any(not isinstance(value, str) or not value for value in data[key]):
            raise RuntimeError(key + ' 必须是配置路径列表；不添加片段时使用 []。')
        # Accept the built-in path from older local configurations after relocation.
        data[key] = ['build/kernel/configs/custom-zram.config' if value == 'kernel/configs/custom-zram.config'
                     else value for value in data[key]]
    return data


def require_inputs(data, action):
    keys = {
        'kernel': ('kernel_source',), 'modules': ('kernel_source', 'kernel_out'),
        'rootfs': ('root_source', 'kernel_out', 'modules_out'),
        'runtime': ('runtime_root',), 'images': ('root_out', 'boot_out'),
        'boot': ('kernel_source', 'kernel_out', 'modules_out'),
        'all': ('kernel_source', 'root_source'),
        'check-inputs': ('kernel_source', 'root_source'),
        'installer-kernel': ('installer_kernel_source',),
        'installer': ('installer_kernel_source', 'installer_kernel_out', 'runtime_out'),
        'build-installer': ('installer_kernel_source', 'runtime_root'),
        'kernel-debs': ('kernel_source', 'kernel_out', 'modules_out', 'boot_out'),
    }[action]
    missing = []
    for key in keys:
        if not isinstance(data.get(key), str) or not resolve(data[key]).is_dir():
            missing.append(key + ': ' + str(data.get(key, '<未设置>')))
    if action in ('images', 'all', 'check-inputs'):
        if not isinstance(data.get('installer_image'), str) or not resolve(data['installer_image']).is_file():
            missing.append('installer_image: 先运行 devbox run build-installer，或填写已有独立 installer.img 的路径')
    if action in ('boot', 'all', 'check-inputs', 'installer', 'build-installer'):
        for key in BOARD_INPUTS:
            value = data.get('inputs', {}).get(key)
            is_file = key in ('AUDIO_TOPOLOGY', 'BUSYBOX')
            exists = isinstance(value, str) and (resolve(value).is_file() if is_file else resolve(value).is_dir())
            if not exists:
                missing.append(key + ': ' + str(value or '<未设置>'))
    if action in ('all', 'rootfs', 'runtime', 'images', 'boot', 'installer', 'build-installer', 'kernel-debs'):
        outputs = ('root_out', 'boot_out', 'bundle_out', 'image_work') if action == 'all' else ({
            'rootfs': ('root_out',), 'runtime': ('runtime_out',), 'images': ('bundle_out', 'image_work'),
            'installer': ('installer_work', 'installer_image'),
            'kernel-debs': ('kernel_debs_out',),
            'boot': ('boot_out',),
            'build-installer': ('runtime_out', 'installer_work', 'installer_image')}[action])
        for key in outputs:
            if resolve(data[key]).exists():
                missing.append(key + ': 输出已存在，请使用新目录或改用其他阶段入口')
    if missing:
        raise RuntimeError('构建输入尚未准备好：\n  ' + '\n  '.join(missing))


def stage(data, action, configure_only=False):
    require_inputs(data, action)
    path = lambda key: str(resolve(data[key]))
    python = shutil.which('python3')
    if action in ('kernel', 'installer-kernel'):
        prefix = 'installer_' if action == 'installer-kernel' else ''
        command = [python, 'build/scripts/build-liuqin-kernel.py', '--custom', '--source', path(prefix + 'kernel_source'),
                   '--out', path(prefix + 'kernel_out'), '--jobs', str(data.get('jobs', 12))]
        if data.get(prefix + 'kernel_config'):
            command += ['--config', str(resolve(data[prefix + 'kernel_config']))]
        for fragment in data.get(prefix + 'kernel_fragments', []):
            command += ['--config-fragment', str(resolve(fragment))]
        if data.get(prefix + 'kernel_localversion'):
            command += ['--localversion=' + data[prefix + 'kernel_localversion']]
        if configure_only:
            command += ['--configure-only']
        # Kernel hardening is selected by Kconfig/Kbuild, rather than generic
        # user-space flags injected by the Nix target compiler wrapper.
        run(command, env={'NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl': ''})
    elif action == 'modules':
        run(['sh', 'build/scripts/build-liuqin-kernel-modules.sh'], env=dict(
            KERNEL_DIR=path('kernel_source'), KERNEL_OUT=path('kernel_out'), OUT_DIR=path('modules_out')))
    elif action == 'rootfs':
        run([python, 'build/scripts/build-liuqin-custom-root.py', '--root', path('root_source'),
             '--kernel-out', path('kernel_out'), '--modules-dir', path('modules_out'), '--out', path('root_out')], root=True)
    elif action == 'runtime':
        run([python, 'build/lib/build-installer-runtime.py', '--root', path('runtime_root'), '--out', path('runtime_out')])
    elif action == 'boot':
        env = {key: str(resolve(data['inputs'][key])) for key in BOARD_INPUTS}
        env.update(KERNEL_SOURCE=path('kernel_source'), KERNEL_OUT=path('kernel_out'),
                   KERNEL_MODULES_DIR=path('modules_out'), LIUQIN_ROOT_PROFILE='native-updatable',
                   INSTALLER_RUNTIME='', OUT_DIR=path('boot_out'),
                   LIUQIN_RECLAIM_MTE='1' if data['kernel_reclaim_mte'] else '0')
        env.update(usb_environment(data))
        run(['sh', 'build/scripts/build-liuqin-native-boot.sh'], env=env)
    elif action == 'images':
        env = dict(CUSTOM_ROOT_OUT=path('root_out'), INSTALLER_IMAGE=path('installer_image'),
                   BOOT_IMAGE=str(resolve(data['boot_out']) / 'boot-liuqin-native.img'),
                   BUILD_WORK_DIR=path('image_work'), OUT_DIR=path('bundle_out'))
        run(['sh', 'build/scripts/build-liuqin-custom-bundle.sh'], env=env)
        print('三文件输出：' + path('bundle_out'))
    elif action == 'kernel-debs':
        run([python, 'build/scripts/build-liuqin-kernel-debs.py', '--kernel-out', path('kernel_out'),
             '--kernel-source', path('kernel_source'),
             '--modules-dir', path('modules_out'), '--boot-image', str(resolve(data['boot_out']) / 'boot-liuqin-native.img'),
             '--out', path('kernel_debs_out'), '--version', data['package_version']])
    elif action == 'installer':
        env = {key: str(resolve(data['inputs'][key])) for key in BOARD_INPUTS}
        env.update(INSTALLER_KERNEL_SOURCE=path('installer_kernel_source'),
                   INSTALLER_KERNEL_OUT=path('installer_kernel_out'),
                   INSTALLER_RUNTIME=path('runtime_out'), OUT_DIR=path('installer_work'))
        env.update(usb_environment(data))
        run(['sh', 'build/scripts/build-liuqin-installer.sh'], env=env)
        sys.path.insert(0, str(PROJECT / 'shared'))
        from liuqin_artifacts import boot_interface
        built = resolve(data['installer_work']) / 'installer.img'
        info = boot_interface(built, installer=True)
        if info.get('interface') != 'liuqin-installer-v2':
            raise RuntimeError('构建结果不是独立安装环境，未发布。')
        destination = resolve(data['installer_image'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Publish only a complete checked image, refusing a concurrent overwrite.
        with tempfile.TemporaryDirectory(prefix='.installer-', dir=destination.parent) as directory:
            temporary = Path(directory) / 'installer.img'
            shutil.copyfile(built, temporary)
            os.link(temporary, destination)
        print('可复用安装镜像：' + str(destination))


def doctor():
    tools = ('make', 'gcc', 'bc', 'bison', 'flex', 'perl', 'pkg-config', 'git', 'curl',
             'tar', 'gzip', 'xz', 'zstd', 'cpio', 'rsync', 'dtc', 'fdtoverlay', 'readelf',
             'depmod', 'mkfs.ext4', 'e2fsck', 'getcap', 'sgdisk', 'unsquashfs', '7z', 'qemu-aarch64', 'dpkg-deb')
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
    for name, source in (
        ('slot-success', 'device/boot/liuqin-mark-slot-successful.c'),
        ('charger-exit', 'device/charger-mode/liuqin-charger-mode-exit.c'),
        ('charger-power-key', 'device/charger-mode/liuqin-charger-mode-power-key.c'),
    ):
        run([cross + 'gcc', '-static', '-Os', source, '-o', str(probe / name)])
        # Never execute helpers that can access real partitions.
    native = probe / 'native.c'
    native.write_text('#include <openssl/crypto.h>\n#include <libelf.h>\nint main(void) { return elf_version(EV_CURRENT) == EV_NONE || !OpenSSL_version(0); }\n')
    flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'openssl', 'libelf'], text=True)
    import shlex
    run(['gcc', str(native), '-o', str(probe / 'native'), *shlex.split(flags)])
    run([str(probe / 'native')])
    print('PASS：宿主构建依赖、ARM64 静态链接、QEMU 和 Python ELF 解析可用。')
    print('rootfs 所有权/ACL 打包需要宿主 sudo；ARM64 chroot 另需宿主 binfmt 注册。')


def test():
    for name in ('project-identity.py', 'project-structure.py', 'devbox-build.py', 'source-build.py', 'stock-rom.py', 'fedora-build.py', 'selinux-packaging.py', 'first-boot-setup.py', 'mkbootimg-fetch.py', 'initramfs-inputs.py', 'sensor-build-environment.py', 'gui-artifacts.py', 'filesystems.py', 'windows-installer.py', 'usb-installer-network.py', 'custom-kernel.py', 'kernel-updates.py', 'gui-engine.py', 'gui-worker.py', 'install-root-single.py'):
        run([shutil.which('python3'), 'tests/' + name])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('doctor', 'init', 'show-config', 'plan', 'prepare', 'check-inputs', 'kernel', 'modules', 'rootfs', 'runtime', 'images', 'all', 'test',
                                         'installer-kernel', 'installer', 'build-installer', 'kernel-debs', 'kernel-packages', 'boot'))
    parser.add_argument('--config', type=Path, default=PROJECT / 'build.json')
    parser.add_argument('--configure-only', action='store_true')
    parser.add_argument('--from-source', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--refresh',action='store_true',help='重新装配官方系统软件包阶段（APT/DNF）')
    args = parser.parse_args()
    if args.action=='kernel-packages':args.action='kernel-debs'
    if args.configure_only and args.action not in ('kernel', 'installer-kernel'):
        parser.error('--configure-only 只用于 kernel 或 installer-kernel')
    if args.from_source and args.action!='init':parser.error('--from-source 只用于 init-build')
    if args.action == 'doctor':
        doctor()
    elif args.action == 'test':
        test()
    elif args.action == 'init':
        if args.config.exists():
            if not args.config.is_file():
                raise RuntimeError('配置路径不是文件：' + str(args.config))
            existing = json.loads(args.config.read_text())
            if not isinstance(existing, dict):
                raise RuntimeError('已有配置必须是 JSON 对象，未修改：' + str(args.config))
            print('配置已存在，已保留全部设置：' + str(args.config))
            if existing.get('rootfs_config'):
                option = '' if args.config.resolve() == (PROJECT / 'build.json').resolve() else ' -- --config ' + shlex.quote(str(args.config))
                print('无需重复初始化。接下来检查配置并运行 devbox run plan' + option + '；确认材料齐全后运行 devbox run build' + option + '。')
            else:
                print('此文件尚未使用当前官方系统配置。如需新配置，运行 devbox run init-build -- --config build.new.json，后续命令带同一 --config。')
            return
        with args.config.open('xb') as target:
            target.write(EXAMPLE.read_bytes())
        print('已创建 ' + str(args.config) + '，请填写实际输入路径。')
    else:
        data = config(args.config, read_inputs=args.action in BOARD_ACTIONS)
        if args.action == 'show-config':
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif data.get('rootfs_config'):
            sys.path.insert(0,str(PROJECT/'build/lib'))
            from source_flow import execute
            execute(data,args.action,run,refresh=args.refresh,configure_only=args.configure_only)
        elif args.action in ('plan','prepare'):
            raise ValueError('旧配置不支持此命令。使用 init-build --config build.new.json 创建当前配置，后续命令带同一 --config。')
        elif args.action == 'check-inputs':
            require_inputs(data, args.action)
            print('PASS：所需源码、根目录和板级素材路径存在。')
        elif args.action == 'all':
            require_inputs(data, 'all')
            for action in ('kernel', 'modules', 'rootfs', 'boot', 'images'):
                stage(data, action)
        elif args.action == 'build-installer':
            require_inputs(data, 'build-installer')
            for action in ('installer-kernel', 'runtime', 'installer'):
                stage(data, action)
        else:
            stage(data, args.action, args.configure_only)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print('构建停止：' + str(error), file=sys.stderr)
        sys.exit(1)
