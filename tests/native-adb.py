#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Native ADB image, gadget, session and fallback isolation checks; no USB access."""
import base64,importlib.util,json,os,subprocess,sys,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_artifacts import boot_interface
from liuqin_gui.native_adb import probe_adb
from liuqin_gui.native_recovery_transport import probe_native
from liuqin_gui import host,runtime
def load(name,path):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
start=load('native_adb_start',PROJECT/'device/recovery/native-adb-start.py');gateway=load('native_adb_gateway',PROJECT/'device/recovery/native-adb-shell.py');fixture=load('native_adb_fixture',PROJECT/'tests/native-recovery.py')
class AdbTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.boot=str(uuid.uuid4())
        self.manifest=dict(interface='liuqin-native-recovery-v1',native_adb_version=1,usb_transport='adb')
        self.put('etc/liuqin-native-recovery.json',json.dumps(self.manifest));self.put('proc/sys/kernel/random/boot_id',self.boot)
    def put(self,name,text):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    def test_binary_safe_command_encoding_and_stale_boot(self):
        value="printf 'quoted \\\" data';\nprintf '中文'";encoded=base64.b64encode(value.encode()).decode()
        self.assertEqual(gateway.command(self.boot,encoded,self.root),value)
        for boot,data in [(str(uuid.uuid4()),encoded),(self.boot,'not-base64!'),(self.boot,base64.b64encode(b'x\0y').decode()),(self.boot,'a'*90001)]:
            with self.assertRaises((ValueError,RuntimeError)):gateway.command(boot,data,self.root)
    def test_adb_images_have_distinct_roles_and_no_network_marker(self):
        p=self.root/'recovery.img'
        for installer in (False,True):
            m=fixture.fixture(p,installer=installer,adb=True);info=boot_interface(p,installer=True)
            self.assertEqual(info['usb_network'],'adb-native');self.assertEqual(m['inspection_only'],not installer)
            (p.parent/'native-recovery-manifest.json').write_text(json.dumps(m));self.assertEqual(runtime.backend_hint(str(p)),'adb-native')
    def test_adb_probe_checks_identity_and_never_falls_back_when_requested(self):
        value=dict(interface='liuqin-native-recovery-v1',device='liuqin',serial='TEST',usb_transport='adb',boot_id=self.boot)
        with patch.object(host,'adb_path',return_value='mock-adb'),patch.object(host,'run_process',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(value).encode(),stderr=b'')):
            self.assertEqual(probe_adb('TEST'),value)
            with self.assertRaises(RuntimeError):probe_adb('OTHER')
        with patch('liuqin_gui.native_adb.probe_adb',side_effect=RuntimeError('offline')),patch('socket.socket',side_effect=AssertionError('network access forbidden')) as network:
            with self.assertRaises(RuntimeError):probe_native('TEST')
            network.assert_not_called()
    def test_gadget_uses_only_functionfs_and_allowlisted_environment(self):
        (self.root/'sys/class/udc/a600000.dwc3').mkdir(parents=True);(self.root/'sys/kernel/config/usb_gadget').mkdir(parents=True);(self.root/'run').mkdir()
        value=dict(device='liuqin',sealed=True,unlocked=True,slot='a',serial='TEST')
        runner=Mock();calls=[]
        def spawn(args,**kwargs):
            calls.append((args,kwargs));g=self.root/'sys/kernel/config/usb_gadget/liuqin-adb';(g/'functions/ffs.adb/ready').write_text('1');(g/'UDC').write_text('a600000.dwc3');return SimpleNamespace(pid=42,poll=lambda:None)
        with patch.object(start.status,'collect',return_value=value),patch.object(start,'aliases'):
            self.assertEqual(start.start(self.root,runner,spawn,lambda _:None)['transport'],'adb')
        env=calls[0][1]['env'];self.assertEqual(env['ADBD_USB'],'1');self.assertFalse(any(k in env for k in ('ADBD_LISTEN','ADBD_PORT','ADB_TCP_PORT')))
        self.assertTrue((self.root/'sys/kernel/config/usb_gadget/liuqin-adb/functions/ffs.adb').exists())
        self.assertFalse((self.root/'sys/kernel/config/usb_gadget/liuqin-adb/functions/rndis.usb0').exists())
        self.assertIn('functionfs',runner.call_args.args[0])
        self.assertFalse(any('telnet' in str(c) or 'udhcp' in str(c) for c in calls))
    def test_unsealed_or_wrong_device_never_configures_usb(self):
        for change in (dict(sealed=False),dict(device='other'),dict(slot='b'),dict(unlocked=False)):
            runner=Mock();spawn=Mock();value=dict(device='liuqin',sealed=True,unlocked=True,slot='a',serial='TEST');value.update(change)
            with patch.object(start.status,'collect',return_value=value),patch.object(start,'aliases') as aliases:
                with self.assertRaises(RuntimeError):start.start(self.root,runner,spawn,lambda _:None)
                aliases.assert_not_called();runner.assert_not_called();spawn.assert_not_called()
    def test_init_adb_branch_cannot_fall_through_to_network(self):
        text=(PROJECT/'device/boot/initramfs/init').read_text();branch=text[text.index('# ADB has its own USB composition'):text.index('gadget=/sys/kernel/config/usb_gadget/liuqin\n')]
        self.assertIn('while :; do sleep 60; done',branch);self.assertIn('native-adb-start.py',branch)
        self.assertNotIn('telnetd ',branch);self.assertNotIn('rndis.usb0',branch)
    def test_retired_network_recovery_rejected_before_usb_or_network(self):
        from liuqin_gui.native_recovery_transport import NativeRecoveryTransport
        from liuqin_gui.engine import Engine
        from liuqin_gui import compatibility,native_profiles
        image=self.root/'old.img';fixture.fixture(image)
        with patch.object(host,'run_process',side_effect=AssertionError('device access forbidden')),patch.object(host,'fastboot_path',side_effect=AssertionError('tool lookup forbidden')),patch('socket.socket',side_effect=AssertionError('network access forbidden')):
            with self.assertRaisesRegex(RuntimeError,'ADB Rec'):
                NativeRecoveryTransport(SimpleNamespace(manifest={'native_recovery_manifest':{'usb_transport':'rndis'}}),'TEST')
            _,status=compatibility.select_image(dict(kind='fastboot',properties={}),str(image))
            self.assertFalse(status['ready'])
            e=Engine(lambda _:None,self.root/'state');e.manifest={'installer_usb_network':'rndis'}
            try:
                with self.assertRaisesRegex(RuntimeError,'旧 USB 网络'):e.connect_ram(False)
            finally:e.close()
        with patch.object(native_profiles,'image_paths',return_value=[str(image)]):self.assertEqual(native_profiles.available(str(image)),[])
    def test_discovery_only_queries_adb_and_fastboot(self):
        from liuqin_gui.engine import Engine
        e=Engine(lambda _:None,self.root/'discovery');e.native_discovery=True
        def response(args,**kw):
            if args==['mock-fastboot','devices','-l']:return SimpleNamespace(stdout='TEST fastboot\n')
            if args==['mock-adb','devices','-l']:return SimpleNamespace(stdout='List of devices attached\n')
            raise AssertionError(args)
        try:
            with patch.object(host,'fastboot_path',return_value='mock-fastboot'),patch.object(host,'adb_path',return_value='mock-adb'),patch.object(host,'run_process',side_effect=response),patch('socket.socket',side_effect=AssertionError('network access forbidden')):
                e.list_devices()
                with patch('liuqin_gui.compatibility.adb_state',return_value=''),patch.object(host,'run_process',return_value=SimpleNamespace(stdout='')):
                    self.assertEqual(__import__('liuqin_gui.compatibility',fromlist=['']).inspect_device('TEST',native=True)['kind'],'disconnected')
        finally:e.close()
    def test_built_native_init_excludes_network_gadget_and_never_exits_pid1(self):
        builder=load('native_only_builder',PROJECT/'build/recovery/build-native-installer.py')
        init=builder.native_init().decode()
        for fragment in ('rndis.usb0','ncm.usb0','ecm.usb0','udhcpd /run','telnetd -b','192.168.7.2','start_stage1_telnet'):
            self.assertNotIn(fragment,init)
        self.assertIn('native-adb-start.py',init)
        self.assertTrue(init.endswith('while :; do sleep 60; done\n'))
    def test_adb_feature_lists_accept_line_and_comma_separators(self):
        from liuqin_gui.native_adb import features
        for raw in (b'stat_v2,shell_v2,cmd\n',b'stat_v2\nshell_v2\ncmd\n',b'shell_v2\r\n'):
            self.assertIn('shell_v2',features(raw))
        for raw in (b'',b'stat_v2,cmd',b'no_shell_v2'):
            self.assertNotIn('shell_v2',features(raw))
    def test_reverse_service_status_is_checked_separately_from_cli_exit(self):
        from liuqin_gui.native_adb import reverse_reply,ReverseBindError
        self.assertEqual(reverse_reply(b'OKAY000540001',40001),40001)
        with self.assertRaisesRegex(ReverseBindError,'denied'):reverse_reply(b'FAIL0006denied',40001)
        for raw in (b'',b'OKAY',b'OKAY000540002',b'OKAYzzzz40001',b'OKAY000640001',b'OKAY000540001extra',b'XXXX000540001'):
            with self.subTest(raw=raw),self.assertRaises(RuntimeError):reverse_reply(raw,40001)
    def test_reverse_uses_ipv4_loopback_and_retries_only_port_collisions(self):
        from liuqin_gui import native_adb
        t=native_adb.NativeAdbMixin.__new__(native_adb.NativeAdbMixin)
        t.remote=Mock(side_effect=[b'40001\n',b'40002\n']);calls=[]
        def adb(*args,**kwargs):
            calls.append(args)
            if args[0]=='raw':
                self.assertIn('reverse:forward:norebind:tcp:localhost:',args[1])
                if ':40001;' in args[1]:
                    body=b'cannot bind listener: Address already in use'
                    return b'FAIL'+f'{len(body):04x}'.encode()+body
                return b'OKAY000540002'
            self.assertEqual(args,('reverse','--remove','tcp:40002'));return b''
        t.adb=adb;server=Mock(server_port=38000)
        with patch.object(native_adb.http.server,'ThreadingHTTPServer',return_value=server) as factory,patch.object(native_adb.threading,'Thread'):
            with t.http_server(object) as url:self.assertEqual(url,'http://127.0.0.1:40002')
        factory.assert_called_once_with(('127.0.0.1',0),object)
        self.assertEqual(len(calls),3);self.assertEqual(t.remote.call_count,2)
        for call in t.remote.call_args_list:
            self.assertIn('ip link set dev lo up',call.args[0])
            self.assertNotIn('AF_INET6',call.args[0]);self.assertNotIn('usb0',call.args[0])
        server.shutdown.assert_called_once();server.server_close.assert_called_once()
    def test_reverse_failure_and_exception_cleanup_do_not_remove_others(self):
        from liuqin_gui import native_adb
        for reply,remove in [(b'FAIL0006denied',False),(b'OKAY000540002',True),(b'OKAY000540001',True)]:
            t=native_adb.NativeAdbMixin.__new__(native_adb.NativeAdbMixin);t.remote=Mock(return_value=b'40001\n')
            t.adb=Mock(return_value=reply);server=Mock(server_port=38000)
            with patch.object(native_adb.http.server,'ThreadingHTTPServer',return_value=server),patch.object(native_adb.threading,'Thread'):
                with self.assertRaises(RuntimeError):
                    with t.http_server(object):raise RuntimeError('transfer cancelled')
            removals=[c.args for c in t.adb.call_args_list if c.args[0]=='reverse']
            self.assertEqual(removals,[('reverse','--remove','tcp:40001')] if remove else [])
            self.assertEqual(t.remote.call_count,1);server.server_close.assert_called_once()
    def test_raw_cli_cannot_consume_worker_input(self):
        from liuqin_gui import native_adb
        t=native_adb.NativeAdbMixin.__new__(native_adb.NativeAdbMixin);t.adb_path='mock-adb';t.serial='TEST'
        pipe_fds=os.pipe()
        def reply(arguments,**kwargs):
            self.assertEqual(arguments,['mock-adb','-s','TEST','raw','reverse:forward:norebind:tcp:localhost:40001;tcp:38000'])
            self.assertEqual(kwargs['stdin'],pipe_fds[0])
            self.assertNotIn('input',kwargs)
            for fd in pipe_fds:os.fstat(fd)
            return SimpleNamespace(returncode=0,stdout=b'OKAY000540001',stderr=b'')
        with patch.object(native_adb.os,'pipe',return_value=pipe_fds),patch.object(host,'run_process',side_effect=reply):
            self.assertEqual(t.adb('raw','reverse:forward:norebind:tcp:localhost:40001;tcp:38000'),b'OKAY000540001')
        for fd in pipe_fds:
            with self.assertRaises(OSError):os.fstat(fd)
        with patch.object(host,'run_process',return_value=SimpleNamespace(returncode=0,stdout=b'shell_v2',stderr=b'')) as run:
            t.adb('features')
        self.assertEqual(run.call_args.kwargs['input'],b'')
        self.assertNotIn('stdin',run.call_args.kwargs)
        with patch.object(host,'run_process',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'adb: unknown command raw')):
            with self.assertRaisesRegex(RuntimeError,'Platform-Tools'):t.adb('raw','service')

    @unittest.skipIf(sys.platform=='win32','select on anonymous pipes requires POSIX')
    def test_raw_pipe_keeps_stdin_open_until_fragmented_response_finishes(self):
        from liuqin_gui.native_adb import NativeAdbMixin,reverse_reply
        script=self.root/'fake-adb'
        script.write_text('#!'+sys.executable+'\n'+'''import os,select,sys,time
ready,_,_=select.select([sys.stdin.fileno()],[],[],0.1)
if ready:
    # Model the raw service losing its pending reply after stdin EOF.
    sys.exit(0 if os.read(sys.stdin.fileno(),1)==b'' else 2)
for fragment in (b'OK',b'AY0005',b'40001'):
    os.write(sys.stdout.fileno(),fragment)
    time.sleep(0.015)
''')
        script.chmod(0o755)
        command=[str(script),'-s','TEST','raw','service']
        previous=subprocess.run(command,input=b'',capture_output=True,timeout=5,check=True)
        self.assertEqual(previous.stdout,b'')
        transport=NativeAdbMixin.__new__(NativeAdbMixin)
        transport.adb_path=str(script);transport.serial='TEST'
        response=transport.adb('raw','service',timeout=5)
        self.assertEqual(response,b'OKAY000540001')
        self.assertEqual(reverse_reply(response,40001),40001)

    def test_raw_timeout_closes_both_private_pipe_descriptors(self):
        from liuqin_gui import native_adb
        transport=native_adb.NativeAdbMixin.__new__(native_adb.NativeAdbMixin)
        transport.adb_path='mock-adb';transport.serial='TEST'
        pipe_fds=os.pipe()
        def timed_out(arguments,**kwargs):
            self.assertEqual(kwargs['stdin'],pipe_fds[0]);self.assertEqual(kwargs['timeout'],2)
            for fd in pipe_fds:os.fstat(fd)
            raise subprocess.TimeoutExpired(arguments,2)
        with patch.object(native_adb.os,'pipe',return_value=pipe_fds),patch.object(host,'run_process',side_effect=timed_out):
            with self.assertRaises(subprocess.TimeoutExpired):transport.adb('raw','service',timeout=2)
        for fd in pipe_fds:
            with self.assertRaises(OSError):os.fstat(fd)
if __name__=='__main__':unittest.main(verbosity=2)
