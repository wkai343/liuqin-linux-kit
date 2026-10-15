#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Portable-host checks, including native Windows locks when run on Windows."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui import host
from liuqin_gui.demo import DemoEngine
from liuqin_gui.engine import Engine
from liuqin_gui.artifacts import inspect_package

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='liuqin-windows-')
        self.root=Path(self.temp.name)/'中文 路径';self.root.mkdir()
    def tearDown(self):self.temp.cleanup()
    def test_engine_imports_without_the_unix_lock_module(self):
        code="import sys;sys.modules['fcntl']=None;sys.path.insert(0,"+repr(str(PROJECT/'installer'))+");import liuqin_gui.engine;print('OK')"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
    def test_sdk_search_handles_spaces_and_unicode(self):
        installer=self.root/'installer';installer.mkdir()
        sdk=self.root/'platform-tools';sdk.mkdir();exe=sdk/'fastboot.exe';exe.write_bytes(b'OFFLINE FIXTURE NEVER EXECUTED')
        with patch.object(host,'IS_WINDOWS',True),patch.object(host,'INSTALLER',installer),patch.dict(os.environ,{},clear=True):
            self.assertEqual(host.fastboot_path(),str(exe.resolve()))
            with patch.dict(os.environ,{'LIUQIN_FASTBOOT':str(self.root/'missing.exe')}):
                with self.assertRaises(FileNotFoundError):host.fastboot_path()
    def test_os_lock_rejects_another_process_and_releases_on_close(self):
        path=self.root/'device.lock'
        code="import sys;sys.path.insert(0,"+repr(str(PROJECT/'installer'))+");from liuqin_gui.host import FileLock;lock=FileLock(sys.argv[1],blocking=False)"
        with host.FileLock(path,blocking=False):
            result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
        result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_backup_index_keeps_utf8_data(self):
        engine=Engine(lambda _event:None,self.root/'运行记录')
        try:
            engine.remember_backup({'serial':'TEST','path':str(self.root/'设备备份')})
            value=json.loads((engine.state_dir/'backups-index.json').read_text(encoding='utf-8'))
            self.assertEqual(value['real:TEST'],str(self.root/'设备备份'))
        finally:engine.close()
    def test_windows_old_installer_is_rejected_before_usb(self):
        with patch.object(host,'IS_WINDOWS',True):
            with self.assertRaisesRegex(RuntimeError,'新版 installer.img'):
                host.check_installer_support({'installer_usb_network':'ncm'})
            host.check_installer_support({'installer_usb_network':'rndis'})
            self.assertEqual(host.server_address('192.168.7.1'),('192.168.7.1',37631))
            with self.assertRaises(RuntimeError):host.server_address('192.168.1.3')
    def test_network_elevation_quotes_the_whole_script_path(self):
        installer=self.root/"O'Neil installer";installer.mkdir();(installer/'windows-network.ps1').write_text('# fixture')
        with patch.object(host,'INSTALLER',installer):
            command=host.windows_network_command()
        decoded=base64.b64decode(command[-1]).decode('utf-16le')
        self.assertIn('-Verb RunAs',decoded)
        self.assertIn('"'+str(installer).replace("'","''"),decoded)
        self.assertNotIn('Set-ExecutionPolicy',decoded)
    def test_windows_processes_use_argument_arrays_and_no_console(self):
        with patch.object(host,'IS_WINDOWS',True),patch.object(host.subprocess,'run') as run:
            host.run_process(['fastboot.exe','boot',r'C:\中文 空间\installer.img'],text=True)
        self.assertEqual(run.call_args.args[0][2],r'C:\中文 空间\installer.img')
        self.assertEqual(run.call_args.kwargs['creationflags'],0x08000000)
        self.assertEqual(run.call_args.kwargs['encoding'],'utf-8')
        self.assertNotIn('shell',run.call_args.kwargs)
    def test_demo_records_the_windows_compatible_installer_channel(self):
        engine=DemoEngine(lambda _event:None,self.root/'demo')
        try:self.assertEqual(inspect_package(engine.demo_bundle)['installer_usb_network'],'rndis')
        finally:engine.close()
    def test_network_script_is_limited_to_the_usb_identity_and_port(self):
        source=(PROJECT/'installer/windows-network.ps1').read_text(encoding='utf-8-sig')
        for value in ('VID_1D6B&PID_1041','020000000701','-LocalPort 37631',"-RemoteAddress '192.168.7.2'",'-InterfaceAlias $adapter.Name'):
            self.assertIn(value,source)
        self.assertNotIn('Set-NetFirewallProfile',source)
        self.assertNotIn('Set-ExecutionPolicy',source)
        self.assertNotIn('-DefaultGateway',source)

if __name__=='__main__':unittest.main(verbosity=2)
