# SPDX-License-Identifier: MIT
"""Registration rules for installing one Linux into a reserved rootfs."""
from __future__ import annotations
import copy
from liuqin_multiboot import identifier
from liuqin_android import canonical
from liuqin_partition_names import root_name, validate_names


def next_linux_registry(old, system, transaction, set_default):
    os_id = identifier(system['id']); identifier(transaction)
    slots = old.get('rootfs_slots', [])
    slot = next((s for s in slots if s['id'] == os_id), None)
    if not slot or slot['status'] != 'reserved' or os_id in {s['id'] for s in old['systems']}:
        raise ValueError('请选择尚未安装系统的预留 rootfs；不会覆盖已有系统。')
    target = dict(slot['target'], boot_generation=transaction)
    if (system.get('type') != 'linux' or system.get('partition') != slot['partition'] or
        system.get('target') != target or system.get('name') != slot['name'] or
        system.get('image') != 'systems/'+os_id+'/images/'+transaction+'.img'):
        raise ValueError('Linux 登记与目标 rootfs 不一致。')
    result = copy.deepcopy(old)
    result['systems'].append(copy.deepcopy(system))
    for row in result['rootfs_slots']:
        if row['id'] == os_id: row.update(status='installed', target=target)
    result['linux_transaction'] = transaction
    if set_default: result.update(default_id=os_id, default_generation=transaction)
    return result


def validate_linux_plan(plan, serial):
    validate_names(plan['inventory'])
    if plan.get('schema_version') != 1 or type(plan.get('set_default')) is not bool:
        raise ValueError('Linux 安装计划版本无效。')
    identifier(plan['transaction'])
    old = plan['registry']; system = plan['system']; target = system['target']
    if old.get('serial') != serial: raise ValueError('Linux 安装计划不属于当前设备。')
    candidate = next_linux_registry(old, system, plan['transaction'], plan['set_default'])
    if canonical(candidate) != canonical(plan['candidate_registry']):
        raise ValueError('Linux 安装改变了其他系统登记。')
    actual = plan['inventory'].get(system['partition']['name'])
    if (not actual or actual['name'] != root_name(system['id']) or
        actual['parent'] != 'sda' or actual['partuuid'] != target['root_partuuid'] or
        actual['start'] != system['partition']['start'] or actual['bytes'] != system['partition']['bytes'] or
        target['root_fstype'] not in ('ext4', 'btrfs') or target['root_path'] != 'native-root'):
        raise ValueError('Linux 写入目标不属于所选预留 rootfs。')
    return actual
