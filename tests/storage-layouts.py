#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Capacity and confirmation regressions; all tablet operations are mocked."""
import copy
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', project / 'installer/install-liuqin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
L = installer.layout
sys.path.insert(0, str(project / 'installer'))
from liuqin_gui.demo import DemoEngine


def make_bundle(root):
    demo = DemoEngine(lambda event: None, root / 'demo-state')
    bundle = root / 'bundle'
    try:
        shutil.copytree(demo.demo_bundle, bundle)
    finally:
        demo.close()
    return bundle


def stock(capacity, offset=0, userdata_number=35):
    disk = copy.deepcopy(L.reference_stock_disk())
    disk.total_sectors = disk.total_sectors * capacity // 256
    disk.last_usable = disk.total_sectors - 6
    userdata = disk.by_name('userdata')
    userdata.first_sector += offset
    userdata.last_sector = disk.last_usable
    userdata.number = userdata_number
    return disk


def args(mode, **changes):
    result = dict(layout=mode, android_size=None, root_size=None, keep_home=False)
    result.update(changes)
    return SimpleNamespace(**result)


def refuses(call, fragment):
    try:
        call()
    except (RuntimeError, L.LayoutError) as error:
        assert fragment in str(error), error
    else:
        raise AssertionError('expected a refusal: ' + fragment)


for capacity in (128, 256, 512):
    disk = stock(capacity, offset=512, userdata_number=63)
    for requested in (args('linux-only'), args('linux-only', root_size='75%'),
                      args('dual', android_size='32G', root_size='50%'), args('linux-single')):
        plan = installer.plan_for_disk(requested, disk)
        installer.confirm_plan(plan, installer.plan_for_disk(requested, copy.deepcopy(disk)))
        if requested.layout == 'linux-single':
            assert len(plan.entries) == 1 and plan.entry('userdata').action == 'format'
            assert plan.entry('userdata').start == disk.by_name('userdata').start
            assert plan.entry('userdata').end == disk.usable_end
            assert L.home_partition_name(requested.layout) is None
            refuses(lambda: L.sgdisk_arguments(plan, '/never-a-device', {}), 'does not modify')
        else:
            assert plan.entry('linux_root').start % L.ALIGNMENT == 0
            assert plan.entry('linux_home').end == disk.usable_end
    print(f'PASS: {capacity} GB, changed userdata offset/number, absolute and percentage layouts')

refuses(lambda: installer.plan_for_disk(args('dual'), stock(128)), 'linux_home')
refuses(lambda: installer.plan_for_disk(args('linux-single', keep_home=True), stock(128)), 'no separate')
damaged = stock(128)
damaged.partitions[0].number = 36
plan = installer.plan_for_disk(args('linux-only'), damaged)
assert plan.entry('linux_root').number == 37 and plan.entry('linux_home').number == 38
assert all(e.number != 36 for e in plan.entries if e.action == 'create')
print('PASS: new Linux entries never replace occupied stock partition numbers')

for mutation in ('size', 'prefix', 'name'):
    disk = stock(128)
    plan = installer.plan_for_disk(args('linux-single'), disk)
    altered = copy.deepcopy(disk)
    if mutation == 'size':
        altered.total_sectors -= 1024
        altered.last_usable -= 1024
        altered.by_name('userdata').last_sector -= 1024
    elif mutation == 'prefix':
        altered.partitions[0].number = 36
    else:
        altered.partitions[0].name = 'changed-prefix'
    refuses(lambda: installer.confirm_plan(plan, installer.plan_for_disk(args('linux-single'), altered)),
            'changed after confirmation')
ambiguous = stock(128)
ambiguous.partitions[0].number = ambiguous.partitions[1].number
refuses(lambda: installer.plan_for_disk(args('linux-single'), ambiguous), 'duplicate')
overlap = stock(128)
overlap.by_name('userdata').first_sector = overlap.partitions[-2].last_sector
refuses(lambda: installer.plan_for_disk(args('linux-only'), overlap), 'overlapping')
print('PASS: disk changes, duplicate entries and overlapping partitions are refused')


