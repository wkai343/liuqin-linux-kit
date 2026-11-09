#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline tests: no Fastboot, tablet connection or block-device writes."""
import hashlib
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', project / 'installer/install-liuqin.py')
installer_source = (project / 'installer/install-liuqin.py').read_text()
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

sys.path.insert(0,str(project/'installer'))
from liuqin_gui.demo import DemoEngine

def make_bundle(root):
    demo = DemoEngine(lambda event: None, root/'demo-state')
    files = {}
    for name in ('boot.img','installer.img','rootfs.tar.gz'):
        data=(demo.demo_bundle/name).read_bytes();(root/name).write_bytes(data)
        files[name]=hashlib.sha256(data).hexdigest()
    demo.close()
    return files


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = make_bundle(root)
    demo_boot = (root/'boot.img').read_bytes()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files}))
    args = ['python3', str(project / 'installer/install-liuqin.py'), '--bundle', str(root), '--check']
    subprocess.run(args, check=True)
    obsolete=subprocess.run(args + ['--enable-rescue'],capture_output=True)
    assert obsolete.returncode!=0 and b'unrecognized arguments: --enable-rescue' in obsolete.stderr
    (root / 'boot.img').write_bytes(b'corrupted')
    assert subprocess.run(args, capture_output=True).returncode != 0
    (root / 'boot.img').write_bytes(demo_boot)
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                               'status': 'OFFLINE_ASSEMBLED'}))
    class StopFlow(Exception):
        """Not an OSError/RuntimeError, so the RAM-channel retry loop lets it through."""

    pinned_boot_bytes = json.loads((project / 'shared/liuqin-rom-images.json').read_text()
                                   )['images']['boot.img']['bytes']

    def run_install(argv_extra, reported, stdin_tty=False, answer=None, boot_size=hex(pinned_boot_bytes),
                    max_download='0x30000000'):
        calls = []

        def fastboot(command, **kwargs):
            calls.append(command)
            assert Path(command[0]).name in ('fastboot','fastboot.exe')
            assert command[1:3] == ['-s', 'TEST_SERIAL']
            if command[3] == 'boot':
                return SimpleNamespace(stdout='booting\n')
            name = command[-1]
            values = {'product': 'liuqin', 'unlocked': 'yes', 'current-slot': 'a',
                      'partition-size:userdata': reported, 'partition-size:boot_a': boot_size,
                      'partition-size:boot_b': boot_size, 'max-download-size': max_download}
            return SimpleNamespace(stdout=name + ': ' + values[name] + '\n')

        stdin = SimpleNamespace(isatty=lambda: stdin_tty)
        with patch.object(installer.sys, 'argv', ['install.py', '--bundle', str(root),
                          '--serial', 'TEST_SERIAL', '--backup', str(root.parent / 'unused-backup'),
                          '--erase-userdata', '--allow-unverified', '--layout', 'linux-single',
                          *argv_extra]), \
             patch.object(installer.subprocess, 'run', side_effect=fastboot), \
             patch.object(installer.sys, 'stdin', stdin), \
             patch('builtins.input', lambda *a: answer), \
             patch.object(installer, 'command', side_effect=StopFlow('stop after boot')):
            try:
                installer.main()
            except (SystemExit, RuntimeError, StopFlow):
                pass
            else:
                raise AssertionError('installation unexpectedly completed: ' + reported)
        return calls

    for reported in ('0x100000', hex(8 * 1024**3)):
        calls = run_install([], reported)
        assert calls[-1][-1] == 'partition-size:userdata', (reported, calls)
    print('PASS: undersized userdata layouts are rejected before RAM boot')

    # If fastboot cannot report userdata, its existence is decided by the partition
    # table itself, read in the RAM installer, not by this pre-boot probe.
    calls = run_install(['--yes'], 'unknown')
    assert calls[-1][3] == 'boot', calls
    print('PASS: a tablet without userdata is referred to the on-device table check')

    for reported in (hex(16 * 1024**3), hex(471789528 * 512)):
        calls = run_install(['--yes'], reported)
        assert calls[-1][3] == 'boot', (reported, calls)
    print('PASS: userdata layouts of 16 GiB and above are admitted')

    calls = run_install([], hex(471789528 * 512), stdin_tty=False)
    assert not any(call[3:4] == ['boot'] for call in calls)
    calls = run_install([], hex(471789528 * 512), stdin_tty=True, answer='YES')
    assert calls[-1][3] == 'boot'
    print('PASS: an interactive session or --yes is required before RAM boot; '
          'erasure confirmation waits for the actual partition table')

    # Only boot_a is a write target for a single-root installation.
    calls = run_install(['--yes'], hex(16 * 1024**3))
    assert ['partition-size:boot_a'] == [c[-1] for c in calls if c[-1].startswith('partition-size:boot')], calls
    print('PASS: the single-root layout checks boot_a only')

    # The project image is flashed zero-filled to the whole partition, so a
    # boot partition of another size, or a bootloader that cannot take the
    # whole partition in one raw download, stops before the RAM installer.
    for boot_size, max_download in ((hex(pinned_boot_bytes + 4096), '0x30000000'),
                                    (hex(pinned_boot_bytes - 4096), '0x30000000'),
                                    (hex(pinned_boot_bytes), hex(pinned_boot_bytes - 1))):
        calls = run_install(['--yes'], hex(16 * 1024**3), boot_size=boot_size, max_download=max_download)
        assert not any(call[3:4] == ['boot'] for call in calls), (boot_size, max_download, calls)
    calls = run_install(['--yes'], hex(16 * 1024**3), max_download=str(pinned_boot_bytes))
    assert calls[-1][3] == 'boot', calls
    print('PASS: boot partitions must be the pinned size and fit one fastboot download')

