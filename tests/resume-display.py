#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Replay resume, output hotplug and policy changes against the actual controller."""
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('resume_display', str(ROOT / 'device/resume-display/liuqin-resume-display'))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


class Backend:
    def __init__(self):
        self.count = 20
        self.sid = '1'
        self.wake = 'pmic_pwrkey'
        self.closed = False
        self.allowed = True
        self.on = False
        self.ready = False
        self.requests = []
        self.after_query = lambda: None
        self.fail_send = False

    def success(self): return self.count
    def session(self): return self.sid
    def source(self): return self.wake
    def source_allowed(self, source): return self.allowed
    def lid_closed(self): return self.closed
    def is_on(self): return self.on
    def ready_outputs(self):
        if not self.ready:
            raise ValueError('placeholder screen')
        self.after_query()
    def turn_on(self):
        self.requests.append('on')
        if self.fail_send:
            raise TimeoutError('client timed out after sending')


class Tests(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.now = 0
        self.events = []
        self.c = m.Controller(self.b, clock=lambda: self.now, emit=lambda event, **data: self.events.append((event, data)))

    def resume(self):
        self.c.prepare(True)
        self.b.count += 1
        self.c.prepare(False)

    def test_resume_before_output_reappears(self):
        self.resume()
        self.assertTrue(self.c.tick())
        self.assertEqual(self.b.requests, [])
        self.now = 0.5
        self.b.ready = True
        self.assertTrue(self.c.tick())
        self.assertEqual(self.b.requests, ['on'])
        self.b.on = True
        self.assertFalse(self.c.tick())
        # A later normal idle blank must not be undone.
        self.b.on = False
        self.assertFalse(self.c.tick())
        self.assertEqual(len(self.b.requests), 1)

    def test_no_action_on_startup_or_restart(self):
        self.b.ready = True
        self.c.prepare(False)
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_inactive_or_missing_session_cannot_arm(self):
        for sid in (None, 'other-session'):
            self.b.sid = sid
            self.resume()
            self.b.ready = True
            self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_background_and_unknown_sources_never_wake(self):
        for source in (None, 'rtc0', 'pm8xxx_rtc_alarm', 'wlan', 'nanosic-fold'):
            self.b.wake = source
            self.resume()
            self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_requires_one_completed_suspend(self):
        for delta in (0, 2, -1):
            self.c.prepare(True)
            self.b.count += delta
            self.c.prepare(False)
            self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_display_already_on(self):
        self.b.on = True
        self.resume()
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_lid_close_cancels_without_rearming_on_open(self):
        self.resume()
        self.b.closed = True
        self.assertFalse(self.c.tick())
        self.b.closed = False
        self.b.ready = True
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_session_switch_cancels(self):
        self.resume()
        self.b.sid = '2'
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_transient_no_active_session_waits(self):
        self.resume()
        self.b.sid = None
        self.assertTrue(self.c.tick())
        self.b.sid = '1'
        self.b.ready = True
        self.c.tick()
        self.assertEqual(len(self.b.requests), 1)

    def test_rechecks_lid_and_session_after_output_query(self):
        for change in (lambda: setattr(self.b, 'closed', True), lambda: setattr(self.b, 'sid', '2')):
            self.b.closed = False
            self.b.sid = '1'
            self.b.ready = True
            self.resume()
            self.b.after_query = change
            self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_muted_keyboard_does_not_wake(self):
        self.b.wake = 'nanosic-data'
        self.b.allowed = False
        self.resume()
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_never_retries_a_possibly_delivered_request(self):
        self.b.ready = True
        self.b.fail_send = True
        self.resume()
        self.c.tick()
        self.c.tick()
        self.assertEqual(len(self.b.requests), 1)
        self.now += m.VERIFY_SECONDS + 0.1
        self.assertFalse(self.c.tick())

    def test_timeout_cannot_later_wake_hotplugged_display(self):
        self.resume()
        self.now = m.WAIT_SECONDS
        self.assertFalse(self.c.tick())
        self.b.ready = True
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_new_sleep_discards_old_request(self):
        self.resume()
        self.c.prepare(True)
        self.b.ready = True
        self.assertFalse(self.c.tick())
        self.assertEqual(self.b.requests, [])

    def test_irq_numbers_are_discovered_and_background_sources_rejected(self):
        text = '  44: 0 2 pmic_arb 1302935 Edge pmic_pwrkey\n 700: 1 0 msmgpio 50 Edge nanosic-data\n 9: 0 1 pmic_arb 123 Edge pm8xxx_rtc_alarm\n'
        self.assertEqual(m.wake_source('44\n', text), 'pmic_pwrkey')
        self.assertEqual(m.wake_source('700', text), 'nanosic-data')
        for irq in ('0', '', '9', '23', 'bad'):
            self.assertIsNone(m.wake_source(irq, text))

    def test_external_display_prevents_global_dpms_request(self):
        internal = dict(name='DSI-1', connected=True, enabled=True)
        self.assertIsNone(m.require_internal_only({'outputs': [internal, dict(name='DP-1', connected=False, enabled=False)]}))
        for outputs in ([], [dict(internal, enabled=False)], [dict(internal, connected=False)], [internal, internal],
                        [dict(name='invalid/name')], [internal, dict(name='DP-1', connected=True, enabled=False)]):
            with self.assertRaises(ValueError):
                m.require_internal_only({'outputs': outputs})

    def test_folio_state_is_strict(self):
        self.assertFalse(m.posture('folded=0 lid_closed=0 input_muted=0 tablet_mode=0')['lid_closed'])
        for text in ('', 'lid_closed=0', 'folded=0 lid_closed=2 input_muted=0 tablet_mode=0', 'folded=0 lid_closed=0 input_muted=0 tablet_mode=0 folded=0'):
            with self.assertRaises(ValueError):
                m.posture(text)

    def test_same_session_inactive_is_transient_but_different_user_is_not(self):
        backend = object.__new__(m.Backend)
        backend.uid = 1000
        session = dict(User=(1000, '/user/1000'), Type='wayland', Class='user',
                       Active=False, Remote=False, Seat=('seat0', '/seat/seat0'))
        backend.props = lambda service, path, interface: {'ActiveSession': ('1', '/session/1')} if interface.endswith('.Seat') else session
        self.assertIsNone(backend.session())
        session['Active'] = True
        self.assertEqual(backend.session(), '1')
        session['User'] = (1001, '/user/1001')
        self.assertEqual(backend.session(), 'other-session')


if __name__ == '__main__':
    unittest.main()
