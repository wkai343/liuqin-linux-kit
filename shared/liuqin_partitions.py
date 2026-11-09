# SPDX-License-Identifier: MIT
"""Read-only discovery and additive partition planning; never resize userdata."""
from __future__ import annotations
import copy
import hashlib
import uuid
from liuqin_layout import ALIGNMENT, GIB, STOCK_TABLE, check_geometry, parse_print
from liuqin_multiboot import MAX_ROOTS, identifier, normalize, minimum_store_gib
from liuqin_android import canonical, managed_layout
from liuqin_partition_names import is_root_name, root_name, validate_name, validate_names


def free_regions(disk):
    """Only genuine GPT gaps in the original userdata tail are allocatable."""
    check_geometry(disk)
    if disk.sector_size != 4096: raise ValueError('仅支持 4096 字节扇区。')
    floor = next(first for _,first,_,name in STOCK_TABLE if name=='userdata')*4096
    cursor=floor; result=[]
    for part in sorted(disk.partitions,key=lambda p:p.start):
        if part.end <= floor: continue
        if part.start > cursor:
            start=(cursor+ALIGNMENT-1)//ALIGNMENT*ALIGNMENT;end=part.start//ALIGNMENT*ALIGNMENT
            if end>start: result.append(dict(start=start,end=end,bytes=end-start))
        cursor=max(cursor,part.end)
    start=(cursor+ALIGNMENT-1)//ALIGNMENT*ALIGNMENT;end=disk.usable_end//ALIGNMENT*ALIGNMENT
    if end>start:result.append(dict(start=start,end=end,bytes=end-start))
    return result


def snapshot_id(state):
    value={key:state.get(key) for key in ('report','details','inventory','registry','files','error')}
    if value['inventory'] is not None:value['inventory']={n:stable_partition(p) for n,p in value['inventory'].items()}
    return hashlib.sha256(canonical(value)).hexdigest()


def stable_partition(part):
    # A dynamic partition minor is a handle for this kernel epoch, not a GPT
    # identity. Every block open still verifies the fresh live major/minor.
    return {k:v for k,v in part.items() if k!='dev'}


def discover(state, serial):
    disk=parse_print(state['report']);check_geometry(disk)
    gaps=free_regions(disk)
    summary=dict(serial=serial,snapshot_id=snapshot_id(state),report=state['report'],
        partitions=[dict(name=p.name,number=p.number,start=p.start,bytes=p.size) for p in disk.partitions],
        free_regions=gaps,free_bytes=sum(p['bytes'] for p in gaps),largest_free_bytes=max((p['bytes'] for p in gaps),default=0),
        has_userdata=disk.by_name('userdata') is not None,has_store=disk.by_name('linux_boot') is not None,
        can_create=False,reason='',root_count=0,has_managed_android=False)
    try:
        if state.get('error'):raise ValueError(state['error'])
        inventory=state['inventory']
        validate_names(inventory)
        if {p.name for p in disk.partitions}!={name for name,p in inventory.items() if p['parent']=='sda'}:
            raise ValueError('GPT 报告与内核分区列表不完整或不一致。')
        for p in disk.partitions:
            actual=inventory.get(p.name)
            if not actual or any(actual.get(k)!=v for k,v in dict(parent='sda',number=p.number,start=p.start,bytes=p.size).items()):
                raise ValueError('GPT 与内核分区视图不一致。')
        prefix={name:(number,first,last) for number,first,last,name in STOCK_TABLE if name!='userdata'}
        for name,(number,first,last) in prefix.items():
            p=disk.by_name(name)
            if not p or (p.number,p.first_sector,p.last_sector)!=(number,first,last):
                raise ValueError('原厂前缀分区已修改，只读展示，不自动接管。')
        if summary['has_store']:
            layout=managed_layout(disk,inventory,state,serial,require_android=False)
            summary['root_count']=len(layout['slots'])
            summary['has_managed_android']=bool(layout['android'])
        else:
            if {p.name for p in disk.partitions}-set(prefix)-{'userdata'}:
                raise ValueError('存在未登记的分区，只读展示，不自动接管。')
            userdata=disk.by_name('userdata')
            if userdata and (userdata.number!=35 or userdata.size<16*GIB):
                raise ValueError('userdata 的位置或容量不受支持。')
        summary['can_create']=True
        summary['reason']='已有分区保持不变；只能使用下面的未分配空间。' if gaps else '没有未分配空间，不能新增分区；不会缩小或重建 userdata。'
    except (ValueError,KeyError,TypeError) as error:
        summary['reason']=str(error)
    return summary


def _userdata_bytes(config):
    return config.get('userdata_bytes', config['userdata_gib'] * GIB)


