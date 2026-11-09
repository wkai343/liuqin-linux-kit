#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Scoped native writes and GUI workflow integration, using files and simulated devices."""
import contextlib,copy,hashlib,importlib.util,json,os,socket,subprocess,sys,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
import liuqin_native_install as gate
from liuqin_artifacts import boot_interface
from liuqin_gui import native_profiles,workflow
from liuqin_gui.native_installer import NativeInstallerMixin
from liuqin_gui.native_recovery_transport import NativeRecoveryTransport
from liuqin_gui.engine import InstallerError,Transport,digest_file
from liuqin_gui.rom_preview import demo_capture
from liuqin_gui.demo import DemoEngine,DemoTransport
from liuqin_gui.partition_demo import inspect as inventory
from liuqin_partitions import snapshot_id
def load(name,path):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
fixture=load('native_installer_fixture',PROJECT/'tests/native-recovery.py')
agent=load('native_installer_agent',PROJECT/'device/recovery/native-install.py')
M=dict(interface='liuqin-native-recovery-v1',installer_version=1,inspection_only=False,installer_capabilities=gate.CAPABILITIES,usage='temporary-boot-only',build_id='a'*64)
def part(name='boot_a',size=192*1024**2):return dict(name=name,parent='sde' if name=='boot_a' else 'sda',partuuid=str(uuid.uuid4()),start=4096,bytes=size)

