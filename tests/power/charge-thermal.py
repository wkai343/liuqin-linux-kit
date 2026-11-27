#!/usr/bin/env python3
"""Charging policy hysteresis and fail-closed host control regressions."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader('charge_thermal', str(
    ROOT / 'device/board-overlay/usr/local/libexec/liuqin-charge-thermal'))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
CONFIG = json.loads((ROOT / 'device/board-overlay/usr/share/liuqin/charge-thermal.json').read_text())


def sample(temp):
    # Isolate policy temperature from the independently tested weighted formula.
    return dict.fromkeys(CONFIG['weights'], temp)


class Tests(unittest.TestCase):
    def policy(self):
        config = dict(CONFIG, weights={'battery': 1}, weight_sum=1, compensation_mc=0)
        return mod.Policy(config)

    def test_oem_weighted_temperature_and_screen_levels(self):
        policy = mod.Policy(CONFIG)
        self.assertEqual(policy.update(sample(40000), True), (39411, 3))
        self.assertEqual(policy.update(sample(40000), False), (39411, 1))

    def test_heat_multiple_bands_hold_hysteresis_and_natural_cooldown(self):
        policy = self.policy()
        self.assertEqual(policy.update({'battery': 25000}, True), (25000, 0))
        self.assertEqual(policy.update({'battery': 42000}, True), (42000, 9))
        self.assertEqual(policy.update({'battery': 41000}, True), (41000, 9))
        self.assertEqual(policy.update({'battery': 40999}, True), (40999, 7))
        self.assertEqual(policy.update({'battery': 39000}, True), (39000, 3))
        self.assertEqual(policy.update({'battery': 36999}, True), (36999, 2))
        self.assertEqual(policy.update({'battery': 34999}, True), (34999, 0))
        self.assertEqual(policy.update({'battery': 46000}, False), (46000, 15))
        self.assertEqual(policy.update({'battery': 25000}, False), (25000, 0))

    def test_restart_retains_band_inside_hysteresis(self):
        self.assertEqual(self.policy().update({'battery': 36000}, True), (36000, 2))
        self.assertEqual(self.policy().update({'battery': 44500}, True), (44500, 15))

    def test_partial_or_invalid_sample_never_becomes_a_cool_reading(self):
        policy = mod.Policy(CONFIG)
        for values in ({}, {'battery': 25000}, sample(-40000), sample(125000)):
            with self.assertRaises(ValueError):
                policy.update(values, True)
        self.assertIsNone(policy.band)

    def test_transient_failure_holds_then_maximum_and_fresh_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            state.write_text('9\n')

            class Device:
                temperature = None

                def cooling(self):
                    return state

                def sample(self, names):
                    if self.temperature is None:
                        raise OSError('sensor offline')
                    return {'battery': self.temperature}

                def screen_on(self):
                    return True

            device = Device()
            controller = mod.Controller(self.policy(), device)
            for _ in range(2):
                self.assertIn('error', controller.tick())
                self.assertEqual(state.read_text(), '9\n')
            self.assertEqual(controller.tick()['applied_level'], 15)
            device.temperature = 42000
            self.assertEqual(controller.tick()['applied_level'], 12)
            device.temperature = 25000
            self.assertEqual(controller.tick()['applied_level'], 0)

    def test_write_failure_is_not_reported_as_applied(self):
        writes = []

        class State:
            def read_text(self):
                return '0'

            def open(self, *args, **kwargs):
                assert args == ('wb',) and kwargs == {'buffering': 0}
                return self

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def write(self, value):
                writes.append(value)
                raise OSError('firmware rejected command')

        class Device:
            def cooling(self):
                return State()

            def sample(self, names):
                return {'battery': 45000}

            def screen_on(self):
                return True

        with self.assertRaisesRegex(OSError, 'firmware rejected'):
            mod.Controller(self.policy(), Device()).tick()
        self.assertEqual(writes, [b'15\n'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
