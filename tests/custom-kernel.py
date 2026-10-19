#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise build orchestration with a mock compiler; does not compile a kernel."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('kernel_builder', project / 'build/scripts/build-liuqin-kernel.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.out = self.root / 'out'
        self.config = self.root / 'custom.config'; self.config.write_text('CONFIG_ZRAM=y\n')
        self.compiler = self.root / 'mock-compiler'; self.compiler.write_bytes(b'MOCK COMPILER ONLY\n')
        self.calls = []
        self.commit = '1' * 40

    def tearDown(self):
        self.temporary.cleanup()

    def output(self, *command):
        if 'rev-parse' in command: return self.commit
        if 'status' in command: return ''
        if '--version' in command: return 'mock compiler'
        if 'show' in command: return '2026-10-10T00:00:00+00:00'
        self.fail(str(command))

    def compiler_run(self, command, **kwargs):
        self.calls.append(command)
        if 'defconfig' in command:
            (self.out / '.config').write_text('CONFIG_MOCK=y\n')
        if 'Image' in command:
            for name in ('arch/arm64/boot/Image', 'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb', 'vmlinux', 'System.map'):
                path = self.out / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'MOCK ONLY')

    def invoke(self, *arguments):
        with patch.object(sys, 'argv', ['builder', '--source', str(self.source), '--out', str(self.out), *arguments]), \
             patch.object(builder, 'output', side_effect=self.output), \
             patch.object(builder.shutil, 'which', return_value=str(self.compiler)), \
             patch.object(builder.subprocess, 'run', side_effect=self.compiler_run):
            builder.main()

    def test_custom_source_and_config_record_actual_config_hash(self):
        self.invoke('--custom', '--config', str(self.config))
        info = json.loads((self.out / 'build-info.json').read_text())
        self.assertEqual(info['build_kind'], 'custom')
        self.assertEqual(info['commit'], self.commit)
        self.assertEqual(info['config_sha256'], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertFalse(any('defconfig' in command for command in self.calls))
        self.assertTrue(any('Image' in command for command in self.calls))

    def test_explicit_source_record_supplies_its_own_fragments_and_identity(self):
        record=json.loads((project/'build/kernel/source.json').read_text())
        record.update(commit=self.commit,upstream_base='2'*40,config_fragments=[str(self.config)])
        lock=self.root/'source.json';lock.write_text(json.dumps(record))
        self.invoke('--custom','--source-lock',str(lock),'--configure-only')
        merge=next(command for command in self.calls if any(str(x).endswith('merge_config.sh') for x in command))
        self.assertIn(str(self.config),merge)
        identity=json.loads((self.out/'build-inputs.json').read_text())
        self.assertEqual(identity['upstream_base'],'2'*40)
        self.assertEqual(identity['source_lock_sha256'],hashlib.sha256(lock.read_bytes()).hexdigest())

    def test_locked_build_still_rejects_unlocked_source(self):
        with self.assertRaises(SystemExit): self.invoke('--configure-only')
        self.assertFalse(self.calls)

    def test_extra_fragment_is_applied_after_default_device_fragments(self):
        fragment = project / 'build/kernel/configs/custom-zram.config'
        self.invoke('--custom', '--config-fragment', str(fragment), '--configure-only')
        merge = next(command for command in self.calls if 'merge_config.sh' in ' '.join(command))
        self.assertEqual(merge[-1], str(fragment))
        self.assertFalse(any('Image' in command for command in self.calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
