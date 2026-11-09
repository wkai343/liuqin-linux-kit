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
import sys
from kernel_fixtures import kernel_fixture, install_fixture

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'shared'))
from liuqin_boot_target import encode
source = (project / 'device/installer/install-root.sh').read_text()
search_path = os.environ.get('PATH', '') + ':/usr/sbin:/sbin'
busybox = os.environ.get('HOST_BUSYBOX') or shutil.which('busybox', path=search_path)
mkfs = shutil.which('mkfs.ext4', path=search_path)
label = shutil.which('e2label', path=search_path)
host_tar = shutil.which('tar', path=search_path)
assert busybox and mkfs and label and host_tar, 'BusyBox, GNU tar and e2fsprogs are required'


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def execute(single, *, provision_failure=False, options=(), snap=True, independent=True, contract_failure=None, missing_contract=False, updatable=True, filesystem='ext4', root_support=True, installer_support=True, archive_dot=False, bound=False, wrong_partuuid=False, wrong_range=False, native=False, preformatted=False):
    independent = independent or updatable
    target = dict(schema_version=1, os_id='11111111-1111-4111-8111-111111111111',
        root_partuuid='22222222-2222-4222-8222-222222222222',
        root_fsuuid='33333333-3333-4333-8333-333333333333', root_fstype=filesystem,
        root_path='native-root', boot_generation='44444444-4444-4444-8444-444444444444')
    target_name = 'lqr_111111111111'
    with tempfile.TemporaryDirectory(prefix='liuqin-single-root-test-') as temporary:
        root = Path(temporary)
        bin = root / 'bin'
        log = root / 'commands'
        log.touch()
        (root / 'state').mkdir()
        (root / 'dev/disk/by-partlabel').mkdir(parents=True)
        (root / 'sys/class/block').mkdir(parents=True)
        for number, name in ((1, 'persist'), (35, target_name if bound else 'userdata')) if single else (
                (1, 'persist'), (36, 'linux_root'), (37, 'linux_home')):
            node = 'sda' + str(number)
            directory = root / 'sys/devices/sda' / node
            directory.mkdir(parents=True)
            (root / 'sys/class/block' / node).symlink_to(directory)
            (directory / 'uevent').write_text('PARTNAME=' + name + '\n' + ('PARTUUID=' + ('55555555-5555-4555-8555-555555555555' if wrong_partuuid else target['root_partuuid']) + '\n' if bound and number == 35 else ''))
            (directory / 'size').write_text('33554432\n')
            (directory / 'start').write_text('5500000\n')
            (directory / 'dev').write_text('8:' + str(number) + '\n')
            image = root / 'dev' / node
            with image.open('wb') as stream:
                stream.truncate((256 if filesystem == 'btrfs' else 64) * 1024 * 1024)
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
        (root / 'etc/liuqin-filesystems').write_text('ext4\nbtrfs\n' if installer_support else 'ext4\n')
        (root / 'filesystems').write_text('nodev\tbtrfs\n')
        (root / 'etc/liuqin-installer').write_text('liuqin\n')
        (root / 'etc/liuqin-multiboot').write_text('1\n')
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
        contents = {'etc/liuqin-native-root': 'liuqin-native-root-v1\n',
                             'etc/liuqin-filesystems': 'ext4\nbtrfs\n',
                             'etc/liuqin-root-filesystem': 'ext4\n',
                             'home/default-user/note': 'home skeleton\n',
                             'usr/lib/snapd/snap-confine': 'fixture\n'}
        if bound: contents.update({'etc/liuqin-system-id': 'unbound\n', 'etc/liuqin-boot-target': 'unbound\n'})
        contents.update({f'etc/liuqin-fixture-{i}': 'device interface fixture\n' for i in range(8)})
        if not root_support:contents.pop('etc/liuqin-filesystems')
        for rel, content in contents.items():
            p = tree / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        archive = root / 'rootfs.tar.gz'
        if not snap:
            (tree / 'usr/lib/snapd/snap-confine').unlink()
        if updatable:
            versioned = kernel_fixture(root / 'versioned-input', '6.18.0-liuqin1')
            install_fixture(tree, '6.18.0-liuqin1', versioned)
        with tarfile.open(archive, 'w:gz') as tar:
            for path in tree.iterdir():
                tar.add(path, arcname=('./' if archive_dot else '') + path.name)
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        root_options = []
        if independent:
            (root / 'etc/liuqin-installer-interface').write_text('liuqin-installer-v2\n')
            contract = root / 'run/target-root.contract'
            contract.parent.mkdir(parents=True, exist_ok=True)
            contract.write_bytes(versioned[3])
            if contract_failure == 'root':
                contract.write_text(contract.read_text().split('modules_sha256=')[0] + 'modules_sha256=' + '0' * 64 + '\n')
            if contract_failure == 'syntax': contract.write_text(contract.read_text().replace('ROOT_INTERFACE_V2', 'ROOT_INTERFACE_V99'))
            digest = hashlib.sha256(contract.read_bytes()).hexdigest()
            if contract_failure == 'hash': digest = '0' * 64
            if not missing_contract: root_options = ['ROOT-CONTRACT', str(contract), digest]

        if bound:
            binding = root / 'run/boot-target'
            binding.parent.mkdir(parents=True, exist_ok=True)
            binding.write_bytes(encode(target))
            root_options += ['BOOT-TARGET', str(binding), hashlib.sha256(binding.read_bytes()).hexdigest(),
                             'ROOT-RANGE',str(512*(5500001 if wrong_range else 5500000)),str(33554432*512)]
        executable(bin / 'bb', '''#!/bin/sh
printf 'bb %s\n' "$*" >>"$TEST_LOG"
command=$1; shift
case $command in
stat)
    if [ "$2" = '%a %u %g' ]; then
        printf '%s 0 0\n' "$("$TEST_BUSYBOX" stat -c %a "$3")"
    else exec "$TEST_BUSYBOX" stat "$@"; fi ;;
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
if [ "$last" = "$TEST_ROOT/run/persist" ]; then touch "$TEST_ROOT/persist-mounted"; fi
''')
        executable(bin / 'umount', '''#!/bin/sh
printf 'umount %s\n' "$*" >>"$TEST_LOG"
if [ "$1" = "$TEST_ROOT/run/persist" ]; then rm -f "$TEST_ROOT/persist-mounted"; fi
''')
        executable(bin / 'mkfs', '#!/bin/sh\nprintf "mkfs %s\\n" "$*" >>"$TEST_LOG"\n'
                   'exec "$TEST_MKFS" "$@"\n')
        executable(bin / 'btrfs', '#!/bin/sh\nprintf "btrfs %s\\n" "$*" >>"$TEST_LOG"\n'
                   '[ "$1:$2" = subvolume:create ] && mkdir "$3"\n')
        executable(bin / 'getcap', '#!/bin/sh\nprintf "getcap %s\\n" "$*" >>"$TEST_LOG"\nprintf "fixture cap_setuid=ep\\n"\n')
        executable(bin / 'tar', '#!/bin/sh\nprintf "tar %s\\n" "$*" >>"$TEST_LOG"\n'
                   'exec "$TEST_TAR" "$@"\n')
        # The host fixtures model files created by the root RAM session.
        executable(bin / 'stat', '#!/bin/sh\nif [ "$2" = "%u %g" ]; then printf "0 0\\n"; '
                   'else exec ' + shutil.which('stat') + ' "$@"; fi\n')
        executable(root / 'usr/lib/liuqin/provision.sh', '''#!/bin/sh
printf 'provision %s\n' "$*" >>"$TEST_LOG"
[ "$TEST_PROVISION_FAIL" != 1 ]
''')
        (root / 'usr/lib/liuqin/boot-target-v1.sh').write_bytes((project/'device/boot/boot-target-v1.sh').read_bytes())
        (root / 'usr/lib/liuqin/root-interface-v2.sh').write_bytes((project / 'device/boot/root-interface-v2.sh').read_bytes())
        if native:
            (root / 'etc/liuqin-native-recovery.json').touch()
            (root / 'usr/lib/liuqin/native-root-guard.py').write_text('''import os, sys
from pathlib import Path
if sys.argv[1:] == ['failed']:
    sys.exit(0)
root = Path(os.environ['TEST_ROOT'])
assert not (root / 'persist-mounted').exists(), 'persist is still mounted'
assert all(p.read_text().strip() == '1' for p in (root / 'state').iterdir())
if os.environ['TEST_NATIVE_BTRFS'] == '1':
    assert sys.argv[3:] == ['PREPARE-BTRFS'], 'Btrfs retry preparation missing'
    import importlib.util
    sys.path.insert(0, os.environ['TEST_PROJECT']+'/shared')
    spec=importlib.util.spec_from_file_location('agent',os.environ['TEST_PROJECT']+'/device/installer/android-device.py')
    agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
    with (root / 'dev/sda35').open('r+b') as image:
        agent.clear_btrfs_superblocks(image.fileno(),image.seek(0,2))
else:
    assert sys.argv[3:] == []
with Path(os.environ['TEST_LOG']).open('a') as stream:
    stream.write('native guard passed\\n')
''')
        replacements = {
            '/bin/busybox': str(bin / 'bb'), '/usr/sbin/mkfs.ext4': str(bin / 'mkfs'),
            '/usr/sbin/mkfs.btrfs': str(bin / 'mkfs'), '/usr/bin/btrfs': str(bin / 'btrfs'),
            '/proc/filesystems': str(root / 'filesystems'),
            '/usr/bin/tar': str(bin / 'tar'),
            '/usr/sbin/getcap': str(bin / 'getcap'),
            '/proc/sys/kernel/random/boot_id': str(root / 'boot_id'),
            '/proc/self/mountinfo': str(root / 'mountinfo'), '/proc/meminfo': str(root / 'meminfo'),
            '/usr/lib/liuqin/': str(root / 'usr/lib/liuqin') + '/',
            '/sys/class/': str(root / 'sys/class') + '/', '/dev/': str(root / 'dev') + '/',
            '/etc/': str(root / 'etc') + '/', '/mnt/': str(root / 'mnt') + '/',
            '/run/persist': str(persist),
            '/run/': str(root / 'run') + '/',
        }
        if native:
            replacements['/usr/bin/python3'] = sys.executable
        script = source
        for old, new in replacements.items():
            script = re.sub(r'(?<![A-Za-z0-9_./-])' + re.escape(old), lambda unused: new, script)
        # The fixture consists exclusively of regular image files.
        script = script.replace('[ -b ', '[ -f ')
        script_path = root / 'install-root.sh'
        script_path.write_text(script)
        env = dict(os.environ, PATH=str(bin) + ':' + search_path,
                   TEST_LOG=str(log), TEST_ROOT=str(root), TEST_BUSYBOX=busybox,
                   TEST_PROJECT=str(project), TEST_NATIVE_BTRFS='1' if filesystem=='btrfs' else '0',
                   TEST_ARCHIVE=str(archive), TEST_MKFS=shutil.which('mkfs.btrfs',path=search_path) if filesystem=='btrfs' else mkfs,
                   TEST_TAR=host_tar,
                   TEST_PROVISION_FAIL='1' if provision_failure else '0')
        if preformatted:
            assert bound and native and filesystem=='btrfs'
            subprocess.run([env['TEST_MKFS'],'-f','-K','-U',target['root_fsuuid'],'-s','4096',str(root/'dev/sda35')],
                           check=True,capture_output=True)
        result = subprocess.run([busybox, 'sh', str(script_path),
                                 (root / 'boot_id').read_text().strip(), 'fixture-url',
                                 archive_hash, str(archive.stat().st_size), 'ERASE-LIUQIN-USERDATA',
                                 target_name if bound else 'userdata' if single else 'linux_root',
                                 '-' if single else 'linux_home', *root_options, 'FILESYSTEM', filesystem, *options],
                                env=env, capture_output=True, text=True)
        commands = log.read_text().splitlines()
        if options or provision_failure or contract_failure or missing_contract or not single or not root_support or not installer_support or wrong_partuuid or wrong_range:
            assert result.returncode != 0, result.stdout
            if options or missing_contract or contract_failure in ('hash', 'syntax') or not single or not root_support or not installer_support or wrong_partuuid or wrong_range:
                assert not any(c.startswith('mkfs ') for c in commands), commands
            for state in (root / 'state').iterdir():
                assert state.read_text().strip() == '1', (state, commands)
            return
        assert result.returncode == 0, result.stderr + '\n' + result.stdout
        assert 'ROOT_INSTALLED' in result.stdout
        assert any(c.startswith('getcap ') for c in commands) == snap
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
        if filesystem=='btrfs':
            assert '-K' in formats[0].split(), 'whole-device TRIM must not block installation'
            metadata_reads = [c for c in commands if c.startswith('tar -xOf ')]
            assert metadata_reads and all('--occurrence=1' in c.split() for c in metadata_reads), commands
            first_write = next(i for i,c in enumerate(commands) if c.startswith('bb blockdev --setrw '))
            assert all(i < first_write for i,c in enumerate(commands) if c.startswith('tar -xOf ')), commands
            with formatted.open('rb') as stream:
                stream.seek(65600);assert stream.read(8)==b'_BHRfS_M'
                stream.seek(65672);assert stream.read(8)==b'\x01'+b'\0'*7
            observed=subprocess.check_output(['blkid','-p','-s','LABEL','-o','value',str(formatted)],text=True).strip()
            assert any('mount -t btrfs -o subvolid=5,noatime,compress=zstd:3' in c for c in commands),commands
            assert any(c.startswith('btrfs subvolume create ') for c in commands),commands
            assert (system/'etc/liuqin-root-filesystem').read_text()=='btrfs\n'
        else:
            observed = subprocess.check_output([label, str(formatted)], text=True).strip()
        assert observed == ('LQ_111111111111' if bound else 'LIUQIN_ROOT'), observed
        if bound:
            assert (system/'etc/liuqin-system-id').read_text().strip() == target['os_id']
            assert (system/'etc/liuqin-boot-target').read_bytes() == encode(target)
        if native:
            persist_mounts = [i for i, command in enumerate(commands)
                              if command.startswith('mount -t ext4 -o ro,noload ') and command.endswith(str(persist))]
            persist_unmounts = [i for i, command in enumerate(commands) if command == 'umount '+str(persist)]
            guard = commands.index('native guard passed')
            first_write = next(i for i, command in enumerate(commands) if command.startswith('bb blockdev --setrw '))
            assert len(persist_mounts) == len(persist_unmounts) == 2, commands
            assert persist_mounts[0] < persist_unmounts[0] < guard < persist_mounts[1] < first_write < persist_unmounts[1], commands
            assert not (root / 'persist-mounted').exists()

        for state in (root / 'state').iterdir():
            assert state.read_text().strip() == '1', (state, commands)


