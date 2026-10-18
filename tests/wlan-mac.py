#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise private WLAN admission using fake sysfs, udev and ip only."""
import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
OVERLAY = PROJECT / 'device/gnome-overlay'
SCRIPT = OVERLAY / 'usr/local/sbin/liuqin-wlan-mac'
MAC = '02:00:00:00:00:01'

MOCK = r'''
import json, os, sys
from pathlib import Path
root=Path(os.environ['FIXTURE'])
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with (root/'calls').open('a') as f:f.write(json.dumps([name,*args])+'\n')
if name=='sleep':
    count=int((root/'ticks').read_text())+1
    (root/'ticks').write_text(str(count))
    if count==int(os.environ.get('READY_AFTER','-1')):(root/'ready').touch()
    if count==int(os.environ.get('APPEAR_AFTER','-1')):
        p=root/'net/wlp1s0';(p/'device').mkdir(parents=True)
        (p/'device/vendor').write_text('0x17cb\n')
        (p/'device/device').write_text('0x1103\n')
        (p/'address').write_text('00:00:00:00:00:00\n')
    sys.exit(0)
if name=='udevadm':
    assert args[:3]==['wait','--timeout=0','--initialized=yes'],args
    p=Path(args[3])
    if os.environ.get('RENAME_ON_QUERY')=='1' and p.name=='wlan0':
        p.rename(p.with_name('wlp1s0'));sys.exit(1)
    sys.exit(0 if (root/'ready').exists() and p.is_dir() else 1)
if name=='ip':
    assert (root/'ready').exists(),'ip must not run before target initialization'
    assert args[:3]==['link','set','dev'] and len(args)>=5,args
    p=root/'net'/args[3]
    assert p.is_dir(),args
    action=args[4]
    if os.environ.get('FAIL_IP')==action:sys.exit(7)
    if action=='address' and os.environ.get('DO_NOT_RETAIN')!='1':
        (p/'address').write_text(args[5]+'\n')
    sys.exit(0)
raise AssertionError(name)
'''


class WlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.net = self.root/'net'; self.net.mkdir()
        self.state = self.root/'wlan-mac'
        self.state.write_text(MAC+'\n'); self.state.chmod(0o600)
        (self.root/'ticks').write_text('0')
        for name in ['ip', 'udevadm', 'sleep']:
            p = self.bin/name
            p.write_text('#!'+sys.executable+'\n'+MOCK); p.chmod(0o755)
        self.env = dict(os.environ, FIXTURE=str(self.root),
                        PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        LIUQIN_WLAN_MAC_STATE=str(self.state),
                        LIUQIN_SYS_CLASS_NET=str(self.net),
                        LIUQIN_IP=str(self.bin/'ip'), LIUQIN_UDEVADM=str(self.bin/'udevadm'),
                        LIUQIN_WLAN_MAC_TEST_ALLOW_UNPRIVILEGED='1',
                        LIUQIN_WLAN_MAC_WAIT_SECONDS='3')

    def tearDown(self): self.temp.cleanup()

    def card(self, name='wlp1s0', vendor='0x17cb', device='0x1103'):
        p = self.net/name; (p/'device').mkdir(parents=True)
        (p/'device/vendor').write_text(vendor+'\n')
        (p/'device/device').write_text(device+'\n')
        (p/'address').write_text('00:00:00:00:00:00\n')

    def calls(self, name):
        p=self.root/'calls'
        return [a for line in (p.read_text().splitlines() if p.exists() else [])
                if (a:=json.loads(line))[0]==name]

    def run_helper(self, success=True, **extra):
        result = subprocess.run(['sh', str(SCRIPT)], env=dict(self.env, **extra),
                                capture_output=True, text=True, timeout=8)
        if success: self.assertEqual(result.returncode, 0, result.stderr)
        else: self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(MAC, result.stdout+result.stderr)
        return result

    def test_ready_target_uses_final_name_and_never_waits_globally(self):
        self.card(); self.card('eth0', vendor='0x1234')
        (self.root/'ready').touch()
        self.run_helper()
        self.assertEqual(self.calls('ip'), [
            ['ip','link','set','dev','wlp1s0','down'],
            ['ip','link','set','dev','wlp1s0','address',MAC],
            ['ip','link','set','dev','wlp1s0','up']])
        self.assertEqual(len(self.calls('udevadm')), 1)
        self.assertEqual(self.calls('sleep'), [])

    def test_waits_for_target_udev_initialization(self):
        self.card(); self.run_helper(READY_AFTER='2')
        self.assertEqual(len(self.calls('sleep')), 2)
        self.assertEqual(len(self.calls('udevadm')), 3)
        self.assertEqual(len(self.calls('ip')), 3)

    def test_target_can_appear_late(self):
        self.run_helper(APPEAR_AFTER='2', READY_AFTER='2')
        self.assertEqual(len(self.calls('sleep')), 2)
        self.assertEqual(len(self.calls('ip')), 3)

    def test_rename_during_query_is_retried(self):
        self.card('wlan0'); (self.root/'ready').touch()
        self.run_helper(RENAME_ON_QUERY='1')
        self.assertEqual(len(self.calls('sleep')), 1)
        self.assertTrue(all(c[4]=='wlp1s0' for c in self.calls('ip')))

    def test_missing_target_has_bounded_wait_and_never_configures_other_card(self):
        self.card('eth0', device='0xffff'); (self.root/'ready').touch()
        result=self.run_helper(success=False)
        self.assertIn('did not appear', result.stderr)
        self.assertEqual(len(self.calls('sleep')), 3)
        self.assertEqual(self.calls('udevadm'), [])
        self.assertEqual(self.calls('ip'), [])

    def test_existing_but_uninitialized_target_times_out_without_ip(self):
        self.card(); self.run_helper(success=False)
        self.assertEqual(len(self.calls('sleep')), 3)
        self.assertEqual(self.calls('ip'), [])

    def test_private_identity_validation_precedes_device_changes(self):
        self.card(); (self.root/'ready').touch()
        for value in ['00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff',
                      '01:00:00:00:00:01', 'garbage', MAC+'\n'+MAC]:
            with self.subTest(value=value):
                self.state.write_text(value+'\n'); self.run_helper(success=False)
        self.assertEqual(self.calls('ip'), [])
        self.assertEqual(self.calls('udevadm'), [])

    def test_missing_symlink_and_insecure_private_file_are_rejected(self):
        self.state.chmod(0o644); self.run_helper(success=False)
        self.state.unlink(); self.run_helper(success=False)
        target=self.root/'secret'; target.write_text(MAC+'\n'); target.chmod(0o600)
        self.state.symlink_to(target); self.run_helper(success=False)
        self.assertEqual(self.calls('ip'), [])

    def test_missing_udev_tool_fails_before_ip(self):
        self.run_helper(success=False, LIUQIN_UDEVADM=str(self.bin/'missing'))
        self.assertEqual(self.calls('ip'), [])

    def test_failed_address_change_does_not_raise_link(self):
        self.card(); (self.root/'ready').touch()
        self.run_helper(success=False, FAIL_IP='address')
        self.assertEqual([c[5] for c in self.calls('ip')], ['down','address'])

    def test_kernel_must_retain_private_address_before_link_up(self):
        self.card(); (self.root/'ready').touch()
        self.run_helper(success=False, DO_NOT_RETAIN='1')
        self.assertEqual([c[5] for c in self.calls('ip')], ['down','address'])

    def test_networkmanager_requires_successful_admission_without_global_settle(self):
        parser=configparser.ConfigParser(interpolation=None)
        parser.read(OVERLAY/'etc/systemd/system/liuqin-wlan-mac.service')
        self.assertIn('NetworkManager.service', parser['Unit']['Before'].split())
        self.assertNotIn('systemd-udev-settle.service',
                         parser['Unit'].get('After','').split()+parser['Unit'].get('Wants','').split())
        parser.read(OVERLAY/'etc/systemd/system/NetworkManager.service.d/20-liuqin-wlan-mac.conf')
        self.assertIn('liuqin-wlan-mac.service', parser['Unit']['Requires'].split())
        self.assertIn('liuqin-wlan-mac.service', parser['Unit']['After'].split())


if __name__ == '__main__': unittest.main(verbosity=2)
