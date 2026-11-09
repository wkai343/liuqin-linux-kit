#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Keep per-cell verification independent from the optional adapter handshake."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'device/board-overlay/usr/local/libexec/liuqin-mipps-auth'
auth = types.ModuleType('mipps_startup_test')
exec(compile(SOURCE.read_text(), str(SOURCE), 'exec'), auth.__dict__)


class Tests(unittest.TestCase):
    def test_plain_pd_still_verifies_both_cells_before_skipping_adapter_vdm(self):
        calls = []
        with patch.object(sys, 'argv', ['mipps', '--sysfs', '/fixture', '--no-data-role-swap']), \
             patch.object(auth, 'wait_usb_online', return_value=True), \
             patch.object(auth, 'write_state_file'), \
             patch.object(auth, 'vdm_state', return_value=(0, '')), \
             patch.object(auth, 'auth_already_completed', return_value=False), \
             patch.object(auth, 'wait_real_type', return_value='PD'), \
             patch.object(auth, 'current_adapter_svid', return_value=0), \
             patch.object(auth, 'enable_battery_auth', side_effect=lambda root: calls.append('cells')), \
             patch.object(auth, 'should_skip_pdo2', side_effect=lambda root: calls.append('adapter') or True), \
             patch.object(auth, 'send_vdm') as send, \
             patch.object(auth, 'write_node') as write:
            with self.assertRaises(auth.UnsupportedChargerError):
                auth.main()
            self.assertEqual(calls, ['cells', 'adapter'])
            send.assert_not_called()
            write.assert_not_called()  # no PD gate, voltage or current override

    def test_failed_cell_verification_cannot_continue_to_adapter_or_gate(self):
        with patch.object(sys, 'argv', ['mipps', '--sysfs', '/fixture', '--no-data-role-swap']), \
             patch.object(auth, 'wait_usb_online', return_value=True), \
             patch.object(auth, 'write_state_file'), \
             patch.object(auth, 'vdm_state', return_value=(0, '')), \
             patch.object(auth, 'auth_already_completed', return_value=False), \
             patch.object(auth, 'wait_real_type', return_value='PD'), \
             patch.object(auth, 'current_adapter_svid', return_value=0), \
             patch.object(auth, 'enable_battery_auth', side_effect=RuntimeError('cell authentication failed')), \
             patch.object(auth, 'should_skip_pdo2') as adapter, \
             patch.object(auth, 'send_vdm') as send, \
             patch.object(auth, 'write_node') as write:
            with self.assertRaisesRegex(RuntimeError, 'cell authentication failed'):
                auth.main()
            adapter.assert_not_called()
            send.assert_not_called()
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
