#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract the Rec tools and their ELF closure from a Buildroot ARM64 musl target."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from elftools.elf.elffile import ELFFile

TOOLS = ('tar', 'python3', 'mkfs.ext4', 'mkfs.f2fs', 'mkfs.btrfs',
         'btrfs', 'e2fsck', 'e2label', 'getcap', 'sgdisk')
USER_TOOLS = {'tar', 'python3', 'btrfs'}
LOADER = 'lib/ld-musl-aarch64.so.1'


def target_path(root, name):
    """Resolve target absolute links inside root, never against the build host."""
    parts = list(Path(name.lstrip('/')).parts)
    resolved = []
    links = 0
    while parts:
        part = parts.pop(0)
        if part == '..':
            if not resolved:
                raise ValueError('target link escapes root: ' + name)
            resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('target symlink loop: ' + name)
            link = os.readlink(candidate)
            if link.startswith('/'):
                resolved = []
            parts = list(Path(link.lstrip('/')).parts) + parts
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


def find(root, name, directories):
    if Path(name).name != name:
        raise ValueError('invalid ELF dependency: ' + name)
    for directory in directories:
        path = target_path(root, directory + '/' + name)
        if path.is_file():
            return path
    raise ValueError('missing target tool/library: ' + name)


def extract(root, out, compiler=None):
    if out.exists():
        raise ValueError('output must be a new directory')
    pending = [('usr/' + ('bin/' if name in USER_TOOLS else 'sbin/') + name,
                find(root, name, ('usr/bin', 'usr/sbin', 'bin', 'sbin'))) for name in TOOLS]
    busybox = find(root, 'busybox', ('bin', 'usr/bin'))
    with busybox.open('rb') as stream:
        if any(s['p_type'] == 'PT_INTERP' for s in ELFFile(stream).iter_segments()):
            raise ValueError('Rec BusyBox must be static')
    pending += [('bin/busybox', busybox), ('usr/bin/install', busybox),
                (LOADER, target_path(root, LOADER))]
    files = {}
    version = target_path(root, 'usr/bin/python3').name
    if not version.startswith('python3.'):
        raise ValueError('cannot identify target Python standard library')
    standard = root / 'usr/lib' / version
    if not (standard / 'encodings/__init__.py').is_file():
        raise ValueError('target Python standard library is incomplete')
    for source in standard.rglob('*'):
        if not source.is_file() or source.is_symlink() or any(part in
                ('__pycache__', 'test', 'tests', 'site-packages', 'dist-packages')
                for part in source.relative_to(standard).parts):
            continue
        relative = source.relative_to(root).as_posix()
        if source.suffix == '.so':
            pending.append((relative, source))
        elif source.suffix == '.py':
            files[relative] = source
    while pending:
        relative, source = pending.pop()
        if relative in files:
            continue
        with source.open('rb') as stream:
            elf = ELFFile(stream)
            if elf['e_machine'] != 'EM_AARCH64':
                raise ValueError('expected ARM64 executable: ' + relative)
            for segment in elf.iter_segments():
                if segment['p_type'] == 'PT_INTERP':
                    interpreter = segment.get_interp_name().lstrip('/')
                    if interpreter != LOADER:
                        raise ValueError('expected musl interpreter: ' + relative)
                    pending.append((interpreter, target_path(root, interpreter)))
                if segment['p_type'] == 'PT_DYNAMIC':
                    for tag in segment.iter_tags():
                        if tag.entry.d_tag == 'DT_NEEDED':
                            pending.append(('lib/' + tag.needed,
                                find(root, tag.needed, ('lib', 'usr/lib'))))
        files[relative] = source
    files['etc/mke2fs.conf'] = root / 'etc/mke2fs.conf'
    out.mkdir(parents=True)
    for relative, source in sorted(files.items()):
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o644 if relative == 'etc/mke2fs.conf' or relative.endswith('.py') else 0o755)
    # BusyBox selects install by argv[0]; no full coreutils binary is needed.
    reboot = out / 'usr/sbin/liuqin-reboot'
    source = Path(__file__).resolve().parents[2] / 'device/charger-mode/liuqin-charger-mode-exit.c'
    subprocess.run([compiler or os.environ.get('CROSS_COMPILE', 'aarch64-unknown-linux-musl-') + 'gcc',
                    '-Os', '-static', '-s', str(source), '-o', str(reboot)], check=True)
    (out / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
    (out / 'etc/group').write_text('root:x:0:\n')
    write_manifest(out)


def write_manifest(out):
    manifest = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out.rglob('*')) if p.is_file() and p.name != 'runtime-files.json'}
    (out / 'runtime-files.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    extract(args.root.resolve(), args.out.resolve())