def report(disk):
    lines = [f'Disk /dev/sda: {disk.total_sectors} sectors',
             f'Sector size (logical/physical): {disk.sector_size}/{disk.sector_size} bytes',
             f'First usable sector is {disk.first_usable}, last usable sector is {disk.last_usable}',
             f'Partition table holds up to {disk.entry_count} entries',
             'Number  Start (sector) End (sector) Size Code Name']
    lines += [f'{p.number} {p.first_sector} {p.last_sector} {p.size / L.GIB:.3f} GiB 8300 {p.name}'
              for p in disk.partitions]
    return '\n'.join(lines) + '\n'


class Server:
    server_port = 8000

    def __init__(self, *unused): pass
    def serve_forever(self): pass
    def shutdown(self): pass
    def server_close(self): pass


def run_host(mode, *, capacity=128, answer='YES', change_after_confirmation=False,
             options=(), metadata=True, corrupt_boot=False):
    """Run main() past the real planning boundary with a synthetic RAM endpoint."""
    operations = []
    boot_id = '11111111-1111-4111-8111-111111111111'
    disk = stock(capacity)
    reports = 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bundle = make_bundle(root)
        interface = installer.load_artifacts().inspect_package(bundle)
        hashes = {name: installer.sha(bundle / name)
                  for name in ('boot.img', 'installer.img', 'rootfs.tar.gz')}
        if not metadata:
            (bundle / 'bundle.json').unlink(missing_ok=True)
        if corrupt_boot:
            (bundle / 'boot.img').write_bytes(b'not a boot image')

        def fastboot(argv, **unused):
            if argv[1:] == ['devices']:
                return SimpleNamespace(stdout='TEST_SERIAL\tfastboot\n')
            operations.append(('fastboot', argv[3:]))
            values = {'product': 'liuqin', 'unlocked': 'yes',
                      'partition-size:userdata': hex(disk.by_name('userdata').size),
                      'partition-size:boot_a': hex(201326592),
                      'partition-size:boot_b': hex(201326592),
                      'max-download-size': '0x30000000'}
            if argv[3] == 'getvar':
                return SimpleNamespace(stdout=argv[-1] + ': ' + values[argv[-1]])
            assert argv[3] == 'boot', argv
            return SimpleNamespace(stdout='')

        def command(address, text, timeout=60):
            nonlocal reports
            if text.endswith('cat /proc/sys/kernel/random/boot_id'):
                return boot_id.encode()
            statement = text.split(' && ', 1)[-1]
            if statement == 'cat /proc/cmdline':
                return b'androidboot.serialno=TEST_SERIAL'
            if statement == 'uname -r':
                return interface['installer_kernel_release'].encode()
            if statement == 'cat /etc/liuqin-installer-interface':
                return interface['installer_interface'].encode()
            if statement.startswith('/bin/busybox base64 -d '):
                return b''
            if 'install-layout.sh' in statement:
                assert ' report persist' in statement, statement
                reports += 1
                current = copy.deepcopy(disk)
                if reports > 1 and change_after_confirmation:
                    current.partitions[0].number = 36
                return report(current).encode()
            if 'install-root.sh' in statement:
                operations.append(('install-root', shlex.split(statement)))
                return b'liuqin-install: ROOT_INSTALLED\n'
            assert 'liuqin-reboot bootloader' in statement, statement
            return b''

        def backup(*unused):
            operations.append(('backup',))
            return '0' * 64

        def padded(*unused):
            return bundle / 'boot.img', hashes['boot.img']

        def input_answer(*unused):
            operations.append(('confirm',))
            assert reports == 1, 'confirmation must follow actual-table discovery'
            return answer

        argv = ['--bundle', str(bundle), '--layout', mode, '--serial', 'TEST_SERIAL',
                '--backup', str(root / 'backup'), '--erase-userdata', '--allow-unverified',
                '--host-address', '192.168.7.1', *options]
        output = io.StringIO()
        error = None
        with patch.object(installer.host, 'fastboot_path', return_value='fastboot-offline'), \
             patch.object(installer.subprocess, 'run', side_effect=fastboot), \
             patch.object(installer.subprocess, 'check_output', return_value='TEST_SERIAL\tfastboot\n'), \
             patch.object(installer, 'command', side_effect=command), \
             patch.object(installer, 'backup_partition', side_effect=backup), \
             patch.object(installer, 'backup_partition_table', return_value=root / 'backup/gpt'), \
             patch.object(installer, 'pad_boot_image', side_effect=padded), \
             patch.object(installer, 'stage_blob', side_effect=lambda *a: operations.append(('stage-contract',))), \
             patch.object(installer.http.server, 'ThreadingHTTPServer', Server), \
             patch.object(installer, 'reference_plan', side_effect=AssertionError('reference used for real install')), \
             patch.object(installer, 'apply_layout', side_effect=lambda *a: operations.append(('apply-layout',))), \
             patch.object(installer, 'finish_in_fastboot', side_effect=lambda *a: operations.append(('finish', a[1]))), \
             patch.object(installer.sys, 'stdin', SimpleNamespace(isatty=lambda: True)), \
             patch('builtins.input', side_effect=input_answer), redirect_stdout(output):
            try:
                installer.main(argv)
            except (SystemExit, RuntimeError, ValueError) as caught:
                error = caught
        return operations, output.getvalue(), error


