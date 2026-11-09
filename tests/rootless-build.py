#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""UID-map and privilege routing checks; no mounts or sudo are executed."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'build/lib'))
import rootless_build
spec=importlib.util.spec_from_file_location('rootless_build_driver',PROJECT/'build/devbox/build.py')
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)

class MappingTests(unittest.TestCase):
    def test_complete_maps_are_required_including_uninteresting_middle_ids(self):
        self.assertTrue(rootless_build.mapped_ids('0 1000 1\n1 524288 65536\n'))
        self.assertFalse(rootless_build.mapped_ids('0 1000 1\n'))
        self.assertFalse(rootless_build.mapped_ids('0 0 4294967295\n'))
        self.assertFalse(rootless_build.mapped_ids('0 1000 1\n1 524288 1\n65534 600000 1\n'))
        with self.assertRaises(ValueError):rootless_build.mapped_ids('0 1000 0\n')

    def command(self,root,env=None,missing=False):
        def which(name):return None if missing and name=='podman' else '/tools/'+name
        with patch.object(driver.os,'geteuid',return_value=1000), \
             patch.object(driver.shutil,'which',side_effect=which), \
             patch.dict(os.environ,{'LIUQIN_ROOTFS_MODE':'rootless'}), \
             patch.object(driver.subprocess,'run') as run:
            driver.run(['python3','rootfs.py'],root=root,env=env)
            return run.call_args.args[0]

    def test_root_stages_use_namespace_launcher_and_regular_stages_stay_unprivileged(self):
        command=self.command(True)
        self.assertEqual(command[:3],['/tools/podman','unshare','--'])
        self.assertIn(str(PROJECT/'build/lib/rootless_build.py'),command)
        self.assertNotIn('/tools/sudo',command)
        self.assertEqual(command[-2:],['python3','rootfs.py'])
        self.assertEqual(self.command(False),['python3','rootfs.py'])

    def test_sudo_requires_explicit_selection_and_missing_podman_never_prompts(self):
        self.assertEqual(self.command(True,{'LIUQIN_ROOTFS_MODE':'sudo'})[:2],['/tools/sudo','--'])
        with self.assertRaisesRegex(RuntimeError,'不自动请求 sudo'):self.command(True,missing=True)
        with self.assertRaises(ValueError):self.command(True,{'LIUQIN_ROOTFS_MODE':'invalid'})

if __name__=='__main__':unittest.main(verbosity=2)
