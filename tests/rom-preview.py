#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""ROM input integrity, projected firmware conditions and non-executable plans."""
import copy,hashlib,importlib.util,io,json,struct,sys,tarfile,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
PROJECT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(PROJECT/'shared'),str(PROJECT/'installer')]
from liuqin_rom_analysis import analyze,reference,sha
from liuqin_rom_preview import build,validate_layout
from liuqin_universal_recovery import avb_measurement
from liuqin_firmware import fingerprint_id
from liuqin_gui.rom_preview import demo_capture,preview
from liuqin_gui.engine import Engine,Cancelled,InstallerError
from liuqin_gui import workflow
spec=importlib.util.spec_from_file_location('payload_fixture',PROJECT/'tests/miku-rom.py');fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def make(self,defect=None):
        f=fixture.PayloadTests();f.setUp();self.addCleanup(f.temp.cleanup)
        for i,name in enumerate(('vbmeta','vbmeta_system')):
            data=bytearray(4096);data[:4]=b'AVB0';struct.pack_into('>II',data,4,1,0);struct.pack_into('>I',data,120,3 if i==0 else 0);data[128]=i+1;f.data[name]=bytes(data)
        f.make(defect)
        p=dict(id='fixture-miku',label='fixture Miku',rom_abl=None,images={n:[dict(bytes=len(data),sha256=sha(data))] for n,data in f.data.items()},projected={},archive=f.profile['archive'])
        p['images']['super']=[{k:f.profile['images']['super.img'][k] for k in ('bytes','sha256')}]
        for n in ('vbmeta','vbmeta_system'):
            raw=f.data[n].ljust(131072,b'\0');digest=sha(raw);p['projected'][n]=dict(measurement=dict(partition_bytes=len(raw),hash_bytes=len(raw),sha256=digest),avb=avb_measurement(raw,digest))
        return f,p
    def test_known_roms_project_after_policy_not_rom_name(self):
        snapshot,rows=demo_capture('TEST');before=copy.deepcopy(snapshot)
        for pid,mode,status,abl,flags in [
            ('miui14','preserve','requires-avb-mode','miui-abl-20230422',0),
            ('hyperos206','preserve','requires-avb-mode','miui-abl-20230422',0),
            ('hyperos206','rom','offline-ready','hyperos-abl-20250318',0),
            ('miku26101322','rom','offline-ready','miui-abl-20230422',3)]:
            r=build(reference(pid),snapshot,rows,mode);self.assertEqual((r['rec_after']['status'],r['rec_after']['baseline_id'],r['rec_after']['root_avb_flags']),(status,abl,flags))
            self.assertFalse(r['executable']);self.assertNotIn('plan_id',r);self.assertFalse(r['rec_after']['fixed_install_allowed']);self.assertTrue(r['assumptions'])
            self.assertNotIn('recovery_a',[x['partition'] for x in r['writes']]);self.assertIn('linux_boot',r['protected'])
        self.assertEqual(snapshot,before)
    def test_unknown_current_abl_is_not_inferred_from_miku(self):
        snapshot,rows=demo_capture('TEST');s=snapshot['slots']['a'];s['match']['abl']['sha256']='f'*64;s['measured']=copy.deepcopy(s['match']);s['fingerprint_id']=fingerprint_id(s['match'])
        self.assertEqual(build(reference('miku26101322'),snapshot,rows)['rec_after']['status'],'unknown-baseline')
    def test_rom_abl_projection_cannot_invent_partition_capacity(self):
        snapshot,rows=demo_capture('TEST');s=snapshot['slots']['a'];s['match']['abl'].update(partition_bytes=4194304,hash_bytes=4194304,sha256='f'*64)
        s['measured']=copy.deepcopy(s['match']);s['fingerprint_id']=fingerprint_id(s['match'])
        r=build(reference('hyperos206'),snapshot,rows,'rom')
        self.assertEqual(r['rec_after']['status'],'unknown-baseline');self.assertTrue(any('ABL 分区容量' in b for b in r['blockers']))
    def test_missing_targets_and_writable_or_duplicate_layout(self):
        snapshot,rows=demo_capture('TEST');r=build(reference('miui14'),snapshot,[x for x in rows if x['name']!='userdata'])
        self.assertTrue(any('缺少 userdata' in s for s in r['blockers']))
        with self.assertRaises(ValueError):validate_layout(rows+[rows[0]])
        rows[0]['ro']='0'
        with self.assertRaises(ValueError):build(reference('miui14'),snapshot,rows)
    def test_directory_and_tar_parse_images_without_extracting_scripts(self):
        f,p=self.make();r=analyze(f.root/'reference',catalog=[p]);self.assertFalse(r['missing']);self.assertFalse(r['issues'])
        archive=self.root/'renamed.tar.gz'
        with tarfile.open(archive,'w:gz') as z:
            for path in (f.root/'reference').iterdir():z.add(path,arcname='rom/images/'+path.name)
            item=tarfile.TarInfo('flash_all.sh');body=b'touch /THIS_MUST_NOT_RUN';item.size=len(body);z.addfile(item,io.BytesIO(body))
        r=analyze(archive,catalog=[p]);self.assertEqual(r['profile']['id'],'fixture-miku');self.assertEqual(r['source']['kind'],'tar')
    def test_payload_archive_verified_without_full_extraction(self):
        f,p=self.make();r=analyze(f.path,catalog=[p]);self.assertEqual(r['profile']['id'],'fixture-miku');self.assertFalse(r['issues']);self.assertFalse(r['missing']);self.assertEqual(r['projected']['vbmeta']['avb']['flags'],3)
        self.assertFalse((f.root/'escape.sh').exists())
    def test_wrong_device_incremental_and_corrupt_payload_rejected(self):
        for defect in ('device','incremental','data-corrupt','overlap','source-copy'):
            with self.subTest(defect=defect):
                f,p=self.make(defect)
                with self.assertRaises((ValueError,KeyError)):analyze(f.path,catalog=[p])
    def test_changed_zip_not_promoted_by_filename(self):
        f,p=self.make();p['archive']['sha256']='f'*64
        r=analyze(f.path,catalog=[p]);self.assertTrue(any('完整包哈希不同' in s for s in r['issues']))
    def test_conflicting_avb_or_unknown_boot_is_rejected(self):
        f,p=self.make();directory=f.root/'reference';data=bytearray((directory/'vbmeta.img').read_bytes());data[128]^=1;(directory/'vbmeta.img').write_bytes(data)
        with self.assertRaisesRegex(ValueError,'AVB 元数据'):analyze(directory,catalog=[p])
        d=self.root/'unknown';d.mkdir();(d/'boot.img').write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError,'无法唯一识别'):analyze(d,catalog=[p])
    def test_flag_only_patch_is_observed_but_not_install_qualification(self):
        f,p=self.make();d=f.root/'reference';data=bytearray((d/'vbmeta.img').read_bytes());struct.pack_into('>I',data,120,0);(d/'vbmeta.img').write_bytes(data)
        r=analyze(d,catalog=[p]);self.assertEqual(r['projected']['vbmeta']['avb']['flags'],0);self.assertTrue(r['issues'])
        self.assertEqual(r['projected']['vbmeta']['measurement']['partition_bytes'],131072)
        self.assertEqual(r['projected']['vbmeta']['measurement']['sha256'],sha(bytes(data).ljust(131072,b'\0')))
    def test_symlinks_duplicate_members_and_changed_sources(self):
        f,p=self.make();d=f.root/'reference';link=self.root/'linked';link.symlink_to(d,target_is_directory=True)
        with self.assertRaises(ValueError):analyze(link,catalog=[p])
        archive=self.root/'duplicate.tar'
        with tarfile.open(archive,'w') as z:
            z.add(d/'boot.img',arcname='one/boot.img');z.add(d/'boot.img',arcname='two/boot.img')
        with self.assertRaisesRegex(ValueError,'重名'):analyze(archive,catalog=[p])
        once=[]
        def change():
            if not once:once.append(1);(d/'boot.img').touch()
        with self.assertRaisesRegex(ValueError,'发生变化'):analyze(d,change,catalog=[p])
    def test_cancel_is_not_swallowed(self):
        f,p=self.make()
        def stop():raise Cancelled('cancel')
        with self.assertRaises(Cancelled):analyze(f.path,stop,catalog=[p])
    def test_cancelled_backend_releases_readonly_state(self):
        e=Engine(lambda x:None,self.root/'state')
        def cancel(_):e.cancel_event.set();return reference('miui14')
        with patch('liuqin_gui.rom_preview.reference',side_effect=cancel):
            with self.assertRaises(Cancelled):preview(e,dict(serial='TEST',source_mode='reference',reference_id='miui14'))
        self.assertEqual(e.state,'cancelled');self.assertFalse(e.cancel_event.is_set());self.assertIsNone(e.rom_preview)
    def test_backend_preview_cannot_execute_or_boot_and_binds_fresh_capture(self):
        events=[];e=Engine(events.append,self.root/'state');snapshot,rows=demo_capture('TEST');e.transport=Mock();e.create_backup=Mock(side_effect=AssertionError('backup not expected'));e.state='wizard_scanned'
        with patch('liuqin_gui.rom_preview.capture',return_value=(snapshot,rows)) as capture:
            preview(e,dict(serial='TEST',source_mode='reference',reference_id='hyperos206',firmware_policy='preserve',firmware={'forged':True}))
            capture.assert_called_once_with(e,'TEST','')
        self.assertEqual(e.state,'wizard_scanned');self.assertFalse(e.rom_preview['executable']);self.assertEqual(e.transport.mock_calls,[]);e.create_backup.assert_not_called()
        path=Path(e.rom_preview['record_path']);self.assertEqual(path.stat().st_mode&0o777,0o600)
        with self.assertRaises(InstallerError):workflow.install(e,dict(plan_id=e.rom_preview['preview_id']))
        with patch('liuqin_gui.rom_preview.capture',side_effect=InstallerError('disconnected')):
            with self.assertRaises(InstallerError):preview(e,dict(serial='TEST',source_mode='reference',reference_id='miui14'))
        self.assertIsNone(e.rom_preview);self.assertEqual(e.state,'wizard_scanned')
if __name__=='__main__':unittest.main(verbosity=2)