class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.boot=str(uuid.uuid4());self.token=uuid.uuid4().hex
        self.put(gate.MARKER,M);self.put('proc/sys/kernel/random/boot_id',self.boot);self.p=part()
        self.probe=dict(device='liuqin',serial='TEST',slot='a',unlocked=True,sealed=True,boot_id=self.boot,build_id=M['build_id'])
        self.request=dict(token=self.token,plan_id='b'*64,boot_id=self.boot,build_id=M['build_id'],serial='TEST',scope=dict(kind='android',parts=[self.p],previous_boot_sha256='c'*64,gpt_sha256=None,root_name=None,root_archive_sha256=None))
        self.env=patch.dict(os.environ,{gate.TOKEN:self.token});self.env.start();self.addCleanup(self.env.stop)
    def put(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value if isinstance(value,str) else json.dumps(value))
    def start(self):return gate.begin(self.request,self.probe,self.root)
    def test_readonly_image_and_missing_confirmation_cannot_write(self):
        with self.assertRaises(RuntimeError):gate.authorize_target(self.p,root=self.root)
        self.put(gate.MARKER,dict(M,inspection_only=True))
        with self.assertRaises(RuntimeError):self.start()
        gate.authorize_action('partition-inspect',{},self.root)
    def test_exact_partition_and_boot_session_boundaries(self):
        self.start();gate.authorize_target(self.p,root=self.root)
        for changed in [dict(self.p,name='abl_a'),dict(self.p,partuuid=str(uuid.uuid4())),dict(self.p,start=8192),dict(self.p,bytes=4096)]:
            with self.assertRaises(RuntimeError):gate.authorize_target(changed,root=self.root)
        with self.assertRaises(RuntimeError):gate.require('wrong',self.root)
        self.put('proc/sys/kernel/random/boot_id',str(uuid.uuid4()))
        with self.assertRaises(RuntimeError):gate.require(self.token,self.root)
    def test_protected_or_duplicate_partitions_rejected_at_begin(self):
        for parts in ([part('abl_a')],[part('recovery_a')],[self.p,self.p]):
            self.request['scope']['parts']=parts
            with self.assertRaises(ValueError):self.start()
    def test_partition_scope_includes_metadata_and_retiring_store_journal(self):
        userdata=part('userdata');metadata=part('metadata');old=part('linux_boot');new=part('linux_boot')
        snap=dict(kind='partitions',target='',source=None,request={'set_default':False},recovery_mode='off',previous_boot_sha256='c'*64,
            edit=dict(source={'inventory':{'userdata':userdata,'metadata':metadata,'linux_boot':old}},projected={'inventory':{'userdata':userdata,'metadata':metadata,'linux_boot':new}},
                formats=[dict(role='android',partition=userdata)],partitions=[],deletes=[old],changes=True))
        s=gate.scope(snap);gate.validate_scope(s)
        self.assertIn(gate.identity(metadata),s['parts']);self.assertIn(gate.identity(old),s['parts']);self.assertIn(gate.identity(new),s['parts'])
        del snap['edit']['projected']['inventory']['linux_boot'];s=gate.scope(snap)
        self.assertIn(gate.identity(old),s['parts'])
        old['name']='linux_r_9748571b5637'
        with self.assertRaises(ValueError):
            gate.scope(snap)
    def test_gpt_plan_and_action_type_must_match(self):
        plan=dict(deletes=['userdata']);self.request['scope']['gpt_sha256']=gate.digest(plan);self.start()
        gate.authorize_gpt(plan,self.root)
        with self.assertRaises(RuntimeError):gate.authorize_gpt(dict(deletes=['abl_a']),self.root)
        with self.assertRaises(RuntimeError):gate.authorize_action('linux-stage',dict(native_token=self.token),self.root)
        with self.assertRaises(RuntimeError):gate.authorize_action('arbitrary-write',dict(native_token=self.token),self.root)
    def test_switch_scope_is_limited_to_boot_and_store(self):
        store=part('linux_boot',size=4096)
        plan=dict(parts=[self.p,store],previous_boot_sha256='c'*64)
        self.request['scope']=gate.scope(dict(kind='switch',switch_plan=plan))
        with self.assertRaisesRegex(ValueError,'不支持切换'):
            self.start()
        self.put(gate.MARKER,dict(M,boot_switch_version=1))
        self.start()
        gate.authorize_target(self.p,root=self.root)
        gate.authorize_target(store,root=self.root)
        with self.assertRaises(RuntimeError):gate.authorize_target(part('userdata'),root=self.root)
        with self.assertRaises(RuntimeError):gate.authorize_action('partition-edit',dict(native_token=self.token),self.root)
        with self.assertRaises(RuntimeError):gate.authorize_gpt({},self.root)
    def test_android_guard_uses_native_transport_and_android_scope(self):
        transport=NativeRecoveryTransport.__new__(NativeRecoveryTransport)
        transport.engine=SimpleNamespace(manifest={'native_recovery_manifest':M})
        transport.boot_id=self.boot
        transport.status=Mock()
        plan={'transaction':str(uuid.uuid4())}
        request={'plan':plan}

        def recovery_rpc(transport_arg,action,forwarded):
            self.assertIs(transport_arg,transport)
            self.assertEqual(action,'guard')
            self.assertEqual(forwarded,dict(request,native_token=self.token))
            gate.authorize_action(action,forwarded,self.root)
            return {'guarded':True}

        with patch.object(Transport,'android_rpc',autospec=True,side_effect=recovery_rpc) as rpc:
            with self.assertRaisesRegex(InstallerError,'尚未确认'):
                transport.android_rpc('guard',request)
            rpc.assert_not_called()
            transport.engine.native_write_session=SimpleNamespace(active=True,boot_id=self.boot,token=self.token)
            self.start()
            self.assertEqual(transport.android_rpc('guard',request),{'guarded':True})
            self.assertEqual(request,{'plan':plan})
            self.assertNotIn('write_started',gate.session(self.root))
            gate.finish(dict(token=self.token,success=False),self.probe,self.root)

            root=part('lqr_123456789abc',size=4096)
            linux=dict(kind='linux',parts=[root],root_name=root['name'],root_archive_sha256='d'*64)
            switch=gate.scope(dict(kind='switch',switch_plan=dict(
                parts=[self.p,part('linux_boot',size=4096)],previous_boot_sha256='c'*64)))
            self.put(gate.MARKER,dict(M,boot_switch_version=1))
            for scope,error in ((linux,'系统类型不一致'),(switch,'切换会话禁止')):
                self.request['scope']=scope
                self.start()
                with self.assertRaisesRegex(RuntimeError,error):
                    transport.android_rpc('guard',request)
                self.assertNotIn('write_started',gate.session(self.root))
                gate.finish(dict(token=self.token,success=False),self.probe,self.root)
    def test_scope_tampering_and_wrong_image_rejected(self):
        self.start();s=gate.session(self.root);s['scope']['parts']=[];gate.store(s,self.root)
        with self.assertRaises(RuntimeError):gate.require(self.token,self.root)
        self.put(gate.MARKER,dict(M,build_id='d'*64))
        with self.assertRaises(RuntimeError):gate.require(self.token,self.root)
    def test_fixed_installer_keeps_token_scope_and_boot_session_checks(self):
        self.put(gate.MARKER,dict(M,usage=gate.FIXED_USAGE,fixed_installer_version=1))
        self.start();gate.authorize_target(self.p,root=self.root)
        with self.assertRaises(RuntimeError):gate.authorize_target(part('recovery_a'),root=self.root)
        with self.assertRaises(RuntimeError):gate.require('other-token',self.root)
        self.put('proc/sys/kernel/random/boot_id',str(uuid.uuid4()))
        with self.assertRaises(RuntimeError):gate.require(self.token,self.root)
    def test_failure_after_write_locks_recovery_but_prewrite_abort_can_retry(self):
        self.start();r=gate.finish(dict(token=self.token,success=False),self.probe,self.root);self.assertEqual(r['state'],'complete')
        self.start();gate.mark_write(self.root);r=gate.finish(dict(token=self.token,success=False),self.probe,self.root);self.assertEqual(r['state'],'failed')
        with self.assertRaises(RuntimeError):self.start()
    def test_success_requires_storage_resealed(self):
        self.start();gate.mark_write(self.root)
        self.assertEqual(gate.finish(dict(token=self.token,success=True),dict(self.probe,sealed=False),self.root)['state'],'failed')
    def test_only_scoped_recovery_can_follow_a_failed_session(self):
        self.put(gate.MARKER,dict(M,boot_store_version=1))
        self.start();gate.mark_write(self.root)
        old=gate.session(self.root)
        plan=dict(operation='restore-switch',parts=[self.p,part('linux_boot',size=4096)],previous_boot_sha256='c'*64)
        self.request['scope']=gate.scope(dict(kind='maintenance',maintenance_plan=plan))
        with self.assertRaisesRegex(RuntimeError,'前一次安装'):self.start()
        gate.finish(dict(token=self.token,success=False),self.probe,self.root)
        self.start()
        lease=gate.session(self.root)
        self.assertEqual(lease['superseded_session']['plan_id'],old['plan_id'])
        self.assertEqual(lease['scope']['kind'],'maintenance')
        for name in ('userdata','metadata','abl_a'):
            with self.assertRaises(RuntimeError):gate.authorize_target(part(name),root=self.root)
        with self.assertRaises(RuntimeError):gate.authorize_action('linux-stage',dict(native_token=self.token),self.root)
    def test_cleanup_cannot_hide_an_unrelated_failed_install(self):
        self.put(gate.MARKER,dict(M,boot_store_version=1))
        self.start();gate.mark_write(self.root);gate.finish(dict(token=self.token,success=False),self.probe,self.root)
        plan=dict(operation='cleanup',parts=[part('linux_boot',size=4096)],previous_boot_sha256='c'*64)
        self.request['scope']=gate.scope(dict(kind='maintenance',maintenance_plan=plan))
        with self.assertRaisesRegex(RuntimeError,'前一次安装'):self.start()
    def test_boot_store_connection_requires_declared_maintenance_capability(self):
        transport=NativeRecoveryTransport.__new__(NativeRecoveryTransport)
        transport.serial='TEST'
        manifest=dict(M,boot_store_version=1)
        transport.engine=SimpleNamespace(manifest={'native_recovery_manifest':manifest},receipt={'operation':'boot-store'})
        transport.probe_current=Mock(return_value=dict(self.probe,serial='TEST'))
        transport.validate_probe=Mock();transport.verify_ram_interface=Mock()
        transport.remote=Mock(return_value=b'201326592\n')
        value=transport.check_device()
        self.assertEqual(value['product'],'liuqin');self.assertEqual(value['boot_bytes'],201326592)
        self.assertTrue(transport.inspection_attached)
        transport.engine.manifest['native_recovery_manifest']=M
        transport.probe_current.reset_mock()
        with self.assertRaises(InstallerError):transport.check_device()
        transport.probe_current.assert_not_called()
    def test_candidate_manifest_and_explicit_selection(self):
        p=self.root/'candidate.img';m=fixture.fixture(p,installer=True);info=boot_interface(p,installer=True);self.assertTrue(gate.capable(info['native_recovery_manifest']))
        s,_=demo_capture('TEST');s['runtime_build_id']=m['build_id'];r=native_profiles.native_image(p)
        selected,status=native_profiles.selection(s,str(p),[r]);self.assertEqual(selected,str(p.resolve()));self.assertTrue(status['install_allowed']);self.assertFalse(status['auto_selected']);self.assertFalse(status['automatic_boot_allowed'])
        s['runtime_build_id']='f'*64;_,status=native_profiles.selection(s,str(p),[r]);self.assertFalse(status['ready']);self.assertFalse(status['install_allowed'])
    def test_unknown_firmware_does_not_allow_install(self):
        from liuqin_firmware import fingerprint_id
        s,_=demo_capture('TEST');slot=s['slots']['a'];slot['match']['abl']['sha256']='f'*64;slot['measured']=copy.deepcopy(slot['match']);slot['fingerprint_id']=fingerprint_id(slot['match'])
        self.assertFalse(native_profiles.installer_status(s,M)['install_allowed'])
    def test_android_anti_is_never_guessed(self):
        t=NativeInstallerMixin();t.engine=type('E',(),{})();t.engine.manifest={'native_recovery_manifest':M};t.engine.android_profile={'anti_version':1}
        for value in (None,'1',2):
            t.engine.device={'anti_version':value}
            with self.assertRaises(InstallerError):t.validate_android_install()
        t.engine.device={'anti_version':1};t.validate_android_install()

