#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check content identity independently with Git, without network or device access."""
import contextlib
import hashlib
import importlib.util
import io
import marshal
from pathlib import Path
import py_compile
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('aosp_fetch_test', PROJECT / 'build/lib/aosp_mkbootimg.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.files = {'mkbootimg.py': (0o755, b'print("test help")\n'),
                      'unpack_bootimg.py': (0o755, b'print("test unpack help")\n'),
                      'gki/helper.py': (0o644, b'# import dependency\n'),
                      'a/file': (0o644, b'directory sorting fixture\n'),
                      'a.txt': (0o644, b'file sorting fixture\n')}
        repository = self.root / 'git-source'; repository.mkdir()
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        for name, (mode, payload) in self.files.items():
            file = repository / name; file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(payload); file.chmod(mode)
        subprocess.run(['git', '-C', str(repository), '-c', 'core.filemode=true', 'add', '.'], check=True)
        self.tree = subprocess.check_output(['git', '-C', str(repository), 'write-tree'], text=True).strip()
        self.hashes = {name: hashlib.sha256(self.files[name][1]).hexdigest() for name in ('mkbootimg.py', 'unpack_bootimg.py')}

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self, name='fixture.tar.gz', *, files=None, mtime=0, extra=None):
        path = self.root / name
        with tarfile.open(path, 'w:gz') as archive:
            for name, (mode, payload) in (self.files if files is None else files).items():
                info = tarfile.TarInfo(name); info.mode = mode; info.size = len(payload)
                info.mtime = mtime; info.uid = mtime; info.gid = mtime
                archive.addfile(info, io.BytesIO(payload))
            if extra is not None: archive.addfile(extra)
        return path

    def check(self, path):
        return fetch.archive_files(path, expected_tree=self.tree, entry_hashes=self.hashes)

    def test_repacked_archive_matches_git_tree_despite_different_archive_hash(self):
        first = self.archive('first.tar.gz')
        second = self.archive('second.tar.gz', mtime=123, files=dict(reversed(list(self.files.items()))))
        self.assertNotEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
        self.assertEqual(self.check(first), self.check(second))
        self.assertEqual(fetch.source_tree(self.files), self.tree)

    def test_changed_imported_dependency_is_rejected_even_when_entry_scripts_match(self):
        files = dict(self.files); files['gki/helper.py'] = (0o644, b'modified dependency\n')
        with self.assertRaisesRegex(ValueError, '固定 AOSP 提交'):
            self.check(self.archive(files=files))

    def test_missing_extra_or_executable_mode_changed_files_are_rejected(self):
        variants = [dict(self.files), dict(self.files), dict(self.files)]
        variants[0].pop('gki/helper.py')
        variants[1]['unexpected.py'] = (0o644, b'extra\n')
        variants[2]['gki/helper.py'] = (0o755, self.files['gki/helper.py'][1])
        for index, files in enumerate(variants):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, '固定 AOSP 提交'):
                self.check(self.archive(str(index)+'.tar.gz', files=files))

    def test_traversal_links_and_duplicate_entries_are_rejected(self):
        traversal = tarfile.TarInfo('../outside'); traversal.size = 0
        link = tarfile.TarInfo('linked'); link.type = tarfile.SYMTYPE; link.linkname = '/outside'
        duplicate = tarfile.TarInfo('mkbootimg.py'); duplicate.size = 0
        for index, extra in enumerate((traversal, link, duplicate)):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.check(self.archive('unsafe-'+str(index)+'.tar.gz', extra=extra))
        self.assertFalse((self.root.parent / 'outside').exists())

    def prepare_fixture(self, archive):
        downloads = self.root / 'project/inputs/downloads'; downloads.mkdir(parents=True)
        cached = downloads / ('aosp-mkbootimg-'+fetch.COMMIT+'.tar.gz')
        cached.write_bytes(archive.read_bytes())
        return self.root / 'project', cached

    def test_existing_archive_and_empty_old_directory_work_without_redownload(self):
        project, cached = self.prepare_fixture(self.archive())
        target = project / 'inputs/aosp-mkbootimg'; target.mkdir()
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             patch.object(fetch.urllib.request, 'urlopen') as network, contextlib.redirect_stdout(io.StringIO()):
            fetch.prepare(project); fetch.prepare(project)
            network.assert_not_called()
        self.assertEqual((target / 'gki/helper.py').read_bytes(), self.files['gki/helper.py'][1])
        self.assertFalse(any(target.rglob('__pycache__')))
        self.assertTrue(cached.is_file())

    def test_corrupt_existing_archive_is_preserved_and_never_published(self):
        files = dict(self.files); files['gki/helper.py'] = (0o644, b'changed')
        project, cached = self.prepare_fixture(self.archive(files=files)); original = cached.read_bytes()
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             patch.object(fetch.urllib.request, 'urlopen') as network, self.assertRaisesRegex(ValueError, '固定 AOSP 提交'):
            fetch.prepare(project)
        network.assert_not_called()
        self.assertEqual(cached.read_bytes(), original)
        self.assertFalse((project / 'inputs/aosp-mkbootimg').exists())

    def test_cached_dependency_tampering_is_detected_and_preserved(self):
        project, _ = self.prepare_fixture(self.archive())
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             contextlib.redirect_stdout(io.StringIO()):
            target = fetch.prepare(project)
            dependency = target / 'gki/helper.py'; dependency.write_bytes(b'changed cached dependency')
            with self.assertRaisesRegex(ValueError, '固定 AOSP 提交'): fetch.prepare(project)
        self.assertEqual(dependency.read_bytes(), b'changed cached dependency')

    def test_verified_source_cache_removes_bytecode_before_reuse(self):
        project, _ = self.prepare_fixture(self.archive())
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             patch.object(fetch.urllib.request, 'urlopen') as network, contextlib.redirect_stdout(io.StringIO()):
            target = fetch.prepare(project)
            helper = target / 'gki/helper.py'
            cache = Path(py_compile.compile(str(helper), doraise=True))
            # Valid timestamp/size metadata must not make unchecked cached code
            # executable even with -B, which only disables bytecode writes.
            header = cache.read_bytes()[:16]
            cache.write_bytes(header + marshal.dumps(compile(
                'raise RuntimeError("unchecked cached code executed")', str(helper), 'exec')))
            other = cache.with_name('helper.cpython-999.pyc')
            other.write_bytes(b'old interpreter cache')
            fetch.prepare(project)
            fetch.prepare(project)
            network.assert_not_called()
        self.assertFalse(any(target.rglob('__pycache__')))
        subprocess.run([sys.executable, '-B', '-c',
                        'import sys; sys.path.insert(0, sys.argv[1]); import gki.helper', str(target)], check=True)
        self.assertFalse(any(target.rglob('__pycache__')))

    def test_changed_source_preserves_bytecode_and_stops_cleanup(self):
        project, _ = self.prepare_fixture(self.archive())
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             contextlib.redirect_stdout(io.StringIO()):
            target = fetch.prepare(project)
            helper = target / 'gki/helper.py'
            cache = Path(py_compile.compile(str(helper), doraise=True))
            original = cache.read_bytes()
            helper.write_bytes(b'changed source')
            with self.assertRaisesRegex(ValueError, '固定 AOSP 提交'):
                fetch.prepare(project)
        self.assertEqual(cache.read_bytes(), original)
        self.assertEqual(helper.read_bytes(), b'changed source')

    def test_unrecognized_cache_files_and_links_are_preserved_and_rejected(self):
        for kind in ('file', 'orphan', 'link', 'directory-link'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary)
                target = project / 'inputs/aosp-mkbootimg'; target.mkdir(parents=True)
                for name, (mode, payload) in self.files.items():
                    file = target / name; file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(payload); file.chmod(mode)
                cache = target / 'gki/__pycache__'
                if kind == 'directory-link':
                    cache.symlink_to(self.root, target_is_directory=True)
                    extra = cache
                else:
                    cache.mkdir()
                    extra = cache / ('notes.txt' if kind == 'file' else
                                     'missing.cpython-311.pyc' if kind == 'orphan' else 'helper.cpython-311.pyc')
                    if kind == 'link':extra.symlink_to(self.root / 'git-source/gki/helper.py')
                    else:extra.write_bytes(b'preserve unexpected file')
                with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
                     self.assertRaisesRegex(ValueError, '缓存'):
                    fetch.prepare(project)
                self.assertTrue(extra.exists())
                if kind in ('link', 'directory-link'):self.assertTrue(extra.is_symlink())

    def test_failed_download_never_becomes_a_cache_or_tool_directory(self):
        payload = self.archive(files={'mkbootimg.py': self.files['mkbootimg.py']}).read_bytes()
        project = self.root / 'project'
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             patch.object(fetch.urllib.request, 'urlopen', return_value=io.BytesIO(payload)), self.assertRaises(ValueError):
            fetch.prepare(project)
        self.assertFalse((project / 'inputs/aosp-mkbootimg').exists())
        self.assertEqual(list((project / 'inputs/downloads').iterdir()), [])

    def test_new_download_is_verified_published_and_then_reused(self):
        payload = self.archive().read_bytes(); project = self.root / 'project'
        with patch.object(fetch, 'TREE', self.tree), patch.object(fetch, 'ENTRY_SHA256', self.hashes), \
             patch.object(fetch.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as network, \
             contextlib.redirect_stdout(io.StringIO()):
            target = fetch.prepare(project); fetch.prepare(project)
            network.assert_called_once()
        cached = project / 'inputs/downloads' / ('aosp-mkbootimg-'+fetch.COMMIT+'.tar.gz')
        self.assertEqual(cached.read_bytes(), payload)
        self.assertEqual((target / 'mkbootimg.py').read_bytes(), self.files['mkbootimg.py'][1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
