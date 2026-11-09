#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline native-image integrity, read-only transport and status regressions."""
import copy
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import uuid
import zipfile

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_gui import compatibility,host
from liuqin_gui.engine import Engine,InstallerError,Transport
from liuqin_gui.native_recovery_transport import NativeRecoveryTransport
from liuqin_artifacts import boot_interface
from liuqin_recovery_image import add,pack
from liuqin_native_recovery import MARKER,AGENT,BOARD_HELPER,UI_FILES,TOUCH_FILES,BCB_FILES,STORE_FILES
from liuqin_board_identity import identify


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def sha(data):return hashlib.sha256(data).hexdigest()


def fixture(path,broken='',ui=False,touch=False,installer=False,adb=False,bcb=False,boot_store=False,retention=False):
    kernel=b'Linux version test-native\0';dtb=b'fixture: xiaomi,liuqin\0';entries={}
    for name,body in {'etc/liuqin-storage-mode':b'readonly\n','etc/liuqin-installer':b'liuqin\n',
        'etc/liuqin-installer-interface':b'liuqin-installer-v2\n','etc/liuqin-installer-kernel.release':b'test-native\n',
        'etc/liuqin-installer-usb-network':b'rndis\n','etc/liuqin-filesystems':b'ext4\nbtrfs\n',
        'etc/liuqin-multiboot':b'1\n','etc/liuqin-android-installer':b'1\n',
        'init':b'#!/bin/busybox sh\n',AGENT:b'# status fixture\n',BOARD_HELPER:b'# board fixture\n'}.items():add(entries,name,body)
    manifest=dict(schema=1,interface='liuqin-native-recovery-v1',device='liuqin',build_id='a'*64,
        usage='temporary-boot-only',status='DEVICE_UNTESTED',inspection_only=True,kernel_release='test-native',
        kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),board_identity_version=1,payload_sha256={n:sha(entries[n][1]) for n in ('init',AGENT,BOARD_HELPER)})
    if ui:
        for name in UI_FILES:add(entries,name,b'UI fixture '+name.encode(),0o100755)
        manifest['ui_version']=1
        manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in UI_FILES})
    if touch:
        for name in TOUCH_FILES:add(entries,name,b'touch fixture '+name.encode())
        manifest['touch_version']=1
        manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in TOUCH_FILES})
    if adb:
        from liuqin_native_recovery import ADB_FILES
        for name in ADB_FILES:add(entries,name,b'ADB fixture '+name.encode(),0o100755)
        add(entries,'etc/liuqin-native-adb',b'1\n');entries.pop('etc/liuqin-installer-usb-network')
        manifest.update(native_adb_version=1,usb_transport='adb')
        manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in ADB_FILES})
    if installer:
        from liuqin_native_recovery import INSTALL_FILES
        from liuqin_native_install import CAPABILITIES
        for name in INSTALL_FILES:add(entries,name,b'installer fixture '+name.encode(),0o100644)
        manifest.update(installer_version=1,installer_capabilities=CAPABILITIES,inspection_only=False)
        manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in INSTALL_FILES})
    if bcb:
        for name in BCB_FILES:add(entries,name,b'BCB fixture '+name.encode(),0o100755)
        manifest['bcb_cleanup_version']=1
        manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in BCB_FILES})
    if boot_store:
        for name in STORE_FILES:add(entries,name,b'store fixture '+name.encode())
        manifest.update(boot_store_version=1,boot_switch_version=1)
        manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in STORE_FILES})
    if installer or adb:
        if retention:
            from liuqin_native_recovery import RETENTION_FILES
            for name in RETENTION_FILES:add(entries,name,b'retention fixture '+name.encode())
            manifest['payload_sha256'].update({name:sha(entries[name][1]) for name in RETENTION_FILES})
            manifest['boot_store_retention_version']=1
        identity=dict(payload_sha256=dict(manifest['payload_sha256']),kernel_sha256=manifest['kernel_sha256'],dtb_sha256=manifest['dtb_sha256'],inspection_only=manifest['inspection_only'],installer_capabilities=manifest.get('installer_capabilities',[]))
        if retention:identity['boot_store_retention_version']=1
        if bcb:identity['bcb_cleanup_version']=1
        if boot_store:identity.update(boot_store_version=1,boot_switch_version=1)
        manifest.update(identity=identity,build_id=sha((json.dumps(identity,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()))
    if broken=='write-mode':manifest['inspection_only']=False
    if broken in entries:add(entries,broken,b'changed')
    add(entries,MARKER,json.dumps(manifest).encode())
    body=gzip.compress(pack(entries));header=bytearray(4096);header[:8]=b'ANDROID!'
    struct.pack_into('<I',header,8,len(kernel));struct.pack_into('<I',header,16,len(body))
    struct.pack_into('<II',header,36,4096,2);struct.pack_into('<II',header,1644,1660,len(dtb))
    if broken=='kernel':kernel=b'X'+kernel[1:]
    if broken=='dtb':dtb=b'X'+dtb[1:]
    path.write_bytes(header+kernel+bytes(-len(kernel)%4096)+body+bytes(-len(body)%4096)+dtb+bytes(-len(dtb)%4096))
    return manifest


def adb_package_fixture(path):
    manifest=fixture(path,adb=True)
    (path.parent/'native-recovery-manifest.json').write_text(json.dumps(manifest))
    (path.parent/'STATUS.txt').write_text('OFFLINE TEST FIXTURE; NEVER BOOT')
    import tarfile
    source=path.parent/'adbd-fixture-source.tar.xz'
    with tarfile.open(source,'w:xz') as archive:
        body=b'Offline synthetic image; no executable adbd is included.\n'
        item=tarfile.TarInfo('README.txt');item.size=len(body);archive.addfile(item,io.BytesIO(body))
    return source


def transport():
    t=NativeRecoveryTransport.__new__(NativeRecoveryTransport)
    t.engine=SimpleNamespace(receipt={'operation':'scan-partitions'},save_receipt=Mock(),emit=Mock(),
        manifest={'native_recovery_manifest':dict(build_id='a'*64,kernel_release='test-native')})
    t.serial='tablet';t.boot_id=str(uuid.uuid4());t.ram_active=True;t.remote=Mock();return t


class NativeTests(unittest.TestCase):
    def test_retention_requires_store_and_authenticates_new_payload(self):
        from liuqin_native_recovery import RETENTION_FILES
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'recovery.img'
            fixture(image,adb=True,installer=True,retention=True)
            with self.assertRaisesRegex(ValueError,'retention capability'):boot_interface(image,installer=True)
            fixture(image,adb=True,installer=True,boot_store=True,retention=True)
            self.assertEqual(boot_interface(image,installer=True)['native_recovery_manifest']['boot_store_retention_version'],1)
            fixture(image,adb=True,installer=True,boot_store=True,retention=True,broken=RETENTION_FILES[0])
            with self.assertRaises(ValueError):boot_interface(image,installer=True)

    def test_abl_rewritten_identity_needs_all_board_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);dt=root/'proc/device-tree';dt.mkdir(parents=True)
            files={'compatible':b'qcom,cape-mtp\0qcom,cape\0qcom,mtp\0',
                'model':b'Liuqin based on Qualcomm Technologies, Inc SM8475\0',
                'xiaomi,miboard-id':struct.pack('>II',0x10,0),'qcom,board-id':struct.pack('>II',0x10008,0)}
            for name,value in files.items():(dt/name).write_bytes(value)
            cmd=root/'proc/cmdline';cmd.write_text('hwid.project=16 androidboot.serialno=tablet')
            self.assertEqual(identify(root)['device'],'liuqin')
            for name in files:
                original=(dt/name).read_bytes();(dt/name).unlink()
                self.assertEqual(identify(root)['device'],'unknown',name)
                (dt/name).write_bytes(original)
            for text in ('','hwid.project=17','hwid.project=16 hwid.project=17'):
                cmd.write_text(text);self.assertEqual(identify(root)['device'],'unknown')
            (dt/'compatible').write_bytes(b'xiaomi,liuqin\0qcom,sm8475\0')
            self.assertEqual(identify(root)['device'],'unknown','contradictory hardware project must fail')
            cmd.write_text('hwid.project=16');self.assertEqual(identify(root)['device'],'liuqin')

    def test_native_console_dtb_keeps_storage_and_uses_bootloader_framebuffer(self):
        if not all(shutil.which(p) for p in ('dtc','fdtoverlay','fdtget','fdtput')):self.skipTest('device tree tools required')
        builder=load('native_display_builder',PROJECT/'build/recovery/build-native.py')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'test.dts';source.write_text('''/dts-v1/;
/ { chosen { bootargs="earlycon=simplefb console=drm_log console=tty0 keep_bootcon ignore_loglevel initcall_debug initcall_blacklist=simplefb_driver_init rootwait"; };
soc@0 { gpu@3d00000 { status="okay"; }; ufs@1d84000 { status="okay"; }; clock-controller@af00000 { status="okay"; };
display-subsystem@ae00000 { displayport-controller@ae90000 { status="okay"; };
dsi@ae94000 { status="okay"; }; dsi@ae96000 { status="okay"; }; }; }; };
''')
            subprocess.run(['dtc','-q','-I','dts','-O','dtb','-o',str(root/'input.dtb'),str(source)],check=True)
            builder.prepare_console_dtb((root/'input.dtb').read_bytes(),root)
            def prop(node,key='status'):return subprocess.check_output(['fdtget','-t','s',str(root/'console.dtb'),node,key],text=True).strip()
            self.assertEqual(prop('/soc@0/gpu@3d00000'),'disabled')
            self.assertEqual(prop('/soc@0/display-subsystem@ae00000'),'disabled')
            self.assertEqual(prop('/soc@0/clock-controller@af00000'),'disabled')
            for node in ('/soc@0/ufs@1d84000','/soc@0/display-subsystem@ae00000/dsi@ae94000','/soc@0/display-subsystem@ae00000/dsi@ae96000'):
                self.assertEqual(prop(node),'okay')
            args=prop('/chosen','bootargs').split()
            self.assertIn('consoleblank=0',args);self.assertIn('earlycon=simplefb',args)
            self.assertNotIn('console=drm_log',args);self.assertNotIn('initcall_blacklist=simplefb_driver_init',args)
            self.assertNotIn('keep_bootcon',args);self.assertNotIn('ignore_loglevel',args)

    def test_native_manifest_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'recovery.img';manifest=fixture(p)
            result=boot_interface(p,installer=True)
            self.assertEqual(result['native_recovery_manifest'],manifest)
            self.assertEqual(result['usb_network'],'rndis')
            with self.assertRaises(ValueError):boot_interface(p)
            for broken in ['write-mode','kernel','dtb','init',AGENT,BOARD_HELPER]:
                fixture(p,broken)
                with self.subTest(broken=broken),self.assertRaises(ValueError):boot_interface(p,installer=True)

    def test_bcb_maintenance_requires_install_role_and_authenticates_both_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'recovery.img'
            fixture(image,ui=True,adb=True,bcb=True)
            with self.assertRaisesRegex(ValueError,'BCB cleanup capability'):
                boot_interface(image,installer=True)
            fixture(image,ui=True,adb=True,installer=True,bcb=True)
            self.assertEqual(boot_interface(image,installer=True)['native_recovery_manifest']['bcb_cleanup_version'],1)
            for path in BCB_FILES:
                fixture(image,broken=path,ui=True,adb=True,installer=True,bcb=True)
                with self.subTest(path=path),self.assertRaises(ValueError):
                    boot_interface(image,installer=True)

    def test_boot_store_requires_install_role_and_authenticates_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'store.img'
            fixture(image,adb=True,boot_store=True)
            with self.assertRaises(ValueError):boot_interface(image,installer=True)
            fixture(image,adb=True,installer=True,boot_store=True)
            self.assertEqual(boot_interface(image,installer=True)['native_recovery_manifest']['boot_store_version'],1)
            fixture(image,broken=STORE_FILES[0],adb=True,installer=True,boot_store=True)
            with self.assertRaises(ValueError):boot_interface(image,installer=True)

    def test_engine_keeps_native_role_and_logical_filename(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'recovery.img';fixture(p,adb=True)
            e=Engine(lambda _:None,root/'state');e.runtime_path=str(p);e.validate_installer(d)
            self.assertEqual(e.manifest['runtime_file'],'native-recovery.img')
            self.assertIn('native_recovery_manifest',e.manifest)
            self.assertNotIn('recovery_manifest',e.manifest)

    def test_lvgl_and_action_helpers_are_bound_to_the_image(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'ui.img';fixture(path,ui=True)
            self.assertEqual(boot_interface(path,installer=True)['native_recovery_manifest']['ui_version'],1)
            for name in UI_FILES:
                fixture(path,broken=name,ui=True)
                with self.subTest(name=name),self.assertRaises(ValueError):boot_interface(path,installer=True)

    def test_touch_module_loader_and_firmware_are_bound_to_image(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'touch.img';fixture(path,ui=True,touch=True)
            self.assertEqual(boot_interface(path,installer=True)['native_recovery_manifest']['touch_version'],1)
            for name in TOUCH_FILES:
                fixture(path,broken=name,ui=True,touch=True)
                with self.subTest(name=name),self.assertRaises(ValueError):boot_interface(path,installer=True)

    def test_legacy_cli_cannot_bypass_inspection_only(self):
        from liuqin_gui.demo import DemoEngine
        from liuqin_gui.engine import cli
        with tempfile.TemporaryDirectory() as d:
            engine=DemoEngine(lambda _:None,Path(d)/'state')
            fixture(engine.demo_bundle/'installer.img')
            error=io.StringIO()
            with patch.object(host,'run_process',side_effect=AssertionError('device access forbidden')),contextlib.redirect_stderr(error):
                with self.assertRaises(SystemExit):cli.main(['--bundle',str(engine.demo_bundle),'--allow-unverified'])
            self.assertIn('CLI 不开放',error.getvalue());engine.close()

    def test_discovery_allows_explicit_readonly_test_across_android_labels(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'recovery.img';fixture(p,adb=True)
            for label in ['MIUI','HyperOS','MikuUI','Linux (unknown firmware)']:
                env=dict(serial='tablet',label=label,kind='fastboot',properties={})
                selected,status=compatibility.select_image(env,str(p))
                self.assertTrue(status['ready']);self.assertTrue(status['inspection_only'])
                self.assertIn('固件指纹尚未读取',status['reason'])
                self.assertFalse(status['auto_selected']);self.assertFalse(status['automatic_boot_allowed'])
            env=dict(serial='tablet',kind='android',properties={'device':'liuqin'})
            self.assertTrue(compatibility.select_image(env,str(p))[1]['ready'])
            env['properties']['device']='pipa'
            self.assertFalse(compatibility.select_image(env,str(p))[1]['ready'])

    def test_non_scan_operations_stop_before_any_device_access(self):
        t=transport();t.engine.receipt['operation']='wizard'
        with patch('liuqin_gui.native_adb.probe_adb') as probe:
            with self.assertRaisesRegex(InstallerError,'仅供'):t.check_device()
            probe.assert_not_called()

    def test_flash_slot_switch_reboot_and_rpc_mutations_are_rejected(self):
        t=transport()
        for args in [('flash','boot_a','image'),('--set-active=b',),('reboot',),('reboot','bootloader')]:
            with self.subTest(args=args),self.assertRaises(InstallerError):t.fastboot(*args)
        for action in ('partition-edit','android-direct','install','linux-stage','partition-resume'):
            with self.subTest(action=action),self.assertRaises(InstallerError):t.android_rpc(action,{})
        t.remote.assert_not_called()

    def test_session_must_match_identity_lock_and_readonly_state(self):
        t=transport();good=dict(serial='tablet',build_id='a'*64,kernel='test-native',slot='a',
            inspection_only=True,sealed=True,unlocked=True,boot_id=t.boot_id,usb_transport='adb')
        t.validate_probe(good)
        for key,bad in [('serial','other'),('build_id','b'*64),('kernel','other'),('slot','b'),('sealed',False),('unlocked',False)]:
            with self.subTest(key=key),self.assertRaises(InstallerError):t.validate_probe(dict(good,**{key:bad}))

    def test_cleanup_retains_the_running_recovery(self):
        t=transport();t.cancel_cleanup()
        self.assertTrue(t.engine.receipt['recovery_retained'])
        t.remote.assert_called_once_with('/usr/bin/python3 -I -B /usr/lib/liuqin/native-status.py show complete',timeout=5)

    def test_partition_read_status_does_not_mask_rpc_failure(self):
        t=transport()
        with patch.object(Transport,'android_rpc',side_effect=InstallerError('original failure')):
            with self.assertRaisesRegex(InstallerError,'original failure'):t.android_rpc('partition-inspect',{})
        self.assertTrue(t.remote.call_args_list[0].args[0].endswith('show reading'))
        self.assertTrue(t.remote.call_args_list[-1].args[0].endswith('show error'))

    def test_status_collects_readonly_data_and_detects_mounts(self):
        module=load('native_status_fixture',PROJECT/'device/recovery/native-status.py')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            files={MARKER:json.dumps({'build_id':'a'*64}),'proc/cmdline':'androidboot.serialno=tablet androidboot.slot_suffix=_a androidboot.verifiedbootstate=orange',
                'proc/sys/kernel/random/boot_id':str(uuid.uuid4()),'proc/sys/kernel/osrelease':'test-native',
                'sys/class/block/sda/ro':'1','sys/class/block/sda/dev':'8:0','proc/self/mountinfo':''}
            for name,value in files.items():p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
            (root/'proc/device-tree').mkdir();(root/'proc/device-tree/compatible').write_bytes(b'xiaomi,liuqin\0qcom,sm8475\0')
            result=module.collect(root);self.assertTrue(result['sealed']);self.assertTrue(result['unlocked'])
            self.assertEqual(result['battery'],'unavailable');self.assertIn('READ ONLY',module.render(result))
            before={n:(root/n).read_bytes() for n in files}
            module.show('reading',root)
            self.assertEqual({n:(root/n).read_bytes() for n in files},before)
            self.assertEqual(module.collect(root)['phase'],'reading')
            (root/'proc/self/mountinfo').write_text('1 0 8:0 / /mnt rw - ext4 /dev/sda rw\n')
            self.assertFalse(module.collect(root)['sealed'])

    def test_native_package_is_separate_and_discoverable(self):
        package=load('native_package',PROJECT/'installer/package.py')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);image=root/'recovery.img';sources=adb_package_fixture(image)
            archive=package.package(root/'kit.zip',native_recovery=image,adbd_sources=sources)
            with zipfile.ZipFile(archive) as z:
                base=package.INSTALLER_ARCHIVE_ROOT+'/'
                self.assertIn(base+'installer/runtime/native-recovery.img',z.namelist())
                self.assertNotIn(base+'installer/runtime/recovery.img',z.namelist())
                self.assertIn(base+'docs/NATIVE-RECOVERY.md',z.namelist());z.extractall(root/'extracted')
            locator=load('native_locator',root/'extracted'/package.INSTALLER_ARCHIVE_ROOT/'installer/liuqin_gui/runtime.py')
            self.assertEqual(Path(locator.locate()).name,'native-recovery.img')
            state=root/'old-settings';state.mkdir()
            (state/'runtime.json').write_text(json.dumps(dict(installer_path=str(image),scope='standard')))
            self.assertEqual(Path(locator.locate(state_dir=state)).name,'native-recovery.img')
            (state/'runtime.json').write_text(json.dumps(dict(installer_path=str(image),scope=locator.settings_scope())))
            self.assertEqual(Path(locator.locate(state_dir=state)).name,'native-recovery.img')
            self.assertEqual(Path(locator.locate(explicit=image,state_dir=state)),image.resolve())
            with self.assertRaisesRegex(RuntimeError,'分开打包'):package.package(root/'bad.zip',native_recovery=image,recovery_image=image)


if __name__=='__main__':unittest.main(verbosity=2)
