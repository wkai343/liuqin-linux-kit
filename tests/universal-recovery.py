#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Universal v2 packaging, AVB conditions, and non-authorizing routing tests."""
import copy,gzip,hashlib,importlib.util,json,struct,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
from liuqin_universal_recovery import USAGE,pack_v2,inspect,validate_dtbo,validate_policy,avb_measurement,assess
from liuqin_fixed_recovery import LIMIT,encoded,sha
from liuqin_native_recovery import MARKER,AGENT,BOARD_HELPER,UI_FILES,TOUCH_FILES,ADB_FILES,INSTALL_FILES
from liuqin_native_install import FIXED_USAGE,CAPABILITIES,capable
from liuqin_recovery_image import add,pack
from liuqin_firmware import fingerprint_id,validate_snapshot,load_catalog
from liuqin_artifacts import boot_interface
from liuqin_gui import native_profiles
POLICY=json.loads((PROJECT/'build/recovery/universal/liuqin-v2-policy.json').read_text())
spec=importlib.util.spec_from_file_location('fixed_fixture',PROJECT/'tests/fixed-recovery.py');fixed=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixed)

def observed(rom_id,baseline,flags):
    match=copy.deepcopy(load_catalog()[0]['match']);base=next(x for x in POLICY['baselines'] if x['id']==baseline);rom=next(x for x in POLICY['roms'] if x['id']==rom_id)
    match['abl']['sha256']=base['abl_partition_sha256']
    value=dict(status='complete',match=match,measured=copy.deepcopy(match),fingerprint_id=fingerprint_id(match),
      avb=dict(status='complete',metadata_bytes=rom['vbmeta']['metadata_bytes'],metadata_sha256='a'*64,normalized_flags_sha256=rom['vbmeta']['normalized_flags_sha256'],flags=flags,partition_sha256=match['vbmeta']['sha256']),
      avb_system=dict(status='complete',metadata_bytes=1024,metadata_sha256=rom['vbmeta_system_metadata_sha256'],normalized_flags_sha256='b'*64,flags=0,partition_sha256=match['vbmeta_system']['sha256']))
    return dict(schema=1,device='liuqin',serial='unit-test',boot_id=str(uuid.uuid4()),runtime_build_id='c'*64,active_slot='a',unlocked=True,sealed=True,slots={'a':value,'b':copy.deepcopy(value)})

class UniversalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixed.FixedTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    sign_raw=fixed.FixedTests.sign_raw
    def fixture(self,usage=USAGE,adb=False,installer=False):
        kernel=b'Linux universal test kernel\0'*100
        dtb=b'\xd0\x0d\xfe\xed'+bytes(36)+b'xiaomi,liuqin\0';dtb=dtb[:4]+struct.pack('>I',len(dtb))+dtb[8:]
        table=struct.pack('>8I',0xd7b7ab1e,64+len(dtb),32,32,1,32,4096,0)+struct.pack('>8I',len(dtb),64,0,0,0,0,0,0)+dtb
        p=copy.deepcopy(POLICY);p.update(kernel_sha256=sha(kernel),dtb_sha256=sha(dtb));p['embedded_dtbo']=dict(bytes=len(table),sha256=sha(table),entry_count=1,liuqin_index=0,liuqin_entry_sha256=sha(dtb))
        if installer:p['inspection_only']=False;adb=True;usage=FIXED_USAGE
        entries={}
        names=('init',AGENT,BOARD_HELPER,*UI_FILES,*TOUCH_FILES,*(ADB_FILES if adb else ()),*(INSTALL_FILES if installer else ()))
        for name in names:add(entries,name,('unit-test '+name).encode(),0o100755)
        add(entries,'etc/liuqin-storage-mode',b'readonly\n');payload={name:sha(entries[name][1]) for name in names}
        identity=dict(universal_policy=p,kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),embedded_dtbo_sha256=sha(table),payload_sha256=payload,avb_public_key_sha256=sha(self.pub))
        m=dict(schema=1,device='liuqin',interface='liuqin-native-recovery-v1',usage=usage,universal_version=1,fixed_boot_header=2,fixed_path_external_dtbo_required=False,universal_policy=p,identity=identity,build_id=sha(encoded(identity)),status='DEVICE_UNTESTED',inspection_only=True,
         kernel_sha256=sha(kernel),dtb_sha256=sha(dtb),payload_sha256=payload,board_identity_version=1,ui_version=1,touch_version=1)
        if adb:
            add(entries,'etc/liuqin-native-adb',b'1\n')
            identity.update(inspection_only=True,installer_capabilities=[])
            m.update(native_adb_version=1,usb_transport='adb',build_id=sha(encoded(identity)))
        if installer:
            versions=dict(installer_version=1,generic_ota_version=2,combined_install_version=1,boot_switch_version=1,android_sparse_store_version=1,fixed_installer_version=1)
            identity.update({key:value for key,value in versions.items() if key!='installer_version'})
            identity.update(inspection_only=False,installer_capabilities=CAPABILITIES)
            m.update(versions,inspection_only=False,installer_capabilities=CAPABILITIES,kernel_release='6.17-liuqin-test',build_id=sha(encoded(identity)))
            for name,value in [('etc/liuqin-installer',b'liuqin\n'),('etc/liuqin-installer-interface',b'liuqin-installer-v2\n'),('etc/liuqin-installer-kernel.release',b'6.17-liuqin-test\n'),('etc/liuqin-android-installer',b'1\n'),('etc/liuqin-multiboot',b'1\n'),('etc/liuqin-filesystems',b'ext4\nbtrfs\n')]:add(entries,name,value)
        add(entries,MARKER,encoded(m));header=bytearray(4096);header[:8]=b'ANDROID!'
        raw=pack_v2(gzip.compress(kernel,mtime=0),gzip.compress(pack(entries),mtime=0),dtb,table,header)
        path,data=self.sign_raw(raw);return path,data,p,table
    def test_full_v2_roundtrip(self):
        path,_,_,_=self.fixture();r=inspect(path);self.assertEqual(r['manifest']['usage'],USAGE);self.assertTrue(r['avb']['self_signature_verified']);self.assertGreater(r['dtb_offset'],r['dtbo_offset'])
    def test_adb_fixed_candidate_binds_readonly_identity_and_payloads(self):
        path,_,_,_=self.fixture(adb=True)
        manifest=inspect(path)['manifest']
        self.assertEqual(manifest['usb_transport'],'adb')
        self.assertTrue(manifest['inspection_only'])
        self.assertEqual(manifest['identity']['installer_capabilities'],[])
        self.assertTrue(set(ADB_FILES)<=set(manifest['payload_sha256']))
    def test_fixed_installer_is_usable_by_gui_and_keeps_temporary_boot_blocked(self):
        from types import SimpleNamespace
        from liuqin_gui.native_recovery_transport import NativeRecoveryTransport
        path,_,_,_=self.fixture(installer=True)
        checked=inspect(path);info=boot_interface(path,installer=True);manifest=info['native_recovery_manifest']
        self.assertTrue(capable(manifest));self.assertEqual(manifest,checked['manifest'])
        self.assertEqual(info['usb_network'],'adb-native');self.assertEqual(info['android_version'],1)
        self.assertEqual(info['multiboot_version'],1)
        transport=NativeRecoveryTransport(SimpleNamespace(manifest={'native_recovery_manifest':manifest}),'unit-test')
        with self.assertRaisesRegex(RuntimeError,'Recovery 分区启动'):transport.fastboot('boot',str(path))
        with self.assertRaisesRegex(RuntimeError,'进入已安装的固定'):transport.boot_ram()
        with self.assertRaises(ValueError):boot_interface(path)
        forged=dict(manifest,usage=USAGE)
        self.assertFalse(capable(forged))
        self.assertFalse(capable(dict(manifest,fixed_installer_version=0)))
    def test_running_fixed_installer_wins_over_default_temporary_choice(self):
        from types import SimpleNamespace
        path,_,_,_=self.fixture(installer=True);info=native_profiles.native_image(path)
        snapshot=observed('miui14','miui-abl-20230422',3);snapshot['runtime_build_id']=info['build_id']
        selected=str(path.parent/'temporary.img')
        temporary=dict(info,path=selected,build_id='f'*64,manifest=dict(info['manifest'],usage='temporary-boot-only'))
        probe={'build_id':info['build_id']};environment={'properties':probe}
        with patch.object(native_profiles,'find_running',return_value=str(path)),patch.object(native_profiles,'native_image',side_effect=lambda p:temporary if str(p)==selected else info),patch.object(native_profiles,'available',return_value=[temporary,info]),patch('liuqin_gui.firmware.verified_reader'),patch('liuqin_gui.firmware.collect',return_value=snapshot),patch.object(native_profiles,'save_snapshot',return_value='test.json'):
            _,chosen,status=native_profiles.inspect_running(SimpleNamespace(),environment,selected)
        self.assertEqual(chosen,str(path));self.assertTrue(status['ready']);self.assertTrue(status['install_allowed'])
    def test_offset_tampering_even_with_valid_self_signature(self):
        _,data,_,_=self.fixture();size=struct.unpack_from('>Q',data,LIMIT-64+12)[0];raw=bytearray(data[:size]);struct.pack_into('<Q',raw,1636,0)
        path,_=self.sign_raw(raw)
        with self.assertRaises(ValueError):inspect(path)
    def test_embedded_table_corruption_or_policy_mismatch(self):
        _,_,p,table=self.fixture();bad=bytearray(table);bad[-1]^=1
        with self.assertRaises(ValueError):validate_dtbo(bytes(bad),p)
        p['embedded_dtbo']['liuqin_index']=2
        with self.assertRaises(ValueError):validate_policy(p)
    def test_candidate_cannot_enter_temporary_gui_path(self):
        path,_,_,_=self.fixture()
        with self.assertRaisesRegex(ValueError,'通用 Recovery 候选'):boot_interface(path,installer=True)
        with self.assertRaises(ValueError):native_profiles.native_image(path)
    def test_wrong_candidate_role(self):
        path,_,_,_=self.fixture(usage='temporary-boot-only')
        with self.assertRaises(ValueError):inspect(path)
    def test_flag_normalization_does_not_ignore_keys_or_other_metadata(self):
        raw=bytearray(256);raw[:4]=b'AVB0';struct.pack_into('>II',raw,4,1,0);first=avb_measurement(raw)
        struct.pack_into('>I',raw,120,3);second=avb_measurement(raw)
        self.assertEqual(first['normalized_flags_sha256'],second['normalized_flags_sha256']);self.assertNotEqual(first['metadata_sha256'],second['metadata_sha256'])
        raw[128]=1;self.assertNotEqual(second['normalized_flags_sha256'],avb_measurement(raw)['normalized_flags_sha256'])
    def test_rom_and_baseline_matrix_is_never_authorization(self):
        for rom in POLICY['roms']:
            for base in POLICY['baselines']:
                for flags in range(4):
                    s=observed(rom['id'],base['id'],flags);r=assess(s,dict(policy=POLICY,artifact={}))
                    expected=not base['requires_verification_disabled'] or bool(flags&2)
                    self.assertEqual(r['requirements_satisfied'],expected,(rom['id'],base['id'],flags))
                    self.assertFalse(r['automatic_boot_allowed']);self.assertFalse(r['fixed_install_allowed']);self.assertFalse(r['automatic_avb_changes'])
    def test_no_external_dtbo_or_system_metadata_when_disabled(self):
        s=observed('miku26101322','miui-abl-20230422',3);slot=s['slots']['a'];measured=copy.deepcopy(slot['match']);del measured['dtbo'];del measured['vbmeta_system']
        s['slots']['a']=dict(status='incomplete',errors=['unit missing optional external payloads'],measured=measured,avb=slot['avb'])
        self.assertTrue(assess(s,dict(policy=POLICY,artifact={}))['requirements_satisfied'])
    def test_retained_verification_requires_system_metadata(self):
        s=observed('hyperos206','hyperos-abl-20250318',0);s['slots']['a']['avb_system']=dict(status='unavailable')
        r=assess(s,dict(policy=POLICY,artifact={}));self.assertEqual(r['status'],'requires-system-metadata');self.assertFalse(r['requirements_satisfied'])
    def test_miku_never_guesses_abl_and_stale_avb_is_rejected(self):
        s=observed('miku26101322','miui-abl-20230422',3);slot=s['slots']['a'];slot['match']['abl']['sha256']='f'*64;slot['measured']=copy.deepcopy(slot['match']);slot['fingerprint_id']=fingerprint_id(slot['match'])
        self.assertEqual(assess(s,dict(policy=POLICY,artifact={}))['status'],'unknown-baseline')
        s=observed('miui14','miui-abl-20230422',2);s['slots']['a']['avb']['partition_sha256']='f'*64
        with self.assertRaises(ValueError):validate_snapshot(s)
    def test_bad_policy_cannot_enable_avb_mutation(self):
        p=copy.deepcopy(POLICY);p['automatic_avb_changes']=True
        with self.assertRaises(ValueError):validate_policy(p)
    def test_packaged_candidate_is_separate_and_must_match_catalog(self):
        import zipfile
        def module(name,path):
            spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
        packager=module('universal_packager',PROJECT/'installer/package.py');native=module('universal_native_fixture',PROJECT/'tests/native-recovery.py')
        candidate,_,policy,_=self.fixture();checked=inspect(candidate)
        catalog=dict(policy=policy,artifact=dict(image_sha256=checked['image_sha256'],build_id=checked['manifest']['build_id']))
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);temp=root/'temporary.img';sources=native.adb_package_fixture(temp)
            with patch('liuqin_universal_recovery.load_catalog',return_value=catalog):
                out=packager.package(root/'kit.zip',native_recovery=temp,universal_candidate=candidate,adbd_sources=sources)
            with zipfile.ZipFile(out) as z:
                prefix=packager.INSTALLER_ARCHIVE_ROOT+'/';name=prefix+'installer/runtime/universal-candidates/'+checked['image_sha256']+'/recovery.img'
                self.assertEqual(sha(z.read(name)),checked['image_sha256'])
                self.assertEqual(z.read(prefix+'installer/runtime/native-recovery.img'),temp.read_bytes())
                self.assertFalse(any('/native-recoveries/' in n for n in z.namelist()))
                self.assertIn(prefix+'shared/liuqin-universal-recovery-profiles.json',z.namelist())
            catalog['artifact']['image_sha256']='f'*64
            with patch('liuqin_universal_recovery.load_catalog',return_value=catalog):
                with self.assertRaisesRegex(RuntimeError,'不一致'):packager.package(root/'bad.zip',native_recovery=temp,universal_candidate=candidate,adbd_sources=sources)
            self.assertFalse((root/'bad.zip').exists())
    def test_packaged_fixed_installer_is_discoverable_with_temporary_default(self):
        import zipfile
        spec=importlib.util.spec_from_file_location('fixed_installer_packager',PROJECT/'installer/package.py')
        packager=importlib.util.module_from_spec(spec);spec.loader.exec_module(packager)
        spec=importlib.util.spec_from_file_location('fixed_installer_native_fixture',PROJECT/'tests/native-recovery.py')
        native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
        candidate,_,_,_=self.fixture(installer=True);checked=inspect(candidate)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);temporary=root/'temporary.img';sources=native.adb_package_fixture(temporary)
            fixed_dir=root/'fixed';fixed_dir.mkdir();image=fixed_dir/'recovery.img';image.write_bytes(candidate.read_bytes())
            metadata=dict(checked['manifest'],image_sha256=checked['image_sha256'])
            (fixed_dir/'native-recovery-manifest.json').write_text(json.dumps(metadata))
            for name in ('STATUS.txt','NOTICE.md','SHA256SUMS','policy.json','recovery.avbpubkey'):(fixed_dir/name).write_text('fixture')
            out=packager.package(root/'kit.zip',native_recovery=temporary,fixed_installer=image,adbd_sources=sources)
            with zipfile.ZipFile(out) as archive:
                prefix=packager.INSTALLER_ARCHIVE_ROOT+'/installer/runtime/'
                self.assertEqual(archive.read(prefix+'native-recovery.img'),temporary.read_bytes())
                self.assertEqual(sha(archive.read(prefix+'fixed-installers/'+checked['image_sha256']+'/recovery.img')),checked['image_sha256'])
            with self.assertRaisesRegex(RuntimeError,'默认启动镜像需要临时版'):
                packager.package(root/'wrong-default.zip',native_recovery=image,adbd_sources=sources)
            metadata['build_id']='0'*64;(fixed_dir/'native-recovery-manifest.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(RuntimeError,'清单不一致'):
                packager.package(root/'bad.zip',native_recovery=temporary,fixed_installer=image,adbd_sources=sources)
if __name__=='__main__':unittest.main(verbosity=2)
