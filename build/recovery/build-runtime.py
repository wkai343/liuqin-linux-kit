#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the distribution-independent Rec userspace with pinned Buildroot + musl.

Run inside Devbox, without sudo. --work can resume an unchanged configuration;
--out must be new. This builds no kernel, init system, UI or adbd.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

PROJECT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT / 'build/runtime'
sys.path.insert(0, str(PROJECT / 'build/lib'))
from source_build import download, sha, tree_identity


def encoded(value):
    return json.dumps(value, sort_keys=True, indent=2) + '\n'


def inputs():
    names = ['buildroot-source.json', 'rec.defconfig', 'busybox.config']
    names += [p.relative_to(CONFIG).as_posix() for p in sorted((CONFIG / 'patches').rglob('*.patch'))]
    return {name: sha(CONFIG / name) for name in names}


def source_hashes():
    return {name: sha(PROJECT / name) for name in (
        'build/recovery/build-runtime.py', 'build/lib/build-installer-runtime.py',
        'build/lib/source_build.py', 'device/charger-mode/liuqin-charger-mode-exit.c',
        'LICENSE', 'devbox.json', 'devbox.lock', 'build/devbox/env.sh', 'build/devbox/nix/flake.nix',
        'build/devbox/nix/flake.lock',
        *(p.relative_to(PROJECT).as_posix() for p in sorted((PROJECT / 'build/devbox/nix/patches').rglob('*.patch'))),
        *('build/runtime/' + name for name in inputs()))}


def build(args):
    work, out = args.work.resolve(), args.out.resolve()
    for path in (work, out):
        if not path.is_relative_to(PROJECT / 'out') or path == PROJECT / 'out':
            raise ValueError('work and output must be below project/out')
    if out.exists():
        raise ValueError('output already exists: ' + str(out))
    if os.geteuid() == 0:
        raise ValueError('Buildroot runs as an ordinary user; do not use sudo')
    recipe = json.loads((CONFIG / 'buildroot-source.json').read_text())
    identity = inputs()
    source_files = source_hashes()
    receipt = work / 'runtime-inputs.json'
    if work.exists() and (not receipt.is_file() or json.loads(receipt.read_text()) != identity):
        raise ValueError('use a fresh work directory after changing the Buildroot recipe')
    work.mkdir(parents=True, exist_ok=True)
    receipt.write_text(encoded(identity))
    archive = download(recipe['url'], PROJECT / 'inputs/buildroot' / Path(recipe['url']).name,
                       recipe['sha256'])
    source = work / ('buildroot-' + recipe['version'])
    source_receipt = work / 'buildroot-tree.sha256'
    if not source.exists():
        subprocess.run(['tar', '-xf', str(archive), '-C', str(work)], check=True)
        source_receipt.write_text(tree_identity(source) + '\n')
    if not source_receipt.is_file() or source_receipt.read_text().strip() != tree_identity(source):
        raise ValueError('cached Buildroot source changed; use a fresh work directory')
    output = work / 'buildroot-output'
    # Nix's host includes must not become target configure-test dependencies.
    env = dict(os.environ, LC_ALL='C', NIX_CFLAGS_COMPILE='', NIX_LDFLAGS='',
               NIX_HARDENING_ENABLE='',
               SOURCE_DATE_EPOCH='1794268800')
    for name in ('LD_LIBRARY_PATH', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                 'PKG_CONFIG_PATH', 'PKG_CONFIG_LIBDIR', 'CROSS_COMPILE', 'ARCH'):
        env.pop(name, None)
    make = ['make', '-C', str(source), 'O=' + str(output),
            'LIUQIN_RUNTIME_CONFIG=' + str(CONFIG),
            'BR2_DL_DIR=' + str(PROJECT / 'inputs/buildroot/dl'),
            'BR2_JLEVEL=' + str(args.jobs)]
    config_receipt = work / 'buildroot-config.sha256'
    if not (output / '.config').exists():
        subprocess.run([*make, 'BR2_DEFCONFIG=' + str(CONFIG / 'rec.defconfig'), 'defconfig'], env=env, check=True)
        config_receipt.write_text(sha(output / '.config') + '\n')
    if not config_receipt.is_file() or config_receipt.read_text().strip() != sha(output / '.config'):
        raise ValueError('cached Buildroot configuration changed; use a fresh work directory')
    selected = set((output / '.config').read_text().splitlines())
    missing = [line for line in (CONFIG / 'rec.defconfig').read_text().splitlines()
               if line.endswith('=y') and line not in selected]
    if missing:
        raise ValueError('Buildroot did not enable required options: ' + ', '.join(missing))
    # We export target tools, not a relocatable SDK. Buildroot's host-finalize
    # removes absolute Nix store RPATHs and breaks its own host tools. Keep those
    # host paths; target-finalize still builds every package and strips/sanitizes
    # the target tree before extraction.
    subprocess.run([*make, '-o', 'host-finalize', 'target-finalize'], env=env, check=True)
    subprocess.run([*make, 'legal-info'], env=env, check=True)
    if inputs() != identity or source_hashes() != source_files:
        raise ValueError('runtime recipe or build scripts changed during compilation')
    spec = importlib.util.spec_from_file_location('runtime_extract', PROJECT / 'build/lib/build-installer-runtime.py')
    extractor = importlib.util.module_from_spec(spec); spec.loader.exec_module(extractor)
    out.mkdir(parents=True)
    runtime = out / 'runtime'
    extractor.extract(output / 'target', runtime, str(output / 'host/bin/aarch64-buildroot-linux-musl-gcc'))
    legal = output / 'legal-info'
    notices = runtime / 'usr/share/licenses/liuqin-runtime'
    shutil.copytree(legal / 'licenses', notices)
    shutil.copy2(legal / 'manifest.csv', notices / 'manifest.csv')
    info = dict(schema=1, architecture='aarch64', libc='musl', buildroot=recipe,
                configuration_sha256=sha(output / '.config'), sources=source_files,
                packages_manifest_sha256=sha(legal / 'manifest.csv'))
    (runtime / 'etc/liuqin-runtime.json').write_text(encoded(info))
    files = extractor.write_manifest(runtime)
    # Keep corresponding sources alongside the image inputs, not in tablet RAM.
    # Buildroot's legal-info includes package sources and licenses; the original
    # Buildroot release supplies its own package patches and build machinery.
    sources = out / 'runtime-sources.tar'
    with tarfile.open(sources, 'w') as bundle:
        bundle.add(legal, arcname='legal-info', recursive=True)
        bundle.add(archive, arcname=archive.name)
        bundle.add(output / '.config', arcname='buildroot.config')
        for name in source_files:
            bundle.add(PROJECT / name, arcname='project/' + name)
    info.update(runtime_files_sha256=sha(runtime / 'runtime-files.json'),
                sources_archive_sha256=sha(sources), files=len(files),
                bytes=sum((runtime / name).stat().st_size for name in files))
    (out / 'runtime-manifest.json').write_text(encoded(info))
    (out / 'SHA256SUMS').write_text(sha(sources) + '  runtime-sources.tar\n' +
                                   sha(runtime / 'runtime-files.json') + '  runtime/runtime-files.json\n')
    print(encoded(dict(runtime=str(runtime), bytes=info['bytes'], files=len(files),
                       sources=str(sources))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=PROJECT / 'out/build/rec-runtime')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=min(os.cpu_count() or 1, 12))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    build(args)
