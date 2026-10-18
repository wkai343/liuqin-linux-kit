#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise boot brightness restoration without opening a real sysfs device."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / 'device/gnome-overlay/usr/local/libexec/liuqin-backlight-default'


class BacklightTests(unittest.TestCase):
    def run_fixture(self, *, power, cmdline='quiet', maximum=2047, curve=None, default=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {'brightness': '800', 'actual_brightness': '0' if power else '1500',
                      'max_brightness': str(maximum), 'bl_power': str(power)}
            if curve is not None:values['brightness_curve']=curve
            if default is not None:values['default_brightness']=str(default)
            if maximum==4095 and not power and default is not None:values['actual_brightness']=str(default)
            for name, value in values.items():
                (root / name).write_text(value + '\n')
            args = root / 'cmdline'
            args.write_text(cmdline + '\n')
            result = subprocess.run(['sh', str(SCRIPT)], capture_output=True, text=True,
                                    env=dict(os.environ, LIUQIN_BACKLIGHT_ROOT=str(root),
                                             LIUQIN_BACKLIGHT_CMDLINE=str(args)))
            return result, {name: (root / name).read_text().strip() for name in values}

    def test_unprepared_panel_stays_dark_until_drm_enables_it(self):
        result, values = self.run_fixture(power=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values['brightness'], '1500')
        self.assertEqual(values['bl_power'], '4')
        self.assertEqual(values['actual_brightness'], '0')

    def test_ready_panel_keeps_its_existing_power_state(self):
        result, values = self.run_fixture(power=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values['brightness'], '1500')
        self.assertEqual(values['bl_power'], '0')

    def test_exact_charger_mode_never_changes_brightness_or_power(self):
        result, values = self.run_fixture(power=4, cmdline='quiet androidboot.mode=charger rootwait')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values['brightness'], '800')
        self.assertEqual(values['bl_power'], '4')
        result, values = self.run_fixture(power=4, cmdline='quiet xandroidboot.mode=charger rootwait')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values['brightness'], '1500')

    def test_oem_curve_uses_kernel_default_without_unblanking(self):
        for power in (0,4):
            result, values = self.run_fixture(power=power,maximum=4095,curve='liuqin-oem',default=510)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(values['brightness'],'510')
            self.assertEqual(values['bl_power'],str(power))
    def test_unknown_curve_or_invalid_default_is_rejected(self):
        for curve,default in [('other',510),('liuqin-oem',0),('liuqin-oem',4096),('liuqin-oem','bad')]:
            result, values = self.run_fixture(power=4,maximum=4095,curve=curve,default=default)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(values['brightness'],'800')
    def test_charger_oem_curve_stays_untouched(self):
        result, values = self.run_fixture(power=4,maximum=4095,curve='liuqin-oem',default=510,cmdline='androidboot.mode=charger')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(values['brightness'],'800')

    def test_unexpected_backlight_is_rejected_without_changes(self):
        result, values = self.run_fixture(power=4, maximum=255)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(values['brightness'], '800')
        self.assertEqual(values['bl_power'], '4')


if __name__ == '__main__':
    unittest.main(verbosity=2)
