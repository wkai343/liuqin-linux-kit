# SPDX-License-Identifier: MIT
"""Build the same static, read-only UFS scope selector used by early boot."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[2]
SOURCES = ('device/boot/liuqin-storage-seal.c', 'device/boot/liuqin-storage-ready.c',
           'device/boot/liuqin-storage-scope.h')
FLAGS = ('-static', '-Os', '-s', '-Wall', '-Wextra', '-Werror')


def inputs(compiler=None):
    name = compiler or os.environ.get('STORAGE_SCOPE_CC') or os.environ.get(
        'CROSS_COMPILE', 'aarch64-unknown-linux-musl-') + 'gcc'
    executable = shutil.which(str(name))
    if not executable:
        raise ValueError('ARM64 storage helper compiler is unavailable; enter Devbox.')
    digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return dict(sources={p: digest(PROJECT/p) for p in SOURCES}, flags=list(FLAGS),
                compiler=str(Path(executable).resolve()), compiler_sha256=digest(Path(executable).resolve()))


def build(output, compiler=None):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('Storage helper output already exists.')
    info = inputs(compiler)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([info['compiler'], *FLAGS, *[str(PROJECT/p) for p in SOURCES[:2]],
                    '-o', str(output)], check=True)
    header = output.read_bytes()[:20]
    segments = subprocess.check_output(['readelf', '-l', str(output)], text=True)
    if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\xb7\x00' or 'INTERP' in segments:
        raise ValueError('Storage helper is not a static AArch64 ELF.')
    output.chmod(0o755)
    return output


# Only untouched files from the retired project payload are removed.
LEGACY_FILES = {'usr/libexec/liuqin-boot-usb-storage': ('01e9adbadf88015fb096b0ecd83dd2f0dce0ab8422bbfdf11c6da2d7f45a1e22',), 'etc/systemd/system/liuqin-boot-usb-storage.service': ('719882a9747de61acb0592389d1ffd131d8e5d5bd1b942efbcf898f1a87ed20c',), 'etc/systemd/system/multi-user.target.d/20-liuqin-boot-usb-storage.conf': ('fe239bdba94facd6a161d3ee70941679169569280d89c60b9948617da1b9582a',), 'etc/systemd/system/liuqin-gnome-usb-rescue.service': ('9b950307b549d0b3b7c088c24adac289fa34b277d3b3cd41aa6cc5e5fbcf37c7',), 'usr/local/sbin/liuqin-gnome-usb-rescue': ('4f683c3f00d40886fb5f55316a4dd12a433bad204d6544056ba5d9f5f4cafca6',), 'usr/local/sbin/liuqin-rescue': ('f28387d6a8ba4e602e2c2068bacbf4389252417f7e9c33a828bd5b470c4acb62',), 'usr/local/bin/liuqin-shell': ('a21426505be164a3a5967c5c1649d1df64ab0df42b5974872850ecb5b6f26161',), 'etc/liuqin-rescue-enabled': ('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',)}
LEGACY_LINKS = {'etc/systemd/system/multi-user.target.wants/liuqin-gnome-usb-rescue.service':'../liuqin-gnome-usb-rescue.service'}

def legacy_cleanup():
    lines = ['if [ -d /run/systemd/system ]; then systemctl stop liuqin-boot-usb-storage.service liuqin-gnome-usb-rescue.service 2>/dev/null || :; fi']
    for name, digests in LEGACY_FILES.items():
        if isinstance(digests, str): digests=(digests,)
        pattern='|'.join(digests)
        path = "/" + name
        lines += [f'if [ -f "{path}" ] && [ ! -L "{path}" ]; then',
                  f'  case "$(sha256sum "{path}" | cut -d " " -f 1)" in',
                  f'    {pattern}) rm -f -- "{path}" ;;',
                  f'    *) echo "liuqin: retained modified legacy file: {path}" >&2 ;;',
                  '  esac', 'fi']
    for name, target in LEGACY_LINKS.items():
        path="/"+name
        lines += [f'if [ -L "{path}" ]; then',
                  f'  if [ "$(readlink "{path}")" = "{target}" ]; then rm -f -- "{path}";',
                  f'  else echo "liuqin: retained modified legacy link: {path}" >&2; fi', 'fi']
    lines += ['if [ -d /run/systemd/system ]; then systemctl daemon-reload || :; fi']
    return "\n".join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--compiler')
    args = parser.parse_args()
    build(args.out, args.compiler)
