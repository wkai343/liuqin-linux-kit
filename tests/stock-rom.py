#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verified ROM retrieval and selective extraction, without downloads/devices."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/lib'))
import stock_rom


class StockRomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root/'rom/images'
        self.required = ['vendor_boot.img', 'dtbo.img', 'super.img', 'NON-HLOS.bin']
        self.payloads = {name: (name+' fixture').encode() for name in self.required}
        self.reference = {'rom': 'test-rom', 'images': {
            name: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            for name, data in self.payloads.items()}}
        self.record = dict(rom='test-rom', archive='rom.tgz', archive_root='rom',
                           required_images=self.required, mirrors=['https://source/one', 'https://source/two'])
        self.archive = self.root/'rom.tgz'
        self.make_archive()

    def make_archive(self, *, wrong=False, duplicate=False, link=False, traversal=False):
        with tarfile.open(self.archive, 'w:gz') as tar:
            entries = [('rom/images/'+name, data) for name, data in self.payloads.items()]
            entries += [('rom/flash_all.sh', b'exit 99'), ('../../outside', b'unrelated data')]
            if duplicate:
                entries.append(entries[0])
            for index, (name, data) in enumerate(entries):
                if index == 0 and wrong:
                    data = b'X'*len(data)
                if index == 0 and traversal:
                    name = 'rom/images/../../vendor_boot.img'
                entry = tarfile.TarInfo(name)
                if index == 0 and link:
                    entry.type = tarfile.SYMTYPE; entry.linkname = '/etc/passwd'; tar.addfile(entry)
                else:
                    entry.size = len(data); tar.addfile(entry, io.BytesIO(data))
        self.record.update(bytes=self.archive.stat().st_size,
                           sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_extracts_only_four_exact_images(self):
        stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        self.assertEqual({p.name for p in self.output.iterdir()}, set(self.required))
        self.assertFalse((self.root/'outside').exists())
        for name, data in self.payloads.items():
            self.assertEqual((self.output/name).read_bytes(), data)

    def test_wrong_member_hash_never_publishes_output(self):
        self.make_archive(wrong=True)
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        self.assertFalse(self.output.exists())

    def test_duplicate_required_member_is_rejected(self):
        self.make_archive(duplicate=True)
        with self.assertRaisesRegex(ValueError, '唯一普通文件'):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        self.assertFalse(self.output.exists())

    def test_required_symlink_is_rejected(self):
        self.make_archive(link=True)
        with self.assertRaisesRegex(ValueError, '普通文件'):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        self.assertFalse(self.output.exists())

    def test_traversal_cannot_satisfy_required_image(self):
        self.make_archive(traversal=True)
        with self.assertRaisesRegex(ValueError, '缺少'):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        self.assertFalse(self.output.exists())

    def test_cache_is_checked_before_reuse_without_network(self):
        stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        with patch.object(stock_rom, 'download_archive', side_effect=AssertionError('no network')):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference)
        damaged = self.output/'dtbo.img'; damaged.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference)
        self.assertEqual(damaged.read_bytes(), b'corrupt')

    def test_bad_archive_checksum_is_rejected(self):
        self.record['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, '固定 SHA256'):
            stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference, archive=self.archive)
        self.assertFalse(self.output.exists())

    def test_damaged_download_cache_is_preserved(self):
        cache = self.root/'cache'; cache.mkdir()
        archive = cache/self.record['archive']; archive.write_bytes(b'bad cache')
        with patch.object(stock_rom.urllib.request, 'urlopen', side_effect=AssertionError('no network')):
            with self.assertRaisesRegex(ValueError, '固定 SHA256'):
                stock_rom.download_archive(self.record, cache)
        self.assertEqual(archive.read_bytes(), b'bad cache')

    def test_falls_back_to_next_download_mirror_and_checks_bytes(self):
        content = self.archive.read_bytes()
        with patch.object(stock_rom.urllib.request, 'urlopen', side_effect=[OSError('unavailable'), io.BytesIO(content)]) as call:
            result = stock_rom.download_archive(self.record, self.root/'cache')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result.read_bytes(), content)

    def test_failed_download_is_not_left_as_valid_cache(self):
        content = b'X'*self.record['bytes']
        with patch.object(stock_rom.urllib.request, 'urlopen', side_effect=lambda *_a, **_k: io.BytesIO(content)):
            with self.assertRaisesRegex(ValueError, '所有 ROM 下载源'):
                stock_rom.download_archive(self.record, self.root/'cache')
        self.assertFalse((self.root/'cache'/self.record['archive']).exists())

    def test_wrong_rom_record_stops_before_any_download(self):
        self.record['rom'] = 'wrong-rom'
        with patch.object(stock_rom, 'download_archive', side_effect=AssertionError('no network')):
            with self.assertRaisesRegex(ValueError, '版本记录不一致'):
                stock_rom.fetch(self.output, self.root/'cache', self.record, self.reference)


if __name__ == '__main__':
    unittest.main(verbosity=2)
