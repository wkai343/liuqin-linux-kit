#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline geometry and unsupported-layout regressions for single-root plans."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
import liuqin_layout as layout


class LayoutTests(unittest.TestCase):
    def test_capacities_and_filesystems_keep_the_entire_original_table(self):
        for capacity in (128, 256, 512):
            for filesystem in ('ext4', 'btrfs'):
                with self.subTest(capacity=capacity, filesystem=filesystem):
                    disk = layout.reference_stock_disk()
                    disk.total_sectors = disk.total_sectors * capacity // 256
                    disk.last_usable = disk.total_sectors - 6
                    disk.by_name('userdata').last_sector = disk.last_usable
                    before = copy.deepcopy(vars(disk))
                    plan = layout.plan_layout(disk, 'linux-single', filesystem)
                    self.assertEqual(len(plan.entries), 1)
                    entry = plan.entries[0]
                    self.assertEqual((entry.name, entry.action), ('userdata', 'format'))
                    self.assertEqual((entry.start, entry.end),
                                     (disk.by_name('userdata').start, disk.usable_end))
                    self.assertIn(filesystem, entry.detail)
                    self.assertEqual([(p.number, p.name, p.start, p.end) for p in disk.partitions],
                                     [(p.number, p.name, p.start, p.end) for p in before['partitions']])

    def test_unsupported_layouts_and_formats(self):
        for mode in ('dual', 'linux-only', 'unknown'):
            with self.assertRaises(layout.LayoutError):
                layout.plan_layout(layout.reference_stock_disk(), mode)
        with self.assertRaises(layout.LayoutError):
            layout.plan_layout(layout.reference_stock_disk(), 'linux-single', 'xfs')

    def test_bad_tables_never_produce_a_format_plan(self):
        for defect in ('duplicate-number', 'duplicate-name', 'overlap', 'missing-userdata',
                       'missing-partition', 'unknown-prefix', 'short-userdata', 'small-root', 'wrong-sector'):
            with self.subTest(defect=defect):
                disk = layout.reference_stock_disk()
                if defect == 'duplicate-number': disk.partitions[0].number = disk.partitions[1].number
                elif defect == 'duplicate-name': disk.partitions[0].name = 'userdata'
                elif defect == 'overlap': disk.partitions[-1].first_sector = disk.partitions[-2].last_sector
                elif defect == 'missing-userdata': disk.partitions[-1].name = 'unknown-root'
                elif defect == 'missing-partition': disk.partitions.pop(0)
                elif defect == 'unknown-prefix': disk.partitions[0].name = 'unknown-prefix'
                elif defect == 'short-userdata': disk.partitions[-1].last_sector -= 1
                elif defect == 'small-root': disk.partitions[-1].first_sector = disk.last_usable - 1024
                elif defect == 'wrong-sector': disk.sector_size = 512
                with self.assertRaises(layout.LayoutError):
                    layout.plan_layout(disk, 'linux-single')

    def test_report_parser_and_unknown_layout(self):
        disk = layout.reference_stock_disk()
        report = [f'Disk /dev/sda: {disk.total_sectors} sectors',
                  'Sector size (logical/physical): 4096/4096 bytes',
                  f'First usable sector is {disk.first_usable}, last usable sector is {disk.last_usable}',
                  f'Partition table holds up to {disk.entry_count} entries',
                  'Number  Start (sector) End (sector) Size Code Name']
        report += [f'{p.number} {p.first_sector} {p.last_sector} 1.0 GiB 8300 {p.name}' for p in disk.partitions]
        parsed = layout.parse_print('\n'.join(report))
        self.assertEqual(layout.classify(parsed), 'stock')
        self.assertEqual(parsed.by_name('userdata').size, disk.by_name('userdata').size)
        parsed.partitions[-1].name = 'unknown-root'
        self.assertEqual(layout.classify(parsed), 'unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
