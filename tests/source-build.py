#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the official-root workflow on files and synthetic ROM structures."""
import contextlib
import codecs
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'build/lib'),str(PROJECT/'shared')]
import android_images as android
import source_build as helpers
import source_flow as flow
import speaker_firmware as speaker


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

root_builder=load('official_root_test',PROJECT/'build/scripts/build-official-root.py')
builder=load('devbox_test',PROJECT/'build/devbox/build.py')


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.project=self.root/'isolated-project';self.project.mkdir()
        # Read build sources without sharing the real build cache or its lock.
        (self.project/'build').symlink_to(PROJECT/'build',target_is_directory=True)
    def tearDown(self):self.tmp.cleanup()

    def test_target_source_lock_does_not_change_the_installer_baseline(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        pipeline=flow.Flow(data,lambda *_a,**_k:self.fail('unexpected build'),distribution='fedora')
        self.assertEqual(pipeline.kernel_source_lock(),PROJECT/'build/kernel/source.json')
        self.assertEqual(pipeline.kernel_source_lock(True),PROJECT/'build/kernel/source-6.17.json')
        target=json.loads(pipeline.kernel_source_lock().read_text())
        installer=json.loads(pipeline.kernel_source_lock(True).read_text())
        self.assertEqual(target['branch'],'v7.3')
        self.assertEqual(installer['commit'],'fe81794b5e1b8a3f11a165f3acbfdb8fa671dbb3')
        data['kernel_source_lock']='custom/target.json'
        self.assertEqual(pipeline.kernel_source_lock(),PROJECT/'custom/target.json')
        self.assertEqual(pipeline.kernel_source_lock(True),PROJECT/'build/kernel/source-6.17.json')

    def test_verified_download_reuses_cache_and_rejects_changed_bytes(self):
        payload=b'official test bytes';target=self.root/'base.tar.gz';expected=hashlib.sha256(payload).hexdigest()
        with patch.object(helpers.urllib.request,'urlopen',return_value=io.BytesIO(payload)) as network:
            helpers.download('https://example.invalid/base',target,expected)
            helpers.download('https://example.invalid/base',target,expected)
            self.assertEqual(network.call_count,1)
            target.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'缓存文件校验失败'):helpers.download('https://example.invalid/base',target,expected)
            self.assertEqual(network.call_count,1)

    def test_current_source_tree_lock_preserves_hardware_test_history_and_rejects_mismatches(self):
        project=self.root/'kernel-tree-lock';source=project/'kernel';source.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(['git','-C',str(source),*args],text=True).strip()
        git('init','-q')
        (source/'driver.c').write_text('old\n')
        git('add','driver.c')
        git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','commit','-qm','base')
        base=git('rev-parse','HEAD');tested_tree=git('rev-parse','HEAD^{tree}')
        patch_path=project/'build/kernel/patches/fix.patch';patch_path.parent.mkdir(parents=True)
        patch_path.write_text('--- a/driver.c\n+++ b/driver.c\n@@ -1 +1 @@\n-old\n+fixed\n')
        (project/'build/lib').mkdir()
        shutil.copyfile(PROJECT/'build/lib/kernel_adaptation.py',project/'build/lib/kernel_adaptation.py')
        git('apply','--cached',str(patch_path));expected_tree=git('write-tree');git('read-tree','HEAD')
        record=dict(commit=base,target_patches=[str(patch_path.relative_to(project))],
                    tested_source_commit=base,tested_source_tree=tested_tree,expected_source_tree=expected_tree)
        lock=project/'source.json'
        pipeline=object.__new__(flow.Flow)
        pipeline.data={'kernel_source':str(source)};pipeline.code={};pipeline.memo={}
        pipeline.cache=helpers.Cache(project/'cache',None)
        cases=(
            ({'expected_source_tree':expected_tree,'tested_source_tree':tested_tree},True),
            ({'expected_source_tree':tested_tree,'tested_source_tree':expected_tree},False),
            ({'tested_source_tree':expected_tree},True),
            ({'tested_source_tree':tested_tree},False),
            ({'expected_source_tree':'','tested_source_tree':tested_tree},False),
        )
        with patch.object(flow,'PROJECT',project),patch.object(pipeline,'kernel_source_lock',return_value=lock):
            for fields,accepted in cases:
                with self.subTest(fields=fields):
                    current={k:v for k,v in record.items() if k not in ('expected_source_tree','tested_source_tree')}
                    current.update(fields);lock.write_text(json.dumps(current))
                    if accepted:
                        result,commit=pipeline.kernel_source()
                        self.assertEqual((result/'driver.c').read_text(),'fixed\n')
                        tree=subprocess.check_output(['git','-C',str(result),'rev-parse','HEAD^{tree}'],text=True).strip()
                        self.assertEqual(tree,expected_tree)
                        self.assertNotEqual(commit,base)
                        self.assertEqual(json.loads(lock.read_text()),current)
                    else:
                        with self.assertRaisesRegex(ValueError,'源码树与核验记录不同'):
                            pipeline.kernel_source()
        self.assertEqual(git('rev-parse','HEAD'),base)
        self.assertEqual(git('status','--porcelain'),'')
        self.assertEqual((source/'driver.c').read_text(),'old\n')

    def test_cache_changes_only_the_requested_stage_and_detects_tampering(self):
        cache=helpers.Cache(self.root/'cache',None);calls=[]
        def build(out):calls.append(out);(out/'image').write_bytes(b'complete')
        first=cache.stage('kernel',{'config':'zram'},build,['image'])
        installer=cache.stage('installer',{'protocol':2},build,['image'])
        desktop=cache.stage('rootfs',{'packages':['plasma']},build,['image'])
        cache.stage('rootfs',{'packages':['plasma','editor']},build,['image'])
        self.assertEqual(cache.stage('kernel',{'config':'zram'},build,['image']),first)
        self.assertEqual(cache.stage('installer',{'protocol':2},build,['image']),installer)
        self.assertEqual(len(calls),4)
        (desktop/'image').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'缓存损坏'):cache.stage('rootfs',{'packages':['plasma']},build,['image'])

    def test_failed_stage_is_retained_but_never_published(self):
        cache=helpers.Cache(self.root/'cache',None)
        def fail(out):(out/'partial').write_bytes(b'incomplete');raise RuntimeError('simulated failure')
        with self.assertRaises(RuntimeError):cache.stage('rootfs',{},fail,['image'])
        self.assertEqual(len(list((self.root/'cache').glob('.attempt-*'))),1)
        self.assertEqual(list((self.root/'cache').glob('rootfs-*')),[])

    def test_tree_cache_checks_link_and_mode_changes(self):
        cache=helpers.Cache(self.root/'cache',None)
        def build(out):
            (out/'tree').mkdir();(out/'tree/file').write_bytes(b'data');(out/'tree/link').symlink_to('file')
        output=cache.stage('board',{},build,['tree'])
        (output/'tree/link').unlink();(output/'tree/link').symlink_to('other')
        with self.assertRaisesRegex(ValueError,'缓存损坏'):cache.stage('board',{},build,['tree'])

    def test_plan_does_not_download_compile_or_access_a_tablet(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        with patch.object(helpers,'download',side_effect=AssertionError('network')), \
             patch.object(flow.subprocess,'check_output',side_effect=AssertionError('Git or device')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            flow.execute(data,'plan',lambda *_a,**_k:self.fail('unexpected command'))
        result=json.loads(output.getvalue());self.assertFalse(result['tablet_access'])
        self.assertEqual(result['recipe']['desktop'],'plasma')
        self.assertEqual(result['usb_device'],{'usb_manufacturer':'Xiaomi','usb_product':'Pad 6 Pro'})

    def test_usb_settings_reach_both_images_and_only_change_image_cache_inputs(self):
        configurations = [builder.config(PROJECT/'build/devbox/build.example.json') for _ in range(2)]
        configurations[1].update(usb_manufacturer='我的设备',usb_product='小米平板')
        stages = []
        for data in configurations:
            pipeline=flow.Flow(data,lambda *_a,**_k:None,distribution='fedora')
            records=[]
            def stage(name, dependency, build, artifacts):
                out=self.root/('usb-'+str(len(stages))+'-'+name);out.mkdir()
                build(out)
                records.append((name,helpers.key(dependency)))
                return out
            with patch.object(pipeline,'kernel',return_value=(self.root,self.root)), \
                 patch.object(pipeline,'modules',return_value=self.root), \
                 patch.object(pipeline,'runtime',return_value=self.root), \
                 patch.object(pipeline,'board',return_value=self.root), \
                 patch.object(pipeline,'board_inputs',return_value={}), \
                 patch.object(flow,'sha',side_effect=lambda path:str(path)), \
                 patch.object(flow,'tree_identity',side_effect=lambda path:str(path)), \
                 patch.object(pipeline,'stage',side_effect=stage),patch.object(pipeline,'run') as run:
                pipeline.boot();pipeline.boot(True)
            self.assertEqual(len(run.call_args_list),2)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs['env']['LIUQIN_USB_PRODUCT'],data['usb_product'])
                self.assertEqual(call.kwargs['env']['LIUQIN_USB_MANUFACTURER'],data['usb_manufacturer'])
            self.assertEqual([name for name,_ in records],['boot','installer'])
            stages.append(dict(records))
        for name in ('boot','installer'):
            self.assertNotEqual(stages[0][name],stages[1][name])

    def test_boot_argument_changes_invalidate_only_the_selected_image(self):
        device=self.project/'device';device.mkdir()
        for name in ('native-bootargs.txt','installer-bootargs.txt'):
            shutil.copyfile(PROJECT/'device'/name,device/name)
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        pipeline=flow.Flow(data,lambda *_a,**_k:self.fail('unexpected build'),distribution='fedora')
        records={}
        def capture(name,dependency,_build,_artifacts):
            records[name]=helpers.key(dependency)
            return self.root
        def digest(path):
            return helpers.sha(path) if path.name in ('native-bootargs.txt','installer-bootargs.txt') else str(path)
        with patch.object(flow,'PROJECT',self.project), \
             patch.object(pipeline,'kernel',return_value=(self.root,self.root)), \
             patch.object(pipeline,'modules',return_value=self.root), \
             patch.object(pipeline,'runtime',return_value=self.root), \
             patch.object(pipeline,'board',return_value=self.root), \
             patch.object(pipeline,'board_inputs',return_value={}), \
             patch.object(flow,'sha',side_effect=digest), \
             patch.object(flow,'tree_identity',side_effect=lambda path:str(path)), \
             patch.object(pipeline,'stage',side_effect=capture):
            pipeline.boot();pipeline.boot(True);first=dict(records)
            path=device/'native-bootargs.txt';path.write_text(path.read_text()+' consoleblank=0\n')
            pipeline.boot();pipeline.boot(True);second=dict(records)
            self.assertNotEqual(first['boot'],second['boot'])
            self.assertEqual(first['installer'],second['installer'])
            path=device/'installer-bootargs.txt';path.write_text(path.read_text()+' consoleblank=0\n')
            pipeline.boot();pipeline.boot(True)
            self.assertEqual(second['boot'],records['boot'])
            self.assertNotEqual(second['installer'],records['installer'])

    def test_splash_runtime_reaches_only_target_boot_and_has_an_independent_cache_identity(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        pipeline=flow.Flow(data,lambda *_a,**_k:None,distribution='fedora')
        runtime=self.root/'splash-runtime';runtime.mkdir();revision=['first'];records={}
        def identity(path):return revision[0] if path==runtime else str(path)
        def capture(name,dependency,build,_artifacts):
            records[name]=helpers.key(dependency);build(self.root/name);return self.root/name
        with patch.object(pipeline,'kernel',return_value=(self.root,self.root)), \
             patch.object(pipeline,'modules',return_value=self.root), \
             patch.object(pipeline,'runtime',return_value=self.root), \
             patch.object(pipeline,'board',return_value=self.root), \
             patch.object(pipeline,'board_inputs',return_value={}), \
             patch.object(pipeline,'boot_splash_runtime',return_value=runtime) as splash, \
             patch.object(flow,'sha',side_effect=lambda path:str(path)), \
             patch.object(flow,'tree_identity',side_effect=identity), \
             patch.object(pipeline,'stage',side_effect=capture),patch.object(pipeline,'run') as run:
            pipeline.boot();pipeline.boot(True);before=dict(records)
            self.assertEqual(run.call_args_list[0].kwargs['env']['BOOT_SPLASH_RUNTIME'],str(runtime))
            self.assertNotIn('BOOT_SPLASH_RUNTIME',run.call_args_list[1].kwargs['env'])
            revision[0]='updated'
            pipeline.boot();pipeline.boot(True)
            self.assertNotEqual(before['boot'],records['boot'])
            self.assertEqual(before['installer'],records['installer'])
            self.assertEqual(splash.call_count,2)

    def test_missing_rom_stops_full_build_before_network_or_privileged_work(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json');data['rom_images']=str(self.root/'missing')
        with patch.object(flow,'PROJECT',self.project), patch.object(flow,'download',side_effect=AssertionError('network')):
            with self.assertRaisesRegex(ValueError,'原厂 ROM 缺少'):
                flow.execute(data,'all',lambda *_a,**_k:self.fail('unexpected command'))

    def test_native_display_fragment_reaches_build_and_only_invalidates_target_kernel(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        data['kernel_fragments']=['/custom/override.config']
        pipeline=flow.Flow(data,lambda *_a,**_k:None,distribution='fedora')
        records={};revision=['first']
        native='build/kernel/configs/native-display.config'
        def digest(path):
            return revision[0] if str(path).endswith(native) else str(path)
        def capture(name,dependency,build,_artifacts):
            records[name]=helpers.key(dependency)
            build(self.root/name)
            return self.root/name
        with patch.object(pipeline,'kernel_source',return_value=(self.root,'a'*40)), \
             patch.object(pipeline,'stage',side_effect=capture), \
             patch.object(flow.shutil,'which',return_value='/compiler/gcc'), \
             patch.object(flow,'sha',side_effect=digest), \
             patch.object(flow,'tree_identity',side_effect=lambda path:str(path)), \
             patch.object(pipeline,'run') as run:
            pipeline.kernel();pipeline.kernel(True);first=dict(records)
            target=run.call_args_list[0].args[0];installer=run.call_args_list[1].args[0]
            self.assertLess(target.index(str(PROJECT/native)),target.index('/custom/override.config'))
            self.assertNotIn(str(PROJECT/native),installer)
            revision[0]='changed'
            pipeline.kernel();pipeline.kernel(True)
            self.assertNotEqual(first['kernel'],records['kernel'])
            self.assertEqual(first['installer-kernel'],records['installer-kernel'])
        self.assertEqual(data['kernel_fragments'],['/custom/override.config'])
        explicit=dict(data,kernel_fragments=[native,'/custom/override.config'])
        self.assertEqual(flow.kernel_fragments(explicit).count(native),1)

    def test_configure_only_uses_the_same_native_fragment_order(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        data['kernel_fragments']=['/custom/override.config']
        data['kernel_source_lock']='custom/target.json'
        native=str(self.project/'build/kernel/configs/native-display.config')
        with patch.object(flow,'PROJECT',self.project), \
             patch.object(flow.Flow,'kernel_source',return_value=(self.root,'b'*40)), \
             patch.object(flow,'sha',side_effect=lambda path:str(path)), \
             patch.object(flow,'tree_identity',side_effect=lambda path:str(path)), \
             contextlib.redirect_stdout(io.StringIO()):
            commands=[]
            run=lambda argv,**_kwargs:commands.append(argv)
            flow.execute(data,'kernel',run,configure_only=True)
            flow.execute(data,'installer-kernel',run,configure_only=True)
        self.assertLess(commands[0].index(native),commands[0].index('/custom/override.config'))
        self.assertNotIn(native,commands[1])
        self.assertEqual(commands[0][commands[0].index('--source-lock')+1],
                         str(self.project/'custom/target.json'))
        self.assertEqual(commands[1][commands[1].index('--source-lock')+1],
                         str(self.project/'build/kernel/source-6.17.json'))

    def test_kernel_only_dispatch_does_not_require_rootfs_or_rom(self):
        data=builder.config(PROJECT/'build/devbox/build.example.json');data['rom_images']=str(self.root/'missing')
        with patch.object(flow,'PROJECT',self.project), patch.object(flow.Flow,'kernel',return_value=('source','kernel')) as kernel, \
             patch.object(flow.Flow,'board',side_effect=AssertionError('unrelated ROM')), \
             patch.object(flow.Flow,'apps',side_effect=AssertionError('unrelated rootfs')):
            flow.execute(data,'kernel',lambda *_a,**_k:self.fail('unexpected command'))
        kernel.assert_called_once()

    def test_root_recipe_rejects_unsupported_system_and_shell_arguments(self):
        data=json.loads((PROJECT/'build/rootfs/ubuntu-plasma.json').read_text());path=self.root/'recipe.json'
        for changes in ({'distribution':'fedora'},{'packages':['--allow-unauthenticated']},{'remove':['systemd']},{'user':'root'}):
            path.write_text(json.dumps(dict(data,**changes)))
            with self.assertRaises(ValueError):root_builder.recipe(path)

    def test_desktop_content_keeps_catalogs_and_project_licenses_without_untrimming_manpages(self):
        root=self.root/'desktop';root.mkdir()
        root_builder.prepare_desktop_content(root)
        text=(root/'etc/dpkg/dpkg.cfg.d/zz-liuqin-desktop-content').read_text()
        self.assertIn('path-include=/usr/share/locale/*/LC_MESSAGES/*.mo',text)
        self.assertIn('path-include=/usr/share/doc/liuqin-*/*',text)
        self.assertNotIn('/usr/share/man/',text)
        (root/'etc/locale.gen').write_text('# zh_CN.UTF-8 UTF-8\n')
        with patch.object(root_builder,'chroot') as run:
            root_builder.configure_locales(root);root_builder.configure_locales(root)
        entries=(root/'etc/locale.gen').read_text().splitlines()
        self.assertEqual(entries.count('zh_CN.UTF-8 UTF-8'),1)
        self.assertEqual(entries.count('en_US.UTF-8 UTF-8'),1)
        self.assertEqual(run.call_args.args[1],['locale-gen'])

    def test_first_boot_recipe_has_no_account_and_preserves_explicit_old_accounts(self):
        data=json.loads((PROJECT/'build/rootfs/ubuntu-plasma.json').read_text());path=self.root/'recipe.json'
        path.write_text(json.dumps(data));normalized=root_builder.recipe(path)
        self.assertTrue(normalized['first_boot_setup']);self.assertNotIn('user',normalized)
        self.assertEqual(normalized['hostname'],'liuqin')
        for changes in ({'password_file':'private.txt'},{'user':'pad'},{'first_boot_setup':'true'},{'remove':['sddm']}):
            path.write_text(json.dumps(dict(data,**changes)))
            with self.assertRaises(ValueError):root_builder.recipe(path)
        data.pop('first_boot_setup');data.update(user='pad',password_file=None)
        path.write_text(json.dumps(data));self.assertFalse(root_builder.recipe(path)['first_boot_setup'])

    def test_first_boot_configuration_creates_no_regular_account_or_credentials(self):
        root=self.root/'first-boot';(root/'etc').mkdir(parents=True)
        (root/'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n')
        for name in ('usr/libexec/plasma-setup','usr/lib/systemd/system/plasma-setup.service'):
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        with patch.object(root_builder,'chroot') as run:root_builder.configure_first_boot(root)
        commands=[call.args[1] for call in run.call_args_list]
        self.assertNotIn('chpasswd',[command[0] for command in commands])
        self.assertEqual([c for c in commands if c[0]=='useradd'],[['useradd','-D','-s','/bin/bash']])
        self.assertEqual((root/'etc/xdg/plasmasetuprc').read_text(),'[Accounts]\nUserGroups=sudo\n')
        self.assertEqual(os.readlink(root/'etc/systemd/system/multi-user.target.wants/plasma-setup.service'),'/usr/lib/systemd/system/plasma-setup.service')
        self.assertFalse((root/'etc/plasma-setup-done').exists())
        with patch.object(root_builder,'chroot',side_effect=AssertionError('must stop')):
            (root/'etc/passwd').write_text('pad:x:1000:1000:pad:/home/pad:/bin/bash\n')
            with self.assertRaisesRegex(ValueError,'预置普通用户'):root_builder.configure_first_boot(root)

    def test_overlay_cannot_traverse_existing_symlink_parent_or_runtime_directory(self):
        target=self.root/'target';target.mkdir();source=self.root/'overlay';source.mkdir()
        (target/'etc').symlink_to('/etc');(source/'etc').mkdir();(source/'etc/file').write_bytes(b'config')
        with self.assertRaisesRegex(ValueError,'符号链接'):root_builder.overlay(source,target)
        shutil.rmtree(source);source.mkdir();(source/'dev').mkdir()
        with self.assertRaisesRegex(ValueError,'运行时目录'):root_builder.overlay(source,target)

    def test_sudo_receives_explicit_stage_environment_without_running_it(self):
        def which(name):return '/mock/'+name
        with patch.object(builder.os,'geteuid',return_value=1000),patch.object(builder.shutil,'which',side_effect=which),patch.object(builder.subprocess,'run') as run, \
             patch.dict(builder.os.environ,{'LC_ALL':'C'}):
            builder.run(['build-sensors'],env={'ROM_SENSORS':'/input/sensors','OUT_DIR':'/new/output'},root=True)
        argv=run.call_args.args[0]
        self.assertIn('ROM_SENSORS=/input/sensors',argv);self.assertIn('OUT_DIR=/new/output',argv)
        self.assertIn('LC_ALL=C',argv)

    def test_native_execution_needs_no_binfmt_mutation(self):
        with patch.object(helpers.os,'uname',return_value=type('Host',(),{'machine':'aarch64'})()), \
             patch.object(helpers.subprocess,'run',side_effect=AssertionError('unexpected host mutation')):
            with helpers.arm64_execution():pass

    @unittest.skipUnless(shutil.which('qemu-aarch64'),'Devbox static QEMU required')
    def test_foreign_execution_removes_only_its_own_temporary_registration(self):
        registry=self.root/'binfmt';registry.mkdir();(registry/'register').touch()
        existing=registry/'unrelated';existing.write_text('original registration')
        original_write=Path.write_bytes;registrations=[]
        def write(path,data):
            result=original_write(path,data)
            if path==registry/'register':
                fields=data.split(b':');name=fields[1].decode()
                magic=codecs.escape_decode(fields[4])[0];mask=codecs.escape_decode(fields[5])[0]
                (registry/name).write_text('enabled\ninterpreter '+fields[6].decode()+'\nflags: '+fields[7].decode()+'\noffset 0\nmagic '+magic.hex()+'\nmask '+mask.hex()+'\n')
                registrations.append(name)
            return result
        def directory(value):return registry if str(value)=='/proc/sys/fs/binfmt_misc' else Path(value)
        with patch.object(helpers,'Path',side_effect=directory),patch.object(Path,'write_bytes',write), \
             patch.object(helpers.os,'uname',return_value=type('Host',(),{'machine':'x86_64'})()):
            with self.assertRaisesRegex(RuntimeError,'simulated'):
                with helpers.arm64_execution():raise RuntimeError('simulated failure')
        self.assertEqual(len(registrations),1)
        self.assertEqual((registry/registrations[0]).read_text(),'-1')
        self.assertEqual(existing.read_text(),'original registration')

    def test_binfmt_record_keeps_complete_arm64_identity_and_rejects_host_elf(self):
        registration=helpers.arm64_registration('liuqin-test','/usr/bin/qemu-aarch64')
        self.assertNotIn(b'\0',registration)
        fields=registration.split(b':')
        magic=codecs.escape_decode(fields[4])[0];mask=codecs.escape_decode(fields[5])[0]
        self.assertEqual(len(magic),20);self.assertEqual(len(mask),20)
        self.assertEqual(magic[18:20],b'\xb7\0');self.assertEqual(mask[18:20],b'\xff\xff')
        matches=lambda header: all((actual^expected)&bits==0 for actual,expected,bits in zip(header,magic,mask))
        for kind in (2,3):
            arm64=b'\x7fELF\x02\x01\x01\0'+bytes(8)+struct.pack('<HH',kind,183)
            self.assertTrue(matches(arm64))
        for architecture in (3,40,62,243):
            other=b'\x7fELF\x02\x01\x01\0'+bytes(8)+struct.pack('<HH',3,architecture)
            self.assertFalse(matches(other))
        host=Path('/usr/bin/true').read_bytes()[:20]
        if host[18:20] != b'\xb7\0':self.assertFalse(matches(host))

    @unittest.skipUnless(shutil.which('qemu-aarch64'),'Devbox static QEMU required')
    def test_truncated_binfmt_readback_stops_before_execution_and_removes_owned_rule(self):
        registry=self.root/'binfmt';registry.mkdir();(registry/'register').touch()
        existing=registry/'unrelated';existing.write_text('original registration')
        original_write=Path.write_bytes;registered=[]
        def write(path,data):
            result=original_write(path,data)
            if path==registry/'register':
                name=data.split(b':',2)[1].decode();registered.append(name)
                (registry/name).write_text('enabled\nmagic 7f454c46020101\nmask fffffffffffefe\n')
            return result
        def directory(value):return registry if str(value)=='/proc/sys/fs/binfmt_misc' else Path(value)
        with patch.object(helpers,'Path',side_effect=directory),patch.object(Path,'write_bytes',write), \
             patch.object(helpers.os,'uname',return_value=type('Host',(),{'machine':'x86_64'})()):
            with self.assertRaisesRegex(ValueError,'规则回读不完整'):
                with helpers.arm64_execution():self.fail('invalid handler must not reach command execution')
        self.assertEqual((registry/registered[0]).read_text(),'-1')
        self.assertEqual(existing.read_text(),'original registration')

    def test_chroot_uses_host_executable_before_setting_target_path(self):
        with patch.object(root_builder.shutil,'which',return_value='/host/tools/chroot'), \
             patch.object(root_builder.subprocess,'run') as run:
            root_builder.chroot(self.root,['apt-get','update'])
        self.assertEqual(run.call_args.args[0],['/host/tools/chroot',str(self.root),'apt-get','update'])
        self.assertEqual(run.call_args.kwargs['env']['PATH'],'/usr/sbin:/usr/bin:/sbin:/bin')

    def test_three_file_publication_and_credentials_are_separate(self):
        sys.path.insert(0,str(PROJECT/'tests'))
        from kernel_fixtures import kernel_fixture,install_fixture,archive_root,installer_fixture
        data=builder.config(PROJECT/'build/devbox/build.example.json')
        data['rootfs_config']=str(PROJECT/'build/rootfs/ubuntu-plasma.json')
        pipeline=flow.Flow(data,lambda *_a,**_k:self.fail('unexpected command'))
        pipeline.recipe['first_boot_setup']=False
        fixture=kernel_fixture(self.root/'kernel-input','6.17.0-liuqin-test')
        root=self.root/'root';root.mkdir();install_fixture(root,'6.17.0-liuqin-test',fixture)
        packed=self.root/'packed';packed.mkdir();archive_root(root,packed/'rootfs.tar.gz')
        installer=self.root/'installer.img';installer.write_bytes(installer_fixture())
        apps=self.root/'apps';apps.mkdir();(apps/'login.txt').write_text('SYNTHETIC CREDENTIALS\n');(apps/'packages.tsv').write_text('fixture\n')
        fake_project=self.root/'project';(fake_project/'out').mkdir(parents=True)
        with patch.object(flow,'PROJECT',fake_project),patch.object(pipeline,'boot',side_effect=lambda is_installer=False:installer if is_installer else fixture[2]), \
             patch.object(pipeline,'rootfs',return_value=packed),patch.object(pipeline,'apps',return_value=apps), \
             patch.object(pipeline,'kernel',return_value=(self.root,fixture[0])):
            output=pipeline.images();again=pipeline.images()
        self.assertEqual(output,again)
        self.assertEqual({p.name for p in output.iterdir()},{'installer.img','boot.img','rootfs.tar.gz'})
        login=next((fake_project/'out/login').glob('*.txt'))
        self.assertEqual(login.stat().st_mode & 0o777,0o600)
        self.assertFalse(any('login' in p.name for p in output.iterdir()))
        # The same publisher must not produce host-side passwords for an OEM image.
        pipeline.recipe['first_boot_setup']=True
        oem_project=self.root/'oem-project';(oem_project/'out').mkdir(parents=True)
        with patch.object(flow,'PROJECT',oem_project),patch.object(pipeline,'boot',side_effect=lambda is_installer=False:installer if is_installer else fixture[2]), \
             patch.object(pipeline,'rootfs',return_value=packed),patch.object(pipeline,'apps',return_value=apps), \
             patch.object(pipeline,'kernel',return_value=(self.root,fixture[0])):
            output=pipeline.images()
        self.assertFalse((oem_project/'out/login').exists())
        report=json.loads(next((oem_project/'out/reports').glob('*.json')).read_text())
        self.assertEqual(report['account_setup'],'first-boot');self.assertIsNone(report['login_file'])

    def test_preexisting_service_policy_is_preserved_when_mount_setup_stops(self):
        target=self.root/'root';(target/'usr/sbin').mkdir(parents=True);policy=target/'usr/sbin/policy-rc.d'
        policy.write_text('original policy\n')
        with patch.object(helpers.subprocess,'run',return_value=type('Result',(),{'returncode':0})()), \
             patch.object(helpers.os,'mknod',side_effect=lambda path,*_a:Path(path).touch()):
            with self.assertRaisesRegex(ValueError,'已有服务启动策略'):
                with helpers.mounted_root(target):self.fail('should stop')
        self.assertEqual(policy.read_text(),'original policy\n')

    def test_configuration_write_never_follows_an_overlay_link(self):
        target=self.root/'root';(target/'etc').mkdir(parents=True)
        outside=self.root/'outside';outside.write_text('original')
        (target/'etc/hostname').symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'配置写入路径'):
            root_builder.write_config(target,'etc/hostname','changed')
        self.assertEqual(outside.read_text(),'original')

    def test_speaker_program_and_tuning_are_renamed_without_copying_private_calibration(self):
        vendor=self.root/'vendor';(vendor/'firmware').mkdir(parents=True)
        records=[]
        for name,target,_digest in speaker.FILES:
            payload=('synthetic '+name).encode();(vendor/'firmware'/name).write_bytes(payload)
            records.append((name,target,hashlib.sha256(payload).hexdigest()))
        (vendor/'firmware/crus_calr.bin').write_bytes(b'private factory data')
        source=self.root/'speaker-files';target=self.root/'package'
        with patch.object(speaker,'FILES',records):
            speaker.extract(vendor,source)
            (source/'usr/lib/firmware/cirrus/private-calr.bin').write_bytes(b'private')
            speaker.install(source,target)
        files=list((target/'usr/lib/firmware/cirrus').iterdir())
        self.assertEqual({p.name for p in files},{record[1] for record in records})
        self.assertTrue(all(p.stat().st_mode & 0o777==0o644 for p in files))
        for name,renamed,_digest in records:
            self.assertEqual((target/'usr/lib/firmware/cirrus'/renamed).read_bytes(),(vendor/'firmware'/name).read_bytes())
        # Validate the entire input set before creating a partial package tree.
        (source/'usr/lib/firmware/cirrus'/records[-1][1]).write_bytes(b'corrupted')
        rejected=self.root/'rejected'
        with patch.object(speaker,'FILES',records),self.assertRaisesRegex(ValueError,'校验失败'):
            speaker.install(source,rejected)
        self.assertFalse(rejected.exists())

    def test_speaker_input_links_are_rejected_before_extraction(self):
        vendor=self.root/'vendor';(vendor/'firmware').mkdir(parents=True)
        blob=self.root/'blob';blob.write_bytes(b'test')
        name,target,_digest=speaker.FILES[0];(vendor/'firmware'/name).symlink_to(blob)
        with patch.object(speaker,'FILES',[(name,target,hashlib.sha256(blob.read_bytes()).hexdigest())]),self.assertRaisesRegex(ValueError,'校验失败'):
            speaker.extract(vendor,self.root/'speaker-files')
        self.assertFalse((self.root/'speaker-files').exists())


class AndroidTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_sparse_random_reads_match_the_original_file(self):
        raw=self.root/'raw';sparse=self.root/'sparse'
        payload=b'a'*4096+bytes(4096)+b'abcd'*1024+b'b'*4096
        raw.write_bytes(payload)
        if shutil.which('img2simg'):
            subprocess.run(['img2simg',str(raw),str(sparse)],check=True)
        else:
            header=struct.pack('<IHHHHIIII',0xed26ff3a,1,0,28,12,4096,4,4,0)
            chunks=[]
            for index in range(4):chunks.append(struct.pack('<HHII',0xcac1,0,1,4108)+payload[index*4096:(index+1)*4096])
            sparse.write_bytes(header+b''.join(chunks))
        image=android.Image(sparse)
        try:
            for offset,size in ((0,len(payload)),(4000,5000),(8193,1234)):
                self.assertEqual(image.read(offset,size),payload[offset:offset+size])
        finally:image.close()

    def super_image(self):
        raw=bytearray(65536)
        geometry=bytearray(struct.pack('<II32sIII',0x616c4467,52,bytes(32),4096,2,4096))
        geometry[8:40]=hashlib.sha256(geometry).digest();raw[4096:4148]=geometry
        partition=struct.pack('<36sIIII',b'vendor_a',1,0,2,0)
        extents=struct.pack('<QIQI',4,0,96,0)+struct.pack('<QIQI',4,0,112,0)
        group=struct.pack('<36sIQ',b'default',0,0)
        device=struct.pack('<QIIQ36sI',64,4096,0,len(raw),b'super',0)
        table=partition+extents+group+device
        header=bytearray(128);struct.pack_into('<IHHI',header,0,0x414c5030,10,0,128)
        struct.pack_into('<I',header,44,len(table));header[48:80]=hashlib.sha256(table).digest()
        offset=0
        for location,count,width in ((80,1,52),(92,2,24),(104,1,48),(116,1,64)):
            struct.pack_into('<III',header,location,offset,count,width);offset+=count*width
        header[12:44]=hashlib.sha256(header).digest();raw[12288:12416]=header;raw[12416:12416+len(table)]=table
        raw[96*512:100*512]=b'a'*2048;raw[112*512:116*512]=b'b'*2048
        path=self.root/'super.img';path.write_bytes(raw);return path

    def test_logical_vendor_extents_are_concatenated_without_mounting(self):
        source=self.super_image();output=self.root/'vendor.img'
        android.vendor_partition(source,output)
        self.assertEqual(output.read_bytes(),b'a'*2048+b'b'*2048)

    def test_super_checksum_corruption_stops_before_output(self):
        source=self.super_image();data=bytearray(source.read_bytes());data[12420]^=1;source.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'table 校验失败'):android.vendor_partition(source,self.root/'vendor.img')
        self.assertFalse((self.root/'vendor.img').exists())

    def test_boot_dtbs_and_dtbo_are_split_with_bounds_checks(self):
        def fdt(payload):return struct.pack('>10I',0xd00dfeed,40+len(payload),40,40,40,17,16,0,len(payload),0)+payload
        trees=[fdt(bytes([value])*4) for value in (1,2,3)]
        vendor=self.root/'vendor_boot.img';header=bytearray(4096);header[:8]=b'VNDRBOOT'
        struct.pack_into('<II',header,8,3,4096);struct.pack_into('<II',header,2096,2112,sum(map(len,trees)))
        vendor.write_bytes(header+b''.join(trees))
        dtbo=self.root/'dtbo.img';payload=trees[0]
        dtbo.write_bytes(struct.pack('>8I',0xd7b7ab1e,64+len(payload),32,32,1,32,4096,0)+struct.pack('>8I',len(payload),64,0,0,0,0,0,0)+payload)
        android.boot_assets(vendor,dtbo,self.root/'assets')
        for index,data in enumerate(trees):self.assertEqual((self.root/f'assets/dtbs/dtb-{index:02d}.dtb').read_bytes(),data)
        self.assertEqual((self.root/'assets/dtbo/entry.0').read_bytes(),payload)


@unittest.skipUnless(shutil.which('dpkg-deb'),'Debian tools required')
class DevicePackageTests(unittest.TestCase):
    def test_sensor_deb_leaves_distribution_libraries_to_apt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);layer=root/'layer';library=layer/'usr/lib/aarch64-linux-gnu'
            library.mkdir(parents=True)
            for name in ('libhexagonrpc.so.0.5','libssc.so.2','libgudev-1.0.so.0.3.0','libqmi-glib.so.5.12.0',
                         'libqrtr-glib.so.0.0.0','libpolkit-gobject-1.so.0.0.0','libprotobuf-c.so.1.0.0',
                         'libqrtr.so.1.1','libfuture-distro.so.1'):
                (library/name).write_bytes(b'SYNTHETIC SHARED LIBRARY')
            (library/'libgudev-1.0.so.0').symlink_to('libgudev-1.0.so.0.3.0')
            for name in ('usr/bin/hexagonrpcd','usr/bin/ssccli','usr/local/libexec/liuqin-iio-sensor-proxy',
                         'usr/local/sbin/liuqin-sensor-registry-import','etc/udev/rules.d/80-liuqin-fastrpc.rules',
                         'etc/systemd/system/liuqin-ssc-sample-gate.service'):
                path=layer/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture\n');path.chmod(0o755)
            dropin=layer/'etc/systemd/system/iio-sensor-proxy.service.d/90-liuqin-ssc.conf';dropin.parent.mkdir(parents=True)
            dropin.write_text('[Service]\nExecStart=\nExecStart=/usr/local/libexec/liuqin-iio-sensor-proxy\n')
            archive=root/'sensors.tar'
            subprocess.run(['tar','-C',str(layer),'-cf',str(archive),'.'],check=True)
            out=root/'debs'
            subprocess.run(['sh',str(PROJECT/'build/scripts/build-liuqin-debs.sh'),'sensors'],check=True,
                env=dict(os.environ,DEVICE_DESKTOP='generic',SENSOR_STACK_TAR=str(archive),
                         SENSOR_STACK_SHA256=helpers.sha(archive),OUT_DIR=str(out)))
            package=next(out.glob('liuqin-sensors_*.deb'));extracted=root/'extracted'
            subprocess.run(['dpkg-deb','-x',str(package),str(extracted)],check=True)
            actual={path.name for path in (extracted/'usr/lib/aarch64-linux-gnu').iterdir()}
            self.assertEqual(actual,{'libhexagonrpc.so.0.5','libssc.so.2'})
            dependencies=subprocess.check_output(['dpkg-deb','-f',str(package),'Depends'],text=True)
            for name in ('libgudev-1.0-0','libqmi-glib5','libqrtr-glib0','libpolkit-gobject-1-0','libprotobuf-c1'):
                self.assertIn(name,dependencies)

    def test_generic_package_needs_no_gnome_binary_compiler_or_author_rootfs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);busy=root/'busybox';busy.write_bytes(b'SYNTHETIC BUSYBOX FILE');busy.chmod(0o755)
            out=root/'debs'
            env=dict(os.environ,DEVICE_DESKTOP='generic',BUSYBOX=str(busy),BUSYBOX_SHA256=helpers.sha(busy),
                     OUT_DIR=str(out),UBUNTU_DESKTOP_ROOT='/not/an/author/rootfs',POWER_SETTINGS_BINARY='/not/gnome',POWER_KEY_CC='/not/a/compiler')
            subprocess.run(['sh',str(PROJECT/'build/scripts/build-liuqin-debs.sh'),'device-support'],env=env,check=True)
            subprocess.run(['sh',str(PROJECT/'build/scripts/build-liuqin-debs.sh'),'meta'],env=env,check=True)
            package=next(out.glob('liuqin-device-support_*.deb'));extracted=root/'extracted'
            subprocess.run(['dpkg-deb','-x',str(package),str(extracted)],check=True)
            depends=subprocess.check_output(['dpkg-deb','-f',str(package),'Depends'],text=True)
            self.assertNotIn('gnome-control-center',depends)
            self.assertTrue((extracted/'usr/local/sbin/liuqin-gnome-storage-guard').is_file())
            self.assertFalse((extracted/'usr/local/libexec/liuqin-power-key-action').exists())
            self.assertFalse((extracted/'usr/bin/gnome-control-center').exists())
            meta=next(out.glob('liuqin-device-base_*.deb'))
            self.assertNotIn('liuqin-kernel',subprocess.check_output(['dpkg-deb','-f',str(meta),'Depends'],text=True))


class AblSymbolFormattingTests(unittest.TestCase):
    def test_sink_extraction_accepts_restored_dtc_labels(self):
        symbols=load('abl_symbols_test',PROJECT/'build/lib/abl-symbols.py')
        for count in (0,1,1781):
            labels=''.join('stock_'+str(i)+': ' for i in range(count))
            text='/dts-v1/;\n/ {\n\t'+labels+'liuqin-abl-overlay-sink {\n\t\tphandle = <0x16e>;\n\t};\n};\n'
            body=symbols.extract_block(text,'liuqin-abl-overlay-sink')
            self.assertIsNotNone(body)
            self.assertEqual(symbols.block_properties(body),{'phandle':'<0x16e>'})

    def test_label_support_does_not_match_a_different_node_name(self):
        symbols=load('abl_symbols_name_test',PROJECT/'build/lib/abl-symbols.py')
        for name in ('other-liuqin-abl-overlay-sink','liuqin-abl-overlay-sink-other'):
            self.assertIsNone(symbols.extract_block('label: '+name+' {\n};\n','liuqin-abl-overlay-sink'))


if __name__=='__main__':unittest.main(verbosity=2)
