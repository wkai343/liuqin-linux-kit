#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise offline RTC restoration without changing the host clock or alarms."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
OVERLAY = PROJECT / 'device/gnome-overlay'
HELPER = OVERLAY / 'usr/libexec/liuqin-rtc'
rtc = types.ModuleType('liuqin_rtc')
exec(compile(HELPER.read_text(), str(HELPER), 'exec'), rtc.__dict__)
NOW = 1792591200.25
RAW = 110526491
DEVICE = {'machine_id': '1' * 32, 'rtc_name': 'rtc-pm8xxx c400000.spmi:pmic@0:rtc@6100'}


def report(**changes):
    # chrony -c includes both hexadecimal reference ID and source address.
    row = ['771CCEC1', '119.28.206.193', '3', str(NOW - 30), '-0.000000197',
           '-0.000639916', '0.004892773', '-20.502', '-0.009', '0.261',
           '0.112702928', '0.024057245', '1038.6', 'Normal']
    for index, value in changes.items():
        row[int(index)] = str(value)
    return ','.join(row) + '\n'


def anchor(**changes):
    return dict({'schema': 1, 'device': DEVICE.copy(), 'rtc_seconds': RAW,
                 'unix_seconds': NOW, 'source': 'chrony'}, **changes)


class RtcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.device = self.root / 'rtc7'
        self.device.mkdir()
        self.path = self.state / 'anchor.json'
        for name, value in [('STATE', self.state)]:
            patcher = patch.object(rtc, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__(); self.addCleanup(redirect.__exit__, None, None, None)

    def test_offline_reboot_and_poweroff_elapsed_time(self):
        for elapsed in (1, 300, 86400, 30 * 86400, rtc.MAX_AGE):
            self.assertEqual(rtc.estimate(anchor(), DEVICE, RAW + elapsed), NOW + elapsed)

    def test_reject_reset_wrap_stale_and_foreign_state(self):
        for raw in (0, RAW - 1, RAW + rtc.MAX_AGE + 1):
            with self.assertRaises(rtc.InvalidState):
                rtc.estimate(anchor(), DEVICE, raw)
        for change in ({'device': {}}, {'schema': 2}, {'rtc_seconds': True},
                       {'unix_seconds': float('nan')}, {'unix_seconds': float('inf')},
                       {'unix_seconds': None}, {'unix_seconds': rtc.MAX_TIME},
                       {'rtc_seconds': -1}, {'rtc_seconds': 2**32}):
            with self.subTest(change=change), self.assertRaises(rtc.InvalidState):
                rtc.estimate(anchor(**change), DEVICE, RAW)
        with self.assertRaises(rtc.InvalidState):
            rtc.estimate([], DEVICE, RAW)

    def test_chrony_csv_synchronisation_requires_recent_external_reference(self):
        self.assertTrue(rtc.synchronised(report(), NOW))
        for change in ({'0': '00000000'}, {'0': '7F7F0101'}, {'2': 0}, {'2': 16},
                       {'3': NOW - 3601}, {'3': NOW + 2}, {'4': -60},
                       {'4': 'nan'}, {'10': 'inf'}, {'11': 6}, {'11': -1},
                       {'13': 'Not synchronised'}):
            with self.subTest(change=change):
                self.assertFalse(rtc.synchronised(report(**change), NOW))
        for value in ('', 'invalid', report() + report()):
            self.assertFalse(rtc.synchronised(value, NOW))

    def operation(self, action, *, wall=NOW, raw=RAW, **kwargs):
        with patch.object(rtc, 'identity', return_value=(self.device, DEVICE)), \
             patch.object(rtc, 'ticking_sample', return_value=(raw, wall)), \
             patch.object(rtc.time, 'time', return_value=wall):
            rtc.operate(action, **kwargs)

    def test_unsynchronised_save_preserves_last_good_anchor(self):
        self.path.write_bytes(b'previous good calibration')
        with patch.object(rtc, 'tracking', return_value=report(**{'13': 'Not synchronised'})):
            self.operation('save')
        self.assertEqual(self.path.read_bytes(), b'previous good calibration')

    def test_chrony_query_uses_loopback_monitor_and_missing_daemon_is_not_calibration(self):
        import subprocess
        with patch.object(rtc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, report())) as run:
            self.assertEqual(rtc.tracking(), report())
        self.assertEqual(run.call_args.args[0][1:3], ['-h', '127.0.0.1'])
        with patch.object(rtc.subprocess, 'run', side_effect=subprocess.TimeoutExpired('chronyc', 5)):
            self.assertEqual(rtc.tracking(), '')

    def test_synced_and_manual_calibration_are_atomic_private_files(self):
        with patch.object(rtc, 'tracking', return_value=report()):
            self.operation('save')
        self.assertEqual(json.loads(self.path.read_text()), anchor())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.operation('save', manual=True, raw=RAW + 10, wall=NOW + 10)
        self.assertEqual(json.loads(self.path.read_text())['source'], 'manual')
        self.assertEqual(list(self.state.glob('.anchor-*')), [])

    def test_synchronisation_loss_during_sample_does_not_overwrite(self):
        self.path.write_text('previous')
        with patch.object(rtc, 'tracking', side_effect=[report(), '']), \
             self.assertRaises(rtc.InvalidState):
            self.operation('save')
        self.assertEqual(self.path.read_text(), 'previous')

    def test_failed_atomic_replace_preserves_previous_state(self):
        self.path.write_text('previous')
        with patch.object(rtc.os, 'replace', side_effect=OSError('full')), self.assertRaises(OSError):
            rtc.write_state(self.path, anchor())
        self.assertEqual(self.path.read_text(), 'previous')
        self.assertEqual(list(self.state.glob('.anchor-*')), [])

    def test_restore_sets_future_estimate_but_never_steps_backwards(self):
        with patch.object(rtc, 'load_state', return_value=anchor()), \
             patch.object(rtc.time, 'clock_gettime', return_value=30), \
             patch.object(rtc.time, 'clock_settime') as setter:
            self.operation('restore', wall=RAW + 120, raw=RAW + 120)
            setter.assert_called_once_with(rtc.time.CLOCK_REALTIME, NOW + 120)
            setter.reset_mock()
            self.operation('restore', wall=NOW + 3600, raw=RAW + 120)
            setter.assert_not_called()

    def test_dry_run_status_missing_state_and_late_restore_never_set_time(self):
        with patch.object(rtc.time, 'clock_settime') as setter:
            self.operation('restore')  # no calibration on first boot
            with patch.object(rtc, 'load_state', return_value=anchor()), \
                 patch.object(rtc.time, 'clock_gettime', return_value=301):
                self.operation('restore', dry_run=True, wall=RAW)
                self.operation('status')
                with self.assertRaises(rtc.InvalidState):
                    self.operation('restore', wall=RAW)
            setter.assert_not_called()

    def test_state_reader_rejects_symlink_oversize_bad_json_and_permissions(self):
        target = self.root / 'target'; target.write_text('{}')
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            rtc.load_state(self.path)
        self.path.unlink()
        real_fstat = os.fstat
        def root_fstat(fd):
            fields = list(real_fstat(fd)); fields[4] = 0
            return os.stat_result(fields)
        # The test directory belongs to the runner; emulate root ownership only.
        with patch.object(rtc.os, 'fstat', side_effect=root_fstat):
            for data, mode in (('{' , 0o600), ('x' * 4097, 0o600), ('{}', 0o644)):
                self.path.write_text(data); self.path.chmod(mode)
                with self.assertRaises(ValueError):
                    rtc.load_state(self.path)
            self.path.write_text(json.dumps(anchor())); self.path.chmod(0o600)
            self.assertEqual(rtc.load_state(self.path), anchor())

    def test_sample_rejects_delays_steps_and_bad_counter(self):
        (self.device / 'since_epoch').write_text(str(RAW))
        with patch.object(rtc.time, 'time', side_effect=[NOW, NOW + .01]), \
             patch.object(rtc.time, 'monotonic', side_effect=[0, .01]):
            raw, wall = rtc.sample(self.device)
        self.assertEqual(raw, RAW); self.assertAlmostEqual(wall, NOW + .005)
        for end, elapsed in ((NOW + 10, .01), (NOW + .5, .5)):
            with patch.object(rtc.time, 'time', side_effect=[NOW, end]), \
                 patch.object(rtc.time, 'monotonic', side_effect=[0, elapsed]), \
                 self.assertRaises(rtc.InvalidState):
                rtc.sample(self.device)

    def test_stopped_or_reset_rtc_and_suspend_during_sample_are_rejected(self):
        for end, elapsed, wall_delta in ((RAW, 1.1, 1.1), (RAW - 1, 1.1, 1.1),
                                        (RAW + 10, 1.1, 10), (RAW + 5, 5, 5),
                                        (RAW + 1, 1.1, 20)):
            with patch.object(rtc, 'sample', side_effect=[(RAW, NOW), (end, NOW + wall_delta)]), \
                 patch.object(rtc.time, 'sleep'), \
                 patch.object(rtc.time, 'monotonic', side_effect=[0, elapsed]), \
                 self.assertRaises(rtc.InvalidState):
                rtc.ticking_sample(self.device)

    def test_board_and_rtc_selection_do_not_assume_rtc0(self):
        compatible = self.root / 'compatible'; compatible.write_bytes(b'xiaomi,liuqin\0qcom,sm8475\0')
        machine = self.root / 'machine-id'; machine.write_text('1' * 32)
        node = self.device / 'device/of_node'; node.mkdir(parents=True)
        (node / 'compatible').write_bytes(b'qcom,pmk8350-rtc\0')
        (self.device / 'name').write_text(DEVICE['rtc_name'])
        with patch.object(rtc, 'COMPATIBLE', compatible), patch.object(rtc, 'MACHINE_ID', machine), \
             patch.object(rtc, 'RTC_CLASS', self.root):
            self.assertEqual(rtc.identity(), (self.device, DEVICE))
            compatible.write_bytes(b'qcom,unrelated\0')
            with self.assertRaises(rtc.InvalidState):
                rtc.identity()
            compatible.write_bytes(b'qcom,cape-mtp\0qcom,cape\0qcom,mtp\0')
            (self.root / 'model').write_bytes(b'Liuqin based on Qualcomm Technologies, Inc SM8475\0')
            (self.root / 'xiaomi,miboard-id').write_bytes(bytes.fromhex('0000001000000000'))
            self.assertEqual(rtc.identity(), (self.device, DEVICE))
            (self.root / 'xiaomi,miboard-id').write_bytes(bytes.fromhex('0000001100000000'))
            with self.assertRaises(rtc.InvalidState):
                rtc.identity()

    def test_packaged_board_payload_starts_restore_and_non_waking_timer(self):
        spec = importlib.util.spec_from_file_location('board_rtc_test', PROJECT / 'build/scripts/build-liuqin-rpms.py')
        board = importlib.util.module_from_spec(spec)
        import sys
        sys.path.insert(0, str(PROJECT / 'build/lib'))
        spec.loader.exec_module(board)
        busybox = self.root / 'busybox'; busybox.write_bytes(b'fixture'); busybox.chmod(0o755)
        payload = self.root / 'payload'; payload.mkdir()
        board.board_overlay(payload, busybox)
        units = payload / 'etc/systemd/system'
        self.assertIn('Wants=liuqin-rtc-restore.service', (units / 'sysinit.target.d/20-liuqin-rtc.conf').read_text())
        self.assertIn('Wants=liuqin-rtc-save.timer', (units / 'timers.target.d/20-liuqin-rtc.conf').read_text())
        self.assertIn('WakeSystem=no', (units / 'liuqin-rtc-save.timer').read_text())
        self.assertIn('Before=sysinit.target time-set.target chronyd.service chrony.service',
                      (units / 'liuqin-rtc-restore.service').read_text())
        self.assertEqual((payload / 'usr/libexec/liuqin-rtc').read_bytes(), HELPER.read_bytes())
        self.assertTrue((payload / 'usr/libexec/liuqin-rtc').stat().st_mode & 0o111)
        self.assertFalse(list(payload.rglob('*.pyc')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