# A local fake shell supplies a CRLF transcript containing the echoed command.
# Only complete marker lines may finish the transaction, not the echo itself.
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
address = listener.getsockname()

def shell():
    with listener.accept()[0] as connection:
        received = b''
        while not received.endswith(b'\n'):
            received += connection.recv(4096)
        token = re.search(rb'LIUQIN_[a-f0-9]+', received)[0]
        connection.sendall(received.replace(b'\n', b'\r\n'))
        connection.sendall(b'\r\n' + token + b'_START\r\nvalue\r\n' + token + b'_END 0\r\n')

thread = threading.Thread(target=shell)
thread.start()
connect = socket.create_connection
with patch.object(installer.socket, 'create_connection', side_effect=lambda *a, **k: connect(address, **k)):
    assert installer.command('unused', 'printf value') == b'value'
thread.join()
listener.close()
assert subprocess.run(['sh', str(project / 'device/installer/install-root.sh')], capture_output=True).returncode != 0
invalid = subprocess.run(['sh', str(project / 'device/installer/install-root.sh'),
                          'unused', 'unused', 'unused', 'unused', 'ERASE-LIUQIN-USERDATA',
                          'userdata', '-', 'INVALID'], capture_output=True)
assert invalid.returncode != 0 and b'unsupported option: INVALID' in invalid.stderr, invalid.stderr
unauthorized = subprocess.run(['sh', str(project / 'device/installer/install-root.sh'),
                               'unused', 'unused', 'unused', 'unused', 'NO',
                               'userdata', '-'], capture_output=True)
assert unauthorized.returncode != 0 and b'data-erasure acknowledgement' in unauthorized.stderr
for arguments in ([], ['id', 'report'], ['id', 'nonsense', 'x']):
    refused = subprocess.run(['sh', str(project / 'device/installer/install-layout.sh'), *arguments],
                             capture_output=True)
    assert refused.returncode != 0, arguments
print('PASS: checksum rejection, local-only check, CRLF command framing and missing-authorization refusal')

# Argument combinations that must never reach a device.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = make_bundle(root)
    demo_boot = (root/'boot.img').read_bytes()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                                  'status': 'OFFLINE_ASSEMBLED'}))
    base = ['python3', str(project / 'installer/install-liuqin.py'), '--bundle', str(root),
            '--serial', 'TEST_SERIAL', '--backup', str(root.parent / 'unused-backup'),
            '--erase-userdata', '--allow-unverified', '--yes']
    for extra, fragment in (
            ([], b'--layout'),
            (['--layout', 'dual'], b'invalid choice'),
            (['--layout', 'linux-only'], b'invalid choice'),
            (['--layout', 'linux-single', '--restore-partition-table', str(root)], b'separate action')):
        refused = subprocess.run(base + extra, capture_output=True)
        assert refused.returncode != 0 and fragment in refused.stderr, (extra, refused.stderr)
print('PASS: layout and restore argument combinations are checked before any device access')

# The installer RAM image has no /tmp, so everything the host writes on the
# tablet goes to the scratch directory the device script reads from -- and the
# host creates that directory before writing into it, because the first
# command it sends may well be this one.
sent = []
installer.stage_blob(lambda text, timeout=60: sent.append(text), 'head', b'liuqin')
assert sent[0] == 'mkdir -p ' + installer.LAYOUT_WORK, sent
staged = installer.LAYOUT_WORK + '/liuqin-gpt-head.b64'
assert sent[1] == ':>' + staged, sent
assert all('/tmp' not in text for text in sent), sent
assert sent[2] == 'printf %s bGl1cWlu >>' + staged, sent
assert sent[3] == 'printf "\\n" >>' + staged, sent
assert installer.LAYOUT_WORK == '/run/liuqin-layout'
# The device script must read the halves back from that same directory.
layout_script = (project / 'device/installer/install-layout.sh').read_text()
assert 'LIUQIN_LAYOUT_WORK:-' + installer.LAYOUT_WORK in layout_script
print('PASS: the staged partition-table halves land in the scratch directory the RAM image has')

