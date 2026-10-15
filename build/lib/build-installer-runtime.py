#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract installer tools and their ELF dependencies from an Ubuntu ARM64 root."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from elftools.elf.elffile import ELFFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root, out = args.root.resolve(), args.out.resolve()
    if out.exists():
        parser.error('output must be a new directory')
    libraries = root / 'usr/lib/aarch64-linux-gnu'
    pending = [('usr/bin/tar', root / 'usr/bin/tar'),
               ('usr/bin/install', root / 'usr/bin/install'),
               ('usr/sbin/mkfs.ext4', root / 'usr/sbin/mkfs.ext4'),
               ('usr/sbin/mkfs.btrfs', root / 'usr/sbin/mkfs.btrfs'),
               ('usr/bin/btrfs', next((root / name for name in ('usr/bin/btrfs', 'usr/sbin/btrfs')
                                       if (root / name).is_file()), root / 'usr/bin/btrfs')),
               ('usr/sbin/e2fsck', root / 'usr/sbin/e2fsck'),
               # install-root.sh KEEP-HOME reads the home filesystem's label.
               # e2label is tune2fs under another name; the copy keeps the name.
               ('usr/sbin/e2label', root / 'usr/sbin/e2label'),
               ('usr/sbin/getcap', root / 'usr/sbin/getcap'),
               # The layout step edits the GPT on the device, so the partition
               # editor has to be the distribution's, matched with its libuuid.
               ('usr/sbin/sgdisk', root / 'usr/sbin/sgdisk')]
    files = {}
    while pending:
        relative, source = pending.pop()
        if relative in files:
            continue
        with source.open('rb') as stream:
            elf = ELFFile(stream)
            if elf['e_machine'] != 'EM_AARCH64':
                parser.error('expected ARM64 executable: ' + relative)
            for segment in elf.iter_segments():
                if segment['p_type'] == 'PT_INTERP':
                    interpreter = segment.get_interp_name()
                    pending.append((interpreter.lstrip('/'), libraries / Path(interpreter).name))
                if segment['p_type'] == 'PT_DYNAMIC':
                    for tag in segment.iter_tags():
                        if tag.entry.d_tag == 'DT_NEEDED':
                            pending.append(('lib/aarch64-linux-gnu/' + tag.needed, libraries / tag.needed))
        files[relative] = source
    # Keep the distro's ext4 feature defaults with its matching e2fsprogs.
    files['etc/mke2fs.conf'] = root / 'etc/mke2fs.conf'
    out.mkdir(parents=True)
    manifest = {}
    for relative, source in sorted(files.items()):
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o644 if relative == 'etc/mke2fs.conf' else 0o755)
        manifest[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()
    reboot = out / 'usr/sbin/liuqin-reboot'
    source = Path(__file__).resolve().parents[2] / 'device/charger-mode/liuqin-charger-mode-exit.c'
    subprocess.run([os.environ.get('CROSS_COMPILE', 'aarch64-linux-gnu-') + 'gcc',
                    '-Os', '-static', '-s', str(source), '-o', str(reboot)], check=True)
    manifest['usr/sbin/liuqin-reboot'] = hashlib.sha256(reboot.read_bytes()).hexdigest()
    (out / 'runtime-files.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (out / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
    (out / 'etc/group').write_text('root:x:0:\n')
    print(f'Installer runtime: {len(files)} files; no device access')


if __name__ == '__main__':
    main()
