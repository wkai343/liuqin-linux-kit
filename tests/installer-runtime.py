#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise provisioning with synthetic factory data in a disposable ARM64 chroot."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--busybox', type=Path, required=True)
args = parser.parse_args()
project = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='liuqin-runtime-test-') as temporary:
    root = Path(temporary) / 'root'
    shutil.copytree(args.runtime, root)
    for name in ('bin', 'proc', 'dev', 'target/etc', 'factory/wlan', 'factory/bluetooth',
                 'factory/audio', 'factory/sensors/registry/registry'):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.busybox, root / 'bin/busybox')
    (root / 'bin/busybox').chmod(0o755)
    (root / 'bin/sh').symlink_to('busybox')
    (root / 'dev/stdin').symlink_to('/proc/self/fd/0')
    (root / 'dev/null').touch()
    (root / 'target/etc/liuqin-native-root').touch()
    (root / 'target/etc/passwd').write_text('fastrpc:x:0:0::/:/bin/false\n')
    (root / 'target/etc/group').write_text('fastrpc:x:0:\n')
    (root / 'factory/wlan/wlan_mac.bin').write_text('wlan0=020000000001\n')
    (root / 'factory/bluetooth/.bt_nv.bin').write_bytes(bytes.fromhex('020000000002'))
    (root / 'factory/audio/crus_calr.bin').write_bytes(bytes(range(16)))
    for index in range(101):
        (root / f'factory/sensors/registry/registry/item{index}').write_text('fixture\n')
    shutil.copyfile(project / 'device/installer/provision-liuqin-from-persist.sh', root / 'provision.sh')
    subprocess.run(['mount', '-t', 'proc', 'proc', str(root / 'proc')], check=True)
    try:
        subprocess.run(['chroot', str(root), '/bin/sh', '/provision.sh', '/target'],
                       env=dict(os.environ, PERSIST_SRC='/factory', PATH='/bin:/usr/bin:/usr/sbin'), check=True)
    finally:
        subprocess.run(['umount', str(root / 'proc')], check=True)
    assert (root / 'target/var/lib/liuqin-private/wlan-mac').read_text().strip() == '02:00:00:00:00:01'
    assert (root / 'target/var/lib/liuqin-private/bluetooth-address').read_text().strip() == '02:00:00:00:00:02'
    for index, channel in enumerate(('TL', 'TR', 'BL', 'BR')):
        assert (root / f'target/usr/lib/firmware/cirrus/cs35l41-liuqin-{channel}-calr.bin').read_bytes() == bytes(range(index * 4, index * 4 + 4))
print('PASS: installer runtime provisions synthetic data; no tablet or real calibration accessed')
