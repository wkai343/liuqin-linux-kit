#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Firmware routing must never turn discovery or an unknown ROM into a reboot."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'installer'),str(PROJECT/'shared')]
from liuqin_gui import compatibility as c, host
from liuqin_gui.engine import InstallerError,Transport
from liuqin_gui.recovery_transport import RecoveryTransport

VENDOR='Xiaomi/liuqin/liuqin:13/TKQ1/V14.0.3.0.TMYCNXM:user/release-keys'
KERNEL='5.10.101-fixture'
MANIFEST={'build_id':'a'*64}
PROFILE=dict(id='test-miui13',device='liuqin',image_sha256='b'*64,build_id='a'*64,
    match=dict(vendor_fingerprint=VENDOR,kernel=KERNEL),validation='TEMPORARY_BOOT_VERIFIED',
    evidence='UNIT TEST FIXTURE, not a physical qualification',android_profile_sha256=['c'*64])

def environment(**changes):
    props=dict(device='liuqin',android='13',build='V14.0.3.0.TMYCNXM',miui='V14',
        vendor_fingerprint=VENDOR,kernel=KERNEL,slot='_a',boot_id='12345678-1234-4234-8234-123456789abc')
    props.update(changes)
    return c.parse_probe('\n'.join(k+'='+v for k,v in props.items()),'tablet')

def transport():
    e=SimpleNamespace(manifest={'recovery_manifest':MANIFEST,'files':{'recovery.img':'b'*64}},
        receipt={},save_receipt=Mock(),check_cancel=Mock(),emit=Mock(),pause=Mock(),phase=Mock(),android_profile_sha='c'*64)
    t=RecoveryTransport.__new__(RecoveryTransport)
    t.engine=e;t.serial='tablet';t.boot_id=None;t.ram_active=False;t.adb_path='mock-adb';t.fastboot_path='mock-fastboot'
    t.adb=Mock();t.fastboot=Mock();t.read_environment=Mock(return_value=environment());return t

