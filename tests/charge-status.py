#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check actual telemetry aggregation with isolated sysfs files, never a device."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'device/board-overlay/usr/local/bin/liuqin-charge-status'
loader = importlib.machinery.SourceFileLoader('charge_status', str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
status = importlib.util.module_from_spec(spec)
loader.exec_module(status)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.usb = self.root / 'class/power_supply/qcom-battmgr-usb'
        self.port = self.root / 'class/typec/port0'
        self.partner = self.port.with_name('port0-partner')
        self.xm = self.root / 'devices/platform/pmic-glink/pmic_glink.power-supply.0/xiaomi'
        for path in (self.usb, self.port, self.partner, self.xm):
            path.mkdir(parents=True)
        for name, value in {'online': 1, 'voltage_now': 9000000, 'current_now': 0}.items():
            (self.usb / name).write_text(str(value))
        (self.port / 'power_role').write_text('source [sink]')
        for name, value in {'bq2597x_bus_voltage': 9000, 'bq2597x_bus_current': 3000,
                            'bq2597x_slave_bus_current': 2800}.items():
            (self.xm / name).write_text(str(value))

    def test_dual_pumps_are_counted_when_usb_current_is_zero(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        r = status.sample(self.root)
        self.assertEqual(r['total_input_current_ua'], 5800000)
        self.assertEqual(r['estimated_input_power_uw'], 52200000)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_each_path_uses_its_own_voltage(self):
        (self.usb / 'current_now').write_text('1500000')
        (self.usb / 'voltage_now').write_text('5000000')
        r = status.sample(self.root)
        self.assertEqual(r['total_input_current_ua'], 7300000)
        self.assertEqual(r['estimated_input_power_uw'], 59700000)
        self.assertEqual(r['components']['usb']['estimated_power_uw'], 7500000)

    def test_ordinary_usb_and_single_pump_paths(self):
        for main, slave in ((0, 0), (2300, 0), (0, 1800)):
            with self.subTest(main=main, slave=slave):
                (self.usb / 'current_now').write_text('500000')
                (self.xm / 'bq2597x_bus_current').write_text(str(main))
                (self.xm / 'bq2597x_slave_bus_current').write_text(str(slave))
                r = status.sample(self.root)
                self.assertEqual(r['total_input_current_ua'], 500000 + (main + slave) * 1000)
                self.assertEqual(r['estimated_input_power_uw'], 4500000 + (main + slave) * 9000)

    def test_offline_and_source_roles_do_not_publish_stale_inputs(self):
        for online, role in ((0, 'source [sink]'), (1, '[source] sink')):
            with self.subTest(online=online, role=role):
                (self.usb / 'online').write_text(str(online))
                (self.port / 'power_role').write_text(role)
                r = status.sample(self.root)
                self.assertEqual(r['total_input_current_ua'], 0)
                self.assertEqual(r['estimated_input_power_uw'], 0)
                self.assertIsNone(r['components'])

    def test_missing_pump_channel_does_not_publish_a_partial_total(self):
        (self.xm / 'bq2597x_slave_bus_current').unlink()
        with self.assertRaises(FileNotFoundError):
            status.sample(self.root)

    def test_failed_transport_does_not_use_old_or_zero_data(self):
        with patch.object(status, 'number', side_effect=TimeoutError('GLINK timeout')):
            with self.assertRaises(TimeoutError):
                status.sample(self.root)

    def test_invalid_and_overflowing_measurements_are_rejected(self):
        for value in ('-1', '4294967295', 'not-ready', '2147483'):
            with self.subTest(value=value):
                (self.xm / 'bq2597x_bus_current').write_text(value)
                with self.assertRaises(ValueError):
                    status.sample(self.root)

    def test_current_without_voltage_is_not_a_zero_power_success(self):
        (self.xm / 'bq2597x_bus_voltage').write_text('0')
        with self.assertRaisesRegex(status.Unavailable, 'without a bus voltage'):
            status.sample(self.root)

    def test_disconnect_replacement_and_role_change_invalidate_snapshot(self):
        initial = status.connection(self.root)
        for changed in ((0, *initial[1:]), (1, 'source', *initial[2:]),
                        (1, 'sink', ((27, 900, 222),), initial[3])):
            with self.subTest(changed=changed):
                with patch.object(status, 'connection', side_effect=[initial, changed]):
                    with self.assertRaisesRegex(status.Unavailable, 'connection changed'):
                        status.sample(self.root)

    def test_missing_or_ambiguous_online_interfaces_are_rejected(self):
        (self.port / 'power_role').write_text('unknown')
        with self.assertRaisesRegex(status.Unavailable, 'role'):
            status.sample(self.root)
        (self.port / 'power_role').write_text('sink')
        (self.xm.parent.with_name('another') / 'xiaomi').mkdir(parents=True)
        with self.assertRaisesRegex(status.Unavailable, 'ambiguous'):
            status.sample(self.root)

    def test_json_cli_preserves_units_and_reports_unavailable_without_totals(self):
        def run():
            return subprocess.run([sys.executable, str(SOURCE), '--sysfs', str(self.root), '--json'],
                                  text=True, capture_output=True, timeout=5)
        good = run()
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)['total_input_current_ua'], 5800000)
        (self.xm / 'bq2597x_bus_current').unlink()
        bad = run()
        self.assertEqual(bad.returncode, 1)
        payload = json.loads(bad.stdout)
        self.assertFalse(payload['valid'])
        self.assertNotIn('total_input_current_ua', payload)
        self.assertNotIn('estimated_input_power_uw', payload)


if __name__ == '__main__':
    unittest.main(verbosity=2)
