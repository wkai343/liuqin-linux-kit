#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Canonical node snapshots using disposable files, never real disks."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dev = self.root/'fake dev'; self.dev.mkdir()
        self.calls = self.root/'calls'
        self.busybox = shutil.which('busybox')
        if not self.busybox: raise RuntimeError('Host BusyBox is required')
        self.wrapper = self.root/'bb'
        self.wrapper.write_text('#!'+sys.executable+'\n'+r'''
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['CALLS']).open('a') as f:f.write(json.dumps(args)+'\n')
if args[0]=='realpath':
    fault=os.environ.get('BATCH_FAULT','')
    if fault=='unavailable':sys.exit(127)
    if fault=='empty':sys.exit(0)
    if fault=='reverse':
        print('\n'.join(str(Path(p).resolve()) for p in reversed(args[1:])))
        sys.exit(0)
    if fault in ['remove','alias','directory']:
        p=Path(args[-1])
        if fault=='alias':
            target=p.with_name('real-node');p.rename(target);p.symlink_to(target)
        else:
            p.unlink()
            if fault=='directory':p.mkdir()
os.execv(os.environ['REAL_BB'],[os.environ['REAL_BB'],*args])
''')
        self.wrapper.chmod(0o755)
        source=(PROJECT/'device/boot/initramfs/init').read_text()
        functions='\n'.join(re.search(r'^'+name+r'\(\) \{\n.*?^\}',source,re.M|re.S)[0]
                            for name in ['storage_path_is_block','storage_collect_sd_nodes'])
        self.script=self.root/'collect.sh'
        self.script.write_text(functions+'\nstorage_collect_sd_nodes "$1"\n')

    def tearDown(self): self.temp.cleanup()

    def nodes(self, names=('sda','sda1','sda35','sdb')):
        for name in names:(self.dev/name).touch()

    def run_collect(self, success=True, root=None, **env):
        self.calls.unlink(missing_ok=True)
        environment=dict(os.environ,BB=str(self.wrapper),CALLS=str(self.calls),
                         REAL_BB=self.busybox,LIUQIN_INIT_STORAGE_TEST_ONLY='1')
        environment.update(env)
        result=subprocess.run([self.busybox,'sh',str(self.script),str(root or self.dev)],
                              env=environment,
                              capture_output=True,text=True,timeout=5)
        if success:self.assertEqual(result.returncode,0,result.stderr)
        else:
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout,'')
        calls=[json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result,calls

    def test_complete_sorted_namespace_uses_one_canonicalization(self):
        self.nodes()
        result,calls=self.run_collect()
        self.assertEqual(result.stdout,'sda\nsda1\nsda35\nsdb\n')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0],['realpath',*[str(p) for p in sorted(self.dev.iterdir())]])

    def test_empty_namespace_never_produces_a_snapshot(self):
        self.run_collect(success=False)

    def test_regular_files_are_rejected_without_explicit_host_test_mode(self):
        self.nodes()
        self.run_collect(success=False,LIUQIN_INIT_STORAGE_TEST_ONLY='0')

    def test_node_alias_and_dangling_symlink_are_rejected(self):
        self.nodes()
        p=self.dev/'sdb';p.unlink();p.symlink_to('sda')
        self.run_collect(success=False)
        p.unlink();p.symlink_to('missing')
        self.run_collect(success=False)

    def test_directory_alias_is_rejected(self):
        self.nodes();alias=self.root/'alias';alias.symlink_to(self.dev)
        self.run_collect(success=False,root=alias)

    def test_failed_batch_runs_full_original_checks(self):
        self.nodes()
        result,calls=self.run_collect(BATCH_FAULT='unavailable')
        self.assertEqual(result.stdout,'sda\nsda1\nsda35\nsdb\n')
        self.assertEqual([c[0] for c in calls],['realpath',*['readlink']*4])
        (self.dev/'sdb').unlink();(self.dev/'sdb').symlink_to('sda')
        self.run_collect(success=False,BATCH_FAULT='unavailable')

    def test_empty_or_reordered_batch_output_cannot_prove_paths(self):
        self.nodes()
        for fault in ['empty','reverse']:
            with self.subTest(fault=fault):self.run_collect(success=False,BATCH_FAULT=fault)

    def test_removal_during_batch_is_rejected_even_by_fallback(self):
        self.nodes();self.run_collect(success=False,BATCH_FAULT='remove')

    def test_alias_introduced_during_batch_is_rejected(self):
        self.nodes();self.run_collect(success=False,BATCH_FAULT='alias')

    def test_block_type_is_rechecked_after_batch(self):
        self.nodes();self.run_collect(success=False,BATCH_FAULT='directory')

    def test_namespace_changes_are_visible_to_the_next_checkpoint(self):
        self.nodes();before,_=self.run_collect()
        (self.dev/'sdc').touch();after,_=self.run_collect()
        self.assertNotEqual(before.stdout,after.stdout)
        self.assertEqual(after.stdout,before.stdout+'sdc\n')

    def test_larger_partition_sets_keep_noncontiguous_names(self):
        names=['sda','sdb']+[f'sda{i}' for i in range(1,70)]+[f'sdb{i}' for i in range(1,68)]
        self.nodes(names)
        result,calls=self.run_collect()
        self.assertEqual(result.stdout.splitlines(),sorted(names))
        self.assertEqual(len(calls),1)


if __name__ == '__main__':unittest.main(verbosity=2)
