#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise role-first ordering, bounded readiness, detach and current-attach gates.

Fake sysfs and charger responses test policy; they do not measure charging power.
Role ordering cases follow upstream xiaomipad-6pro-mainline's mipps-role-first test.
"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
import errno
import io
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'device/board-overlay/usr/local/libexec/liuqin-mipps-auth'
auth = types.ModuleType('mipps_startup_test')
exec(compile(SOURCE.read_text(), str(SOURCE), 'exec'), auth.__dict__)
notify_completed_charge = auth.notify_completed_charge
partner_source_usb_data = auth.partner_source_usb_data
request_data_role = auth.request_data_role
usb_is_online = auth.usb_is_online
print_status = auth.print_status


class Clock:
    now = 0.0
    on_sleep = None

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError('sleep length must be non-negative')
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


class Tests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in dict(real_type='PD_PPS', current_state='SNK_Ready',
                                pdo2='0001912c', adapter_svid='2717', adapter_id='12345678',
                                pd_verifed='0', verify_process='0', request_vdm_cmd='9,Null',
                                fastchg_mode='0', power_max='67', data_role='host [device]').items():
            self.put(name, value)
        self.clock = Clock()
        for name in ('ATTEMPT_STATE_FILE', 'AUTHENTICATED_STATE_FILE', 'VERIFIED_STATE_FILE',
                     'READY_STATE_FILE', 'USB_ONLINE_STATE_FILE',
                     'NOTIFICATION_STATE_FILE', 'CHARGE_PROFILE_STATE_FILE',
                     'PENDING_EVENT_FILE'):
            self.stack.enter_context(patch.object(auth, name, str(self.root / name)))
        self.online = self.mock('usb_is_online', return_value=True)
        self.partner = self.mock('current_attach_token', return_value='partner-1')
        self.cells = self.mock('enable_battery_auth')
        self.notify = self.mock('notify_completed_charge')
        self.mock('print_status')
        self.start = self.mock('start_systemd_unit')
        self.mock('find_typec_port', return_value=str(self.root))
        self.usb_data = self.mock('partner_source_usb_data', return_value=False)
        self.restore_role = self.mock('request_data_role', return_value=(0, ''))
        self.role = self.mock('request_host_data_role', side_effect=self.take_host)
        self.stack.enter_context(patch.object(auth.time, 'monotonic', side_effect=lambda: self.clock.now))
        self.stack.enter_context(patch.object(auth.time, 'sleep', side_effect=self.clock.sleep))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.commands = []
        self.payloads = []
        self.delays = {}
        self.pending_response = None
        self.silent_commands = set()
        self.original_read = auth.read_node
        self.mock('read_node', side_effect=self.charger_read)
        self.original_write = auth.write_node
        self.write = self.mock('write_node', side_effect=self.charger_write)
        self.digest_ok = True
        self.silent = False
        self.disconnect_at = None

    def mock(self, name, **kwargs):
        return self.stack.enter_context(patch.object(auth, name, **kwargs))

    def put(self, name, value):
        (self.root / name).write_text(value)

    def get(self, name):
        return (self.root / name).read_text().strip()

    def take_host(self, port, timeout):
        self.cells.assert_called()
        self.assertEqual(self.commands, [])
        self.put('data_role', '[host] device')
        if self.get('request_vdm_cmd').startswith('0,'):
            self.put('request_vdm_cmd', '9,Null')
        return 0, ''

    def charger_read(self, root, name):
        if name == 'request_vdm_cmd' and self.pending_response:
            when, response = self.pending_response
            if self.clock.now >= when:
                self.put(name, response)
                self.pending_response = None
        return self.original_read(root, name)

    def charger_write(self, root, name, value):
        if name == 'pd_verifed' and value == '1':
            self.assertEqual(self.get('verify_process'), '0')
        if name != 'request_vdm_cmd':
            return self.original_write(root, name, value)
        cmd, payload = value.split(',', 1)
        cmd = int(cmd)
        self.assertIn('[host]', self.get('data_role'))
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(self.get('verify_process'), '1')
        self.commands.append(cmd)
        self.payloads.append((cmd, payload))
        if cmd == self.disconnect_at:
            self.online.return_value = False
        if self.silent or cmd in self.silent_commands:
            return
        response = 'Null'
        if cmd == 5:
            digest = auth.hmac.new(bytes.fromhex(auth.KEYS[0]), bytes.fromhex(payload + '12345678'),
                                   auth.hashlib.sha256).hexdigest()
            response = digest[:32] if self.digest_ok else '00' * 16
        if cmd in self.delays:
            self.pending_response = (self.clock.now + self.delays[cmd], f'{cmd},{response}')
        else:
            self.put(name, f'{cmd},{response}')

    def run_main(self, *extra):
        unit = PROJECT / 'device/board-overlay/etc/systemd/system/liuqin-mipps-auth.service'
        import shlex
        flags = shlex.split(unit.read_text().split('/usr/local/libexec/liuqin-mipps-auth ', 1)[1].splitlines()[0])
        with patch.object(sys, 'argv', ['mipps', *flags, '--sysfs', str(self.root), '--seed-index', '0', *extra]):
            return auth.main()

    def test_android_sequence_after_cells_and_host(self):
        self.run_main()
        self.cells.assert_called_once()
        self.role.assert_called_once()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')
        self.assertEqual(self.get('verify_process'), '0')
        self.assertLess(self.clock.now, 0.1)  # No artificial 500 ms settles.
        self.assertEqual(Path(auth.VERIFIED_STATE_FILE).read_text().strip(), 'partner-1')

    def test_battery_only_never_changes_adapter_even_offline(self):
        self.online.return_value = False
        self.run_main('--battery-auth-only')
        self.cells.assert_called_once()
        self.role.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_cell_transport_error_can_retry_an_independent_challenge(self):
        self.mock('node_is_one', return_value=False)
        verify = self.mock('verify_fuel_gauge_once', side_effect=[OSError(errno.EAGAIN, 'busy'), True])
        self.assertTrue(auth.verify_fuel_gauge(str(self.root), False))
        self.assertEqual(verify.call_count, 2)
        self.assertAlmostEqual(self.clock.now, 0.2)
        self.assertEqual(self.commands, [])

    def test_cell_retries_stop_after_ten_attempts(self):
        self.mock('node_is_one', return_value=False)
        verify = self.mock('verify_fuel_gauge_once', side_effect=OSError(errno.ETIMEDOUT, 'timeout'))
        with self.assertRaisesRegex(RuntimeError, 'after 10 attempts'):
            auth.verify_fuel_gauge(str(self.root), False)
        self.assertEqual(verify.call_count, 10)
        self.assertAlmostEqual(self.clock.now, 1.8)
        self.assertEqual(self.commands, [])

    def test_cell_permission_error_is_not_retried(self):
        self.mock('node_is_one', side_effect=PermissionError(errno.EACCES, 'denied'))
        verify = self.mock('verify_fuel_gauge_once')
        with self.assertRaises(PermissionError):
            auth.verify_fuel_gauge(str(self.root), False)
        verify.assert_not_called()

    def test_stale_gate_is_cleared_when_cells_fail(self):
        self.put('pd_verifed', '1')
        self.cells.side_effect = RuntimeError('cell authentication failed')
        with self.assertRaisesRegex(RuntimeError, 'cell authentication failed'):
            self.run_main()
        self.role.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_waits_for_both_snk_ready_and_late_pdo(self):
        self.put('pdo2', '00000000')
        self.put('current_state', 'SNK_Startup')
        def progress():
            if self.clock.now >= 0.3:
                self.put('pdo2', '0001912c')
            if self.clock.now >= 0.6:
                self.put('current_state', 'SNK_Ready')
        self.clock.on_sleep = progress
        self.run_main()
        self.assertGreaterEqual(self.clock.now, 0.6)
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])

    def test_unready_wait_is_bounded_and_late_capability_event_can_retry(self):
        self.put('pdo2', '00000000')
        for attempt in range(auth.READINESS_ATTEMPTS):
            with self.assertRaisesRegex(auth.AdapterReadinessTimeout, 'capabilities not ready'):
                self.run_main()
            self.assertEqual(Path(auth.PENDING_EVENT_FILE).exists(),
                             attempt + 1 < auth.READINESS_ATTEMPTS)
        self.assertLess(self.clock.now, 41)
        self.assertEqual(self.start.call_count, auth.READINESS_ATTEMPTS - 1)
        elapsed = self.clock.now
        self.run_main()
        self.assertEqual(self.clock.now, elapsed)
        self.role.assert_not_called()
        self.put('pdo2', '0001912c')
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])

    def test_transient_read_failure_can_recover_before_commands(self):
        original = auth.read_node
        reads = 0
        def read(root, name):
            nonlocal reads
            if name == 'current_state':
                reads += 1
                if reads == 2:
                    raise OSError(errno.EAGAIN, 'transient')
            return original(root, name)
        self.mock('read_node', side_effect=read)
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])

    def test_readiness_timeout_cannot_cache_the_previous_healthy_snapshot(self):
        original = auth.read_node
        reads = 0
        def read(root, name):
            nonlocal reads
            if name == 'current_state':
                reads += 1
                if reads > 1:
                    raise OSError(errno.ETIMEDOUT, 'transport unavailable')
            return original(root, name)
        with patch.object(auth, 'read_node', side_effect=read):
            with self.assertRaises(auth.AdapterReadinessTimeout):
                self.run_main()
        self.assertIsNone(auth.read_state_file(auth.ATTEMPT_STATE_FILE))
        self.assertIsNone(auth.readiness_retry_state()['key'][1])
        self.assertTrue(Path(auth.PENDING_EVENT_FILE).exists())
        # The queued retry is enough; no new charger notification is needed.
        self.run_main()
        self.assertFalse(Path(auth.PENDING_EVENT_FILE).exists())
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_read_errors_exhaust_only_automatic_waits_and_recovery_still_works(self):
        original = auth.read_node
        def read(root, name):
            if name == 'current_state':
                raise OSError(errno.EIO, 'offline transport')
            return original(root, name)
        with patch.object(auth, 'read_node', side_effect=read):
            for _ in range(auth.READINESS_ATTEMPTS):
                with self.assertRaises(auth.AdapterReadinessTimeout):
                    self.run_main()
            elapsed = self.clock.now
            self.run_main()
            self.assertEqual(self.clock.now, elapsed)
        self.assertFalse(Path(auth.PENDING_EVENT_FILE).exists())
        self.assertEqual(self.commands, [])
        self.assertEqual(self.start.call_count, auth.READINESS_ATTEMPTS - 1)
        self.run_main()
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_old_readiness_cache_is_ignored_after_upgrade(self):
        Path(auth.READY_STATE_FILE).write_text(auth.json.dumps(
            ['partner-1', 'PD_PPS', 'SNK_Ready', 0x2717, '0001912c']))
        self.run_main()
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_new_partner_does_not_inherit_exhausted_readiness_budget(self):
        self.put('pdo2', '00000000')
        for _ in range(auth.READINESS_ATTEMPTS):
            with self.assertRaises(auth.AdapterReadinessTimeout):
                self.run_main()
        self.partner.return_value = 'partner-2'
        with self.assertRaises(auth.AdapterReadinessTimeout):
            self.run_main()
        self.assertEqual(auth.readiness_retry_state()['attempts'], 1)
        self.assertTrue(Path(auth.PENDING_EVENT_FILE).exists())

    def test_readiness_permission_error_does_not_queue_a_retry(self):
        original = auth.read_node
        def read(root, name):
            if name == 'current_state':
                raise PermissionError(errno.EACCES, 'denied')
            return original(root, name)
        with patch.object(auth, 'read_node', side_effect=read):
            with self.assertRaises(PermissionError):
                self.run_main()
        self.start.assert_not_called()
        self.role.assert_not_called()

    def test_slow_connection_read_cannot_make_retry_delay_negative(self):
        self.put('pdo2', '00000000')
        with self.assertRaises(auth.AdapterReadinessTimeout):
            self.run_main()
        original = auth.require_current_attach
        checks = 0
        def slow_check():
            nonlocal checks
            original()
            checks += 1
            if checks == 4:  # Inside the retry delay, after its deadline check.
                self.clock.now += 3
        with patch.object(auth, 'require_current_attach', side_effect=slow_check):
            with self.assertRaises(auth.AdapterReadinessTimeout):
                self.run_main()
        self.assertEqual(auth.readiness_retry_state()['attempts'], 2)

    def test_role_refusal_stops_before_vdm_and_is_not_repeated(self):
        self.role.side_effect = None
        self.role.return_value = (1, 'refused')
        self.run_main()
        self.run_main()
        self.role.assert_called_once()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_transient_role_errors_retry_before_any_vdm(self):
        attempts = 0
        def swap(port, timeout):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return errno.EAGAIN, 'temporarily not ready'
            return self.take_host(port, timeout)
        self.role.side_effect = swap
        self.run_main()
        self.assertEqual(self.role.call_count, 3)
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_role_retries_are_bounded_across_repeated_events(self):
        self.role.side_effect = None
        self.role.return_value = (errno.EBUSY, 'busy')
        self.run_main()
        self.run_main()
        self.assertEqual(self.role.call_count, 4)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('verify_process'), '0')

    def test_blocked_role_store_has_an_overall_deadline(self):
        def blocked(_port, timeout):
            self.clock.now += timeout
            return errno.ETIMEDOUT, 'store timed out'
        self.role.side_effect = blocked
        self.run_main()
        self.assertLessEqual(self.clock.now, 12.1)
        self.assertEqual(self.role.call_count, 2)
        self.assertEqual(self.commands, [])

    def test_role_timeout_after_success_does_not_repeat_the_swap(self):
        def swap(port, timeout):
            self.take_host(port, timeout)
            return errno.ETIMEDOUT, 'child timed out after swap'
        self.role.side_effect = swap
        self.run_main()
        self.role.assert_called_once()
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_delayed_host_role_is_observed_before_retry(self):
        self.role.side_effect = None
        self.role.return_value = (0, '')
        self.clock.on_sleep = lambda: self.put('data_role', '[host] device') if self.clock.now >= 0.25 else None
        self.run_main()
        self.role.assert_called_once()
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_role_helper_preserves_errno(self):
        code, _ = request_data_role(str(self.root / 'missing'), 'host', 2)
        self.assertEqual(code, errno.ENOENT)

    def test_unknown_or_non_xiaomi_source_does_not_swap_or_send_vdm(self):
        self.usb_data.return_value = None
        for svid in ('0000', '1234'):
            self.put('adapter_svid', svid)
            self.put('pd_verifed', '1')
            self.run_main()
            self.assertEqual(self.get('data_role'), 'host [device]')
            self.assertEqual(self.get('pd_verifed'), '0')
        self.role.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_power_only_pps_discovers_and_authenticates_without_opt_in(self):
        self.put('adapter_svid', '0000')
        def role(*args):
            result = self.take_host(*args)
            self.clock.on_sleep = lambda: self.put('adapter_svid', '2717')
            return result
        self.role.side_effect = role
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_usb_data_source_blocks_even_cached_xiaomi_identity_and_explicit_probe(self):
        self.usb_data.return_value = True
        self.run_main('--probe-unknown-pps')
        self.role.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('data_role'), 'host [device]')

    def test_missing_source_capability_blocks_stale_known_svid(self):
        self.usb_data.return_value = None
        self.run_main()
        self.role.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_late_source_pdo_is_awaited_before_unknown_probe(self):
        self.put('adapter_svid', '0000')
        self.usb_data.side_effect = lambda: False if self.clock.now >= 0.5 else None
        def role(*args):
            result = self.take_host(*args)
            self.put('adapter_svid', '2717')
            return result
        self.role.side_effect = role
        self.run_main()
        self.assertGreaterEqual(self.clock.now, 0.5)
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_non_xiaomi_power_source_restores_previous_device_role(self):
        self.put('adapter_svid', '0000')
        self.run_main()
        self.restore_role.assert_called_once_with(str(self.root), 'device', 8)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_cached_identity_disappearing_after_swap_does_not_send_vdm(self):
        def role(*args):
            result = self.take_host(*args)
            self.put('adapter_svid', '0000')
            return result
        self.role.side_effect = role
        self.run_main()
        self.restore_role.assert_called_once_with(str(self.root), 'device', 8)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_identity_rechecked_at_handshake_entry(self):
        self.put('adapter_svid', '1234')
        with self.assertRaisesRegex(auth.UnsupportedChargerError, 'identity missing'):
            auth.authenticate_adapter(str(self.root), types.SimpleNamespace(seed_index=0), 'partner-1')
        self.assertEqual(self.commands, [])

    def test_source_pdo_parser_rejects_missing_malformed_and_non_first_fixed_supply(self):
        partner = self.root / 'partner'
        fixed = partner / 'usb_power_delivery/source-capabilities/1:fixed_supply'
        fixed.mkdir(parents=True)
        with patch.object(auth.glob, 'glob', return_value=[str(partner)]):
            self.assertIsNone(partner_source_usb_data())
            for voltage, current, data, expected in (
                    ('5000mV', '3000mA', '0', False),
                    ('5000mV', '3000mA', '1', True),
                    ('9000mV', '3000mA', '0', None),
                    ('5000mV', '0mA', '0', None),
                    ('5000mV', 'broken', '0', None),
                    ('5000mV', '3000mA', 'invalid', None)):
                with self.subTest(voltage=voltage, current=current, data=data):
                    for name, value in [('voltage', voltage), ('maximum_current', current),
                                        ('usb_communication_capable', data)]:
                        (fixed / name).write_text(value)
                    self.assertIs(partner_source_usb_data(), expected)

    def test_explicit_pps_probe_discovers_identity_only_after_role_swap(self):
        self.put('adapter_svid', '0000')
        def role(*args):
            result = self.take_host(*args)
            self.clock.on_sleep = lambda: self.put('adapter_svid', '2717')
            return result
        self.role.side_effect = role
        self.run_main('--probe-unknown-pps')
        self.assertEqual(self.get('pd_verifed'), '1')
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])

    def test_explicit_probe_cannot_swap_plain_pd_computer(self):
        self.put('adapter_svid', '0000')
        self.put('real_type', 'PD')
        self.run_main('--probe-unknown-pps')
        self.role.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_failed_probe_does_not_send_vdm(self):
        self.put('adapter_svid', '0000')
        self.run_main('--probe-unknown-pps')
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_response_timeout_completes_with_failure_and_is_not_replayed_or_bypassed(self):
        self.silent = True
        self.put('pd_verifed', '1')
        policy = self.mock('unverified_adapter_allowed', return_value=(True, 'fixture'))
        with self.assertRaisesRegex(auth.UnsupportedChargerError, 'timed out'):
            self.run_main('--allow-unverified-adapter')
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.payloads[-2:], [(7, '00000000'), (6, '00000000')])
        self.assertLess(self.clock.now, 10.4)
        self.run_main()
        self.put('pdo2', '0002d12c')
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        policy.assert_not_called()
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_seed_and_digest_may_take_longer_than_old_four_second_limit(self):
        self.delays = {4: 4.5, 5: 4.5}
        self.run_main()
        self.assertGreaterEqual(self.clock.now, 9)
        self.assertLess(self.clock.now, 9.2)
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_short_command_response_uses_ten_millisecond_polling(self):
        self.delays = {1: 0.025}
        self.run_main()
        self.assertAlmostEqual(self.clock.now, 0.1)
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_missing_info_or_seed_state_does_not_resend_or_prevent_valid_digest(self):
        self.silent_commands = {1, 4}
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertGreaterEqual(self.clock.now, 5)
        self.assertLess(self.clock.now, 5.2)
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_missing_final_states_reuses_only_this_attach_digest_and_gate(self):
        self.silent_commands = {7, 6}
        self.run_main()
        self.assertTrue(self.get('request_vdm_cmd').startswith('5,'))
        self.assertEqual(self.get('pd_verifed'), '1')
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])

    def test_missing_digest_sends_failure_completion_and_never_uses_pps_bypass(self):
        self.silent_commands = {5}
        bypass = self.mock('unverified_adapter_allowed', return_value=(True, 'fixture'))
        with self.assertRaisesRegex(auth.UnsupportedChargerError, 'digest response timed out'):
            self.run_main('--allow-unverified-adapter')
        self.assertEqual(self.payloads[-2:], [(7, '00000000'), (6, '00000000')])
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(self.get('verify_process'), '0')
        bypass.assert_not_called()

    def test_malformed_digest_is_not_a_completed_mismatch_for_pps_bypass(self):
        def write(root, name, value):
            self.charger_write(root, name, value)
            if name == 'request_vdm_cmd' and value.startswith('5,'):
                self.put(name, '5,Null')
        self.write.side_effect = write
        bypass = self.mock('unverified_adapter_allowed', return_value=(True, 'fixture'))
        with self.assertRaisesRegex(auth.UnsupportedChargerError, 'malformed'):
            self.run_main('--allow-unverified-adapter')
        self.assertEqual(self.payloads[-2:], [(7, '00000000'), (6, '00000000')])
        self.assertEqual(self.get('pd_verifed'), '0')
        bypass.assert_not_called()

    def test_glink_write_error_stops_without_retry_or_completion(self):
        def write(root, name, value):
            if name == 'request_vdm_cmd' and value.startswith('4,'):
                raise OSError(errno.ETIMEDOUT, 'GLINK write timed out')
            return self.charger_write(root, name, value)
        self.write.side_effect = write
        with self.assertRaises(OSError):
            self.run_main()
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3])
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(self.get('verify_process'), '0')

    def test_write_eagain_is_not_the_local_response_timeout(self):
        def write(root, name, value):
            if name == 'request_vdm_cmd':
                raise OSError(errno.EAGAIN, 'write not accepted')
            return self.charger_write(root, name, value)
        self.write.side_effect = write
        with self.assertRaises(OSError):
            self.run_main()
        self.run_main()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(self.get('verify_process'), '0')

    def test_glink_read_timeout_is_distinct_from_response_poll_timeout(self):
        def read(root, name):
            if name == 'request_vdm_cmd' and self.commands:
                raise OSError(errno.ETIMEDOUT, 'GLINK read timed out')
            return self.charger_read(root, name)
        self.mock('read_node', side_effect=read)
        with self.assertRaises(OSError):
            self.run_main()
        self.assertEqual(self.commands, [1])
        self.assertEqual(self.get('verify_process'), '0')
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_transient_response_read_recovers_without_another_write(self):
        failures = 0
        def read(root, name):
            nonlocal failures
            if name == 'request_vdm_cmd' and self.commands == [1] and failures < 2:
                failures += 1
                raise OSError(errno.EAGAIN, 'temporarily unavailable')
            return self.charger_read(root, name)
        self.mock('read_node', side_effect=read)
        self.run_main()
        self.assertEqual(failures, 2)
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_initial_vdm_read_uses_connection_grace_before_any_command(self):
        failures = 0
        def read(root, name):
            nonlocal failures
            if name == 'request_vdm_cmd' and not self.commands and failures < 3:
                failures += 1
                raise OSError(errno.EAGAIN, 'not yet readable')
            return self.charger_read(root, name)
        self.mock('read_node', side_effect=read)
        self.run_main()
        self.assertEqual(failures, 3)
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_slow_glink_reads_do_not_multiply_the_response_budget(self):
        self.silent_commands = {4}
        reads = 0
        def read(root, name):
            nonlocal reads
            if name == 'request_vdm_cmd' and self.commands and self.commands[-1] == 4:
                reads += 1
                self.clock.now += 1
            return self.charger_read(root, name)
        self.mock('read_node', side_effect=read)
        self.run_main()
        self.assertLessEqual(reads, 6)  # Five response reads plus command 5 preflight.
        self.assertLess(self.clock.now, 6.2)
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_precommand_disconnected_state_has_bounded_grace_and_sends_nothing(self):
        self.put('request_vdm_cmd', '0,Null')
        self.put('data_role', '[host] device')
        with self.assertRaisesRegex(auth.UnsupportedChargerError, 'connection not ready'):
            self.run_main()
        self.assertLessEqual(self.clock.now, 0.41)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('verify_process'), '0')

    def test_firmware_disconnect_stops_even_before_usb_offline_event(self):
        def write(root, name, value):
            self.charger_write(root, name, value)
            if name == 'request_vdm_cmd':
                self.put(name, '0,Null')
        self.write.side_effect = write
        with self.assertRaises(auth.ChargerDisconnectedError):
            self.run_main()
        self.assertEqual(self.commands, [1])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_identity_change_between_commands_aborts_the_exchange(self):
        def write(root, name, value):
            self.charger_write(root, name, value)
            if name == 'request_vdm_cmd' and value.startswith('3,'):
                self.put('adapter_svid', '1234')
        self.write.side_effect = write
        with self.assertRaisesRegex(auth.UnsupportedChargerError, 'identity changed'):
            self.run_main()
        self.assertEqual(self.commands, [1, 2, 3])
        self.assertEqual(self.get('verify_process'), '0')

    def test_interrupted_exchange_clears_both_firmware_flags(self):
        def write(root, name, value):
            self.charger_write(root, name, value)
            if name == 'request_vdm_cmd' and value.startswith('5,'):
                raise SystemExit(143)
        self.write.side_effect = write
        with self.assertRaises(SystemExit):
            self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5])
        self.assertEqual(self.get('verify_process'), '0')
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_failed_attach_does_not_block_a_new_connection_for_120_seconds(self):
        self.silent = True
        with self.assertRaises(auth.UnsupportedChargerError):
            self.run_main()
        self.run_main('--clear-notification')
        self.partner.return_value = 'partner-2'
        self.silent = False
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6] * 2)
        self.assertLess(self.clock.now, 11)
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_current_attach_success_is_reused_but_stale_state_is_not(self):
        self.run_main()
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.partner.return_value = 'partner-2'
        self.put('request_vdm_cmd', '0,Null')
        self.put('data_role', 'host [device]')
        self.commands.clear()
        self.run_main()
        self.assertEqual(self.role.call_count, 2)
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')


    def test_firmware_gate_reset_does_not_restart_same_attach(self):
        self.run_main()
        self.put('pd_verifed', '0')
        self.put('request_vdm_cmd', '0,Null')
        self.run_main()
        self.assertEqual(len(self.commands), 7)
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_completed_attach_read_failures_preserve_proof_without_writes_or_replay(self):
        self.run_main()
        for name in ('pd_verifed', 'verify_process', 'request_vdm_cmd'):
            for error in (errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT, errno.EIO):
                with self.subTest(name=name, error=error):
                    self.write.reset_mock()
                    self.notify.reset_mock()
                    def read(root, node):
                        if node == name:
                            raise OSError(error, 'injected read failure')
                        return self.charger_read(root, node)
                    with patch.object(auth, 'read_node', side_effect=read):
                        self.run_main()
                    self.write.assert_not_called()
                    self.notify.assert_not_called()
                    self.assertEqual(self.get('pd_verifed'), '1')
                    self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'partner-1')
                    self.assertEqual(auth.read_state_file(auth.AUTHENTICATED_STATE_FILE), 'partner-1')
                    self.run_main()
                    self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
                    self.assertEqual(self.get('pd_verifed'), '1')

    def test_completed_attach_unknown_online_preserves_but_offline_clears(self):
        self.run_main()
        self.write.reset_mock()
        self.online.return_value = None
        self.run_main()
        self.write.assert_not_called()
        self.assertEqual(self.get('pd_verifed'), '1')
        self.online.return_value = False
        with self.assertRaises(auth.ChargerDisconnectedError):
            self.run_main()
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'none')
        self.assertEqual(len(self.commands), 7)

    def test_unproven_attach_unknown_online_never_authenticates(self):
        self.put('pd_verifed', '1')
        self.online.return_value = None
        with self.assertRaisesRegex(OSError, 'USB power status is unreadable'):
            self.run_main()
        self.cells.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_usb_online_reader_distinguishes_unavailable_from_zero(self):
        self.mock('find_power_supply', return_value=str(self.root))
        for value, expected in (('0', False), ('1', True), ('invalid', None)):
            self.put('online', value)
            self.assertIs(usb_is_online(), expected)
        (self.root / 'online').unlink()
        self.assertIsNone(usb_is_online())
        with patch.object(auth, 'read_node', side_effect=OSError(errno.EAGAIN, 'unavailable')):
            self.assertIsNone(usb_is_online())
        with patch.object(auth, 'find_power_supply', return_value=None):
            self.assertIsNone(usb_is_online())

    def test_definite_firmware_disconnect_overrides_unreadable_gate(self):
        self.run_main()
        self.put('request_vdm_cmd', '0,Null')
        def read(root, name):
            if name == 'pd_verifed':
                raise OSError(errno.ETIMEDOUT, 'unavailable')
            return self.charger_read(root, name)
        with patch.object(auth, 'read_node', side_effect=read):
            self.run_main()
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'none')
        self.assertEqual(len(self.commands), 7)

    def test_completed_malformed_status_defers_without_claiming_new_success(self):
        self.run_main()
        self.write.reset_mock()
        self.notify.reset_mock()
        self.put('request_vdm_cmd', 'invalid')
        self.run_main()
        self.write.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(self.get('pd_verifed'), '1')
        self.assertEqual(len(self.commands), 7)

    def test_status_read_timeout_after_commit_does_not_undo_authentication(self):
        self.mock('print_status', side_effect=print_status)
        seen = []
        def read(root, name):
            if name == 'power_max':
                seen.append(auth.read_state_file(auth.VERIFIED_STATE_FILE))
                raise OSError(errno.ETIMEDOUT, 'diagnostic read timed out')
            return self.charger_read(root, name)
        with patch.object(auth, 'read_node', side_effect=read):
            self.run_main()
        self.assertEqual(seen, ['partner-1'])
        self.assertEqual(self.get('pd_verifed'), '1')
        gate_writes = [c.args[2] for c in self.write.call_args_list if c.args[1] == 'pd_verifed']
        self.assertEqual(gate_writes, ['1'])
        self.run_main()
        self.assertEqual(len(self.commands), 7)

    def test_notification_failure_after_commit_or_reuse_preserves_gate(self):
        self.notify.side_effect = OSError(errno.EIO, 'notification unavailable')
        self.run_main()
        self.run_main()
        self.assertEqual(self.get('pd_verifed'), '1')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'partner-1')
        self.assertEqual(len(self.commands), 7)

    def test_interruption_during_reporting_preserves_committed_verdict(self):
        self.mock('print_status', side_effect=SystemExit(143))
        with self.assertRaises(SystemExit):
            self.run_main()
        self.assertEqual(self.get('pd_verifed'), '1')
        self.assertEqual(self.get('verify_process'), '0')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'partner-1')
        self.run_main()
        self.assertEqual(len(self.commands), 7)

    def test_detach_during_reporting_clears_committed_verdict(self):
        self.mock('print_status', side_effect=lambda _root: setattr(self.online, 'return_value', False))
        self.run_main()
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'none')

    def test_partner_change_during_reporting_clears_committed_verdict(self):
        self.mock('print_status', side_effect=lambda _root: setattr(self.partner, 'return_value', 'partner-2'))
        self.run_main()
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'none')

    def test_offline_udev_during_reporting_cannot_be_overwritten_by_old_worker(self):
        def offline_event(_root):
            # udev can clear proof before the worker sees the partner removal.
            self.run_main('--clear-notification')
        self.mock('print_status', side_effect=offline_event)
        self.run_main()
        self.assertEqual(self.get('pd_verifed'), '0')
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'none')
        self.assertEqual(auth.read_state_file(auth.AUTHENTICATED_STATE_FILE), 'none')

    def test_vdm_six_and_gate_one_without_attach_proof_are_not_success(self):
        self.put('request_vdm_cmd', '6,Null')
        self.put('pd_verifed', '1')
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])

    def test_each_detach_point_clears_gate_and_stops_commands(self):
        for cmd in (1, 2, 3, 4, 5, 7, 6):
            with self.subTest(cmd=cmd):
                self.commands.clear()
                self.put('data_role', 'host [device]')
                self.online.return_value = True
                self.disconnect_at = cmd
                with self.assertRaises(auth.ChargerDisconnectedError):
                    self.run_main('--force')
                expected = [1, 2, 3, 4, 5, 7, 6]
                self.assertEqual(self.commands, expected[:expected.index(cmd) + 1])
                self.assertEqual(self.get('pd_verifed'), '0')

    def test_partner_change_during_cells_prevents_role_request(self):
        self.cells.side_effect = lambda _root: setattr(self.partner, 'return_value', 'partner-2')
        with self.assertRaises(auth.ChargerDisconnectedError):
            self.run_main()
        self.role.assert_not_called()

    def test_missing_partner_clears_old_verdict(self):
        self.partner.return_value = None
        self.put('pd_verifed', '1')
        with self.assertRaises(auth.ChargerDisconnectedError):
            self.run_main()
        self.role.assert_not_called()
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_adapter_digest_mismatch_keeps_gate_closed(self):
        self.digest_ok = False
        self.run_main()
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_permission_failure_is_not_hidden_or_retried(self):
        self.role.side_effect = PermissionError(errno.EACCES, 'denied')
        with self.assertRaises(PermissionError):
            self.run_main()
        self.run_main()
        self.role.assert_called_once()
        self.assertEqual(self.get('pd_verifed'), '0')

    def test_vendor_and_offline_udev_paths_do_no_charger_io(self):
        Path(auth.USB_ONLINE_STATE_FILE).write_text('1')
        read = self.mock('read_node')
        self.run_main('--xm-property-trigger')
        self.run_main('--clear-notification')
        self.assertEqual(self.start.call_count, 2)
        read.assert_not_called()
        self.write.assert_not_called()
        self.cells.assert_not_called()

    def test_typec_event_is_queued_even_before_usb_online(self):
        read = self.mock('read_node')
        self.run_main('--typec-trigger')
        self.assertTrue(Path(auth.PENDING_EVENT_FILE).exists())
        self.start.assert_called_once_with(auth.SYSTEMD_PATH_UNIT)
        read.assert_not_called()
        self.write.assert_not_called()

    def test_event_during_auth_is_preserved_and_does_not_replay_handshake(self):
        auth.queue_auth_event()
        def cells(_root):
            self.assertFalse(Path(auth.PENDING_EVENT_FILE).exists())
            auth.queue_auth_event()
        self.cells.side_effect = cells
        self.run_main()
        self.assertTrue(Path(auth.PENDING_EVENT_FILE).exists())
        self.run_main()
        self.assertFalse(Path(auth.PENDING_EVENT_FILE).exists())
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(self.get('pd_verifed'), '1')

    def test_new_attach_during_worker_keeps_a_pending_retry(self):
        def replace(_root):
            self.partner.return_value = 'partner-2'
            auth.queue_auth_event()
        self.cells.side_effect = replace
        with self.assertRaises(auth.ChargerDisconnectedError):
            self.run_main()
        self.assertTrue(Path(auth.PENDING_EVENT_FILE).exists())
        self.assertEqual(self.commands, [])
        self.cells.side_effect = None
        self.run_main()
        self.assertFalse(Path(auth.PENDING_EVENT_FILE).exists())
        self.assertEqual(self.commands, [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(auth.read_state_file(auth.VERIFIED_STATE_FILE), 'partner-2')

    def test_missing_interfaces_consume_event_without_activation_loop(self):
        auth.queue_auth_event()
        self.mock('find_xiaomi_dir', side_effect=FileNotFoundError('not ready'))
        with patch.object(sys, 'argv', ['mipps', '--consume-event']):
            with self.assertRaisesRegex(auth.UnsupportedChargerError, 'interfaces not ready'):
                auth.main()
        self.assertFalse(Path(auth.PENDING_EVENT_FILE).exists())
        self.assertEqual(self.commands, [])

    def test_unknown_source_does_not_generate_gate_write_event_loop(self):
        self.usb_data.return_value = None
        self.put('adapter_svid', '0000')
        self.run_main()
        self.run_main()
        self.write.assert_not_called()

    def test_authentication_without_fastchg_does_not_announce_mipps(self):
        self.mock('current_battery_capacity', return_value=50)
        self.put('real_type', 'PD')
        self.assertEqual(auth.current_charge_profile(str(self.root), True), ('PD', 67))
        self.put('fastchg_mode', '1')
        self.assertEqual(auth.current_charge_profile(str(self.root), True), ('MiPPS', 67))
        self.assertIn('maximum', auth.charge_mode_body('MiPPS', 67))

    def test_notification_requires_local_digest_proof(self):
        self.put('pd_verifed', '1')
        notify = self.mock('notify_charge_profile')
        notify_completed_charge(str(self.root), 'fixture')
        self.assertFalse(notify.call_args.args[1])
        Path(auth.AUTHENTICATED_STATE_FILE).write_text('partner-1')
        notify_completed_charge(str(self.root), 'fixture')
        self.assertTrue(notify.call_args.args[1])
        self.partner.return_value = 'partner-2'
        notify_completed_charge(str(self.root), 'fixture')
        self.assertFalse(notify.call_args.args[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
