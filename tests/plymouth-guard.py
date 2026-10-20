#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise bounded splash IPC, daemon ownership and root-stage ordering."""
import ast
import importlib.util
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ply_guard', PROJECT/'device/boot/splash/plymouth-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = []
        self.event = patch.object(guard, 'event', lambda name, **kw: self.events.append((name, kw)))
        self.event.start()

    def tearDown(self):
        self.event.stop()
        self.temp.cleanup()

    def test_real_client_deadline_and_exit_status(self):
        start = time.monotonic()
        self.assertEqual(guard.call([sys.executable, '-c', 'import time;time.sleep(10)'], .05), 124)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(guard.call([sys.executable, '-c', 'raise SystemExit(7)'], 1), 7)
        self.assertEqual(guard.call([str(self.root/'missing')], 1), 127)

    def test_start_reuses_or_launches_once(self):
        for replies, repaired, expected in [([0], False, 1), ([1,0], False, 2),
                                            ([124], False, 1), ([124,0], True, 2)]:
            with self.subTest(replies=replies, repaired=repaired):
                with patch.object(guard,'call',side_effect=replies) as call, \
                     patch.object(guard,'release_stalled_daemon',return_value=repaired) as release:
                    self.assertEqual(guard.main(['start']), 0)
                    self.assertEqual(call.call_count, expected)
                    self.assertEqual(release.call_count, int(replies[0]==124))

    def test_failed_cosmetic_requests_do_not_block_boot(self):
        for action in ['show','read-write','quit']:
            for result in [0,1,124,127]:
                with self.subTest(action=action, result=result):
                    with patch.object(guard,'call',return_value=result), \
                         patch.object(guard,'release_stalled_daemon',return_value=True) as release:
                        self.assertEqual(guard.main([action]), 0)
                        self.assertEqual(release.call_count, int(result==124))

    def test_wait_expiry_does_not_kill_a_live_daemon(self):
        with patch.object(guard,'call',return_value=124) as call, \
             patch.object(guard,'release_stalled_daemon') as release:
            self.assertEqual(guard.main(['wait']),0)
            call.assert_called_once_with([guard.CLIENT,'--wait'],5)
            release.assert_not_called()

    def test_unsafe_pid_files_and_unrelated_processes_are_not_signalled(self):
        for value in ['1','garbage',str(os.getpid())]:
            pidfile=self.root/'pid';pidfile.write_text(value)
            with patch.object(guard,'PID_FILE',pidfile), \
                 patch.object(guard.signal,'pidfd_send_signal') as send:
                self.assertFalse(guard.release_stalled_daemon('test'))
                send.assert_not_called()
        link=self.root/'link';link.symlink_to(pidfile)
        with patch.object(guard,'PID_FILE',link), patch.object(guard.signal,'pidfd_send_signal') as send:
            self.assertFalse(guard.release_stalled_daemon('test'))
            send.assert_not_called()

    def exercise_owned_daemon(self, selections, identities=None):
        pidfile=self.root/'pid';pidfile.write_text(str(os.getpid()));pidfile.chmod(0o600)
        reader,writer=os.pipe()
        identity=identities or [('/usr/bin/plymouthd','100')]*2
        info=types.SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o600)
        with patch.object(guard,'PID_FILE',pidfile), patch.object(guard.os,'fstat',return_value=info), \
             patch.object(guard,'identity',side_effect=identity), \
             patch.object(guard.os,'pidfd_open',return_value=reader), \
             patch.object(guard.select,'select',side_effect=selections), \
             patch.object(guard.signal,'pidfd_send_signal') as send:
            result=guard.release_stalled_daemon('test')
            signals=[c.args[1] for c in send.call_args_list]
        os.close(writer)
        return result,signals

    def test_checked_pidfd_termination_and_identity_race(self):
        self.assertEqual(self.exercise_owned_daemon([([1],[],[]),([1],[],[])]),
                         (True,[signal.SIGTERM]))
        self.assertEqual(self.exercise_owned_daemon([([],[],[]),([1],[],[])]),
                         (True,[signal.SIGTERM,signal.SIGKILL]))
        self.assertEqual(self.exercise_owned_daemon([], [('/usr/bin/plymouthd','100'),
                                                        ('/usr/bin/plymouthd','101')]),
                         (False,[]))

    def test_installed_payload_and_service_ordering(self):
        tree=ast.parse((PROJECT/'build/scripts/build-fedora-root.py').read_text())
        function=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='boot_splash_files')
        namespace={'PROJECT':PROJECT}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<splash-files>','exec'),namespace)
        files=namespace['boot_splash_files']()
        self.assertEqual(files['usr/libexec/liuqin-plymouth-guard'],(PROJECT/'device/boot/splash/plymouth-guard.py').read_text())
        start=files['etc/systemd/system/plymouth-start.service.d/10-liuqin.conf']
        readwrite=files['etc/systemd/system/plymouth-read-write.service.d/10-liuqin.conf']
        self.assertIn('ExecStartPost=\n',start)
        self.assertIn('plymouth-guard show',start)
        self.assertIn('TimeoutStartSec=20',start)
        self.assertIn('After=plymouth-start.service',readwrite)
        self.assertIn('TimeoutStartSec=10',readwrite)
        self.assertIn('plymouth-guard read-write',readwrite)
        for name, action in [('quit','quit'),('quit-wait','wait')]:
            unit=files['etc/systemd/system/plymouth-'+name+'.service.d/10-liuqin.conf']
            self.assertIn('TimeoutStartSec=8',unit)
            self.assertIn('plymouth-guard '+action,unit)
        self.assertEqual(guard.main(['unknown']),2)


if __name__=='__main__':
    unittest.main()
