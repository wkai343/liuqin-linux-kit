# SPDX-License-Identifier: MIT
"""Partition editor plans: explicit data-only delete/format/create actions."""
import copy
import hashlib
import uuid
from liuqin_android import canonical,managed_layout
from liuqin_layout import parse_print
from liuqin_multiboot import identifier
from liuqin_partitions import discover,plan_append
from liuqin_partition_names import is_root_name


def report_with(state,inventory):
    header=state['report'].split('Number  Start (sector)',1)[0]
    rows=['Number  Start (sector) End (sector) Size Code Name']
    for p in sorted((p for p in inventory.values() if p['parent']=='sda'),key=lambda p:p['start']):
        rows.append(f"{p['number']} {p['start']//4096} {(p['start']+p['bytes'])//4096-1} 1.0 GiB 8300 {p['name']}")
    return header+'\n'.join(rows)+'\n'


def capabilities(state,serial):
    summary=discover(state,serial);slots={}
    if summary['can_create'] and state.get('registry'):
        slots={s['partition']['name']:s for s in managed_layout(parse_print(state['report']),state['inventory'],state,serial,False)['slots']}
    rows=[]
    for part in state['inventory'].values():
        name=part['name'];role='android' if name=='userdata' else 'linux' if name in slots or is_root_name(name) else 'store' if name=='linux_boot' else 'recovery' if name in ('recovery_a','recovery_b') else 'protected'
        editable=summary['can_create'] and role in ('android','linux','store')
        rows.append(dict(part,role=role,display_name=slots[name]['name'] if name in slots else name,
            system_id=slots[name]['id'] if name in slots else '',status=slots[name]['status'] if name in slots else '',
            can_delete=editable,can_format=editable,can_install=summary['can_create'] and role in ('android','linux'),
            reason='引导存储只能在移除其管理的全部系统/rootfs 后删除或格式化。' if role=='store' else '' if editable else '固件、校准和启动分区通过专用操作管理。'))
    order={'android':0,'linux':1,'store':2,'recovery':3,'protected':4}
    return sorted(rows,key=lambda p:(order[p['role']],p['parent'],p['start']))


