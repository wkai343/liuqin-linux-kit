#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise BCB cleanup on regular files, including partial writes and pending requests."""
import base64
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'shared'))
spec = importlib.util.spec_from_file_location('native_bcb', PROJECT/'device/recovery/native-bcb.py')
bcb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcb)


def request(recovery=b'', command=b'boot-recovery'):
    data = bytearray(65536)
    data[:32] = command.ljust(32, b'\0')
    data[64:64+len(recovery)] = recovery
    # Include nonzero A/B/vendor bytes in the same sector as the BCB.
    data[2048:32768] = bytes(range(256))*120
    data[32768:32775] = bytes.fromhex('02b00a74560000')
    data[32832:] = bytes(range(128))*255 + bytes(64)
    return bytes(data)


class FileHardware:
    def __init__(self, root):
        self.root = root
        self.serial = 'TEST'
        self.path = root/'misc'
        self.path.write_bytes(request())
        self.parent = root/'parent'
        self.parent.write_bytes(b'parent disk')
        self.sibling = root/'userdata'
        self.sibling.write_bytes(b'other partition')
        self.ro_state = {'sda':1, 'sda1':1, 'sda2':1, 'sda3':1}
        self.nodes = {'sda':self.parent, 'sda1':self.path, 'sda2':self.sibling, 'sda3':self.sibling}
        self.fds = {}
        self.writes = 0
        self.opens_writable = 0
        self.failure = None
        self.store = None
        self.parts = dict(misc=dict(name='misc', node='sda1',dev='8:1',parent='sda',parent_dev='8:0',
            partuuid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',start=4096,bytes=65536),
            userdata=dict(name='userdata',node='sda2',dev='8:2',parent='sda',parent_dev='8:0',
            partuuid='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',start=69632,bytes=4096))

    def inventory(self):
        return copy.deepcopy(self.parts)

    def sealed(self, parts):
        if any(self.ro_state[node] != 1 for node in self.ro_state):
            raise RuntimeError('unsealed test device')

    def read(self, part, offset, length):
        return self.path.read_bytes()[offset:offset+length]

    @contextlib.contextmanager
    def node(self, node, dev, writable=False):
        if writable:
            self.opens_writable += 1
            assert not self.ro_state['sda'] and not self.ro_state[node]
        fd = os.open(self.nodes[node], os.O_RDWR if writable else os.O_RDONLY)
        self.fds[fd] = node
        try:
            yield fd
        finally:
            del self.fds[fd]
            os.close(fd)

    def write(self, fd, offset, data):
        self.writes += 1
        failure, self.failure = self.failure, None
        if failure == 'partial':
            os.pwrite(fd, data[:50], offset)
            raise OSError('simulated partial write')
        os.pwrite(fd, data, offset)
        os.fsync(fd)
        if failure == 'corrupt':
            os.pwrite(fd, b'?', offset+100)
        if failure == 'interrupt':
            raise InterruptedError('simulated disconnect')

    @contextlib.contextmanager
    def mount_store(self, part):
        assert self.store is not None
        yield self.store

    def agent(self):
        return SimpleNamespace(ro=lambda fd:self.ro_state[self.fds[fd]],
            set_ro=lambda fd, value:self.ro_state.__setitem__(self.fds[fd], value),
            read_json=lambda path:json.loads(path.read_text()))


class BcbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'run').mkdir()
        self.hardware = FileHardware(self.root)
        self.value = dict(boot_id='12345678-1234-4234-8234-123456789abc',serial='TEST',build_id='a'*64)
        self.addCleanup(patch.stopall)
        patch.object(bcb.os, 'sync').start()

    def capture(self):
        return bcb.capture(self.hardware, self.value, self.root)

    def consume(self):
        return bcb.consume(self.hardware, self.hardware.agent(), self.value, self.root)

    def store(self, complete=False, kind='android'):
        directory = self.root/'store'
        directory.mkdir()
        (directory/'registry.json').write_text('{}')
        transaction = directory/('transactions/'+kind+'-11111111')
        transaction.mkdir(parents=True)
        if complete:
            (transaction/'complete.json').write_text('{}')
        self.hardware.store = directory
        part = copy.deepcopy(self.hardware.parts['userdata'])
        part.update(name='linux_boot',node='sda3',dev='8:3')
        self.hardware.parts['linux_boot'] = part
        return transaction

    def test_capture_never_writes_blocks_and_retains_the_original_snapshot(self):
        original = self.hardware.path.read_bytes()
        first = self.capture()
        self.assertEqual(self.hardware.path.read_bytes(), original)
        self.hardware.path.write_bytes(request(b'recovery\n--wipe_data\n'))
        self.assertEqual(self.capture(), first)
        self.assertEqual(self.hardware.opens_writable, 0)
        self.assertEqual((self.root/bcb.DIRECTORY/'snapshot.json').stat().st_mode & 0o777, 0o600)

    def test_unchanged_bare_request_clears_only_command_and_is_idempotent(self):
        self.hardware.path.write_bytes(request())
        # A previous longer C string may leave bytes after the terminator.
        with self.hardware.path.open('r+b') as stream:
            stream.seek(14)
            stream.write(b'oader')
        original = self.hardware.path.read_bytes()
        self.capture()
        result = self.consume()
        self.assertEqual(result['status'], 'cleared')
        self.assertEqual(self.hardware.path.read_bytes(), bytes(32)+original[32:])
        self.assertEqual((self.root/bcb.DIRECTORY/'misc-before.img').read_bytes(), original)
        self.assertEqual(set(self.hardware.ro_state.values()), {1})
        self.assertEqual(self.consume()['status'], 'empty')
        self.assertEqual(self.hardware.writes, 1)

    def test_argument_free_recovery_header_is_supported(self):
        self.hardware.path.write_bytes(request(b'recovery\n'))
        self.capture()
        self.assertEqual(self.consume()['status'], 'cleared')

    def test_ota_wipe_stage_unknown_command_and_virtual_ab_work_are_preserved(self):
        variants = [request(b'recovery\n--update_package=/data/ota.zip\n'),
                    request(b'recovery\n--wipe_data\n'), request(command=b'bootonce-bootloader')]
        for offset in (32,832,864):
            data = bytearray(request())
            data[offset] = 1
            variants.append(bytes(data))
        for status in (2,3,4,255):
            data = bytearray(request())
            data[32773] = status
            variants.append(bytes(data))
        for data in variants:
            with self.subTest(kind=bcb.request_kind(data)):
                self.hardware.path.write_bytes(data)
                (self.root/bcb.DIRECTORY/'snapshot.json').unlink(missing_ok=True)
                self.capture()
                self.assertEqual(self.consume()['status'], 'preserved')
                self.assertEqual(self.hardware.path.read_bytes(), data)
        self.assertEqual(self.hardware.opens_writable, 0)

    def test_request_arriving_after_startup_is_not_consumed(self):
        self.hardware.path.write_bytes(request(command=b''))
        self.capture()
        self.hardware.path.write_bytes(request())
        self.assertEqual(self.consume()['reason'], 'request-changed')
        self.assertEqual(self.hardware.opens_writable, 0)

    def test_missing_snapshot_wrong_boot_and_changed_partition_stop_cleanup(self):
        self.assertEqual(self.consume()['reason'], 'missing-startup-snapshot')
        self.capture()
        value = dict(self.value, boot_id='different')
        with self.assertRaisesRegex(RuntimeError, '其他启动'):
            bcb.consume(self.hardware,self.hardware.agent(),value,self.root)
        self.hardware.parts['misc']['partuuid'] = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        self.assertEqual(self.consume()['reason'], 'request-changed')
        self.assertEqual(self.hardware.opens_writable, 0)

    def test_partial_write_corruption_and_disconnect_restore_the_original_and_reseal(self):
        for failure in ('partial','corrupt','interrupt'):
            with self.subTest(failure=failure):
                self.hardware.path.write_bytes(request())
                self.capture()
                original = self.hardware.path.read_bytes()
                self.hardware.failure = failure
                with self.assertRaises((OSError,RuntimeError,InterruptedError)):
                    self.consume()
                self.assertEqual(self.hardware.path.read_bytes(), original)
                self.assertEqual(set(self.hardware.ro_state.values()), {1})
                receipt = json.loads((self.root/bcb.DIRECTORY/'receipt.json').read_text())
                self.assertTrue(receipt['rollback_verified'])

    def test_unfinished_native_or_persisted_install_preserves_request(self):
        self.capture()
        path = self.root/'run/liuqin-native-install-session.json'
        for state in ('active','failed'):
            path.write_text(json.dumps(dict(state=state)))
            self.assertEqual(self.consume()['reason'], 'pending-install-session')
        path.unlink()
        transaction = self.store()
        self.assertEqual(self.consume()['reason'], 'pending-install-transaction')
        self.assertEqual(self.hardware.opens_writable, 0)
        (transaction/'complete.json').write_text('{}')
        self.assertEqual(self.consume()['status'], 'cleared')

    def test_snapshot_corruption_and_failed_backup_never_unlock_a_block_device(self):
        self.capture()
        path = self.root/bcb.DIRECTORY/'snapshot.json'
        record = json.loads(path.read_text())
        record['bcb_sha256'] = '0'*64
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, '快照损坏'):
            self.consume()
        path.unlink()
        self.capture()
        with patch.object(bcb, 'save', side_effect=OSError('RAM full')), self.assertRaises(OSError):
            self.consume()
        self.assertEqual(self.hardware.opens_writable, 0)
        self.assertEqual(set(self.hardware.ro_state.values()), {1})

    def test_historical_linux_records_do_not_own_an_android_recovery_request(self):
        self.capture()
        self.store(kind='linux')
        self.assertEqual(self.consume()['status'], 'cleared')


if __name__ == '__main__':
    unittest.main(verbosity=2)
