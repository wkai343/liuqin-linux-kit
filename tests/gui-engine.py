#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Behavioral tests; never open USB, a device socket or a block device."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from liuqin_gui.demo import DemoEngine, DemoTransport, demo_report
from liuqin_gui.engine import Cancelled, Engine, InstallerError, Transport, cli
from liuqin_gui import backups


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.events = []
        self.engine = DemoEngine(self.events.append, self.root / 'state')
        self.no_fastboot = patch.object(subprocess, 'run', side_effect=AssertionError('real process forbidden'))
        self.no_network = patch.object(socket, 'socket', side_effect=AssertionError('device network forbidden'))
        self.no_fastboot.start()
        self.no_network.start()
        self.engine.create_backup(dict(serial='DEMO-LIUQIN', bundle='ignored', backup_parent=str(self.root / 'backups')))
        self.backup_path = self.engine.receipt['backup_dir']
        self.capture_transport = self.engine.transport
        self.engine.prepare(dict(serial='DEMO-LIUQIN', bundle='ignored', backup_dir=self.backup_path))

    def tearDown(self):
        self.engine.close()
        self.no_network.stop()
        self.no_fastboot.stop()
        self.directory.cleanup()

    def consent(self):
        return dict(session_id=self.engine.session_id, plan_id=self.engine.plan_id,
                    confirmation=dict(serial='DEMO-LIUQIN', personal_data_backed_up=True,
                                      erase_userdata=True, allow_experimental=True))

    def test_prepare_is_read_only_and_uses_actual_capacity(self):
        self.assertEqual(self.engine.state, 'prepared')
        self.assertNotIn('install_root', self.engine.transport.calls)
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertEqual(self.engine.plan.entries[0].size, self.engine.device['userdata_bytes'])
        self.assertTrue(self.engine.receipt['backup_verified'])
        self.assertTrue(all(e['simulation'] for e in self.events))

    def test_confirmation_cannot_be_forged_or_replayed(self):
        for name in ('session_id', 'plan_id'):
            payload = self.consent()
            payload[name] = 'stale'
            with self.assertRaises(InstallerError):
                self.engine.install(payload)
        for name, value in (('serial', 'OTHER'), ('erase_userdata', False),
                            ('personal_data_backed_up', False), ('erase_userdata', 'true'),
                            ('allow_experimental', False)):
            payload = self.consent()
            payload['confirmation'][name] = value
            with self.assertRaises(InstallerError):
                self.engine.install(payload)
        self.assertEqual(self.engine.state, 'prepared')
        self.assertNotIn('install_root', self.engine.transport.calls)

    def test_disk_change_stops_before_format(self):
        self.engine.transport.report = lambda: demo_report(128)
        with self.assertRaisesRegex(InstallerError, '分区表发生变化') as caught:
            self.engine.install(self.consent())
        self.engine.fail(caught.exception)
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertNotIn('install_root', self.engine.transport.calls)
        self.assertEqual(self.engine.state, 'failed')

    def test_changed_bundle_and_backup_stop_before_format(self):
        path = Path(self.engine.receipt['backup_dir']) / 'persist.img'
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(InstallerError, '备份校验'):
            self.engine.install(self.consent())
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertNotIn('install_root', self.engine.transport.calls)

    def test_cancel_prepared_returns_without_writing(self):
        self.engine.cancel_prepared()
        self.assertEqual(self.engine.state, 'cancelled')
        self.assertIn('cancel_cleanup', self.engine.transport.calls)
        self.assertNotIn('install_root', self.engine.transport.calls)
        self.assertFalse(self.engine.plan_id)

    def test_completion_requires_actual_first_boot_report(self):
        self.engine.install(self.consent())
        self.assertEqual(self.engine.state, 'awaiting_first_boot')
        self.assertTrue(self.engine.receipt['boot_readback_verified'])
        self.assertFalse(self.engine.receipt['first_boot_verified'])
        with self.assertRaises(InstallerError):
            self.engine.install(self.consent())
        with self.assertRaises(InstallerError):
            self.engine.verify_first_boot(dict(session_id='wrong', desktop_and_reboot_verified=True))
        self.engine.verify_first_boot(dict(session_id=self.engine.session_id, desktop_and_reboot_verified=True))
        self.assertTrue(self.engine.receipt['first_boot_verified'])
        self.assertEqual(self.engine.state, 'verified')
        on_disk = json.loads((Path(self.engine.receipt['run_dir']) / 'receipt.json').read_text())
        self.assertTrue(on_disk['first_boot_verified'])

    def test_boot_readback_failure_does_not_reboot_new_system(self):
        transport = Transport(self.engine, 'DEMO-LIUQIN')
        calls = []
        transport.fastboot = lambda *a, **kw: calls.append(a)
        transport.return_fastboot = lambda: None
        transport.check_device = lambda: {}
        transport.boot_ram = lambda: None
        transport.remote = lambda *a, **kw: b'0' * 64 + b'  /dev/boot_a\n'
        with self.assertRaisesRegex(InstallerError, '回读校验失败'):
            transport.flash_verify_reboot(self.engine.padded, self.engine.padded_digest, 8192)
        self.assertEqual(calls, [('flash', 'boot_a', str(self.engine.padded))])
        self.assertTrue(self.engine.receipt['boot_written'])
        self.assertFalse(self.engine.receipt['boot_readback_verified'])
        self.assertFalse(self.engine.receipt['active_slot_set'])

    def test_writing_refuses_cancel(self):
        self.engine.committing = True
        with self.assertRaises(InstallerError):
            self.engine.request_cancel()

    def test_cancel_during_read_only_prepare_can_start_another_session(self):
        self.engine.cancel_event.set()
        self.engine.fail(Cancelled('cancelled'))
        self.assertEqual(self.engine.state, 'cancelled')
        self.assertFalse(self.engine.cancel_event.is_set())
        self.engine.validate_bundle('ignored')

    def test_read_only_request_does_not_erase_failed_write_receipt(self):
        self.engine.receipt['write_dispatched'] = True
        self.engine.committing = True
        self.engine.fail(InstallerError('write stopped'))
        original = dict(self.engine.receipt)
        with self.assertRaises(InstallerError):
            self.engine.prepare(dict(serial='DEMO', bundle='ignored', backup_parent=str(self.root / 'new')))
        self.assertEqual(self.engine.receipt, original)

    def test_log_disk_failure_is_still_reported_to_ui(self):
        with patch.object(self.engine, 'save_receipt', side_effect=OSError('disk full')), \
             patch.object(self.engine, 'emit', side_effect=OSError('disk full')):
            self.engine.fail(InstallerError('operation stopped'))
        self.assertEqual(self.engine.state, 'failed')
        error = next(e for e in reversed(self.events) if e['type'] == 'error')
        self.assertIn('执行记录保存失败', error['message'])

    def test_framed_output_observer_handles_split_markers(self):
        transcript = []
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def settimeout(self, _value): pass
            def sendall(self, line):
                if line.startswith(b'stty'):
                    import re
                    start, end = re.findall(rb'LIUQIN_[0-9a-f]+_(?:START|END)', line)
                    self.chunks = iter([b'noise\n' + start[:15], start[15:] + b'\r\nphase-one\r\n',
                                        b'phase-two\r\n' + end[:10], end[10:] + b' 0\r\n'])
            def recv(self, _size): return next(self.chunks, b'')
        with patch.object(cli.socket, 'create_connection', return_value=Connection()):
            result = cli.command('unused', 'true', on_output=transcript.append)
        self.assertEqual(result, b'phase-one\nphase-two')
        self.assertEqual(b''.join(transcript), b'phase-one\r\nphase-two\r\n')
        self.assertNotIn(b'LIUQIN_', b''.join(transcript))

    def test_bad_archive_is_rejected_before_hardware_access(self):
        engine = Engine(self.events.append, self.root / 'separate',
                        transport_factory=lambda *_: self.fail('hardware accessed'))
        (self.engine.demo_bundle / 'rootfs.tar.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(InstallerError, '文件校验失败'):
            engine.prepare(dict(serial='DEMO', bundle=str(self.engine.demo_bundle),
                                backup_dir=self.backup_path))

    def test_prepare_requires_separate_backup(self):
        self.engine.cancel_prepared()
        with self.assertRaisesRegex(InstallerError, '独立创建设备备份'):
            self.engine.prepare(dict(serial='DEMO-LIUQIN', bundle='ignored'))

    def test_backup_repeat_reuses_without_device_access(self):
        self.engine.cancel_prepared()
        before = {p.name: p.read_bytes() for p in Path(self.backup_path).iterdir() if p.is_file()}
        with patch.object(self.engine, 'transport_factory', side_effect=AssertionError('duplicate device access')):
            self.engine.create_backup(dict(serial='DEMO-LIUQIN', bundle='ignored',
                                           backup_parent=str(self.root / 'backups')))
        event = next(e for e in reversed(self.events) if e['type'] == 'backup_ready')
        self.assertEqual(event['backup']['source'], 'reused')
        self.assertEqual(event['backup']['path'], self.backup_path)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.backup_path).iterdir() if p.is_file()})

    def test_reprepare_and_logs_do_not_rewrite_backup(self):
        manifest_before = (Path(self.backup_path) / 'backup.json').read_bytes()
        self.engine.cancel_prepared()
        with patch.object(DemoTransport, 'backup', side_effect=AssertionError('backup during preparation')):
            self.engine.prepare(dict(serial='DEMO-LIUQIN', bundle='ignored', backup_dir=self.backup_path))
        self.assertTrue(self.engine.receipt['backup_reused'])
        self.assertNotEqual(self.engine.receipt['run_dir'], self.backup_path)
        self.assertFalse((Path(self.backup_path) / 'receipt.json').exists())
        self.assertFalse((Path(self.backup_path) / 'events.jsonl').exists())
        self.assertEqual((Path(self.backup_path) / 'backup.json').read_bytes(), manifest_before)

    def test_legacy_backup_load_is_read_only(self):
        (Path(self.backup_path) / 'backup.json').unlink()
        before = sorted(str(p.relative_to(self.backup_path)) for p in Path(self.backup_path).rglob('*'))
        record = self.engine.load_backup(self.backup_path, 'DEMO-LIUQIN')
        self.assertTrue(record['legacy'])
        self.assertEqual(before, sorted(str(p.relative_to(self.backup_path)) for p in Path(self.backup_path).rglob('*')))

    def test_other_device_and_simulation_backup_are_refused(self):
        with self.assertRaisesRegex(InstallerError, '序列号'):
            self.engine.load_backup(self.backup_path, 'OTHER-DEVICE')
        real = Engine(lambda _: None, self.root / 'real')
        with self.assertRaisesRegex(InstallerError, '演示备份'):
            real.load_backup(self.backup_path, 'DEMO-LIUQIN')

    def test_persist_changes_after_confirmation_stop_without_new_backup(self):
        def changed(record):
            result = backups.assert_compatible(record, self.engine.device, self.engine.report_text, '0' * 64, cli.layout)
            result['calibration_usable'] = True
            return result
        self.engine.transport.verify_backup = changed
        with self.assertRaisesRegex(InstallerError, '确认后发生变化'):
            self.engine.install(self.consent())
        self.assertFalse(self.engine.receipt['write_dispatched'])
        self.assertNotIn('backup', self.engine.transport.calls)

    def test_reuse_does_not_require_current_boot_to_match_original(self):
        record = self.engine.load_backup(self.backup_path, 'DEMO-LIUQIN')
        transport = Transport(self.engine, 'DEMO-LIUQIN')
        commands = []
        def remote(command, *_args):
            commands.append(command)
            return (record['files']['persist.img'] + '  persist\n').encode()
        transport.remote = remote
        transport.report = lambda: self.engine.report_text
        transport.validate_calibration = lambda: True
        transport.verify_backup(record)
        self.assertEqual(len(commands), 1)
        self.assertIn('persist', commands[0])
        self.assertNotIn('boot_a', commands[0])

    def test_original_persist_snapshot_difference_is_a_warning_and_reusable(self):
        self.engine.cancel_prepared()
        def different_snapshot(transport, record):
            result = backups.assert_compatible(record, transport.engine.device, transport.report(), '1' * 64, cli.layout)
            result['calibration_usable'] = True
            return result
        original = (Path(self.backup_path) / 'persist.img').read_bytes()
        with patch.object(DemoTransport, 'verify_backup', new=different_snapshot):
            self.engine.prepare(dict(serial='DEMO-LIUQIN', bundle='ignored', backup_dir=self.backup_path))
            plan = next(e['plan'] for e in reversed(self.events) if e['type'] == 'plan_ready')
            self.assertTrue(plan['warnings'])
            self.assertTrue(plan['calibration_usable'])
            self.engine.install(self.consent())
        self.assertEqual(self.engine.state, 'awaiting_first_boot')
        self.assertEqual(original, (Path(self.backup_path) / 'persist.img').read_bytes())
        self.assertNotIn('backup', self.engine.transport.calls)

    def test_unusable_current_calibration_stops_before_format(self):
        self.engine.transport.verify_backup = lambda _: dict(calibration_usable=False)
        with self.assertRaisesRegex(InstallerError, '校准数据未通过'):
            self.engine.install(self.consent())
        self.assertFalse(self.engine.receipt['write_dispatched'])

    def test_calibration_probe_is_in_ram_and_read_only_and_cleans_up(self):
        transport = Transport(self.engine, 'DEMO-LIUQIN')
        commands=[]
        def remote(command, *_args):
            commands.append(command)
            return b'provision-liuqin-from-persist: PASS\n' if 'provision.sh' in command else b''
        transport.remote = remote
        self.assertTrue(transport.validate_calibration())
        self.assertTrue(any('mount -t ext4 -o ro,noload' in command for command in commands))
        self.assertTrue(any('PERSIST_SRC=/run/' in command and 'provision.sh /run/' in command for command in commands))
        self.assertTrue(commands[-2].startswith('umount /run/'))
        self.assertTrue(commands[-1].startswith('rm -rf /run/'))
        self.assertFalse(any('--setrw' in command or 'mkfs' in command for command in commands))
        for command in commands:
            guarded = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + '0' * 36 + ' && ' + command
            cli.frame_command(guarded, 'LIUQIN_' + 'a' * 32 + '_START', 'LIUQIN_' + 'a' * 32 + '_END')

    def test_calibration_probe_failure_keeps_source_read_only_and_cleans_up(self):
        transport = Transport(self.engine, 'DEMO-LIUQIN')
        commands=[]
        def remote(command, *_args):
            commands.append(command)
            if 'provision.sh' in command:
                raise InstallerError('calibration failed')
            return b''
        transport.remote = remote
        with self.assertRaises(InstallerError):
            transport.validate_calibration()
        self.assertTrue(commands[-2].startswith('umount /run/'))
        self.assertTrue(commands[-1].startswith('rm -rf /run/'))

    def test_backup_can_run_without_root_archive_memory_budget(self):
        transport = Transport(self.engine, 'DEMO-LIUQIN')
        transport.remote = lambda command, *_args: b'87' if 'capacity' in command else b'1024' if 'MemAvailable' in command else b''
        transport.preflight(require_archive=False)
        with self.assertRaisesRegex(InstallerError, '暂存系统归档'):
            transport.preflight(require_archive=True)

    def test_backup_index_survives_worker_restart(self):
        restarted = DemoEngine(self.events.append, self.engine.state_dir)
        with patch.object(restarted, 'transport_factory', side_effect=AssertionError('device access during discovery')):
            restarted.find_backup(dict(serial='DEMO-LIUQIN', backup_parent=str(self.root / 'missing')))
        event = next(e for e in reversed(self.events) if e['type'] == 'backup_ready')
        self.assertEqual(event['backup']['path'], self.backup_path)
        self.assertEqual(event['backup']['source'], 'reused')
        restarted.close()

    def test_discovery_skips_bad_candidate_without_creating_backup(self):
        self.engine.cancel_prepared()
        with patch.object(self.engine, 'backup_candidates', return_value=[str(self.root / 'incomplete'), self.backup_path]), \
             patch.object(self.engine, 'transport_factory', side_effect=AssertionError('device access')):
            self.engine.find_backup(dict(serial='DEMO-LIUQIN', backup_parent=str(self.root)))
        self.assertTrue(any(e['type'] == 'backup_rejected' for e in self.events))
        event = next(e for e in reversed(self.events) if e['type'] == 'backup_ready')
        self.assertEqual(event['backup']['path'], self.backup_path)