class BootWriterTests(unittest.TestCase):
    def test_real_write_context_reseals_after_failure(self):
        device=load('native_write_context',PROJECT/'device/installer/android-device.py');h=object.__new__(device.Hardware);p=dict(part(),node='sde1',dev='8:1',parent_dev='8:0')
        state={10:1,11:1};calls=[];h.sealed=Mock()
        @contextlib.contextmanager
        def node(name,dev,writable=False):yield 10 if name=='sde' else 12 if writable else 11
        h.node=node
        def set_ro(fd,value):state[fd]=value;calls.append((fd,value))
        with patch.object(gate,'authorize_target'),patch.object(gate,'mark_write') as started,patch.object(device,'ro',side_effect=lambda fd:state[fd]),patch.object(device,'set_ro',side_effect=set_ro),patch.object(device.os,'sync'):
            with self.assertRaisesRegex(RuntimeError,'write failed'):
                with h.writable(p,{'boot_a'}):raise RuntimeError('write failed')
        started.assert_called_once();self.assertEqual(state,{10:1,11:1});self.assertEqual(calls,[(10,0),(11,0),(11,1),(10,1)])
    def test_complete_download_hash_before_write_and_readback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'old';new=root/'new';size=192*1024**2
            for p in (old,new):
                with p.open('wb') as f:f.truncate(size)
            with new.open('r+b') as f:f.write(b'ANDROID!')
            previous=digest_file(old);expected=digest_file(new);p=part();writes=[]
            class Hardware:
                def inventory(self):return {'boot_a':p}
                def sealed(self,parts):pass
                def digest(self,part):return digest_file(old)
                @contextlib.contextmanager
                def writable(self,part,allowed):
                    self.assert_allowed=allowed;writes.append(part['name'])
                    with old.open('r+b') as f:yield f.fileno()
                def write(self,fd,offset,data):os.pwrite(fd,data,offset)
            h=Hardware();request=dict(token='t',bytes=size,previous_sha256=previous,sha256=expected,url='http://127.0.0.1:8888/'+'a'*32+'/boot')
            lease=dict(token='t',scope=dict(previous_boot_sha256=previous));original_tmp=tempfile.TemporaryDirectory
            with patch.object(gate,'require',return_value=lease),patch.object(gate,'authorize_target'),patch.object(agent.tempfile,'TemporaryDirectory',side_effect=lambda **kw:original_tmp(dir=root)):
                with self.assertRaises(RuntimeError):agent.write_boot(dict(request,sha256='f'*64),h,lambda *a,**kw:new.open('rb'))
                self.assertFalse(writes);self.assertEqual(digest_file(old),previous)
                result=agent.write_boot(request,h,lambda *a,**kw:new.open('rb'))
                self.assertTrue(result['boot_readback_verified']);self.assertEqual(digest_file(old),expected);self.assertEqual(writes,['boot_a'])

