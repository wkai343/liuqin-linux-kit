#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the pinned standalone Recovery kernel from kernel.org and local patches."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from kernel_recipe import PROJECT, DEFAULT_LOCK, check_configuration, load, prepare_source, project_file, sha, verify_source


def build(args):
    lock_bytes = args.source_lock.read_bytes()
    recipe = load(args.source_lock)
    out = args.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):
        raise ValueError('use a new output directory below project/out')
    out.mkdir(parents=True)
    source = prepare_source(args.source_cache, out/'assembled', args.source_lock)
    kernel = out/'kernel'; kernel.mkdir()
    config = project_file(recipe['config']['path'])
    shutil.copyfile(config, kernel/'.config')
    compiler = shutil.which('aarch64-unknown-linux-musl-gcc')
    if not compiler:
        raise ValueError('use the Devbox Recovery cross compiler')
    env = dict(os.environ, ARCH='arm64', CROSS_COMPILE='aarch64-unknown-linux-musl-',
               KBUILD_BUILD_USER='liuqin', KBUILD_BUILD_HOST='build', KBUILD_BUILD_VERSION='1',
               KBUILD_BUILD_TIMESTAMP=recipe['build_timestamp'],
               NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl='')
    for name in ('KCONFIG_CONFIG', 'KCONFIG_ALLCONFIG', 'KBUILD_OUTPUT', 'KBUILD_EXTMOD',
                 'LOCALVERSION', 'KCFLAGS', 'KAFLAGS', 'KCPPFLAGS', 'LDFLAGS_vmlinux'):
        env.pop(name, None)
    make = ['make', '-C', str(source), 'O='+str(kernel), 'CC='+compiler, 'RUSTC=false']
    subprocess.run([*make, 'olddefconfig'], env=env, check=True)
    differences = check_configuration(config.read_text(), (kernel/'.config').read_text())
    (out/'configuration-comparison.json').write_text(json.dumps(differences, indent=2)+'\n')
    if args.configure_only:
        print(kernel/'.config')
        return
    cc = ('ccache ' if shutil.which('ccache') else '') + compiler
    maps = f'-ffile-prefix-map={source}=/build/linux -ffile-prefix-map={kernel}=/build/kernel'
    subprocess.run([*make, '-j'+str(args.jobs), 'CC='+cc, 'KCFLAGS='+maps, 'KAFLAGS='+maps,
                    'Image', recipe['dtb'], 'modules_prepare'], env=env, check=True)
    source_identity = verify_source(source, args.source_lock)
    if args.source_lock.read_bytes() != lock_bytes:
        raise ValueError('Recovery source lock changed during compilation')
    files = ['arch/arm64/boot/Image', 'arch/arm64/boot/dts/'+recipe['dtb'], '.config',
             'vmlinux', 'System.map', 'include/config/kernel.release', 'vmlinux.symvers']
    info = dict(source=source_identity, config_sha256=sha(kernel/'.config'),
                configuration_metadata_changes=differences,
                compiler_sha256=sha(Path(compiler).resolve()),
                files={name:sha(kernel/name) for name in files})
    (kernel/'recovery-build.json').write_text(json.dumps(info, indent=2)+'\n')
    # The shared RAM base assembler checks these ordinary kernel build records.
    public = dict(commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'], text=True).strip(),
                  config_sha256=info['config_sha256'], recovery_source=source_identity)
    (kernel/'build-info.json').write_text(json.dumps(public, indent=2)+'\n')
    (kernel/'SHA256SUMS').write_text(''.join(f'{sha(kernel/name)}  {name}\n' for name in [*files,'build-info.json','recovery-build.json']))
    subprocess.run([sys.executable, str(PROJECT/'build/recovery/prepare-native-kernel.py'),
                    '--kernel-source', str(source), '--kernel-out', str(kernel),
                    '--source-lock', str(args.source_lock.resolve()), '--rom-images', str(args.rom_images.resolve()),
                    '--out', str(out/'components')], env=env, check=True)
    print('Recovery kernel components: '+str(out/'components'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-lock', type=Path, default=DEFAULT_LOCK)
    parser.add_argument('--source-cache', type=Path, default=PROJECT/'sources/linux-recovery')
    parser.add_argument('--rom-images', type=Path, default=PROJECT/'inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument('--configure-only', action='store_true')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    build(args)