class CompatibilityTests(unittest.TestCase):
    def test_os_labels_do_not_define_compatibility(self):
        cases=[({},'MIUI'),({'miui':'','hyperos':'OS2.0'},'HyperOS'),({'lineage':'24.0'},'LineageOS'),({'miku':'Blooming'},'MikuUI')]
        for changes,label in cases:
            value=environment(**changes)
            self.assertIn(label,value['label'])
            self.assertTrue(c.assess(value,MANIFEST,'b'*64,[PROFILE])['ready'])
        self.assertEqual(c.parse_probe('kernel=6.1\nOS_RELEASE_BEGIN\nPRETTY_NAME="Fedora Linux"\nOS_RELEASE_END','tablet')['kind'],'linux')
        self.assertEqual(environment(bootmode='recovery')['kind'],'recovery')

    def test_exact_vendor_kernel_and_image_required(self):
        for props in [dict(vendor_fingerprint=''),dict(vendor_fingerprint=VENDOR.replace('14.0.3','14.0.4')),dict(kernel='different'),dict(device='other')]:
            self.assertFalse(c.assess(environment(**props),MANIFEST,'b'*64,[PROFILE])['ready'])
        self.assertFalse(c.assess(environment(),MANIFEST,'d'*64,[PROFILE])['ready'])
        self.assertFalse(c.assess(environment(),{'build_id':'d'*64},'b'*64,[PROFILE])['ready'])

    def test_android_version_alone_and_unknown_modes_never_qualify(self):
        self.assertFalse(c.assess(environment(),MANIFEST,'b'*64,[])['ready'])
        for kind in ['fastboot','linux','recovery','unauthorized','offline','sideload','disconnected']:
            self.assertFalse(c.assess(dict(kind=kind,properties={}),MANIFEST,'b'*64,[PROFILE])['ready'])

    def test_running_recovery_requires_exact_build(self):
        value=environment(paired_build='a'*64)
        self.assertTrue(c.assess(value,MANIFEST,'b'*64,[])['ready'])
        self.assertFalse(c.assess(value,{'build_id':'f'*64},'b'*64,[])['ready'])

    def test_catalog_rejects_incomplete_qualification(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'catalog.json'
            for bad in [dict(PROFILE,match={'android':'13'}),dict(PROFILE,validation='DEVICE_UNTESTED'),dict(PROFILE,evidence=''),dict(PROFILE,android_profile_sha256='c'*64)]:
                p.write_text(json.dumps(dict(schema=1,profiles=[bad])))
                with self.assertRaises(InstallerError):c.profiles(p)
            p.write_text(json.dumps(dict(schema=1,profiles=[PROFILE])))
            self.assertEqual(c.profiles(p),[PROFILE])
        self.assertEqual(c.profiles(),[],'no unperformed hardware tests may be claimed')

    def test_selection_routes_to_only_matching_variant(self):
        with patch.object(c,'profiles',return_value=[PROFILE]),patch.object(c,'image_candidates',return_value=['wrong.img','right.img']),\
             patch.object(c.runtime,'backend_hint',return_value='adb'),patch.object(c,'image_info',side_effect=[(MANIFEST,'f'*64),(MANIFEST,'b'*64)]):
            path,status=c.select_image(environment(),'wrong.img')
            self.assertEqual(path,'right.img');self.assertTrue(status['ready'])

    def test_discovery_uses_readonly_adb_commands(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if argv[-1]=='devices':return SimpleNamespace(stdout='tablet device\n',returncode=0)
            return SimpleNamespace(returncode=0,stdout=b'device=liuqin\nandroid=13\nmiui=V14\n',stderr=b'')
        with patch.object(host,'adb_path',return_value='mock-adb'),patch.object(host,'run_process',side_effect=run):
            self.assertEqual(c.inspect_device('tablet')['kind'],'android')
        self.assertEqual(len(calls),2);self.assertEqual(calls[1][3:5],['shell','-T'])
        self.assertFalse(any(word in ' '.join(argv) for argv in calls for word in [' reboot',' flash','fastboot boot']))

    def test_miui_without_profile_never_reboots(self):
        t=transport()
        with patch.object(host,'run_process',return_value=SimpleNamespace(stdout='tablet device\n')),patch.object(c,'profiles',return_value=[]):
            with self.assertRaisesRegex(InstallerError,'没有.*验收记录'):t.check_device()
        t.adb.assert_not_called();t.fastboot.assert_not_called()
        self.assertEqual(t.engine.receipt['environment']['kind'],'android')

    def test_qualified_android_transitions_only_after_matching(self):
        t=transport();t.wait_bootloader=Mock()
        with patch.object(host,'run_process',return_value=SimpleNamespace(stdout='tablet device\n')),patch.object(c,'profiles',return_value=[PROFILE]),\
             patch.object(Transport,'check_device',return_value={'product':'liuqin'}):
            self.assertEqual(t.check_device()['product'],'liuqin')
        t.adb.assert_called_once_with('reboot','bootloader');t.wait_bootloader.assert_called_once()
        self.assertEqual(t.bootstrap_profile,PROFILE['id'])

    def test_fastboot_without_live_witness_cannot_boot(self):
        t=transport()
        with self.assertRaisesRegex(InstallerError,'没有.*启动依据'):t.boot_ram()
        t.fastboot.assert_not_called()

    def test_live_witness_expires_and_is_bound_to_serial_and_image(self):
        t=transport()
        with patch.object(c.time,'monotonic',return_value=100):c.remember_running(t)
        with patch.object(c.time,'monotonic',return_value=101):self.assertTrue(c.has_running_witness(t))
        with patch.object(c.time,'monotonic',return_value=1001):self.assertFalse(c.has_running_witness(t))
        t.serial='different'
        with patch.object(c.time,'monotonic',return_value=101):self.assertFalse(c.has_running_witness(t))
        t.serial='tablet';t.engine.manifest['files']['recovery.img']='d'*64
        with patch.object(c.time,'monotonic',return_value=101):self.assertFalse(c.has_running_witness(t))

    def test_return_to_android_is_reported_without_probe_or_retry(self):
        t=transport();t.adb.return_value=b'device\n';t.probe=Mock()
        with self.assertRaisesRegex(InstallerError,'当前运行 MIUI'):t.wait_recovery()
        t.probe.assert_not_called();t.engine.pause.assert_not_called()
        self.assertIn('recovery_boot_observed_environment',t.engine.receipt)

    def test_android_destination_must_be_qualified_before_rpc(self):
        t=transport()
        with patch.object(c,'profiles',return_value=[]),patch.object(Transport,'android_rpc') as rpc:
            with self.assertRaisesRegex(InstallerError,'目标 Android ROM'):t.android_rpc('android-direct',{})
            rpc.assert_not_called()
        with patch.object(c,'profiles',return_value=[PROFILE]):t.validate_android_install()

    def test_other_final_recovery_is_rejected_before_workflow_writes(self):
        from liuqin_gui.recovery import validate_temporary_target
        t=transport();t.engine.simulation=False;t.engine.transport=t
        validate_temporary_target(t.engine,{'sha256':'b'*64})
        with self.assertRaisesRegex(InstallerError,'当前已核验'):validate_temporary_target(t.engine,{'sha256':'d'*64})

    def test_fastbootd_rejected_even_with_boot_evidence(self):
        t=transport();t.bootstrap_profile=PROFILE['id'];t.getvar=Mock(return_value='yes')
        with self.assertRaisesRegex(InstallerError,'fastbootd'):t.boot_ram()
        t.fastboot.assert_not_called()

    def test_failed_boot_cannot_reuse_old_running_evidence(self):
        t=transport();t.getvar=Mock(return_value='no')
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'recovery.img';image.write_bytes(b'test image')
            t.engine.installer_snapshot=image
            t.engine.manifest['files']['recovery.img']=hashlib.sha256(image.read_bytes()).hexdigest()
            c.remember_running(t)
            t.fastboot.side_effect=InstallerError('boot command failed')
            with self.assertRaisesRegex(InstallerError,'boot command failed'):t.boot_ram()
            self.assertFalse(c.has_running_witness(t))
            with self.assertRaisesRegex(InstallerError,'没有.*启动依据'):t.boot_ram()
            self.assertEqual(t.fastboot.call_count,1)

    def test_failed_write_diagnostics_preserve_receipt_and_existing_lock(self):
        from liuqin_gui.engine import Engine
        with tempfile.TemporaryDirectory() as d:
            events=[];engine=Engine(events.append,d);engine.state='failed'
            engine.receipt=dict(serial='tablet',write_dispatched=True,error='original failure')
            before=copy.deepcopy(engine.receipt);held=Mock();engine.device_lock=held
            with patch.object(c,'inspect_device',return_value=environment()),patch.object(c,'select_image',return_value=('',{'ready':False})):
                c.inspect(engine,{'serial':'tablet'})
            self.assertEqual(engine.receipt,before);held.close.assert_not_called();self.assertIs(engine.device_lock,held)
            self.assertEqual(events[-1]['type'],'environment');engine.device_lock=None

    def test_variant_packaging_contains_catalog_and_both_images(self):
        import zipfile
        def load(name,path):
            spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
        fixture=load('paired_fixture',PROJECT/'tests/paired-recovery.py')
        package=load('variant_package',PROJECT/'installer/package.py')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=root/'first.img';second=root/'second.img';fixture.paired(first);fixture.paired(second)
            # Distinct, valid complete images (zero padding is outside CPIO).
            second.write_bytes(second.read_bytes()+bytes(4096))
            result=package.package(root/'kit.zip',recovery_image=first,recovery_variants=[second])
            with zipfile.ZipFile(result) as archive:
                prefix=package.INSTALLER_ARCHIVE_ROOT+'/'
                self.assertEqual(archive.read(prefix+'installer/runtime/recovery.img'),first.read_bytes())
                digest=hashlib.sha256(second.read_bytes()).hexdigest()
                self.assertEqual(archive.read(prefix+'installer/runtime/recoveries/'+digest+'/recovery.img'),second.read_bytes())
                self.assertIn(prefix+'shared/liuqin-recovery-profiles.json',archive.namelist())

if __name__=='__main__':unittest.main(verbosity=2)