class NativeDemo(DemoTransport):
    installer_enabled=True
    def boot_ram(self):
        self.ram_active=True;self.boot_id=self.engine.native_demo_boot
    def preflight(self,require_archive=True):
        self.engine.receipt['firmware_fingerprint']={'simulated_native_firmware':1}
        return dict(battery=87,available_memory_bytes=6*1024**3,native_install_allowed=True)
    def native_rpc(self,action,request,image=None):
        probe=dict(device='liuqin',serial=self.serial,slot='a',unlocked=True,sealed=True,boot_id=self.boot_id,build_id=M['build_id'])
        if action=='begin':return gate.begin(request,probe,self.engine.gate_root)
        if action=='finish':return gate.finish(request,probe,self.engine.gate_root)
        raise AssertionError(action)
    def android_rpc(self,action,request):
        if action in gate.WRITE_ACTIONS:
            token=self.engine.native_write_session.token;gate.authorize_action(action,dict(request,native_token=token),self.engine.gate_root)
            if action in ('partition-edit','partition-resume'):gate.authorize_gpt(request['plan'],self.engine.gate_root)
            if action!='guard':gate.mark_write(self.engine.gate_root)
        elif action not in gate.READ_ACTIONS:
            raise InstallerError('不支持该原生安装操作。')
        return super().android_rpc(action,request)
    def write_native_boot(self,image,expected,previous):
        self.engine.native_calls.append('native-boot-write')
        if self.current_boot_digest()!=previous or digest_file(Path(image))!=expected:raise AssertionError('boot mismatch')
        p=inventory(self.engine)['inventory']['boot_a'];gate.authorize_target(p,self.engine.native_write_session.token,self.engine.gate_root)
        self.engine.demo_live_boot=Path(image).read_bytes();self.engine.receipt.update(boot_written=True,boot_readback_verified=True,active_slot_set=False)
    def install_root(self,*a,**kw):
        s=gate.require(self.engine.native_write_session.token,self.engine.gate_root);gate.authorize_target(inventory(self.engine)['inventory'][kw['system']['partition']['name']],s['token'],self.engine.gate_root)
        with patch.dict(os.environ,{gate.TOKEN:s['token']}):gate.authorize_root(kw['system']['partition']['name'],kw['system']['source']['archive_sha256'],self.engine.gate_root)
        self.engine.native_calls.append('native-root');return super().install_root(*a,**kw)
    def status(self,phase):self.engine.native_calls.append('status:'+phase)
    def cancel_cleanup(self):self.engine.native_calls.append('retained')
    def fastboot(self,*a,**kw):raise AssertionError('native installation must not call Fastboot')
    def return_fastboot(self):raise AssertionError('native installation must stay in Recovery')