for capacity in (128, 256, 512):
    for filesystem in ('ext4', 'btrfs'):
        operations, output, error = run_host('linux-single', capacity=capacity,
                                             options=('--filesystem', filesystem))
        assert error is None, error
        assert operations[-1] == ('finish', 'linux-single'), operations
        assert installer.boot_slots('linux-single') == ('a',)
        assert operations.index(('backup',)) < operations.index(('confirm',))
        command = next(op[1] for op in operations if op[0] == 'install-root')
        assert command[7:9] == ['userdata', '-']
        assert 'ROOT-CONTRACT' in command
        assert ('FILESYSTEM' in command) == (filesystem == 'btrfs')
        if filesystem == 'btrfs':
            assert command[command.index('FILESYSTEM') + 1] == 'btrfs'
        assert not any(op[0] == 'apply-layout' for op in operations)
        assert 'Actual tablet layout' in output and '256 GB reference' not in output
        print(f'PASS: actual {capacity} GB single-root CLI flow: {filesystem}')

operations, output, error = run_host('linux-single', metadata=False)
assert error is None and operations[-1] == ('finish', 'linux-single'), error
print('PASS: optional bundle.json is not required for installation')

for mode in ('linux-only', 'dual'):
    operations, output, error = run_host(mode)
    assert error is not None and not operations
print('PASS: unsupported split installation modes are refused before device access')

for settings in ({'answer': 'no'}, {'change_after_confirmation': True}, {'corrupt_boot': True}):
    operations, output, error = run_host('linux-single', **settings)
    assert error is not None
    assert not any(op[0] in ('apply-layout', 'install-root', 'finish') for op in operations), operations
print('PASS: cancellation, a changed GPT and a malformed image never reach destructive commands')

with tempfile.TemporaryDirectory() as temporary:
    bundle = make_bundle(Path(temporary))
    for option in ('--keep-home', '--root-size', '--android-size'):
        argv = ['--bundle', str(bundle), '--check', '--layout', 'linux-single', option]
        if option != '--keep-home':
            argv += ['0' if option == '--android-size' else '100%']
        with patch.object(installer.subprocess, 'run', side_effect=AssertionError('device accessed')):
            try:
                installer.main(argv)
            except SystemExit as error:
                assert error.code != 0
            else:
                raise AssertionError('invalid single-root option was accepted')
print('PASS: single root rejects keep-home and partition sizing before device access')
