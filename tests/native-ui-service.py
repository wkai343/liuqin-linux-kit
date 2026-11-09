#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only operations, one-use exit confirmation and log-export boundaries."""
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
spec=importlib.util.spec_from_file_location('native_ui_service',PROJECT/'device/recovery/native-ui-service.py')
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)
from liuqin_gui.native_logs import decode_export,validate_metadata
import liuqin_native_install as gate


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        files={'etc/liuqin-native-recovery.json':json.dumps({'build_id':'a'*64}),
            'proc/cmdline':'androidboot.serialno=TEST androidboot.slot_suffix=_a androidboot.verifiedbootstate=orange',
            'proc/sys/kernel/random/boot_id':'12345678-1234-4234-8234-123456789abc','proc/sys/kernel/osrelease':'test-native',
            'proc/meminfo':'MemAvailable: 1000000 kB\n','proc/uptime':'60.0 20.0\n','proc/mounts':'rootfs / rootfs rw 0 0\n','proc/self/mountinfo':'',
            'sys/class/block/sda/ro':'1','sys/class/block/sda/dev':'8:0','sys/class/block/sda/uevent':'DEVNAME=sda\n',
            'sys/class/block/sda1/ro':'1','sys/class/block/sda1/dev':'8:1','sys/class/block/sda1/size':'2048',
            'sys/class/block/sda1/uevent':'DEVNAME=sda1\nPARTNAME=userdata\n',
            'sys/class/backlight/ktz8866-backlight/max_brightness':'2047','sys/class/backlight/ktz8866-backlight/brightness':'1500',
            'run/liuqin-native-ui.log':'UI started\n','run/liuqin-native-status.log':'', 'data/private.txt':'must not be exported'}
        for name,value in files.items():p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
        (self.root/'proc/device-tree').mkdir();(self.root/'proc/device-tree/compatible').write_bytes(b'xiaomi,liuqin\0')
        self.runner=Mock(return_value=SimpleNamespace(stdout=b'kernel test\n',returncode=0))
        self.ops=service.Operations(self.root,self.runner)

    def test_readonly_pages_do_not_dispatch_power_or_write_partitions(self):
        result=self.ops.dispatch({'op':'status'});self.assertTrue(result['sealed']);self.assertEqual(result['partition_count'],1)
        self.assertEqual(self.ops.dispatch({'op':'partitions'})['partitions'][0]['name'],'userdata')
        self.assertIn('kernel test',self.ops.dispatch({'op':'logs'})['text'])
        self.runner.assert_called_once_with(['/bin/busybox','dmesg'],capture_output=True,timeout=8,check=True)
        self.assertEqual((self.root/'sys/class/block/sda/ro').read_text(),'1')

    def test_usb_status_uses_live_controller_state_and_never_assumes_adb_authorization(self):
        self.assertEqual(self.ops.summary()['usb_state'],'starting')
        gadget=self.root/'sys/kernel/config/usb_gadget/liuqin-adb'
        (gadget/'functions/ffs.adb').mkdir(parents=True)
        (gadget/'functions/ffs.adb/ready').write_text('1\n')
        (gadget/'UDC').write_text('a600000.usb\n')
        state=self.root/'sys/class/udc/a600000.usb/state'
        state.parent.mkdir(parents=True)
        online=self.root/'sys/class/power_supply/qcom-battmgr-usb/online'
        online.parent.mkdir(parents=True)
        online.write_text('1\n')
        for raw,expected in (('configured','connected'),('not attached','disconnected'),
                             ('addressed','connecting'),('suspended','suspended'),
                             ('unexpected','unknown')):
            state.write_text(raw+'\n')
            result=self.ops.summary()
            self.assertEqual(result['usb_state'],expected)
            self.assertTrue(result['adb_ready'])
            self.assertNotIn('adb_connected',result)
        state.write_text('configured\n')
        online.write_text('0\n')
        self.assertEqual(self.ops.summary()['usb_state'],'disconnected')
        self.assertEqual(self.ops.summary()['usb_controller_state'],'configured')
        self.assertTrue(self.ops.summary()['adb_ready'])
        online.unlink()
        self.assertEqual(self.ops.summary()['usb_state'],'unknown')
        online.write_text('1\n')
        state.unlink()
        self.assertEqual(self.ops.summary()['usb_state'],'unknown')
        (gadget/'UDC').write_text('')
        self.assertEqual(self.ops.summary()['usb_state'],'starting')
        self.assertFalse(self.ops.summary()['adb_ready'])
        self.runner.assert_not_called()

    def test_supervised_socket_bcb_cleanup_runs_on_main_thread(self):
        fixture_spec=importlib.util.spec_from_file_location('bcb_fixture',PROJECT/'tests/native-bcb.py')
        fixture=importlib.util.module_from_spec(fixture_spec);fixture_spec.loader.exec_module(fixture)
        for old_worker_loop in (True,False):
            with self.subTest(old_worker_loop=old_worker_loop), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'run').mkdir()
                hardware=fixture.FileHardware(root);before=hardware.path.read_bytes()
                value=dict(boot_id='12345678-1234-4234-8234-123456789abc',serial='TEST',build_id='a'*64)
                fixture.bcb.capture(hardware,value,root)
                child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
                replies=[];errors=[];path=str(root/'ui.sock')
                class Handler(socketserver.StreamRequestHandler):
                    def handle(self):
                        self.rfile.readline()
                        try:
                            result=fixture.bcb.consume(hardware,hardware.agent(),value,root)
                            reply=dict(status=result['status'],main=threading.current_thread() is threading.main_thread())
                        except Exception as error:
                            reply=dict(error=str(error))
                        self.wfile.write(json.dumps(reply).encode()+b'\n');self.wfile.flush()
                        child.terminate()
                def request():
                    try:
                        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                            sock.settimeout(5);sock.connect(path);sock.sendall(b'poweroff\n')
                            replies.append(json.loads(sock.recv(4096)))
                    except Exception as error:errors.append(error);child.terminate()
                try:
                    with socketserver.UnixStreamServer(path,Handler) as server,patch.object(fixture.bcb.os,'sync'):
                        client=threading.Thread(target=request,daemon=True);client.start()
                        if old_worker_loop:
                            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                            child.wait(timeout=10);server.shutdown();worker.join(timeout=2)
                        else:
                            self.assertFalse(service.supervise_ui(server,child))
                        client.join(timeout=2)
                    self.assertFalse(errors)
                    self.assertEqual(len(replies),1)
                    self.assertEqual(set(hardware.ro_state.values()),{1})
                    if old_worker_loop:
                        self.assertIn('signal only works in main thread',replies[0]['error'])
                        self.assertEqual(hardware.writes,0)
                        self.assertEqual(hardware.path.read_bytes(),before)
                    else:
                        self.assertEqual(replies[0],dict(status='cleared',main=True))
                        self.assertEqual(hardware.path.read_bytes(),bytes(32)+before[32:])
                        self.assertEqual(hardware.writes,1)
                finally:
                    if child.poll() is None:child.kill()
                    child.wait(timeout=5)

    def test_usb_snapshot_expires_and_updates_without_the_operation_guard(self):
        self.ops.guard.acquire()
        try:
            snapshot=self.ops.publish_usb_status()
        finally:self.ops.guard.release()
        path=self.root/'run/liuqin-native-usb.json'
        self.assertEqual(json.loads(path.read_text()),snapshot)
        self.assertEqual(snapshot['boot_id'],'12345678-1234-4234-8234-123456789abc')
        self.assertFalse(path.with_suffix('.tmp').exists())
        self.assertEqual(self.ops.summary()['usb_state'],'starting')
        with patch.object(service.time,'monotonic',return_value=snapshot['monotonic']+6):
            self.assertEqual(self.ops.summary()['usb_state'],'unknown')
        self.ops.publish_usb_status()
        self.assertEqual(self.ops.summary()['usb_state'],'starting')
        self.runner.assert_not_called()

    def test_no_shell_or_disk_mutation_operation_exists(self):
        for op in ('flash','format','install','shell','set-active','../../poweroff'):
            with self.subTest(op=op),self.assertRaises(ValueError):self.ops.dispatch({'op':op,'command':'reboot'})
        self.runner.assert_not_called()

    def test_exit_requires_current_one_use_confirmation(self):
        with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'execute_exit','token':'guessed'})
        token=self.ops.dispatch({'op':'prepare_exit','action':'fastboot'})['token'];self.runner.assert_not_called()
        self.ops.dispatch({'op':'execute_exit','token':token})
        self.runner.assert_called_once_with(['/usr/sbin/liuqin-ui-power','bootloader'],check=True,timeout=8)
        with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'execute_exit','token':token})

    def test_all_three_power_actions_use_fixed_arguments(self):
        for action,target in [('system','normal'),('fastboot','bootloader'),('poweroff','poweroff')]:
            token=self.ops.dispatch({'op':'prepare_exit','action':action})['token'];self.ops.dispatch({'op':'execute_exit','token':token})
            self.assertEqual(self.runner.call_args.args[0],['/usr/sbin/liuqin-ui-power',target])

    def test_cleanup_precedes_each_confirmed_power_action_and_uses_one_guard(self):
        marker = dict(build_id='a'*64, bcb_cleanup_version=1)
        (self.root/gate.MARKER).write_text(json.dumps(marker))
        bcb, hardware, agent = Mock(), Mock(), Mock()
        bcb.consume.return_value = dict(status='cleared')
        self.ops.bcb_backend = Mock(return_value=(bcb, hardware, agent))
        ordered = Mock()
        ordered.attach_mock(bcb.consume, 'cleanup')
        ordered.attach_mock(self.runner, 'power')
        self.ops.capture_bcb()
        bcb.capture.assert_called_once()
        for action in ('system', 'fastboot', 'poweroff'):
            token = self.ops.dispatch(dict(op='prepare_exit', action=action))['token']
            self.ops.dispatch(dict(op='execute_exit', token=token))
            self.assertEqual([call[0] for call in ordered.mock_calls[-2:]], ['cleanup', 'power'])
        self.assertEqual(bcb.consume.call_count, 3)
        self.assertEqual((self.root/'sys/class/block/sda/ro').read_text(), '1')

    def test_cleanup_failure_prevents_reboot_from_both_entry_points(self):
        (self.root/gate.MARKER).write_text(json.dumps(dict(build_id='a'*64, bcb_cleanup_version=1)))
        bcb = Mock()
        bcb.consume.side_effect = RuntimeError('BCB readback failed')
        self.ops.bcb_backend = Mock(return_value=(bcb, Mock(), Mock()))
        token = self.ops.dispatch(dict(op='prepare_exit', action='system'))['token']
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            self.ops.dispatch(dict(op='execute_exit', token=token))
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            self.ops.exit_recovery('bootloader')
        self.runner.assert_not_called()

    def test_confirmation_expiry_session_change_and_new_request_invalidate(self):
        token=self.ops.dispatch({'op':'prepare_exit','action':'system'})['token']
        with patch.object(service.time,'monotonic',return_value=self.ops.pending[token]['expires']+1),self.assertRaises(RuntimeError):
            self.ops.dispatch({'op':'execute_exit','token':token})
        old=self.ops.dispatch({'op':'prepare_exit','action':'system'})['token'];new=self.ops.dispatch({'op':'prepare_exit','action':'poweroff'})['token']
        with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'execute_exit','token':old})
        (self.root/'proc/sys/kernel/random/boot_id').write_text('different-session')
        with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'execute_exit','token':new})
        self.runner.assert_not_called()

    def test_busy_unsealed_or_long_name_blocks_power(self):
        for phase in ('reading','connected','installing','write-failed'):
            service.status.show(phase,self.root)
            with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'prepare_exit','action':'system'})
        service.status.show('complete',self.root)
        (self.root/'sys/class/block/sda/ro').write_text('0')
        with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'prepare_exit','action':'fastboot'})
        (self.root/'sys/class/block/sda/ro').write_text('1')
        (self.root/'sys/class/block/sda1/uevent').write_text('PARTNAME=linux_r_9748571b5637\n')
        with self.assertRaises(ValueError):self.ops.dispatch({'op':'prepare_exit','action':'fastboot'})
        self.runner.assert_not_called()

    def test_operation_lock_blocks_execution_even_after_confirmation(self):
        token=self.ops.dispatch({'op':'prepare_exit','action':'system'})['token']
        with (self.root/'run/liuqin-android-operation.lock').open('a+b') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):self.ops.dispatch({'op':'execute_exit','token':token})
        self.runner.assert_not_called()

    def test_brightness_is_bounded_and_only_updates_known_sysfs_file(self):
        for percent in (0,101,-1,'50',True):
            with self.assertRaises(ValueError):self.ops.dispatch({'op':'brightness','percent':percent})
        self.ops.dispatch({'op':'brightness','percent':50})
        self.assertEqual((self.root/'sys/class/backlight/ktz8866-backlight/brightness').read_text(),'1023\n')
        self.runner.assert_not_called()

    def test_export_is_a_bounded_ram_archive_without_user_files(self):
        result=self.ops.dispatch({'op':'export_logs'});validate_metadata(result)
        archive=self.root/result['file'].lstrip('/');data=archive.read_bytes()
        self.assertEqual(decode_export(result,base64.encodebytes(data)),data)
        with tarfile.open(archive) as stream:
            names=stream.getnames();self.assertIn('device.json',names);self.assertIn('kernel.log',names)
            self.assertFalse(any('private' in n or n.startswith('/') or '..' in n for n in names))
            self.assertNotIn(b'must not be exported',b''.join(stream.extractfile(n).read() for n in names))
        broken=dict(result,sha256='0'*64)
        with self.assertRaises(RuntimeError):decode_export(broken,base64.encodebytes(data))
        for path in ('/data/log.tar.gz','/run/../data/log.tar.gz',result['file']+';reboot'):
            with self.assertRaises(RuntimeError):validate_metadata(dict(result,file=path))

    def test_repeated_exports_do_not_fill_ram(self):
        for _ in range(5):self.ops.dispatch({'op':'export_logs'})
        self.assertEqual(len(list((self.root/'run/liuqin-ui-exports').glob('*.tar.gz'))),3)

    def test_local_switch_is_bound_to_confirmation_and_ends_with_a_sealed_lease(self):
        marker = dict(build_id='a'*64, usage='temporary-boot-only', inspection_only=False,
                      installer_version=1, installer_capabilities=gate.CAPABILITIES,
                      boot_switch_version=1, tablet_boot_switch_version=1)
        (self.root/gate.MARKER).write_text(json.dumps(marker))
        battery = self.root/'sys/class/power_supply/battery'
        battery.mkdir(parents=True)
        (battery/'type').write_text('Battery')
        (battery/'capacity').write_text('85')
        target = str(uuid.uuid4())
        parts = [dict(name=name, parent='sda', start=index*4096, bytes=4096,
                      partuuid=str(uuid.uuid4()))
                 for index,name in enumerate(('boot_a','linux_boot'), 1)]
        plan = dict(system_id=target, target_name='Fedora', target_partition='lqr_123456789abc',
                    previous_name='LineageOS', previous_partition='userdata', parts=parts,
                    previous_boot_sha256='b'*64)
        native = Mock()
        native.preview_switch.return_value = dict(plan=plan)

        def apply(request, hardware, agent, value, *, local):
            self.assertTrue(local)
            lease = gate.require(request['token'], self.root)
            self.assertEqual(lease['scope'], gate.scope(dict(kind='switch', switch_plan=plan)))
            self.assertEqual(request['plan'], plan)
            with patch.dict(os.environ, {gate.TOKEN: request['token']}):
                gate.mark_write(self.root)
            return dict(default_id=target, boot_readback_verified=True)

        native.apply_switch.side_effect = apply
        with patch.object(self.ops, 'switch_backend', return_value=(native, object(), object())):
            token = self.ops.dispatch({'op':'prepare_switch', 'system_id':target})['token']
            self.assertIsNone(gate.session(self.root))
            with self.assertRaisesRegex(RuntimeError, '确认已过期'):
                self.ops.dispatch({'op':'execute_exit', 'token':token})
            (battery/'capacity').write_text('29')
            with self.assertRaisesRegex(RuntimeError, '低于 30%'):
                self.ops.dispatch({'op':'execute_switch', 'token':token})
            native.apply_switch.assert_not_called()
            (battery/'capacity').write_text('85')
            token = self.ops.dispatch({'op':'prepare_switch', 'system_id':target})['token']
            self.assertEqual(self.ops.dispatch({'op':'execute_switch','token':token})['default_id'],target)
            self.assertEqual(gate.session(self.root)['state'], 'complete')
            with self.assertRaisesRegex(RuntimeError, '确认已过期'):
                self.ops.dispatch({'op':'execute_switch', 'token':token})
        self.runner.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
