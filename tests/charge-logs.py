#!/usr/bin/env python3
"""Charger log ownership, protocol validation and collector exit checks."""
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import select
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    'charge_logs', str(ROOT / 'device/board-overlay/usr/local/sbin/liuqin-charge-logs'))
spec = importlib.util.spec_from_loader(loader.name, loader)
logs = importlib.util.module_from_spec(spec)
loader.exec_module(logs)
REPLY = struct.pack('<3I', *logs.HEADER) + b'hello\n\0'.ljust(8192, b'\0')


class Tests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.sysfs = Path(self.work.name)
        self.device = self.sysfs / 'bus/rpmsg/devices/3000000.remoteproc:glink-edge.PMIC_LOGS_ADSP_APPS.-1.-1'
        self.device.mkdir(parents=True)
        (self.device / 'name').write_text(logs.CHANNEL)
        (self.device / 'driver_override').write_text('(null)\n')
        self.driver = self.sysfs / 'bus/rpmsg/drivers/rpmsg_chrdev'
        self.driver.mkdir(parents=True)
        (self.sysfs / 'class/rpmsg').mkdir(parents=True)

    def bind(self):
        (self.device / 'driver').symlink_to(self.driver)
        real = self.device / 'rpmsg/rpmsg7'
        real.mkdir(parents=True)
        (real / 'name').write_text(logs.CHANNEL)
        (real / 'dev').write_text('240:7')
        entry = self.sysfs / 'class/rpmsg/rpmsg7'
        entry.symlink_to(real)
        return entry

    def test_bind_only_log_device_and_reuse_default_endpoint(self):
        write_text = Path.write_text
        writes = []

        def write(path, text, *args, **kwargs):
            writes.append((path, text))
            if path == self.driver / 'bind':
                self.bind()
                return len(text)
            return write_text(path, text, *args, **kwargs)

        with patch.object(Path, 'write_text', write), patch.object(logs.subprocess, 'run') as run:
            first = logs.endpoint(self.sysfs)
            self.assertEqual(first, logs.endpoint(self.sysfs))
            run.assert_called_once_with(['modprobe', 'rpmsg_char'], check=True)
        self.assertEqual([p for p, _ in writes if p.name in ('bind', 'driver_override')],
                         [self.device / 'driver_override', self.driver / 'bind'])
        self.assertTrue((self.device / 'driver').exists())

    def test_refuse_other_owner_and_ctrl_created_endpoint(self):
        other = self.driver.with_name('other')
        other.mkdir()
        (self.device / 'driver').symlink_to(other)
        with patch.object(logs.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'another driver'):
                logs.endpoint(self.sysfs)
            run.assert_not_called()
        (self.device / 'driver').unlink()
        wrong = self.sysfs / 'ctrl/rpmsg/rpmsg2'
        wrong.mkdir(parents=True)
        (wrong / 'name').write_text(logs.CHANNEL)
        (self.sysfs / 'class/rpmsg/rpmsg2').symlink_to(wrong)
        with self.assertRaisesRegex(RuntimeError, 'separately created'):
            logs.endpoint(self.sysfs)

    def test_module_reload_can_bind_existing_override(self):
        (self.device / 'driver_override').write_text(logs.DRIVER)
        # Registering the module probes matching unbound devices immediately.
        def modprobe(*args, **kwargs):
            self.bind()

        with patch.object(logs.subprocess, 'run', side_effect=modprobe):
            self.assertEqual(logs.endpoint(self.sysfs).name, 'rpmsg7')
        self.assertFalse((self.driver / 'bind').exists(), 'must not bind twice')

    def test_reject_missing_log_channel_even_if_main_channel_exists(self):
        (self.device / 'name').write_text('PMIC_RTR_ADSP_APPS')
        with self.assertRaisesRegex(RuntimeError, 'unavailable or ambiguous'):
            logs.endpoint(self.sysfs)

    def test_validate_entire_reply_and_all_header_fields(self):
        self.assertEqual(logs.decode(REPLY), b'hello\n')
        for data in (b'', REPLY[:11], REPLY[:-1], REPLY + b'x'):
            with self.assertRaises(RuntimeError):
                logs.decode(data)
        for index in range(3):
            data = bytearray(REPLY)
            data[index * 4] ^= 1
            with self.assertRaises(RuntimeError):
                logs.decode(data)

    def test_close_file_on_success_error_interrupt_and_bad_device(self):
        entry = self.bind()
        for error in (None, TimeoutError(), RuntimeError('bad reply'), KeyboardInterrupt()):
            output = io.BytesIO()
            with (patch.object(logs.os, 'open', return_value=99),
                  patch.object(logs.os, 'fstat', return_value=SimpleNamespace(
                      st_mode=stat.S_IFCHR, st_rdev=os.makedev(240, 7))),
                  patch.object(logs.os, 'read', side_effect=BlockingIOError()),
                  patch.object(logs.os, 'close') as close,
                  patch.object(logs, 'request', return_value=b'hello\n', side_effect=error),
                  patch.object(Path, 'write_text') as write):
                if error is None:
                    logs.collect(entry, 1, 1, 3, output)
                    self.assertEqual(output.getvalue(), b'hello\n')
                else:
                    with self.assertRaises(type(error)):
                        logs.collect(entry, 1, 1, 3, output)
                close.assert_called_once_with(99)
                write.assert_not_called()
                self.assertTrue((self.device / 'driver').exists())
        with (patch.object(logs.os, 'open', return_value=99),
              patch.object(logs.os, 'fstat', return_value=SimpleNamespace(
                  st_mode=stat.S_IFCHR, st_rdev=os.makedev(240, 8))),
              patch.object(logs.os, 'close') as close,
              patch.object(logs, 'request') as request):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                logs.collect(entry, 1, 1, 3, io.BytesIO())
            close.assert_called_once_with(99)
            request.assert_not_called()

    def test_one_read_only_request_and_bounded_failure(self):
        poller = Mock()
        with (patch.object(logs.select, 'poll', return_value=poller),
              patch.object(logs.os, 'write', return_value=16) as write,
              patch.object(logs.os, 'read', return_value=REPLY)):
            poller.poll.return_value = [(99, select.POLLIN)]
            self.assertEqual(logs.request(99, 3), b'hello\n')
            write.assert_called_once_with(99, struct.pack('<4I', 32778, 1, 0x18, 8192))
            poller.poll.assert_called_once_with(3000)
            poller.poll.return_value = []
            with self.assertRaises(TimeoutError):
                logs.request(99, 3)
            poller.poll.return_value = [(99, select.POLLHUP)]
            with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                logs.request(99, 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
