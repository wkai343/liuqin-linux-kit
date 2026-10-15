#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Persistent worker protocol tests using only the hardware-free demo."""
import json
import os
from pathlib import Path
import queue
import signal
import threading
import subprocess
import sys
import tempfile
import time
import unittest

project = Path(__file__).resolve().parents[1]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.process = subprocess.Popen([sys.executable, '-u', str(project / 'installer/install-gui.py'),
                                         '--worker', '--demo', '--state-dir', str(self.root / 'state')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        self.lines = queue.Queue()
        def read_lines():
            while line := self.process.stdout.readline():
                self.lines.put(line)
            self.lines.put(None)
        self.reader = threading.Thread(target=read_lines, daemon=True)
        self.reader.start()
        self.assertEqual(self.read()['type'], 'ready')

    def tearDown(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=10)
        self.assertEqual(self.process.returncode, 0, self.process.stderr.read().decode())
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()
        self.directory.cleanup()

    def send(self, action, **values):
        self.process.stdin.write((json.dumps(dict(action=action, **values)) + '\n').encode())

    def read(self):
        try:
            line = self.lines.get(timeout=10)
        except queue.Empty:
            raise AssertionError('worker event timed out') from None
        if line is None:
            raise AssertionError('worker exited without expected event')
        event = json.loads(line)
        self.assertEqual(event['schema_version'], 2)
        return event

    def job(self, action, **values):
        self.send(action, **values)
        events = []
        while True:
            event = self.read()
            events.append(event)
            if event['type'] == 'activity' and event['busy'] is False:
                return events

    def prepare(self):
        events = self.job('backup', serial='DEMO-LIUQIN', bundle='ignored',
                          backup_parent=str(self.root / 'backups'))
        backup = next(e['backup'] for e in events if e['type'] == 'backup_ready')
        events = self.job('prepare', serial='DEMO-LIUQIN', bundle='ignored',
                          backup_dir=backup['path'])
        event = next(e for e in events if e['type'] == 'plan_ready')
        return dict(session_id=event['session_id'], plan_id=event['plan']['plan_id'],
                    confirmation=dict(serial='DEMO-LIUQIN', personal_data_backed_up=True,
                                      erase_userdata=True, allow_experimental=True))

    def test_invalid_confirmation_preserves_plan_and_cancel_cleans_up(self):
        payload = self.prepare()
        payload['confirmation']['serial'] = 'WRONG'
        events = self.job('install', **payload)
        self.assertTrue(any(e['type'] == 'request_rejected' for e in events))
        self.assertFalse(any(e.get('state') == 'installing' for e in events))
        events = self.job('cancel')
        self.assertTrue(any(e.get('state') == 'cancelled' for e in events))

    def test_backup_can_be_completed_and_reused_without_preparing_install(self):
        arguments=dict(serial='DEMO-LIUQIN', bundle='ignored', backup_parent=str(self.root / 'backups'))
        first=self.job('backup', **arguments)
        backup=next(e['backup'] for e in first if e['type']=='backup_ready')
        self.assertEqual(backup['source'], 'created')
        self.assertFalse(any(e['type']=='plan_ready' for e in first))
        second=self.job('backup', **arguments)
        reused=next(e['backup'] for e in second if e['type']=='backup_ready')
        self.assertEqual(reused['path'], backup['path'])
        self.assertEqual(reused['source'], 'reused')
        self.assertFalse(any(e.get('phase') in ('boot_installer','backup') for e in second))

    def test_duplicate_install_is_rejected_and_eof_does_not_interrupt_write(self):
        payload = self.prepare()
        self.send('install', **payload)
        self.send('install', **payload)
        self.process.stdin.close()
        events = []
        while True:
            event = self.read()
            events.append(event)
            if event['type'] == 'activity' and event['busy'] is False:
                break
        self.process.wait(timeout=10)
        self.assertTrue(any(e['type'] == 'request_rejected' for e in events))
        self.assertEqual(sum(e.get('phase') == 'format_root' for e in events), 1)
        receipt = next(e['receipt'] for e in events if e['type'] == 'installation_written')
        self.assertTrue(receipt['boot_readback_verified'])
        self.assertFalse(receipt['first_boot_verified'])

    @unittest.skipUnless(os.name == 'posix', 'direct SIGINT delivery requires POSIX')
    def test_sigint_during_idle_and_boot_readback_keeps_worker_alive(self):
        self.process.send_signal(signal.SIGINT)
        self.assertTrue(any(e['type'] == 'devices' for e in self.job('devices')))
        payload = self.prepare()
        self.send('install', **payload)
        events = []
        interrupted = False
        while True:
            event = self.read()
            events.append(event)
            if event.get('phase') == 'verify_boot' and not interrupted:
                self.process.send_signal(signal.SIGINT)
                interrupted = True
            if event['type'] == 'activity' and event['busy'] is False:
                break
        self.assertTrue(interrupted)
        self.assertIsNone(self.process.poll())
        self.assertFalse(any(e['type'] == 'error' for e in events))
        receipt = next(e['receipt'] for e in events if e['type'] == 'installation_written')
        self.assertTrue(receipt['boot_readback_verified'])
        self.assertTrue(receipt['active_slot_set'])
        self.assertTrue(any(e['type'] == 'devices' for e in self.job('devices')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
