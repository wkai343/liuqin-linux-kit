#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise persistent inotify/systemd event handling with a fake charger."""
from pathlib import Path
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def worker(directory, phase):
    # The charger fixture replaces time.sleep/monotonic. Keep real clocks for
    # synchronization with the controller and bound every wait independently.
    real_sleep, real_now = time.sleep, time.monotonic
    directory = Path(directory)
    source = PROJECT / 'tests/mipps-startup.py'
    fixture = types.ModuleType('mipps_event_fixture')
    fixture.__file__ = str(source)
    exec(compile(source.read_text(), str(source), 'exec'), fixture.__dict__)
    case = fixture.Tests('test_android_sequence_after_cells_and_host')
    case.setUp()
    try:
        fixture.auth.PENDING_EVENT_FILE = str(directory / 'pending')
        case.partner.side_effect = lambda: (directory / 'partner').read_text()
        def barrier():
            (directory / 'ready').touch()
            deadline = real_now() + 15
            while not (directory / 'release').exists():
                if real_now() >= deadline:
                    raise TimeoutError('event test controller did not release worker')
                real_sleep(0.02)
        fixture.auth.AUTH_LOCK_FILE = str(directory / 'auth.lock')
        calls = 0
        def process():
            nonlocal calls
            first = calls == 0
            calls += 1
            case.cells.side_effect = (lambda _root: barrier()) if first and phase == 'during-auth' else None
            offset = len(case.commands)
            result = {'start_partner': case.partner(), 'pid': os.getpid()}
            try:
                case.run_main()
            except fixture.auth.ChargerDisconnectedError as exc:
                result['cancelled'] = str(exc)
            result.update(commands=case.commands[offset:], gate=case.get('pd_verifed'))
            with (directory / 'results').open('a') as stream:
                stream.write(json.dumps(result) + '\n')
            if first and phase == 'after-return':
                # An event at the old oneshot exit boundary must also survive
                # the resident worker returning from a transaction.
                barrier()
        fixture.auth.watch_auth_events(process)
    finally:
        case.doCleanups()


class EventTests(unittest.TestCase):
    def setUp(self):
        try:
            probe = subprocess.run(['systemctl', '--user', 'show-environment'],
                                   capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f'user systemd unavailable: {exc}')
        if probe.returncode:
            self.skipTest('user systemd manager unavailable')
        self.temp = tempfile.TemporaryDirectory(prefix='liuqin-mipps-events-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.unit = 'liuqin-mipps-events-' + uuid.uuid4().hex
        self.addCleanup(self.stop)

    def stop(self):
        subprocess.run(['systemctl', '--user', 'stop', self.unit + '.path',
                        self.unit + '.service'], capture_output=True, timeout=10)

    def ctl(self, *args):
        return subprocess.run(['systemctl', '--user', *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('timed out waiting for real systemd worker')
            time.sleep(0.02)

    def results(self):
        path = self.directory / 'results'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def exercise(self, phase):
        (self.directory / 'partner').write_text('partner-1')
        (self.directory / 'pending').touch()
        # Use the production watcher's activation and rate-limit policy.
        path_unit = PROJECT / 'device/board-overlay/etc/systemd/system/liuqin-mipps-auth.path'
        properties = []
        for line in path_unit.read_text().splitlines():
            if line.startswith('PathExists='):
                properties.append('--path-property=PathExists=' + str(self.directory / 'pending'))
            elif line.startswith('TriggerLimitIntervalSec='):
                properties.append('--path-property=' + line)
        subprocess.run(['systemd-run', '--user', '--quiet', '--unit=' + self.unit,
                        '--property=Type=simple', '--property=StartLimitIntervalSec=0',
                        *properties, sys.executable, str(Path(__file__).resolve()),
                        '--worker', str(self.directory), phase], check=True,
                       capture_output=True, text=True, timeout=10)
        self.wait_for(lambda: (self.directory / 'ready').exists())
        self.assertFalse((self.directory / 'pending').exists())
        with (self.directory / 'auth.lock').open('a') as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (self.directory / 'partner').write_text('partner-2')
        (self.directory / 'pending').touch()
        for _ in range(3):
            self.ctl('--no-block', 'start', self.unit + '.service')
        (self.directory / 'release').touch()
        self.wait_for(lambda: len(self.results()) == 2)
        self.assertEqual(self.ctl('show', '--property=ActiveState', '--value', self.unit + '.service'), 'active')
        self.assertFalse((self.directory / 'pending').exists())
        first, second = self.results()
        self.assertEqual(first['start_partner'], 'partner-1')
        self.assertEqual(second['start_partner'], 'partner-2')
        self.assertEqual(second['commands'], [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(second['gate'], '1')
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual(int(self.ctl('show', '--property=MainPID', '--value', self.unit + '.service')), first['pid'])
        # The resident process must release the authentication lock while idle.
        with (self.directory / 'auth.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        time.sleep(0.1)
        self.assertEqual(len(self.results()), 2)
        return first

    def test_new_attach_during_auth_is_processed_by_the_same_worker(self):
        first = self.exercise('during-auth')
        self.assertEqual(first['cancelled'], 'Type-C partner changed')
        self.assertEqual(first['commands'], [])
        self.assertEqual(first['gate'], '0')

    def test_event_after_entry_point_returns_is_not_lost(self):
        first = self.exercise('after-return')
        self.assertEqual(first['commands'], [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual(first['gate'], '1')

    def test_repeated_events_reuse_the_worker_and_never_replay_authentication(self):
        self.exercise('after-return')
        initial = self.results()
        for _ in range(30):
            (self.directory / 'pending').touch()
        self.wait_for(lambda: len(self.results()) > len(initial))
        self.wait_for(lambda: not (self.directory / 'pending').exists())
        results = self.results()
        self.assertEqual({r['pid'] for r in results}, {initial[0]['pid']})
        self.assertTrue(all(r['commands'] == [] and r['gate'] == '1' for r in results[len(initial):]))
        self.assertEqual(self.ctl('show', '--property=NRestarts', '--value', self.unit + '.service'), '0')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main(verbosity=2)
