#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prove completeness against independent GPT fixtures, never real disks."""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib

PROJECT = Path(__file__).resolve().parents[1]


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.work.name)/'ready-fixture'
        cc = shutil.which('cc') or shutil.which('gcc')
        if not cc: raise RuntimeError('C compiler is required')
        subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(PROJECT/'tests/storage-ready-mock.c'), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls): cls.work.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dev = self.root/'dev'; self.dev.mkdir()
        self.sys = self.root/'class/block'; self.sys.mkdir(parents=True)
        self.controller = self.root/'soc/1d84000.ufshc'
        (self.controller/'device_descriptor').mkdir(parents=True)
        (self.controller/'device_descriptor/number_of_luns').write_text('0x02\n')
        self.sector = 4096
        self.disks = {}
        self.disk('sda', 128*1024**3, [(1, 16, 31), (3, 64, 1023)])
        self.disk('sdb', 16*1024**2, [(1, 16, 31)])

    def tearDown(self): self.temp.cleanup()

    def disk(self, name, size, partitions):
        lun = ord(name[2])-ord('a')
        device = self.controller/f'host0/target0:0:0/0:0:0:{lun}'
        node = device/'block'/name; node.mkdir(parents=True, exist_ok=True)
        (node/'device').symlink_to(device)
        (self.sys/name).symlink_to(node)
        (device/'unit_descriptor').mkdir(exist_ok=True)
        (device/'unit_descriptor/lu_enable').write_text('0x02\n')
        (node/'dev').write_text(f'240:{lun*128}\n')
        (node/'size').write_text(f'{size//512}\n')
        self.disks[name] = (size, partitions)
        for number, start, last in partitions:
            part = node/(name+str(number)); part.mkdir(exist_ok=True)
            (self.sys/(name+str(number))).symlink_to(part)
            (self.dev/(name+str(number))).touch()
            for key, value in {'partition':number, 'start':start*self.sector//512,
                               'size':(last-start+1)*self.sector//512, 'dev':f'240:{lun*128+number}'}.items():
                (part/key).write_text(str(value)+'\n')
        self.write_gpt(name)

    def write_gpt(self, name, *, bad_header=False, bad_array=False, mirror_diff=False,
                  overlap=False, oversized=False, empty=False):
        size, partitions = self.disks[name]
        sector = self.sector; last = size//sector-1
        entries = bytearray(16*128)
        for number, first, end in ([] if empty else partitions):
            off=(number-1)*128
            entries[off:off+16]=bytes(range(1,17))
            entries[off+16:off+32]=bytes(range(17,33))
            if overlap and number==3: first=20
            struct.pack_into('<QQQ',entries,off+32,first,end,0)
        nsectors=(len(entries)+sector-1)//sector
        def header(current, alternate, array):
            h=bytearray(sector);h[:8]=b'EFI PART'
            struct.pack_into('<IIIIQQQQ',h,8,0x10000,92,0,0,current,alternate,2+nsectors,last-nsectors-1)
            h[56:72]=b'fixture-disk-id!'
            struct.pack_into('<QIII',h,72,array,0xffffffff if oversized else 16,128,zlib.crc32(entries))
            struct.pack_into('<I',h,16,zlib.crc32(h[:92]))
            return h
        primary=header(1,last,2);backup=header(last,1,last-nsectors)
        if bad_header: primary[16]^=1
        stored=bytearray(entries)
        if bad_array: stored[0]^=1
        mirror=bytearray(stored)
        if mirror_diff: mirror[56]^=1
        with (self.dev/name).open('wb') as f:
            f.truncate(size)
            for lba,data in [(1,primary),(2,stored),(last-nsectors,mirror),(last,backup)]:
                f.seek(lba*sector);f.write(data)

    def run_ready(self, passes):
        result=subprocess.run([self.binary,self.dev,self.sys,self.controller,str(self.sector)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0 if passes else 1,result.stdout+result.stderr)
        if passes: self.assertIn('2 UFS LUNs; 5 block nodes',result.stdout)
        else: self.assertEqual(result.stdout,'')

    def test_128_and_256_gib_geometries_and_noncontiguous_gpt_slots(self):
        self.run_ready(True)
        size, parts=self.disks['sda'];self.disks['sda']=(256*1024**3,parts)
        (self.sys/'sda/size').write_text(str(256*1024**3//512)+'\n')
        self.write_gpt('sda');self.run_ready(True)

    def test_512_byte_sectors(self):
        self.sector=512
        for name,(size,parts) in self.disks.items():
            for number,start,last in parts:
                (self.sys/f'{name}{number}/start').write_text(str(start)+'\n')
                (self.sys/f'{name}{number}/size').write_text(str(last-start+1)+'\n')
            self.write_gpt(name)
        self.run_ready(True)

    def test_missing_lun_does_not_look_ready(self):
        (self.dev/'sdb').unlink();(self.dev/'sdb1').unlink();self.run_ready(False)

    def test_descriptor_absent_or_more_luns_expected(self):
        p=self.controller/'device_descriptor/number_of_luns'
        for value in ['0x03\n','0\n','129\n','-1\n','2junk\n']:
            with self.subTest(value=value): p.write_text(value);self.run_ready(False)
        p.unlink();self.run_ready(False)

    def test_delayed_partition_must_appear_before_success(self):
        p=self.dev/'sda3';p.unlink();self.run_ready(False)
        p.touch();self.run_ready(True)

    def test_extra_partition_is_not_ignored(self):
        (self.dev/'sda4').touch();self.run_ready(False)

    def test_incomplete_or_invalid_gpt_does_not_finish_wait_early(self):
        for keyword in ['bad_header','bad_array','mirror_diff','overlap','oversized','empty']:
            with self.subTest(keyword=keyword): self.write_gpt('sda',**{keyword:True});self.run_ready(False)

    def test_partition_geometry_must_match_gpt(self):
        for field in ['partition','start','size']:
            with self.subTest(field=field):
                p=self.sys/f'sda3/{field}';old=p.read_text();p.write_text('999\n')
                self.run_ready(False);p.write_text(old)

    def test_node_numbers_and_sysfs_parent_are_verified(self):
        p=self.sys/'sda3/dev';old=p.read_text();p.write_text('240:99\n');self.run_ready(False);p.write_text(old)
        p=self.sys/'sda3';old=p.resolve();p.unlink();p.symlink_to(self.sys/'sdb1')
        self.run_ready(False);p.unlink();p.symlink_to(old)

    def test_symlink_node_is_not_canonical(self):
        p=self.dev/'sda3';p.rename(self.dev/'alias');p.symlink_to('alias');self.run_ready(False)

    def test_non_ufs_disk_or_disabled_unit_prevents_early_exit(self):
        p=self.sys/'sdb/device';target=p.resolve();p.unlink();p.symlink_to(self.root)
        self.run_ready(False);p.unlink();p.symlink_to(target)
        (target/'unit_descriptor/lu_enable').write_text('0x00\n');self.run_ready(False)

    def test_truncated_backup_is_rejected(self):
        size,_=self.disks['sda']
        with (self.dev/'sda').open('r+b') as f: f.seek(size-self.sector);f.write(b'\0'*self.sector)
        self.run_ready(False)

    def test_abl_stale_backup_entries_crc_with_identical_arrays_is_ready(self):
        size,_=self.disks['sda']
        with (self.dev/'sda').open('r+b') as f:
            f.seek(size-self.sector);h=bytearray(f.read(self.sector))
            h[88]^=1
            struct.pack_into('<I',h,16,0)
            struct.pack_into('<I',h,16,zlib.crc32(h[:92]))
            f.seek(size-self.sector);f.write(h)
        self.run_ready(True)

    def test_bad_backup_header_crc_is_still_rejected(self):
        size,_=self.disks['sda']
        with (self.dev/'sda').open('r+b') as f:
            f.seek(size-self.sector+16);value=f.read(1)
            f.seek(size-self.sector+16);f.write(bytes([value[0]^1]))
        self.run_ready(False)


if __name__=='__main__': unittest.main(verbosity=2)