def _root_capacity(gaps, pending_userdata_bytes):
    sizes = [gap['end'] - gap['start'] for gap in gaps]
    if not pending_userdata_bytes:
        return max(sizes, default=0)
    suitable = [index for index, size in enumerate(sizes) if size >= pending_userdata_bytes]
    if len(suitable) != 1:
        return max(sizes, default=0) if suitable else 0
    # Roots use first-fit before byte-sized userdata. If only one gap can hold
    # userdata, a new root must fit an earlier gap or leave room in that gap.
    index = suitable[0]
    return max(max(sizes[:index], default=0), sizes[index] - pending_userdata_bytes)


def _append_geometry(disk, config, extra_kind=None):
    """Allocate aligned ranges in the same order for plans and draft previews."""
    userdata = disk.by_name('userdata')
    store = disk.by_name('linux_boot')
    userdata_size = _userdata_bytes(config)
    if userdata and userdata_size:
        raise ValueError('已存在 userdata，只允许保留，不能再创建或调整容量。')
    root_count = sum(is_root_name(p.name) for p in disk.partitions) + len(config['roots'])
    if root_count > MAX_ROOTS:
        raise ValueError('最多保留 16 个 rootfs。')
    system_count = root_count + bool(userdata or userdata_size)
    occupied={p.number for p in disk.partitions};names={p.name for p in disk.partitions}
    gaps=free_regions(disk);added=[]

    def next_root_number():
        return next((n for n in range(1, disk.entry_count+1) if n not in occupied and n!=35), None)

    def allocate(role,name,size,number=None):
        validate_name(name)
        if name in names:raise ValueError('分区名称已存在：'+name)
        names.add(name)
        if number is None:number=next_root_number()
        if number is None or number in occupied or number>disk.entry_count:raise ValueError('GPT 没有可用表项。')
        occupied.add(number)
        gap=next((g for g in gaps if g['end']-g['start']>=size),None)
        if gap is None:raise ValueError('未分配的连续空间不足；不会缩小 userdata 或其他分区。')
        start=gap['start'];gap['start']+=size
        added.append(dict(role=role,name=name,number=number,start=start,end=start+size,bytes=size,
            first_sector=start//4096,last_sector=(start+size)//4096-1,action='create'))

    if config['userdata_gib']:
        allocate('android','userdata',userdata_size,35)
    store_bytes = store.size if store else config['boot_gib']*GIB
    if store_bytes < minimum_store_gib(system_count)*GIB:
        if store:
            raise ValueError('已有引导存储不足以预留新增系统的镜像；不会自动扩大或格式化它。')
        raise ValueError('引导存储须同时预留现有 Android 和新增 Linux 的镜像空间。')
    if not store:
        allocate('store','linux_boot',store_bytes)
    for row in config['roots']:
        allocate('root',root_name(row['id']),row.get('size_bytes',row['size_gib']*GIB))
    max_root_bytes = _root_capacity(gaps, config.get('userdata_bytes',0)) if extra_kind == 'linux' else 0
    if config.get('userdata_bytes'):
        allocate('android','userdata',userdata_size,35)

    maximum = 0
    if store_bytes >= minimum_store_gib(system_count+1)*GIB:
        if extra_kind == 'linux' and root_count<MAX_ROOTS and next_root_number() is not None:
            maximum = max_root_bytes
        if extra_kind == 'android' and not (userdata or userdata_size) and 35 not in occupied and disk.entry_count>=35:
            maximum = max((g['end']-g['start'] for g in gaps),default=0)
    maximum = min(maximum, 4096*GIB)
    return added, sum(g['end']-g['start'] for g in gaps), maximum if maximum>=16*GIB else 0


def addition_capacity(summary, rows, actions, configuration, kind):
    """Find the largest next allocation after the proposed edits and drafts."""
    if kind not in (None, 'linux', 'android'):
        raise ValueError('新建分区类型无效。')
    if not summary.get('can_create'):
        if kind is None:raise ValueError(summary.get('reason') or '请先读取可编辑的分区表。')
        return 0
    disk = parse_print(summary['report'])
    existing = {row['name']: row for row in rows}
    edits = {}
    for action in actions:
        name, operation = action.get('name'), action.get('action')
        row = existing.get(name)
        if (name in edits or operation not in ('delete','format') or not row or
            not row.get('can_'+operation) or row['partuuid'] != action.get('partuuid')):
            raise ValueError('分区操作已过期或不可用。')
        edits[name] = operation
    disk.partitions = [p for p in disk.partitions if edits.get(p.name) != 'delete']
    if 'linux_boot' in edits and (any(is_root_name(p.name) for p in disk.partitions)
                                  or (summary.get('has_managed_android') and 'userdata' not in edits)):
        raise ValueError('引导存储仍被系统使用；先移除所有相关系统和 rootfs。')
    config = normalize(dict(configuration, schema_version=1, partition_only=True),
                       allow_empty=True, allow_exact_sizes=True, reuse_store=disk.by_name('linux_boot') is not None)
    has_drafts = bool(config['roots'] or _userdata_bytes(config) or configuration.get('ensure_store'))
    if kind is None and not has_drafts:
        return 0
    try:
        maximum = _append_geometry(disk,config,kind)[2]
    except ValueError:
        if kind is not None and not has_drafts:
            return 0
        raise
    return maximum


def plan_append(state, configuration, serial, transaction, generation=None):
    summary=discover(state,serial)
    if not summary['can_create']:raise ValueError(summary['reason'])
    identifier(transaction)
    config=normalize(dict(configuration,partition_only=True),allow_empty=True,allow_exact_sizes=True,
                     reuse_store=summary['has_store'])
    userdata_size=_userdata_bytes(config)
    if not config['roots'] and not userdata_size:
        if summary['has_store']:raise ValueError('没有需要新增的分区。')
        if not summary['has_userdata']:raise ValueError('至少需要 userdata 或一个 rootfs。')
    old=state.get('registry')
    generation=old['layout_generation'] if old else identifier(generation or transaction)
    disk=parse_print(state['report']);inventory=state['inventory']
    old_slots=(managed_layout(disk,inventory,state,serial,False)['slots'] if old else [])
    ids={s['id'] for s in old_slots} | ({s['id'] for s in old['systems']} if old else set())
    if any(r['id'] in ids for r in config['roots']):raise ValueError('新 rootfs 身份与已有系统重复。')
    ranges,remaining,_ = _append_geometry(disk,config)
    root_rows = {root_name(r['id']):r for r in config['roots']}
    added=[];slots=[]
    store = None
    if summary['has_store']:
        store = dict(inventory['linux_boot'],role='store',fsuuid=state['store_fsuuid'],action='preserve')
    for part in ranges:
        role,name = part['role'],part['name']
        if role=='android':
            os_id = str(uuid.uuid5(uuid.UUID(generation),'userdata'))
            filesystem = '待安装 Android 时初始化'
        elif role=='store':
            os_id = str(uuid.uuid5(uuid.UUID(generation),'boot-store'))
            filesystem = 'ext4'
            store = part
        else:
            row = root_rows[name]
            os_id,filesystem = row['id'],row['filesystem']
        part.update(filesystem=filesystem,
            partuuid=str(uuid.uuid5(uuid.UUID(os_id),'partition')),
            fsuuid=str(uuid.uuid5(uuid.UUID(os_id),generation+'/filesystem')))
        if part['partuuid'] in {p['partuuid'] for p in inventory.values()} | {p['partuuid'] for p in added}:
            raise ValueError('新分区 GUID 与已有分区冲突。')
        added.append(part)
        if role=='root':
            target=dict(schema_version=1,os_id=os_id,root_partuuid=part['partuuid'],root_fsuuid=part['fsuuid'],
                root_fstype=filesystem,root_path='native-root',boot_generation=generation)
            slots.append(dict(id=os_id,name=row['name'],partition=part,target=target,status='reserved'))
    if old:
        registry=copy.deepcopy(old)
        saved=registry.get('partition_layout')
        if saved is None:
            saved=[dict(role='store',**{k:inventory['linux_boot'][k] for k in ('name','number','start','bytes')})]
            if summary['has_userdata']:saved.append(dict(role='android',**{k:inventory['userdata'][k] for k in ('name','number','start','bytes')}))
            saved += [copy.deepcopy(s['partition']) for s in old_slots]
        registry['partition_layout']=copy.deepcopy(saved)+added
        registry['rootfs_slots']=copy.deepcopy(old_slots)+slots
        if userdata_size:registry['userdata_state']='reserved'
    else:
        preserved=[]
        if summary['has_userdata']:
            p=inventory['userdata'];preserved=[dict(role='android',action='preserve',**{k:p[k] for k in ('name','number','start','bytes','partuuid')})]
        registry=dict(schema_version=1,serial=serial,layout_generation=generation,default_id=None,default_generation=None,
            systems=[],rootfs_slots=slots,partition_layout=preserved+added,
            userdata_state='existing-unverified' if summary['has_userdata'] else 'reserved' if userdata_size else 'absent')
    registry['partition_transaction']=transaction
    return dict(schema_version=1,transaction=transaction,generation=generation,configuration=config,
        partitions=added,store=store,create_store=not summary['has_store'],candidate_registry=registry,
        free_bytes=remaining,source=copy.deepcopy(state))


def validate_append(plan,serial):
    expected=plan_append(plan['source'],plan['configuration'],serial,plan['transaction'],plan['generation'])
    if canonical(expected)!=canonical(plan):raise ValueError('追加分区方案发生变化或试图修改已有分区。')


def partition_arguments(plan):
    validate_names(p['name'] for p in plan['partitions'])
    validate_names(plan['source']['inventory'])
    args=['--set-alignment=1']
    for p in plan['partitions']:
        args += [f"--new={p['number']}:{p['first_sector']}:{p['last_sector']}",
            f"--typecode={p['number']}:8300",f"--partition-guid={p['number']}:{p['partuuid']}",
            f"--attributes={p['number']}:=:0000000000000000",f"--change-name={p['number']}:{p['name']}"]
    return args
