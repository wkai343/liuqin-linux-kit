#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pinned ROM import and raw/sparse plans without devices or downloads."""
import copy
import hashlib
import io
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.android import import_rom
from liuqin_gui.engine import InstallerError
from liuqin_android import load_profile,image_plan,verify_rom,linux_userdata_signature
from liuqin_android_images import Image

class RomTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.profile=copy.deepcopy(load_profile());self.profile['rom']='fixture-rom'
        self.payloads={name:(name+' payload').encode() for name in self.profile['images']}
        for name,data in self.payloads.items():self.profile['images'][name].update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        self.archive=self.root/'rom.tgz';self.cache=self.root/'cache'

    def make_archive(self,defect=None):
        with tarfile.open(self.archive,'w:gz') as tar:
            entries=[('rom/images/'+name,data) for name,data in self.payloads.items()]
            entries += [('rom/flash_all.sh',b'echo must-not-execute'),('../../outside',b'must-not-extract')]
            if defect=='duplicate':entries.append(entries[0])
            for index,(name,data) in enumerate(entries):
                item=tarfile.TarInfo(name)
                if index==0 and defect=='link':item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd';tar.addfile(item)
                else:
                    if index==0 and defect=='contents':data=b'x'*len(data)
                    item.size=len(data);tar.addfile(item,io.BytesIO(data))
        self.profile['archive']=dict(archive='rom.tgz',archive_root='rom',bytes=self.archive.stat().st_size,
            sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_full_rom_imports_only_the_nine_pinned_images(self):
        self.make_archive();result=import_rom(self.archive,self.profile,self.cache,lambda:None)
        self.assertEqual(set(result),set(self.profile['images']))
        self.assertEqual({p.name for p in (self.cache/'fixture-rom').iterdir()},set(self.profile['images']))
        self.assertFalse((self.root/'outside').exists())
        self.assertFalse(any(self.cache.rglob('flash_all.sh')))
        self.assertEqual(import_rom(self.archive,self.profile,self.cache,lambda:None),result)

    def test_corrupt_duplicate_or_linked_image_never_publishes_cache(self):
        for defect in ('contents','duplicate','link'):
            self.make_archive(defect)
            with self.subTest(defect=defect),self.assertRaises((ValueError,InstallerError)):
                import_rom(self.archive,self.profile,self.cache,lambda:None)
            self.assertFalse((self.cache/'fixture-rom').exists())

    def test_wrong_archive_or_modified_cache_is_rejected(self):
        self.make_archive();self.profile['archive']['sha256']='0'*64
        with self.assertRaises(InstallerError):import_rom(self.archive,self.profile,self.cache,lambda:None)
        self.make_archive();result=import_rom(self.archive,self.profile,self.cache,lambda:None)
        Path(result['boot.img']).write_bytes(b'changed')
        with self.assertRaises(ValueError):import_rom(self.archive,self.profile,self.cache,lambda:None)

    def test_sparse_raw_fill_dontcare_and_partition_tail_are_all_accounted_for(self):
        block=4096;raw=b'R'*block;fill=b'ABCD'
        content=struct.pack('<IHHHHIIII',0xed26ff3a,1,0,28,12,block,3,3,0)
        content+=struct.pack('<HHII',0xcac1,0,1,12+block)+raw
        content+=struct.pack('<HHII',0xcac2,0,1,16)+fill
        content+=struct.pack('<HHII',0xcac3,0,1,12)
        path=self.root/'super.img';path.write_bytes(content)
        expected=raw+fill*(block//4)+bytes(block*2)
        part=dict(name='super',bytes=len(expected))
        result=image_plan(path,part,dict(sha256=hashlib.sha256(content).hexdigest(),bytes=len(content)))
        self.assertEqual(result['expanded_bytes'],block*3)
        self.assertEqual(result['whole_sha256'],hashlib.sha256(expected).hexdigest())
        self.assertEqual(result['chunks'],[dict(offset=0,bytes=len(expected),sha256=hashlib.sha256(expected).hexdigest())])
        image=Image(path)
        try:self.assertEqual(image.read(block-2,8),expected[block-2:block+6])
        finally:image.close()
        with self.assertRaises(ValueError):image_plan(path,dict(name='super',bytes=block),dict(sha256='0'*64,bytes=len(content)))

    def test_opaque_android_data_is_not_classified_by_two_magic_bytes(self):
        header=bytearray(b'\xff'*65608);header[1080:1082]=b'\x53\xef'
        self.assertFalse(linux_userdata_signature(header))
        struct.pack_into('<I',header,1048,2);struct.pack_into('<H',header,1112,256)
        self.assertTrue(linux_userdata_signature(header))
        self.assertTrue(linux_userdata_signature(b'LUKS\xba\xbe'+bytes(65602)))

    def test_preservation_profile_covers_only_nonhole_ranges(self):
        profile=load_profile();previous=0
        for segment in profile['super']['verification_ranges']:
            self.assertGreater(segment['bytes'],0);self.assertGreaterEqual(segment['offset'],previous)
            previous=segment['offset']+segment['bytes']
            self.assertLessEqual(previous,profile['super']['expanded_bytes'])
        self.assertEqual(profile['userdata']['filesystem'],'f2fs')
        self.assertEqual(profile['metadata']['filesystem'],'ext4')
        self.assertNotIn('userdata',set(profile['images']))

if __name__=='__main__':unittest.main(verbosity=2)
