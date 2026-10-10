#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the RAM root installer on disposable files, never block devices.

Only absolute RAM paths, block-node predicates, mounts, transfer and provisioning
are redirected. The real option parsing, format selection, extraction, fstab,
contract verification and cleanup run under BusyBox ash. mkfs/e2label use real
ext4 image files; mount operations only log, requiring no privilege.
"""
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

project = Path(__file__).resolve().parents[1]
source = (project / 'tools/lib/install-root.sh').read_text()
search_path = os.environ.get('PATH', '') + ':/usr/sbin:/sbin'
busybox = os.environ.get('HOST_BUSYBOX') or shutil.which('busybox', path=search_path)
mkfs = shutil.which('mkfs.ext4', path=search_path)
label = shutil.which('e2label', path=search_path)
assert busybox and mkfs and label, 'BusyBox and e2fsprogs are required'


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def execute(single, *, provision_failure=False, options=()):
    with tempfile.TemporaryDirectory(prefix='liuqin-single-root-test-') as temporary:
        root = Path(temporary)
        bin = root / 'bin'
        log = root / 'commands'
        log.touch()
        (root / 'state').mkdir()
        (root / 'dev/disk/by-partlabel').mkdir(parents=True)
        (root / 'sys/class/block').mkdir(parents=True)
        for number, name in ((1, 'persist'), (35, 'userdata')) if single else (
                (1, 'persist'), (36, 'linux_root'), (37, 'linux_home')):
            node = 'sda' + str(number)
            directory = root / 'sys/devices/sda' / node
            directory.mkdir(parents=True)
            (root / 'sys/class/block' / node).symlink_to(directory)
            (directory / 'uevent').write_text('PARTNAME=' + name + '\n')
            (directory / 'size').write_text('33554432\n')
            (directory / 'dev').write_text('8:' + str(number) + '\n')
            image = root / 'dev' / node
            with image.open('wb') as stream:
                stream.truncate(64 * 1024 * 1024)
            (root / 'state' / node).write_text('1\n')
            (root / 'dev/disk/by-partlabel' / name).symlink_to(image)
        (root / 'dev/sda').touch()
        (root / 'state/sda').write_text('1\n')
        (root / 'sys/class/block/sda/queue').mkdir(parents=True)
        (root / 'sys/class/block/sda/queue/logical_block_size').write_text('4096\n')
        supply = root / 'sys/class/power_supply/battery'
        supply.mkdir(parents=True)
        (supply / 'type').write_text('Battery\n')
        (supply / 'capacity').write_text('80\n')
        (root / 'etc').mkdir()
        (root / 'etc/liuqin-installer').write_text('liuqin\n')
        (root / 'boot_id').write_text('11111111-1111-4111-8111-111111111111\n')
        (root / 'mountinfo').touch()
        (root / 'meminfo').write_text('MemAvailable: 8388608 kB\n')
        persist = root / 'run/persist'
        for rel in ('wlan/wlan_mac.bin', 'bluetooth/.bt_nv.bin', 'audio/crus_calr.bin',
                    'sensors/registry/registry/sample'):
            p = persist / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('synthetic factory fixture\n')
        tree = root / 'tree'
        for rel, content in {'etc/liuqin-native-root': 'liuqin-native-root-v1\n',
                             'home/default-user/note': 'home skeleton\n',
                             'usr/lib/snapd/snap-confine': 'fixture\n'}.items():
            p = tree / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        archive = root / 'rootfs.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for path in tree.iterdir():
                tar.add(path, arcname=path.name)
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        marker_hash = hashlib.sha256((tree / 'etc/liuqin-native-root').read_bytes()).hexdigest()
        (root / 'etc/liuqin-native-root.contract').write_text(
            'LIUQIN_NATIVE_ROOT_CONTRACT_V1\n' + marker_hash + '  /etc/liuqin-native-root\n')

        executable(bin / 'bb', '''#!/bin/sh
printf 'bb %s\n' "$*" >>"$TEST_LOG"
command=$1; shift
case $command in
blockdev)
    flag=$1; node=${2##*/}
    case $flag in
    --getro) cat "$TEST_ROOT/state/$node" ;;
    --setrw) printf '0\n' >"$TEST_ROOT/state/$node" ;;
    --setro) printf '1\n' >"$TEST_ROOT/state/$node" ;;
    *) exit 2 ;;
    esac ;;
wget) [ "$1" = -O ] || exit 2; cp "$TEST_ARCHIVE" "$2" ;;
*) exec "$TEST_BUSYBOX" "$command" "$@" ;;
esac
''')
        executable(bin / 'mount', '''#!/bin/sh
printf 'mount %s\n' "$*" >>"$TEST_LOG"
for last in "$@"; do :; done
mkdir -p "$last"
''')
        executable(bin / 'umount', '#!/bin/sh\nprintf "umount %s\\n" "$*" >>"$TEST_LOG"\n')
        executable(bin / 'mkfs', '#!/bin/sh\nprintf "mkfs %s\\n" "$*" >>"$TEST_LOG"\n'
                   'exec "$TEST_MKFS" "$@"\n')
        executable(bin / 'getcap', '#!/bin/sh\nprintf "fixture cap_setuid=ep\\n"\n')
        executable(root / 'usr/lib/liuqin/provision.sh', '''#!/bin/sh
printf 'provision %s\n' "$*" >>"$TEST_LOG"
[ "$TEST_PROVISION_FAIL" != 1 ]
''')
        replacements = {
            '/bin/busybox': str(bin / 'bb'), '/usr/sbin/mkfs.ext4': str(bin / 'mkfs'),
            '/usr/sbin/getcap': str(bin / 'getcap'),
            '/proc/sys/kernel/random/boot_id': str(root / 'boot_id'),
            '/proc/self/mountinfo': str(root / 'mountinfo'), '/proc/meminfo': str(root / 'meminfo'),
            '/usr/lib/liuqin/': str(root / 'usr/lib/liuqin') + '/',
            '/sys/class/': str(root / 'sys/class') + '/', '/dev/': str(root / 'dev') + '/',
            '/etc/': str(root / 'etc') + '/', '/mnt/': str(root / 'mnt') + '/',
            '/run/persist': str(persist),
        }
        script = source
        for old, new in replacements.items():
            script = re.sub(r'(?<![A-Za-z0-9_./-])' + re.escape(old), lambda unused: new, script)
        # The fixture consists exclusively of regular image files.
        script = script.replace('[ -b ', '[ -f ')
        script_path = root / 'install-root.sh'
        script_path.write_text(script)
        env = dict(os.environ, PATH=str(bin) + ':' + search_path,
                   TEST_LOG=str(log), TEST_ROOT=str(root), TEST_BUSYBOX=busybox,
                   TEST_ARCHIVE=str(archive), TEST_MKFS=mkfs,
                   TEST_PROVISION_FAIL='1' if provision_failure else '0')
        result = subprocess.run([busybox, 'sh', str(script_path),
                                 (root / 'boot_id').read_text().strip(), 'fixture-url',
                                 archive_hash, str(archive.stat().st_size), 'ERASE-LIUQIN-USERDATA',
                                 'userdata' if single else 'linux_root',
                                 '-' if single else 'linux_home', *options],
                                env=env, capture_output=True, text=True)
        commands = log.read_text().splitlines()
        if options or provision_failure:
            assert result.returncode != 0, result.stdout
            if options:
                assert not any(c.startswith('mkfs ') for c in commands), commands
            for state in (root / 'state').iterdir():
                assert state.read_text().strip() == '1', (state, commands)
            return
        assert result.returncode == 0, result.stderr + '\n' + result.stdout
        assert 'ROOT_INSTALLED' in result.stdout
        formats = [c for c in commands if c.startswith('mkfs ')]
        assert len(formats) == (1 if single else 2), formats
        system = root / 'mnt/install/native-root'
        fstab = (system / 'etc/fstab').read_text()
        assert ('LABEL=LIUQIN_HOME' in fstab) == (not single)
        assert (system / 'home/default-user/note').read_text() == 'home skeleton\n'
        if single:
            assert all('install-home' not in c for c in commands if c.startswith('mount ')), commands
            formatted = root / 'dev/sda35'
        else:
            assert (root / 'mnt/install-home/default-user/note').read_text() == 'home skeleton\n'
            formatted = root / 'dev/sda36'
        observed = subprocess.check_output([label, str(formatted)], text=True).strip()
        assert observed == 'LIUQIN_ROOT', observed
        for state in (root / 'state').iterdir():
            assert state.read_text().strip() == '1', (state, commands)


execute(True)
print('PASS: single root formats only userdata, retains home inside root and omits home fstab')
execute(False)
print('PASS: split layout still formats and mounts its separate home')
execute(True, provision_failure=True)
print('PASS: a single-root failure reseals root and parent without a home device')
execute(True, options=('KEEP-HOME',))
print('PASS: single-root KEEP-HOME is refused before any formatting')
