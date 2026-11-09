#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'installer'))
from liuqin_gui import workflow_capacity as capacity
from liuqin_gui.engine import InstallerError
from liuqin_partitions import snapshot_id


MIB = capacity.MIB
GIB = 1024 * MIB


class WorkflowCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        state_dir = directory/'state'
        backup_dir = directory/'backups'/'prepared'
        state_dir.mkdir()
        backup_dir.mkdir(parents=True)
        runtime = directory/'recovery.img'
        runtime.write_bytes(b'fixture')
        self.engine = SimpleNamespace(state_dir=state_dir, device={'boot_bytes': 192 * MIB})
        self.record = {'sizes': {'boot_a.img': 192 * MIB, 'boot_b.img': 192 * MIB, 'persist.img': 32 * MIB}}
        store = {'bytes': 2 * GIB}
        source = dict(inventory={'linux_boot': store}, free_bytes=GIB, files={})
        edit = dict(source=source, projected=copy.deepcopy(source), create_store=False, formats=[], changes=False)
        job = dict(kind='linux', set_default=False, bound_boot_bytes=80 * MIB)
        self.plan = dict(installations=[job, dict(job, set_default=True)], direct_android=False, edit=edit,
                         connection={'installer_path': str(runtime)}, backup_dir=str(backup_dir),
                         request={'backup_parent': str(backup_dir.parent)})
        disk_usage = patch.object(capacity.shutil, 'disk_usage', return_value=SimpleNamespace(free=64 * GIB))
        disk_usage.start()
        self.addCleanup(disk_usage.stop)

    def prepare(self):
        self.plan['capacity'] = capacity.plan_capacity(self.engine, self.plan, self.record)
        return self.plan['capacity']

    def test_joint_store_budget_checks_current_free_bytes_again(self):
        boot = self.engine.state_dir/'android-boot.img'
        boot.write_bytes(b'fixture')
        self.plan['installations'][1] = dict(kind='android', set_default=True, source={'boot.img': str(boot)})
        required = self.prepare()['store']['after']
        original = self.plan['edit']['source']
        live = dict(original, free_bytes=required - 1)
        self.assertEqual(snapshot_id(live), snapshot_id(original))
        self.assertGreater(live['free_bytes'], 2 * self.engine.device['boot_bytes'] + capacity.STORE_MARGIN)
        with self.assertRaisesRegex(InstallerError, '现有 linux_boot'):
            capacity.check_capacity(self.engine, self.plan, live)
        capacity.check_capacity(self.engine, self.plan, dict(live, free_bytes=required))

    def test_fixed_recovery_snapshots_remain_in_ram_during_the_largest_rootfs(self):
        self.plan['fixed_recovery']={'plan':{'image_bytes':100*MIB}}
        self.plan['installations'][0]['source']={'archive_bytes':2*GIB}
        self.plan['installations'][1]['source']={'archive_bytes':3*GIB}
        required=200*MIB+3*GIB+512*MIB
        self.engine.device['available_memory_bytes']=required
        self.prepare()
        self.engine.device['available_memory_bytes']=required-1
        with self.assertRaisesRegex(InstallerError,'设备 RAM'):
            capacity.check_capacity(self.engine,self.plan,self.plan['edit']['source'])

    def test_shared_volume_needs_both_budgets_at_once(self):
        host = self.prepare()['host']
        available = max(host['state'], host['backup'])
        self.assertGreater(host['shared'], available)
        with patch.object(capacity.shutil, 'disk_usage', return_value=SimpleNamespace(free=available)):
            with self.assertRaisesRegex(InstallerError, '共享卷'):
                capacity.check_capacity(self.engine, self.plan, self.plan['edit']['source'])
            backup = Path(self.plan['backup_dir'])
            # The backup folder still shares its parent filesystem. The state
            # directory is on a different volume in this test.
            with patch.object(capacity, '_same_volume', side_effect=lambda a, b: a == backup):
                capacity.check_capacity(self.engine, self.plan, self.plan['edit']['source'])

    def test_android_sparse_store_budget_fits_existing_store_without_relaxing_dense_budget(self):
        boot = self.engine.state_dir/'android-boot.img'
        boot.write_bytes(b'fixture')
        android = dict(kind='android', set_default=True, source={'boot.img': str(boot)})
        self.plan['installations'] = [android]
        self.plan['edit']['source']['free_bytes'] = 307 * MIB
        with self.assertRaisesRegex(InstallerError, '400 MiB'):
            self.prepare()
        android['store_boot_bytes'] = 105 * MIB
        self.assertEqual(self.prepare()['store']['after'], 121 * MIB)

    def test_linux_sparse_budget_keeps_a_complete_previous_boot(self):
        self.plan['installations']=self.plan['installations'][:1]
        self.plan['edit']['source']['free_bytes']=205*MIB
        with self.assertRaisesRegex(InstallerError,'linux_boot'):self.prepare()
        self.plan['installations'][0]['store_boot_bytes']=120*MIB
        self.assertEqual(self.prepare()['store']['after'],136*MIB)

    def test_later_child_still_needs_full_backup_threshold(self):
        host = self.prepare()['host']
        self.assertGreaterEqual(host['backup'], sum(self.record['sizes'].values()) + 768 * MIB)
        self.assertGreaterEqual(host['state'], 4 * self.engine.device['boot_bytes'] + 512 * MIB + 2 * self.engine.device['boot_bytes'])

    def test_reused_layout_keeps_only_runtime_and_child_boot_requirements(self):
        self.plan['backup_policy'] = 'reuse-layout-v1'
        host = self.prepare()['host']
        self.assertEqual(host['backup'], 0)
        self.assertEqual(host['shared'], host['state'])
        self.assertGreaterEqual(host['state'], 4 * self.engine.device['boot_bytes'] + 512 * MIB)
        # A formatting operation keeps the same GPT geometry and backup.
        self.plan['edit'].update(changes=True, formats=[{'role': 'root'}])
        self.assertEqual(self.prepare()['host']['backup'], 0)

    def test_changed_gpt_budgets_only_one_new_layout_backup(self):
        self.plan['backup_policy'] = 'reuse-layout-v1'
        self.plan['edit'].update(changes=True, partitions=[{'role': 'root'}])
        host = self.prepare()['host']
        backup_bytes = sum(self.record['sizes'].values()) + MIB
        self.assertEqual(host['backup'], max(768 * MIB, backup_bytes))
        self.plan['installations'].append(copy.deepcopy(self.plan['installations'][0]))
        later = self.prepare()['host']
        self.assertEqual(later['backup'], host['backup'])
        self.assertGreater(later['state'], host['state'])
        self.assertGreater(later['shared'], later['state'])

    def test_new_layout_backup_shared_volume_is_checked_once_with_children(self):
        self.plan['backup_policy'] = 'reuse-layout-v1'
        self.plan['edit'].update(changes=True, deletes=[{'name': 'userdata'}])
        host = self.prepare()['host']
        available = max(host['state'], host['backup'])
        self.assertGreater(host['shared'], available)
        with patch.object(capacity.shutil, 'disk_usage', return_value=SimpleNamespace(free=available)):
            with self.assertRaisesRegex(InstallerError, '共享卷'):
                capacity.check_capacity(self.engine, self.plan, self.plan['edit']['source'])
            backup = Path(self.plan['backup_dir'])
            with patch.object(capacity, '_same_volume', side_effect=lambda a, b: a == backup):
                capacity.check_capacity(self.engine, self.plan, self.plan['edit']['source'])

    def test_partition_only_edit_does_not_budget_unused_child_backup(self):
        self.plan.update(backup_policy='reuse-layout-v1', installations=[])
        self.plan['edit'].update(changes=True, partitions=[{'role': 'root'}])
        self.assertEqual(self.prepare()['host']['backup'], 0)

    def test_new_store_capacity_deducts_ext4_overhead(self):
        edit = self.plan['edit']
        edit['source'] = dict(inventory={}, files={})
        edit.update(create_store=True, changes=True)
        self.plan['installations'] = [dict(kind='linux', set_default=False, bound_boot_bytes=180 * MIB)] * 5
        with self.assertRaisesRegex(InstallerError, '新初始化的 linux_boot'):
            self.prepare()
        self.plan['installations'].pop()
        self.prepare()

    def test_format_still_needs_room_to_journal_on_old_store(self):
        edit = self.plan['edit']
        edit.update(changes=True, formats=[{'role': 'store'}])
        edit['source']['free_bytes'] = 0
        with self.assertRaisesRegex(InstallerError, '现有 linux_boot 的分区事务记录'):
            self.prepare()
        edit['source']['free_bytes'] = 32 * MIB
        self.prepare()

    def test_direct_android_requires_no_child_backup_or_store(self):
        self.plan.update(direct_android=True, installations=[dict(kind='android', set_default=True)])
        edit = self.plan['edit']
        edit['source'] = dict(inventory={}, files={})
        edit['projected'] = copy.deepcopy(edit['source'])
        budget = self.prepare()
        self.assertEqual(budget['host']['backup'], 0)
        self.assertEqual(budget['store']['after'], 0)


if __name__ == '__main__':
    unittest.main()