execute(True)
print('PASS: single root formats only userdata, retains home inside root and omits home fstab')
execute(True,filesystem='btrfs')
print('PASS: real Btrfs formatting on a disposable image, compression, root subvolume and cleanup')
execute(True,filesystem='btrfs',archive_dot=True)
print('PASS: Btrfs metadata reads stop early for both published ./ paths and custom archive paths')
execute(True,filesystem='btrfs',root_support=False)
execute(True,filesystem='btrfs',installer_support=False)
print('PASS: missing target-root or installer Btrfs support is rejected before formatting')
execute(True,filesystem='btrfs',provision_failure=True)
print('PASS: Btrfs installation failures reseal userdata and the parent disk')
execute(True, snap=False)
print('PASS: a root without Snap installs without requiring snap-confine capabilities')
execute(False)
print('PASS: unsupported split root is rejected before any formatting')
execute(True, provision_failure=True)
print('PASS: a single-root failure reseals root and parent without a home device')
execute(True, options=('KEEP-HOME',))
print('PASS: single-root KEEP-HOME is refused before any formatting')
execute(True, options=('ENABLE-USB-RESCUE',))
print('PASS: retired desktop USB rescue option is refused before any formatting')
execute(True, independent=True)
print('PASS: an independent RAM image installs using only the host-supplied target boot contract')
for failure in ('hash', 'syntax'):
    execute(True, independent=True, contract_failure=failure)
print('PASS: a corrupt or unsupported target contract is refused before formatting')
execute(True, independent=True, missing_contract=True)
print('PASS: the independent image refuses a missing target contract before formatting')
execute(True, independent=True, contract_failure='root')
print('PASS: extraction still verifies the target boot contract and reseals storage on a mismatch')
execute(True, updatable=True)
print('PASS: installer verifies a versioned root and its complete module set')
execute(True, updatable=True, contract_failure='root')
print('PASS: installer refuses a mismatched versioned module identity and reseals storage')

execute(True, bound=True)
execute(True, bound=True, filesystem='btrfs')
execute(True, bound=True, wrong_partuuid=True)
execute(True, bound=True, wrong_range=True)
print('PASS: bound ext4/Btrfs roots use the selected UUID and reject wrong partitions before formatting')
execute(True, bound=True, native=True)
print('PASS: native root guard checks all storage sealed between read-only calibration mounts')
execute(True, bound=True, native=True, filesystem='btrfs', preformatted=True)
print('PASS: native Btrfs retry clears old superblocks after authorization and before formatting')
