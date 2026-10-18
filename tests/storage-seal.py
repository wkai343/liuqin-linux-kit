#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fault-inject the early RAM seal without accessing real block devices."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class SealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root/'seal-mock'
        cls.production = cls.root/'seal-production'
        cc = shutil.which('cc') or shutil.which('gcc')
        if not cc:
            raise RuntimeError('C compiler is required')
        for source, binary in [('tests/storage-seal-mock.c', cls.binary),
                               ('device/boot/liuqin-storage-seal.c', cls.production)]:
            extra = [str(PROJECT/'device/boot/liuqin-storage-ready.c')] if binary == cls.production else []
            subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(PROJECT/source), *extra, '-o', str(binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_mock(self, scenario, mode='--seal'):
        return subprocess.run([self.binary, scenario, mode], capture_output=True, text=True)

    def test_seals_and_verifies_every_disk_and_partition(self):
        result = self.run_mock('normal')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '3\n')
        self.assertEqual(re.findall(r'^SET (\w+)$', result.stderr, re.M), ['sda', 'sda1', 'sdb'])
        self.assertEqual(re.findall(r'^GET (\w+)$', result.stderr, re.M), ['sda', 'sda1', 'sdb'])
        self.assertLess(result.stderr.rindex('SET '), result.stderr.index('GET '))

    def test_faults_never_prove_storage_safe(self):
        cases = ['empty', 'glob-error', 'symlink', 'regular', 'open-error', 'replace-open',
                 'sysfs-missing', 'sysfs-mismatch', 'sysfs-changed', 'sysfs-extra', 'sysfs-truncated',
                 'read-error', 'set-error', 'get-error', 'still-writable',
                 'added', 'removed', 'renamed', 'replaced', 'disappeared']
        for case in cases:
            with self.subTest(case=case):
                result = self.run_mock(case)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, '')

    def test_a_failed_node_does_not_leave_later_nodes_unattempted(self):
        for case in ['set-error', 'open-error', 'sysfs-mismatch', 'symlink']:
            with self.subTest(case=case):
                result = self.run_mock(case)
                self.assertIn('SET sdb\n', result.stderr)

    def test_rejects_non_ram_or_non_init_context_before_opening_disks(self):
        for case in ['not-root', 'not-init-child', 'desktop-root', 'wrong-init']:
            with self.subTest(case=case):
                result = self.run_mock(case)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn('OPEN ', result.stderr)
                self.assertNotIn('SET ', result.stderr)

    def test_check_is_read_only_and_does_not_require_the_ram_context(self):
        for case in ['normal', 'not-root', 'not-init-child', 'desktop-root', 'wrong-init']:
            with self.subTest(case=case):
                result = self.run_mock(case, '--check')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('SET ', result.stderr)
        result = self.run_mock('check-writable', '--check')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('SET ', result.stderr)

    def test_production_binary_has_no_test_override_or_unchecked_mode(self):
        # This fails before enumeration: the test is not a child of BusyBox PID 1.
        env = dict(os.environ, LIUQIN_INIT_STORAGE_TEST_ONLY='1', LIUQIN_STORAGE_SEAL_TESTING='1')
        result = subprocess.run([self.production, '--seal'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires the BusyBox PID 1 RAM environment', result.stderr)
        self.assertEqual(self.run_mock('normal', '--setrw').returncode, 2)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dev = self.root/'dev'; self.dev.mkdir()
        self.calls = self.root/'calls'
        self.helper = self.root/'helper'
        self.bb = self.root/'busybox'
        self.bb.write_text('#!/bin/sh\nprintf "bb %s\\n" "$*" >>"$CALLS"\n'
                           '[ "${FAIL_BLOCKDEV:-0}" != 1 ]\n')
        self.bb.chmod(0o755)
        source = (PROJECT/'device/boot/initramfs/init').read_text()
        self.function = re.search(r'^storage_seal_scan\(\) \{\n.*?^\}', source, re.M | re.S)[0]
        self.init = source

    def tearDown(self):
        self.temp.cleanup()

    def run_scan(self, nodes=True, helper=0, fallback_fail=False):
        if nodes:
            for name in ['sda', 'sda35', 'sdb']:
                (self.dev/name).touch()
        if helper is not None:
            self.helper.write_text('#!/bin/sh\nprintf "helper %s\\n" "$*" >>"$CALLS"\n'
                                   f'exit {helper}\n')
            self.helper.chmod(0o755)
        script = ('storage_path_is_block() { [ -f "$1" ]; }\nlog() { :; }\n' + self.function +
                  '\nstorage_seal_scan\nstatus=$?\nprintf "%s %s\\n" "$status" "$storage_scan_found"\n')
        env = dict(os.environ, storage_dev_root=str(self.dev), storage_seal_helper=str(self.helper),
                   BB=str(self.bb), CALLS=str(self.calls), FAIL_BLOCKDEV=str(int(fallback_fail)))
        result = subprocess.run(['sh', '-c', script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, self.calls.read_text() if self.calls.exists() else ''

    def test_success_uses_one_process_per_scan(self):
        status, calls = self.run_scan()
        self.assertEqual(status, '0 true\n')
        self.assertEqual(calls, 'helper --seal\n')

    def test_missing_or_failed_helper_uses_original_busybox_path(self):
        for helper in [None, 1]:
            with self.subTest(helper=helper):
                if self.helper.exists(): self.helper.unlink()
                self.calls.unlink(missing_ok=True)
                status, calls = self.run_scan(helper=helper)
                self.assertEqual(status, '0 true\n')
                self.assertEqual(calls.count('bb blockdev --setro '), 3)

    def test_fallback_failure_is_reported(self):
        status, calls = self.run_scan(helper=1, fallback_fail=True)
        self.assertEqual(status, '1 true\n')
        self.assertEqual(calls.count('bb blockdev --setro '), 3)

    def test_no_devices_is_not_a_successful_observation(self):
        status, calls = self.run_scan(nodes=False)
        self.assertEqual(status, '0 false\n')
        self.assertEqual(calls, '')

    def test_late_devices_are_seen_by_the_next_scan(self):
        self.run_scan(nodes=False)
        self.assertEqual(self.run_scan()[0], '0 true\n')

    def test_independent_final_lock_remains_after_wait(self):
        start = self.init.index('log "waiting for SCSI/UFS block devices')
        end = self.init.index('\nif [ "$ufs_safe" != true ]; then', start)
        gate = self.init[start:end]
        self.assertIn('storage_wait_for_ufs', gate)
        self.assertIn('elif storage_lock_all; then', gate)
        lock = re.search(r'^storage_lock_all\(\) \{\n.*?^\}', self.init, re.M | re.S)[0]
        self.assertNotIn('storage_seal_helper', lock)
        self.assertIn('"$BB" blockdev --setro', lock)
        self.assertIn('"$BB" blockdev --getro', lock)


class WaitTests(unittest.TestCase):
    def setUp(self):
        import json
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.clock = self.root/'clock';self.clock.write_text('0\n')
        self.calls = self.root/'calls'
        source=(PROJECT/'device/boot/initramfs/init').read_text()
        self.wait=re.search(r'^storage_wait_for_ufs\(\) \{\n.*?^\}',source,re.M|re.S)[0]
        self.real_clock=re.search(r'^storage_clock_read\(\) \{\n.*?^\}',source,re.M|re.S)[0]
        self.bb=self.root/'bb';self.helper=self.root/'helper'
        prefix='#!'+__import__('sys').executable+'\nimport os,sys\nfrom pathlib import Path\n'
        self.bb.write_text(prefix+'''clock=Path(os.environ['CLOCK']);args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as out:out.write('bb '+' '.join(args)+'\\n')
now=int(clock.read_text())
if args[0]=='mdev':clock.write_text(str(now+5)+'\\n');sys.exit(0)
if args[0]=='sleep':clock.write_text(str(now+round(float(args[1])*100))+'\\n');sys.exit(0)
assert args[:2]==['timeout','1']
if os.environ.get('HANG')=='1':clock.write_text(str(now+100)+'\\n');sys.exit(124)
os.execv(args[2],args[2:])
''');self.bb.chmod(0o755)
        self.helper.write_text(prefix+'''assert sys.argv[1:]==['--ready']
count=Path(os.environ['COUNT']);n=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(n))
with open(os.environ['CALLS'],'a') as out:out.write('ready\\n')
if n<int(os.environ.get('READY_AFTER','1')) or n==int(os.environ.get('FAIL_READY_AT','0')):sys.exit(1)
print('2 UFS LUNs; 5 block nodes')
''');self.helper.chmod(0o755)

    def tearDown(self):self.temp.cleanup()

    def run_wait(self, *, mode='persistent', profile='native-updatable', cmdline='', **settings):
        env=dict(os.environ,BB=str(self.bb),storage_seal_helper=str(self.helper),
                 storage_ready_state=str(self.root/'ready'),CLOCK=str(self.clock),CALLS=str(self.calls),
                 COUNT=str(self.root/'count'),storage_mode=mode,storage_profile=profile,
                 CMDLINE=cmdline,**{k:str(v) for k,v in settings.items()})
        script='''log() { echo "$*"; }
storage_clock_read() { [ "${CLOCK_FAIL:-0}" != 1 ] || return 1; read -r storage_wait_now <"$CLOCK"; }
storage_seal_scan() {
 storage_scan_found=${NODES:-true}
 [ "${SEAL_FAIL:-0}" != 1 ]
}
'''+self.wait+'''\nstorage_wait_for_ufs "$CMDLINE"
printf 'result=%s,%s\\n' "$found_block" "$storage_wait_complete"
'''
        result=subprocess.run(['sh','-c',script],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout,self.calls.read_text(),int(self.clock.read_text())

    def test_two_complete_proofs_retain_one_scan_interval(self):
        out,calls,elapsed=self.run_wait()
        self.assertIn('result=true,true',out);self.assertEqual(calls.count('bb sleep'),1)
        self.assertEqual(calls.splitlines().count('ready'),2)
        self.assertGreaterEqual(elapsed,100);self.assertLess(elapsed,200)

    def test_delayed_readiness_is_retried_then_finishes(self):
        out,calls,elapsed=self.run_wait(READY_AFTER=3)
        self.assertIn('result=true,true',out);self.assertEqual(calls.splitlines().count('ready'),4)
        self.assertEqual(calls.count('bb sleep'),3);self.assertLess(elapsed,1000)

    def test_failed_proof_resets_the_consecutive_confirmation(self):
        out,calls,elapsed=self.run_wait(FAIL_READY_AT=2)
        self.assertIn('result=true,true',out);self.assertEqual(calls.splitlines().count('ready'),4)
        self.assertEqual(calls.count('bb sleep'),3)

    def test_incomplete_proof_keeps_the_full_bounded_window(self):
        out,calls,elapsed=self.run_wait(READY_AFTER=999)
        self.assertIn('result=true,false',out);self.assertGreaterEqual(elapsed,1000)
        self.assertLessEqual(elapsed,1010)

    def test_slow_query_is_counted_inside_the_deadline(self):
        out,calls,elapsed=self.run_wait(HANG=1)
        self.assertIn('result=true,false',out);self.assertLessEqual(elapsed,1010)
        self.assertGreaterEqual(elapsed,1000)

    def test_no_devices_or_failed_seal_cannot_use_early_exit(self):
        for settings in [{'NODES':'false'},{'SEAL_FAIL':'1'}]:
            with self.subTest(settings=settings):
                self.clock.write_text('0\n');self.calls.unlink(missing_ok=True)
                out,calls,elapsed=self.run_wait(**settings)
                self.assertIn(',false',out);self.assertNotIn('ready\n',calls)
                self.assertGreaterEqual(elapsed,1000)

    def test_installer_charger_and_failed_clock_keep_ten_legacy_rounds(self):
        for settings in [{'mode':'readonly','profile':'installer'},
                         {'cmdline':'androidboot.mode=charger rhgb'},{'CLOCK_FAIL':1}]:
            with self.subTest(settings=settings):
                self.clock.write_text('0\n');self.calls.unlink(missing_ok=True)
                out,calls,elapsed=self.run_wait(**settings)
                self.assertIn('result=true,false',out);self.assertNotIn('ready\n',calls)
                self.assertEqual(calls.count('bb sleep 1\n'),10)

    def test_missing_helper_keeps_waiting(self):
        self.helper.unlink();out,calls,elapsed=self.run_wait()
        self.assertIn('result=true,false',out);self.assertNotIn('ready\n',calls)
        self.assertGreaterEqual(elapsed,1000)

    def test_uptime_parser_handles_leading_zero_fraction(self):
        uptime=self.root/'uptime'
        for value,expected in [('0.08 1.23\n','8'),('9.09 15.11\n','909'),('100.00 0.00\n','10000')]:
            with self.subTest(value=value):
                uptime.write_text(value)
                script=self.real_clock.replace('/proc/uptime',str(uptime))+'\nstorage_clock_read && echo "$storage_wait_now"'
                result=subprocess.run(['sh','-c',script],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
