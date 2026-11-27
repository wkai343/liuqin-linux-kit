#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise static ARM64 BusyBox via QEMU using only temporary regular files."""
import argparse
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import shutil
import subprocess
import tempfile

APPLETS = '''awk base64 basename blockdev cat chmod chown chroot chvt cp cttyhack cut date dd
dirname dmesg du echo find findfs grep gzip head hostname id insmod ip kill killall ln
losetup ls mdev mkdir mknod modprobe mount mv od poweroff readlink realpath reboot rm
rmdir rmmod sed setsid sh sha256sum sleep sort stat switch_root sync tail tar telnetd
test timeout touch tr truncate udhcpd umount uname uniq wc wget xargs'''.split()


def check(binary):
    binary = binary.resolve()
    header = binary.read_bytes()[:20]
    assert header[:6] == b'\x7fELF\x02\x01' and header[18:20] == b'\xb7\x00'
    assert 'INTERP' not in subprocess.check_output(['readelf', '-l', binary], text=True)
    qemu = shutil.which('qemu-aarch64') or shutil.which('qemu-aarch64-static')
    if not qemu:
        raise RuntimeError('QEMU ARM64 is required')
    def run(*args, **kw):
        return subprocess.check_output([qemu, str(binary), *map(str, args)], **kw)
    applets = set(run('--list').decode().split())
    assert not set(APPLETS) - applets, sorted(set(APPLETS) - applets)
    with tempfile.TemporaryDirectory(prefix='liuqin-busybox-test-') as temp:
        root = Path(temp); src = root / 'source'; src.mkdir()
        payload = b'liuqin busybox runtime\n' * 100
        file = src / 'payload'; file.write_bytes(payload); file.chmod(0o640)
        (src / 'alias').symlink_to('payload')
        assert run('stat', '-c', '%a', file).strip() == b'640'
        assert run('stat', '-f', '-c', '%T', src).strip()
        assert run('readlink', '-f', src / 'alias').strip().decode() == str(file)
        assert run('sha256sum', file).decode().split()[0] == hashlib.sha256(payload).hexdigest()
        assert run('awk', '{print $2}', input=b'field value\n') == b'value\n'
        assert run('sh', '-c', 'v=0; for n in 1 2 3; do v=$((v+n)); done; echo "$v"') == b'6\n'
        assert gzip.decompress(run('gzip', '-c', file)) == payload
        compressed = root / 'data.xz'; compressed.write_bytes(lzma.compress(payload))
        assert run('xz', '-dc', compressed) == payload
        archive = root / 'payload.tar'
        run('tar', '-cf', archive, '-C', src, '.')
        dest = root / 'dest'; dest.mkdir()
        run('tar', '-xf', archive, '-C', dest)
        assert (dest / 'payload').read_bytes() == payload
        assert (dest / 'payload').stat().st_mode & 0o777 == 0o640
        assert (dest / 'alias').is_symlink() and (dest / 'alias').readlink() == Path('payload')
    return dict(sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), applets=sorted(applets), passed=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--busybox', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = check(args.busybox)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: static ARM64 BusyBox applets, shell, stat, hash, compression and tar metadata')