def plan_edit(state,actions,configuration,serial,transaction):
    identifier(transaction)
    summary=discover(state,serial)
    if not summary['can_create']:raise ValueError(summary['reason'])
    if not isinstance(actions,list):raise ValueError('分区操作列表无效。')
    rows={p['name']:p for p in capabilities(state,serial)}
    projected=copy.deepcopy(state);inventory=projected['inventory'];registry=projected.get('registry')
    if registry and 'rootfs_slots' not in registry:
        registry['rootfs_slots']=managed_layout(parse_print(state['report']),state['inventory'],state,serial,False)['slots']
    store_action=next((a for a in actions if a.get('name')=='linux_boot'),None)
    if store_action:
        deleting={a.get('name') for a in actions if a.get('action')=='delete'}
        android_reset=any(a.get('name')=='userdata' for a in actions)
        if any(s['partition']['name'] not in deleting for s in (registry or {}).get('rootfs_slots',[])) or any(s['type']=='android' and not android_reset for s in (registry or {}).get('systems',[])):
            raise ValueError('引导存储仍被系统使用；先删除所有 rootfs，并清除或删除 Android 数据，才能操作引导存储。')
        # Data roots and Android registration must be removed before the store.
        actions=sorted(actions,key=lambda a:a.get('name')=='linux_boot')
    seen=set();deletes=[];formats=[]
    for action in actions:
        name=action.get('name');kind=action.get('action');part=rows.get(name)
        if name in seen or kind not in ('delete','format') or not part or not part['can_'+kind]:
            raise ValueError('该分区不能删除/格式化，或操作重复：'+str(name))
        seen.add(name);original=state['inventory'][name]
        if action.get('partuuid')!=original['partuuid']:raise ValueError('分区选择已过期。')
        os_id=part['system_id']
        if registry:
            removed={s['id'] for s in registry['systems'] if s['id']==os_id or (name=='userdata' and s['type']=='android')}
            registry['systems']=[s for s in registry['systems'] if s['id'] not in removed]
            if registry.get('default_id') in removed:registry.update(default_id=None,default_generation=None)
        if kind=='delete':
            deletes.append(copy.deepcopy(original));inventory.pop(name)
            projected['details'].pop(str(original['number']),None)
            if registry:
                registry['rootfs_slots']=[s for s in registry['rootfs_slots'] if s['id']!=os_id]
                if name=='userdata':registry['userdata_state']='absent'
                if 'partition_layout' in registry:registry['partition_layout']=[p for p in registry['partition_layout'] if p['name']!=name]
            if name=='linux_boot':registry=None;projected.update(registry=None,files={});projected.pop('store_fsuuid',None)
        else:
            if name=='userdata':
                formats.append(dict(partition=copy.deepcopy(original),filesystem='f2fs',role='android'))
                if registry:registry['userdata_state']='reserved'
                projected.update(userdata_blank=True,userdata_linux_signature=False)
            elif name=='linux_boot':
                formats.append(dict(partition=copy.deepcopy(original),filesystem='ext4',fsuuid=original['fsuuid'],role='store'))
                projected['files']={}
            else:
                filesystem=action.get('filesystem',part['filesystem'])
                if filesystem not in ('ext4','btrfs'):raise ValueError('rootfs 仅支持 ext4 或 Btrfs。')
                fsuuid=str(uuid.uuid5(uuid.UUID(os_id),transaction+'/filesystem'))
                slot=next(s for s in registry['rootfs_slots'] if s['id']==os_id)
                slot.update(status='reserved',format_generation=transaction)
                slot['partition'].update(filesystem=filesystem,fsuuid=fsuuid)
                slot['target'].update(root_fstype=filesystem,root_fsuuid=fsuuid,boot_generation=transaction)
                inventory[name].update(filesystem=filesystem,fsuuid=fsuuid)
                formats.append(dict(partition=copy.deepcopy(original),filesystem=filesystem,fsuuid=fsuuid,role='root'))
                for p in registry.get('partition_layout',[]):
                    if p['name']==name:p.update(filesystem=filesystem,fsuuid=fsuuid)
    projected['report']=report_with(state,inventory)
    new_roots=configuration.get('roots',[]);new_userdata=configuration.get('userdata_gib',0)
    userdata_bytes=configuration.get('userdata_bytes',0)
    append=None
    # A managed boot store is needed for installing systems; only add it if
    # the operation creates a target or the caller explicitly requests it.
    need_store=bool(configuration.get('ensure_store')) and 'linux_boot' not in inventory
    if new_roots or new_userdata or userdata_bytes or need_store:
        if registry:
            raw=canonical(registry);projected['registry_sha256']=hashlib.sha256(raw).hexdigest()
            projected['files']['registry.json']=dict(bytes=len(raw),sha256=projected['registry_sha256'])
        additions=dict(schema_version=1,partition_only=True,userdata_gib=new_userdata,
            boot_gib=configuration.get('boot_gib',2),roots=new_roots,boot_action='install-only')
        if 'userdata_bytes' in configuration:additions['userdata_bytes']=userdata_bytes
        append=plan_append(projected,additions,serial,transaction)
        registry=copy.deepcopy(append['candidate_registry']);projected['registry']=registry
        for p in append['partitions']:
            inventory[p['name']]=dict(name=p['name'],parent='sda',number=p['number'],start=p['start'],bytes=p['bytes'],
                partuuid=p['partuuid'],node='pending',dev='pending',parent_dev=next(i['parent_dev'] for i in inventory.values() if i['parent']=='sda'))
            if p['role']=='store':inventory[p['name']].update(filesystem='ext4',fsuuid=p['fsuuid']);projected['store_fsuuid']=p['fsuuid']
            elif p['role']=='root':inventory[p['name']].update(filesystem='unformatted',fsuuid=None)
            if p['role']=='android':projected.update(userdata_blank=True,userdata_linux_signature=False)
        projected['report']=report_with(state,inventory)
    if registry:
        registry['partition_transaction']=transaction
        raw=canonical(registry);projected['registry_sha256']=hashlib.sha256(raw).hexdigest()
        projected['files']['registry.json']=dict(bytes=len(raw),sha256=projected['registry_sha256'])
    added=append['partitions'] if append else []
    return dict(schema_version=1,transaction=transaction,source=copy.deepcopy(state),actions=copy.deepcopy(actions),
        configuration=copy.deepcopy(configuration),deletes=deletes,formats=formats,partitions=added,
        create_store=bool(append and append['create_store']),store=append['store'] if append else inventory.get('linux_boot'),
        candidate_registry=registry,projected=projected,changes=bool(deletes or formats or added))


def validate_edit(plan,serial):
    expected=plan_edit(plan['source'],plan['actions'],plan['configuration'],serial,plan['transaction'])
    if canonical(plan)!=canonical(expected):raise ValueError('分区编辑方案改变。')