class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.e=DemoEngine(lambda _:None,self.root/'state');self.addCleanup(self.e.close);self.e.pause=lambda _:None;self.e.transport_factory=NativeDemo
        self.e.native_demo_boot=str(uuid.uuid4());self.e.native_calls=[];self.e.gate_root=self.root/'device'
        self.native_manifest=dict(M,combined_install_version=1)
        for name,value in [(gate.MARKER,json.dumps(self.native_manifest)),('proc/sys/kernel/random/boot_id',self.e.native_demo_boot)]:
            p=self.e.gate_root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
        for name in ('validate_installer','validate_bundle'):
            original=getattr(self.e,name)
            def validated(*a,_original=original,**kw):
                result=_original(*a,**kw);self.e.manifest['native_recovery_manifest']=copy.deepcopy(self.native_manifest);return result
            setattr(self.e,name,validated)
        for mock in (patch.object(socket,'socket',side_effect=AssertionError('network forbidden')),patch.object(subprocess,'run',side_effect=AssertionError('real process forbidden'))):mock.start();self.addCleanup(mock.stop)
        workflow.scan(self.e,dict(serial='DEMO-LIUQIN'))
    def req(self):return dict(serial='DEMO-LIUQIN',snapshot_id=snapshot_id(inventory(self.e)),backup_parent=str(self.root/'backups'),actions=[],partition_config=dict(roots=[],userdata_gib=0,boot_gib=2),target='',recovery_mode='off',set_default=True)
    def install(self,r):
        workflow.prepare(self.e,r)
        self.assertFalse(self.e.receipt['write_dispatched'])
        workflow.install(self.e,dict(session_id=self.e.session_id,plan_id=self.e.plan_id,confirmation=dict(serial='DEMO-LIUQIN',accept_operations=True,allow_experimental=True)))
        self.assertTrue(self.e.receipt['completed']);self.assertTrue(self.e.receipt['recovery_retained']);self.assertEqual(gate.session(self.e.gate_root)['state'],'complete')
    def test_partition_linux_install_and_default_boot_stay_in_rec(self):
        r=self.req();old=inventory(self.e)['inventory']['userdata'];id=str(uuid.uuid4());name='lqr_'+id.replace('-','')[:12]
        r.update(actions=[dict(name='userdata',partuuid=old['partuuid'],action='delete')],partition_config=dict(roots=[dict(id=id,name='Linux',size_gib=32,filesystem='ext4',bundle='',boot_image='')],userdata_gib=32,boot_gib=2),target=name,linux_bundle=str(self.e.demo_bundle))
        self.install(r);self.assertIn('native-root',self.e.native_calls);self.assertIn('native-boot-write',self.e.native_calls)
    def test_android_direct_and_default_boot_stay_in_rec(self):
        r=self.req();r.update(target='userdata',rom_path='demo-rom')
        self.install(r);self.assertIn('native-boot-write',self.e.native_calls)

    def test_partition_only_finish_retains_cleanup_result_in_receipt_and_event(self):
        self.test_partition_linux_install_and_default_boot_stay_in_rec()
        original=NativeDemo.native_rpc
        reserved_id=str(uuid.uuid4())
        for status in ('complete','deferred'):
            result=dict(status=status,message='fixture cleanup '+status)
            def rpc(transport,action,request,image=None):
                reply=original(transport,action,request,image)
                if action=='finish':reply['retention']=copy.deepcopy(result)
                return reply
            events=[];self.e.emit_callback=events.append
            r=self.req();r.update(target='',install_targets=[],set_default=False,default_target='')
            if status=='complete':
                r['partition_config']['roots']=[dict(id=reserved_id,name='Reserved',size_gib=16,filesystem='ext4')]
            else:
                reserved=inventory(self.e)['inventory']['lqr_'+reserved_id.replace('-','')[:12]]
                r['actions']=[dict(name=reserved['name'],partuuid=reserved['partuuid'],action='delete')]
            with patch.object(NativeDemo,'native_rpc',rpc):self.install(r)
            self.assertEqual(self.e.receipt['boot_store_retention'],result)
            completed=next(e for e in reversed(events) if e['type']=='wizard_complete')
            self.assertEqual(completed['receipt']['boot_store_retention'],result)
            saved=json.loads((Path(self.e.receipt['run_dir'])/'receipt.json').read_text())
            self.assertEqual(saved['boot_store_retention'],result)
    def test_android_managed_preserves_linux_and_uses_native_boot_commit(self):
        self.test_partition_linux_install_and_default_boot_stay_in_rec()
        before=copy.deepcopy(inventory(self.e));linux=next(s for s in before['registry']['systems'] if s['type']=='linux')
        r=self.req();r.update(target='userdata',rom_path='demo-rom');self.install(r)
        after=inventory(self.e)
        self.assertEqual(before['inventory'][linux['partition']['name']],after['inventory'][linux['partition']['name']])
        self.assertEqual(next(s for s in after['registry']['systems'] if s['id']==linux['id']),linux)
        self.assertNotIn(linux['partition']['name'],{p['name'] for p in gate.session(self.e.gate_root)['scope']['parts']})
    def combined(self,android=True):
        r=self.req();old=inventory(self.e)['inventory']['userdata'];ids=[str(uuid.uuid4()) for _ in range(2)];names=['lqr_'+v.replace('-','')[:12] for v in ids]
        r.update(actions=[dict(name='userdata',partuuid=old['partuuid'],action='delete')],
            partition_config=dict(roots=[dict(id=v,name='Linux '+str(i),size_gib=32,filesystem='ext4') for i,v in enumerate(ids)],userdata_gib=32,boot_gib=2),
            install_targets=(['userdata'] if android else [])+names,linux_bundle=str(self.e.demo_bundle),rom_path='demo-rom',default_target=names[-1])
        return r,names
    def test_combined_android_and_linux_stay_in_rec_and_bind_each_archive(self):
        r,names=self.combined();self.install(r)
        self.assertEqual(self.e.native_calls.count('native-root'),2);self.assertEqual(self.e.native_calls.count('native-boot-write'),1)
        scope=gate.session(self.e.gate_root)['scope'];self.assertEqual(scope['kind'],'combined');self.assertTrue(scope['android_install'])
        self.assertEqual(set(scope['linux_installs']),set(names));self.assertEqual(scope['default_target'],names[-1])
    def test_combined_linux_only_does_not_authorize_android(self):
        r,names=self.combined(False);self.install(r);scope=gate.session(self.e.gate_root)['scope'];gate.validate_scope(scope)
        self.assertFalse(scope['android_install']);self.assertNotIn('super',{p['name'] for p in scope['parts']})
    def test_older_rec_rejects_combined_before_any_write(self):
        self.native_manifest.pop('combined_install_version');r,_=self.combined()
        with self.assertRaisesRegex(InstallerError,'新版 Rec'):workflow.prepare(self.e,r)
        self.assertFalse(self.e.receipt['write_dispatched']);self.assertNotIn('native-root',self.e.native_calls)
    def test_failure_keeps_rec_and_blocks_new_write_session(self):
        r=self.req();r.update(target='userdata',rom_path='demo-rom');workflow.prepare(self.e,r)
        with patch.object(NativeDemo,'write_native_boot',side_effect=RuntimeError('boot commit failed')):
            with self.assertRaisesRegex(RuntimeError,'boot commit failed'):
                workflow.install(self.e,dict(session_id=self.e.session_id,plan_id=self.e.plan_id,confirmation=dict(serial='DEMO-LIUQIN',accept_operations=True,allow_experimental=True)))
        self.assertTrue(self.e.receipt['recovery_retained']);self.assertEqual(gate.session(self.e.gate_root)['state'],'failed');self.assertTrue(self.e.receipt['recovery_required'])

if __name__=='__main__':unittest.main(verbosity=2)
