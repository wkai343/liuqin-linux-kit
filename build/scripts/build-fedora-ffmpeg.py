#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Rebuild the pinned Fedora FFmpeg SRPM with Iris V4L2 fixes; never install it."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile


def prepare(srpm, out, recipe):
    lock = json.loads((recipe / 'source.json').read_text())
    if hashlib.sha256(srpm.read_bytes()).hexdigest() != lock['sha256']:
        raise ValueError('Fedora SRPM SHA-256 mismatch; review a new source before updating the recipe')
    out.mkdir(parents=True, exist_ok=False)
    for name in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
        (out / name).mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(['rpm2cpio', str(srpm)], stdout=archive, check=True)
        archive.seek(0)
        names = subprocess.check_output(['cpio', '-it', '--quiet'], stdin=archive, text=True).splitlines()
        if not names or any(name not in (Path(name).name, './' + Path(name).name)
                            or Path(name).name in ('', '.', '..') for name in names):
            raise ValueError('Unexpected SRPM archive paths')
        archive.seek(0)
        subprocess.run(['cpio', '-idm', '--quiet', '--no-absolute-filenames'],
                       cwd=out / 'SOURCES', stdin=archive, check=True)
    spec_path = out / 'SOURCES/ffmpeg.spec'
    if hashlib.sha256(spec_path.read_bytes()).hexdigest() != lock['spec_sha256']:
        raise ValueError('Fedora spec SHA-256 mismatch')
    spec = spec_path.read_text()
    old_release = 'Release:        3%{?dist}'
    if spec.count(old_release) != 1 or spec.count('\nPatch1:') != 1:
        raise ValueError('Unexpected Fedora spec layout')
    spec = spec.replace(old_release, 'Release:        ' + lock['release'] + '%{?dist}')
    patches = []
    for index, name in enumerate(lock['patches'], 9000):
        shutil.copyfile(recipe / 'patches' / name, out / 'SOURCES' / name)
        patches.append(f'Patch{index}: {name}')
    # Patch tags must be in the package header, before any %description/%files.
    spec = spec.replace('\nPatch1:', '\n' + '\n'.join(patches) + '\n\nPatch1:')
    (out / 'SPECS/ffmpeg.spec').write_text(spec)
    spec_path.unlink()
    decoders = out / 'SOURCES/enable_decoders'
    enabled = {line.split()[0] for line in decoders.read_text().splitlines() if line.strip()}
    with decoders.open('a') as stream:
        for name in lock['decoders']:
            if name not in enabled:
                stream.write(f'\n{name} # V4L2 hardware wrapper, no native software decoder\n')
    (out / 'liuqin-source.json').write_text(json.dumps(lock, indent=2) + '\n')
    return out / 'SPECS/ffmpeg.spec'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--srpm', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    if not args.prepare_only and platform.machine() != 'aarch64':
        parser.error('Build on Fedora aarch64, or use --prepare-only')
    recipe = Path(__file__).resolve().parents[1] / 'ffmpeg'
    out = args.out.resolve()
    spec = prepare(args.srpm.resolve(), out, recipe)
    if not args.prepare_only:
        subprocess.run(['rpmbuild', '-ba', '--define', '_topdir ' + str(out),
                        '--define', '_smp_mflags -j' + str(args.jobs), str(spec)], check=True)
    print(out)


if __name__ == '__main__':
    main()
