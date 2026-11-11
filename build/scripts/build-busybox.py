#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a pinned static ARM64 BusyBox with the Devbox musl cross compiler."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from source_build import download, sha

CONFIG = PROJECT / 'build/busybox'


def inputs():
    compiler = shutil.which(os.environ.get('CROSS_COMPILE', 'aarch64-unknown-linux-musl-') + 'gcc')
    if not compiler:
        raise ValueError('ARM64 musl compiler unavailable; run inside Devbox')
    target = subprocess.check_output([compiler, '-dumpmachine'], text=True).strip()
    if 'aarch64' not in target or 'musl' not in target:
        raise ValueError('BusyBox requires an ARM64 musl compiler')
    return dict(compiler=str(Path(compiler).resolve()), target=target,
                compiler_version=subprocess.check_output([compiler, '--version'], text=True).splitlines()[0],
                sources={p: sha(PROJECT / p) for p in (
                    'build/busybox/source.json', 'build/busybox/busybox.config',
                    'build/scripts/build-busybox.py', 'build/lib/source_build.py',
                    'build/devbox/nix/flake.nix', 'build/devbox/nix/flake.lock')})


def build(out, jobs):
    out = out.resolve()
    if not out.is_relative_to(PROJECT / 'out') or out == PROJECT / 'out' or out.exists():
        raise ValueError('use a new output directory below project/out')
    identity = inputs()
    recipe = json.loads((CONFIG / 'source.json').read_text())
    archive = download(recipe['url'], PROJECT / 'inputs/busybox' / Path(recipe['url']).name, recipe['sha256'])
    out.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter='data')
    source = out / ('busybox-' + recipe['version'])
    shutil.copyfile(CONFIG / 'busybox.config', source / '.config')
    env = dict(os.environ, LC_ALL='C', SOURCE_DATE_EPOCH='1794268800',
               KBUILD_BUILD_TIMESTAMP='2026-11-10 00:00:00 UTC',
               NIX_CFLAGS_COMPILE='', NIX_LDFLAGS='', NIX_HARDENING_ENABLE='',
               NIX_HARDENING_ENABLE_aarch64_unknown_linux_musl='')
    for name in ('CFLAGS', 'CPPFLAGS', 'LDFLAGS', 'CXXFLAGS', 'ARCH', 'LD_LIBRARY_PATH'):
        env.pop(name, None)
    prefix = os.environ.get('CROSS_COMPILE', 'aarch64-unknown-linux-musl-')
    make = ['make', '-C', str(source), 'ARCH=arm64', 'CROSS_COMPILE=' + prefix,
            'EXTRA_CFLAGS=-ffile-prefix-map=' + str(source) + '=.']
    subprocess.run([*make, 'oldconfig'], env=env, stdin=subprocess.DEVNULL, check=True)
    required = {line for line in (CONFIG / 'busybox.config').read_text().splitlines() if line.endswith('=y')}
    if not required.issubset(set((source / '.config').read_text().splitlines())):
        raise ValueError('BusyBox configuration lost required features')
    subprocess.run([*make, '-j' + str(jobs)], env=env, check=True)
    binary = out / 'busybox'
    shutil.copy2(source / 'busybox', binary)
    header = binary.read_bytes()[:20]
    if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\xb7\x00':
        raise ValueError('BusyBox is not an ARM64 ELF')
    if 'INTERP' in subprocess.check_output(['readelf', '-l', str(binary)], text=True):
        raise ValueError('BusyBox must be statically linked')
    if inputs() != identity:
        raise ValueError('BusyBox inputs changed during build')
    shutil.copyfile(source / '.config', out / 'busybox.config')
    shutil.copyfile(source / 'LICENSE', out / 'LICENSE')
    # Keep the exact upstream source and recipe alongside binary distributions.
    with tarfile.open(out / 'busybox-sources.tar', 'w') as tar:
        tar.add(archive, arcname=archive.name)
        for name in identity['sources']:
            tar.add(PROJECT / name, arcname='project/' + name)
        tar.add(out / 'busybox.config', arcname='build.config')
    manifest = dict(schema=1, architecture='aarch64', libc='musl', source=recipe, inputs=identity,
                    binary_sha256=sha(binary), configuration_sha256=sha(out / 'busybox.config'),
                    sources_sha256=sha(out / 'busybox-sources.tar'))
    (out / 'busybox-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built static ARM64 BusyBox: ' + str(binary))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=12)
    args = parser.parse_args()
    build(args.out, args.jobs)