# Verify the real padded payload and flash target, not only command construction.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source = root / 'boot.img'
    source.write_bytes(b'linux-image' * 1024)
    size = 65536
    padded, digest = installer.pad_boot_image(source, installer.sha(source), size, root)
    expected = source.read_bytes() + bytes(size - source.stat().st_size)
    assert padded.read_bytes() == expected
    assert digest == hashlib.sha256(expected).hexdigest()
    sent = []
    installer.finish_in_fastboot(lambda *a: sent.append(a), 'linux-single', padded, digest, size)
    assert sent == [('flash', 'boot_a', str(padded)), ('--set-active=a',), ('reboot',)]
    for payload in (b'corrupted' + expected[9:], expected[:-1]):
        padded.write_bytes(payload)
        sent = []
        try:
            installer.finish_in_fastboot(lambda *a: sent.append(a), 'linux-single', padded, digest, size)
        except RuntimeError:
            pass
        else:
            raise AssertionError('modified padded image was accepted')
        assert not sent
print('PASS: complete zero-filled boot payload, slot A only, mutations refused before flash')

with tempfile.TemporaryDirectory() as directory:
    bundle = Path(directory)
    (bundle / 'rootfs.tar.gz').write_bytes(b'root')
    manifest = {'files': {'rootfs.tar.gz': '0' * 64}}
    for filesystem in ('ext4', 'btrfs'):
        args = SimpleNamespace(filesystem=filesystem)
        built = installer.install_root_command('not-this-boot', 'http://fixture', bundle,
            manifest, args, target_contract=('/run/target.contract', '1' * 64))
        assert built[7:9] == ['userdata', '-']
        assert 'ENABLE-USB-RESCUE' not in built
        assert ('FILESYSTEM' in built) == (filesystem == 'btrfs')
        result = subprocess.run(['sh', project / 'device/installer/install-root.sh', *built[2:]], capture_output=True)
        assert result.returncode != 0 and b'RAM boot identity changed' in result.stderr, result.stderr
print('PASS: current host request shapes are parsed before the RAM boot identity guard')

# Every remote command is one RAM shell line, and ash's line-editing buffer
# refuses a line of 1024 bytes or more (newline included; measured).  command()
# refuses such a line before it connects; a line of 1022 bytes plus the
# newline is still sent.
assert installer.RAM_SHELL_LINE_BYTES == 1023
token = 'LIUQIN_' + '0' * 32  # the shape command() draws from uuid4().hex
start, end = token + '_START', token + '_END'
# 'a' needs no shell quoting, so each one adds exactly one byte to the line.
framing = len(installer.frame_command('a', start, end)) - 1
fits = 'a' * (installer.RAM_SHELL_LINE_BYTES - framing)
line = installer.frame_command(fits, start, end)
assert len(line) == 1023 and line.endswith(b'\n') and b'\n' not in line[:-1], len(line)
connections = []

def refuse_connection(*arguments, **keywords):
    connections.append(arguments)
    raise ConnectionRefusedError('test: past the guard')

with patch.object(installer.socket, 'create_connection', side_effect=refuse_connection):
    try:
        installer.command('unused', fits + 'a')
    except RuntimeError as error:
        assert 'RAM shell accepts at most 1023' in str(error) and 'not sent' in str(error), error
    else:
        raise AssertionError('a 1024-byte RAM shell line was not refused')
    assert not connections, 'the over-long line was refused only after connecting'
    try:
        installer.command('unused', fits)
    except ConnectionRefusedError:
        pass
    assert len(connections) == 1, 'a 1023-byte RAM shell line did not reach the connection'
print('PASS: command() refuses a 1024-byte RAM shell line before connecting and sends a 1023-byte one'
      ' (newline included)')

# Real GPT partition dumps (e.g. 24 KiB sda-head.bin) are staged in chunks.
# Each chunk must stay short as text and, wrapped by remote()'s boot_id guard
# and framed by command(), fit one RAM shell line.
sent_large = []
installer.stage_blob(lambda text, timeout=60: sent_large.append(text), 'head', b'\x00' * (24 * 1024))
assert len(sent_large) > 10, "Large blob should be split into multiple chunks"
assert all(len(cmd) < 700 for cmd in sent_large), sent_large
guard_source = 'guard = \'test "$(cat /proc/sys/kernel/random/boot_id)" = \' + shlex.quote(boot_id)'
assert guard_source in installer_source and "guard + ' && ' + text" in installer_source
guard = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + '00000000-0000-0000-0000-000000000000'
framed = [installer.frame_command(guard + ' && ' + text, start, end) for text in sent_large]
assert max(map(len, framed)) <= installer.RAM_SHELL_LINE_BYTES, max(map(len, framed))
print('PASS: large partition blobs are chunked into RAM shell lines of at most '
      + str(max(map(len, framed))) + ' bytes')