class CapacityTests(unittest.TestCase):
    def test_concurrent_backup_index_updates_keep_both_devices(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            engines=[Engine(lambda _: None, directory) for _ in range(2)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(engine.remember_backup, dict(serial=serial,path='/test/' + serial))
                         for engine,serial in zip(engines,('ONE','TWO'))]
                for future in futures:
                    future.result()
            index=json.loads((Path(directory)/'backups-index.json').read_text())
            self.assertEqual(set(index), {'real:ONE','real:TWO'})

    def test_same_device_lock_across_different_state_directories(self):
        import os
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory}):
            first = Engine(lambda _: None, Path(directory) / 'one')
            second = Engine(lambda _: None, Path(directory) / 'two')
            first.lock_device('TEST-SERIAL')
            try:
                with self.assertRaises(InstallerError):
                    second.lock_device('TEST-SERIAL')
            finally:
                first.close()
            second.lock_device('TEST-SERIAL')
            second.close()
    def test_128_256_512_plans_use_device_report(self):
        for capacity in (128, 256, 512):
            with tempfile.TemporaryDirectory() as directory:
                engine = DemoEngine(lambda _: None, Path(directory) / 'state')
                class CapacityTransport(DemoTransport):
                    def report(self):
                        return demo_report(capacity)
                engine.transport_factory = CapacityTransport
                engine.create_backup(dict(serial='DEMO', bundle='ignored',
                                          backup_parent=str(Path(directory) / 'backups')))
                engine.prepare(dict(serial='DEMO', bundle='ignored', backup_dir=engine.receipt['backup_dir']))
                self.assertEqual(engine.plan.entries[0].size,
                                 engine.plan.disk.by_name('userdata').size)
                self.assertGreater(engine.plan.entries[0].size, capacity * 1024**3 * .65)
                self.assertLess(engine.plan.entries[0].size, capacity * 1024**3)
                self.assertFalse(engine.receipt['write_dispatched'])
                engine.cancel_prepared()


if __name__ == '__main__':
    unittest.main(verbosity=2)
