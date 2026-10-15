#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise DTB memory cleanup without any tablet or physical memory access."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('memory_cleanup',PROJECT/'build/lib/reclaim-memory.py')
cleanup=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleanup)

@unittest.skipUnless(shutil.which('dtc') and shutil.which('fdtput'),'Device tree tools required')
class Tests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.work=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def dtb(self,*,compatible='xiaomi,liuqin',size='0x20000000',consumer=False,mte=True):
        source=self.work/'source.dts';out=self.work/'source.dtb'
        region=('mte: memory@c0000000 { reg=<0 0xc0000000 0 '+size+'>; no-map; };') if mte else ''
        reference='test-consumer { memory-region=<&mte>; };' if consumer else ''
        source.write_text('/dts-v1/; / { compatible="'+compatible+'"; #address-cells=<2>; #size-cells=<2>; '
          'memory@80000000 { device_type="memory"; reg=<0 0x80000000 0 0x6a000000>,<0 0xf1400000 0 0xec00000>; }; '
          'chosen { bootargs="original"; }; reserved-memory { #address-cells=<2>; #size-cells=<2>; ranges; '+region+
          'memory@ea000000 { reg=<0 0xea000000 0 0x3900000>; no-map; }; '
          'memory@ed900000 { reg=<0 0xed900000 0 0x3b00000>; no-map; }; }; '+reference+' };')
        subprocess.run(['dtc','-q','-I','dts','-O','dtb','-o',str(out),str(source)],check=True)
        return out
    def test_removes_mte_but_keeps_firmware_ram_holes_and_trusted_regions(self):
        source=self.dtb();output=self.work/'result.dtb';before=cleanup.tree(source)
        self.assertEqual(cleanup.apply(source,output),512)
        after=cleanup.tree(output)
        self.assertNotIn(cleanup.REGION,after)
        self.assertEqual(before['/memory@80000000'],after['/memory@80000000'])
        for path in ('/chosen','/reserved-memory/memory@ea000000','/reserved-memory/memory@ed900000'):
            self.assertEqual(before[path],after[path])
        self.assertIn(cleanup.REGION,cleanup.tree(source))
    def test_changed_size_is_rejected_before_output_exists(self):
        source=self.dtb(size='0x10000000');out=self.work/'result.dtb'
        with self.assertRaisesRegex(ValueError,'地址、大小'):cleanup.apply(source,out)
        self.assertFalse(out.exists())
    def test_real_phandle_consumer_prevents_removal(self):
        source=self.dtb(consumer=True);out=self.work/'result.dtb'
        with self.assertRaisesRegex(ValueError,'消费者'):cleanup.apply(source,out)
        self.assertFalse(out.exists())
    def test_another_board_is_rejected(self):
        source=self.dtb(compatible='qcom,another-tablet');out=self.work/'result.dtb'
        with self.assertRaisesRegex(ValueError,'liuqin'):cleanup.apply(source,out)
        self.assertFalse(out.exists())
    def test_upstream_already_removed_mte_is_an_exact_noop(self):
        source=self.dtb(mte=False);out=self.work/'result.dtb'
        self.assertEqual(cleanup.apply(source,out),0)
        self.assertEqual(source.read_bytes(),out.read_bytes())
    def test_existing_result_is_never_overwritten(self):
        source=self.dtb();out=self.work/'result.dtb';out.write_bytes(b'retain')
        with self.assertRaisesRegex(ValueError,'覆盖'):cleanup.apply(source,out)
        self.assertEqual(out.read_bytes(),b'retain')

if __name__=='__main__':unittest.main(verbosity=2)
