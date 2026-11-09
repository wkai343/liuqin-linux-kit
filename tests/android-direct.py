#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Direct Android loop on small regular-file partitions, with real mkfs."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('android_tests',PROJECT/'tests/android-device.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
from liuqin_android import image_plan,ANDROID_PARTITIONS,FORMAT_PARTITIONS


class DirectTests(f.DeviceTests):
    # Keep setup/fetch assertions, but do not inherit the unrelated test cases.
    for _name in list(vars(f.DeviceTests)):
        if _name.startswith('test_'):locals()[_name]=None
    def setUp(self):
        super().setUp()
        self.removed={n:p for n,p in self.device.parts.items() if n=='linux_boot' or n.startswith('lqr_')}
        self.device.parts={n:p for n,p in self.device.parts.items() if n not in self.removed}
        original_inventory=self.device.inventory
        self.device.inventory=lambda allow_unknown_store=False:original_inventory()
        def gpt():
            report=f.cli.layout.reference_stock_disk()
            header=[f'Disk /dev/sda: {report.total_sectors} sectors','Sector size (logical/physical): 4096/4096 bytes',
                f'First usable sector is {report.first_usable}, last usable sector is {report.last_usable}',
                'Partition table holds up to 64 entries','Number  Start (sector) End (sector) Size Code Name']
            for p in sorted((p for p in self.device.parts.values() if p['parent']=='sda'),key=lambda p:p['start']):
                header.append(f"{p['number']} {p['start']//4096} {(p['start']+p['bytes'])//4096-1} 1.0 GiB 8300 {p['name']}")
            return '\n'.join(header)+'\n',{}
        self.device.gpt=gpt
        plans=[]
        for name in self.profile['install_images']:
            entry=self.profile['images'][name];plans.append(image_plan(self.engine.demo_android_rom/name,self.device.parts[entry['partition']],entry))
        self.current=dict(inventory=self.device.inventory(),images=plans,previous_boot_sha256=self.boot_before)
    def test_direct_reinstall_only_formats_android_and_never_mounts_store(self):
        before={n:self.device.digest(p) for n,p in self.device.parts.items() if n not in ANDROID_PARTITIONS|FORMAT_PARTITIONS}
        # This fixture deliberately makes super 64 KiB. Full stock topology
        # admission is tested by partition-discovery.py, independently of IO.
        with patch('liuqin_partitions.discover',return_value={'can_create':True}),patch.object(f.module,'fetch',side_effect=self.fetch):
            result=self.ops.direct_install(self.current,'http://fixture')
        self.assertTrue(result['rom_written']);self.assertNotIn('linux_boot:append',self.device.writes)
        self.assertEqual(set(self.device.writes),ANDROID_PARTITIONS|FORMAT_PARTITIONS)
        for n,digest in before.items():self.assertEqual(self.device.digest(self.device.parts[n]),digest)
        self.assertTrue(all(v==1 for v in self.device.flags.values()))
    def test_managed_roots_missing_userdata_or_forged_targets_stop_before_write(self):
        root=next(p for n,p in self.removed.items() if n.startswith('lqr_'))
        self.device.parts[root['name']]=root
        plan=dict(self.current,inventory=self.device.inventory())
        with self.assertRaisesRegex(RuntimeError,'多系统'):self.ops.direct_install(plan,'http://fixture')
        self.device.parts.pop(root['name']);userdata=self.device.parts.pop('userdata')
        with self.assertRaisesRegex(RuntimeError,'数据分区'):self.ops.direct_install(dict(self.current,inventory=self.device.inventory()),'http://fixture')
        self.device.parts['userdata']=userdata
        plan=copy.deepcopy(self.current);plan['images'][0]['partition']['name']='persist'
        with patch('liuqin_partitions.discover',return_value={'can_create':True}),self.assertRaises(ValueError):self.ops.direct_install(plan,'http://fixture')
        self.assertEqual(self.device.writes,[])

if __name__=='__main__':unittest.main(verbosity=2)
