# SPDX-License-Identifier: MIT
"""Pure first-install multi-root planning. No device or filesystem writes."""
from __future__ import annotations

import copy
import math
import re
import uuid
from liuqin_layout import GIB, MIB, ALIGNMENT, LayoutError, check_stock_device
from liuqin_partition_names import root_name, validate_names

SCHEMA = 1
MAX_ROOTS = 16


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('系统身份必须是规范的小写 UUID。')
    return value


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label}必须为 {minimum}–{maximum} 的整数。')
    return value


def minimum_store_gib(count, boot_bytes=192 * MIB):
    # Current + previous per system, a whole-partition backup and staging,
    # plus 25% spare capacity. This remains conservative for small boot images.
    return max(2, math.ceil((2 * count + 2) * boot_bytes * 1.25 / GIB))


def exact_size(value, label):
    size = integer(value, 16 * GIB, 4096 * GIB, label)
    if size % ALIGNMENT:
        raise ValueError(f'{label}必须按 {ALIGNMENT // MIB} MiB 对齐。')
    return size


def normalize(value, *, allow_empty=False, allow_exact_sizes=False, reuse_store=False):
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA:
        raise ValueError('不支持的多系统分区配置版本。')
    partition_only = value.get('partition_only', False)
    if type(partition_only) is not bool:
        raise ValueError('分区操作类型无效。')
    roots = value.get('roots')
    if not isinstance(roots, list) or not (0 if partition_only else 1) <= len(roots) <= MAX_ROOTS:
        raise ValueError(f'请配置 {0 if partition_only else 1}–{MAX_ROOTS} 个 Linux rootfs。')
    userdata_gib = integer(value.get('userdata_gib'), 0, 4096, 'Android 数据空间')
    userdata_bytes = value.get('userdata_bytes', 0)
    if 'userdata_bytes' in value and (type(userdata_bytes) is not int or not allow_exact_sizes):
        raise ValueError('此分区模式不支持按字节设置 Android 数据空间。')
    if userdata_bytes:
        if userdata_gib:
            raise ValueError('Android 数据空间只能使用一种容量单位。')
        userdata_bytes = exact_size(userdata_bytes, 'Android 数据空间')
    has_userdata = bool(userdata_gib or userdata_bytes)
    result = dict(schema_version=SCHEMA,
                  userdata_gib=userdata_gib,
                  boot_gib=integer(value.get('boot_gib'), minimum_store_gib(0 if reuse_store else len(roots) + has_userdata), 128, '引导存储空间'),
                  roots=[], boot_action=value.get('boot_action'), default_id=value.get('default_id', ''))
    if userdata_bytes:
        result['userdata_bytes'] = userdata_bytes
    if partition_only:
        result.update(partition_only=True, boot_action='install-only', default_id='')
    if not roots and not has_userdata and not allow_empty:
        raise ValueError('至少配置 Android userdata 或一个 Linux rootfs。')
    if 0 < result['userdata_gib'] < 16:
        raise ValueError('Android userdata 至少需要 16 GiB；0 表示不保留。')
    ids = set()
    for root in roots:
        if not isinstance(root, dict):
            raise ValueError('rootfs 条目必须是对象。')
        os_id = identifier(root.get('id'))
        if os_id in ids:
            raise ValueError('系统身份重复。')
        ids.add(os_id)
        name = root.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or any(ord(c) < 32 for c in name):
            raise ValueError('系统名称须为 1–64 个可显示字符。')
        filesystem = root.get('filesystem')
        if filesystem not in ('ext4', 'btrfs'):
            raise ValueError('rootfs 只支持 ext4 或 Btrfs。')
        for key in ('bundle', 'boot_image'):
            if not isinstance(root.get(key, ''), str) or '\0' in root.get(key, ''):
                raise ValueError('安装材料路径无效。')
        normalized_root = dict(id=os_id, name=name.strip(),
            size_gib=integer(root.get('size_gib'), 16, 4096, 'rootfs 容量'), filesystem=filesystem,
            bundle=root.get('bundle', ''), boot_image=root.get('boot_image', ''))
        if 'size_bytes' in root:
            if not allow_exact_sizes:
                raise ValueError('此分区模式不支持按字节设置 rootfs 容量。')
            size = exact_size(root['size_bytes'], 'rootfs 容量')
            if normalized_root['size_gib'] != size // GIB:
                raise ValueError('rootfs 的容量描述不一致。')
            normalized_root['size_bytes'] = size
        result['roots'].append(normalized_root)
    if result['boot_action'] not in ('set-default', 'install-only'):
        raise ValueError('请选择设置默认引导，或仅安装并停在 Fastboot。')
    if result['boot_action'] == 'set-default' and result['default_id'] not in ids:
        raise ValueError('默认启动项必须是本次安装的一个 Linux 系统。')
    if result['boot_action'] == 'install-only':
        result['default_id'] = ''
    return result


def plan(disk, configuration, generation):
    """Create only from a verified factory layout; existing multi-root is refused."""
    config = normalize(configuration)
    identifier(generation)
    check_stock_device(disk)
    validate_names(p.name for p in disk.partitions)
    userdata = disk.by_name('userdata')
    sector = disk.sector_size
    align = lambda value: (value + ALIGNMENT - 1) // ALIGNMENT * ALIGNMENT
    start, end = userdata.start, userdata.end
    cursor = start
    partitions = []
    occupied = {p.number for p in disk.partitions}
    free = [n for n in range(1, disk.entry_count + 1) if n not in occupied]
    if len(free) < len(config['roots']) + 1:
        raise LayoutError('GPT 空闲表项不足，无法添加所选 rootfs。')
    if config['userdata_gib']:
        stop = align(cursor + config['userdata_gib'] * GIB)
        partitions.append(dict(role='android', name='userdata', number=userdata.number,
                               start=cursor, end=stop, bytes=stop-cursor, action='recreate',
                               filesystem='待安装 Android 时初始化'))
        cursor = stop
    cursor = align(cursor)
    store_id = str(uuid.uuid5(uuid.UUID(generation), 'boot-store'))
    specs = [('store', 'linux_boot', config['boot_gib'], 'ext4', store_id)]
    specs += [('root', root_name(r['id']), r['size_gib'],
               r['filesystem'], r['id']) for r in config['roots']]
    names = set()
    systems = []
    for index, (role, name, size, filesystem, os_id) in enumerate(specs):
        if name in names:
            raise LayoutError('生成的分区名称重复，请重新创建系统条目。')
        names.add(name)
        stop = cursor + size * GIB
        partuuid = str(uuid.uuid5(uuid.UUID(os_id), 'partition'))
        fsuuid = str(uuid.uuid5(uuid.UUID(os_id), generation + '/filesystem'))
        entry = dict(role=role, name=name, number=free[index], start=cursor, end=stop,
                     bytes=stop-cursor, action='create', filesystem=filesystem,
                     partuuid=partuuid, fsuuid=fsuuid)
        partitions.append(entry)
        if role == 'root':
            item = copy.deepcopy(config['roots'][index - 1])
            item.update(partition=entry, target=dict(schema_version=1, os_id=os_id,
                root_partuuid=partuuid, root_fsuuid=fsuuid, root_fstype=filesystem,
                root_path='native-root', boot_generation=generation))
            systems.append(item)
        cursor = stop
    if cursor > end:
        raise LayoutError(f'空间不足：配置需要 {(cursor-start)/GIB:.2f} GiB，实际可用 {(end-start)/GIB:.2f} GiB。')
    for entry in partitions:
        entry.update(first_sector=entry['start']//sector, last_sector=entry['end']//sector-1)
    return dict(schema_version=1, layout='multi-root', generation=generation, configuration=config,
                source_userdata=dict(number=userdata.number, first_sector=userdata.first_sector,
                                     last_sector=userdata.last_sector),
                sector_size=sector, total_sectors=disk.total_sectors, partitions=partitions,
                systems=systems, store=next(p for p in partitions if p['role']=='store'),
                available_bytes=end-start, allocated_bytes=cursor-start, free_bytes=end-cursor,
                prefix=[dict(number=p.number, name=p.name, first_sector=p.first_sector,
                             last_sector=p.last_sector) for p in disk.partitions if p.name!='userdata'])


def verify_layout(disk, planned):
    if disk.sector_size != planned['sector_size'] or disk.total_sectors != planned['total_sectors']:
        raise LayoutError('写入后的磁盘几何发生变化。')
    expected = planned['prefix'] + planned['partitions']
    actual = {(p.number, p.name, p.first_sector, p.last_sector) for p in disk.partitions}
    wanted = {(p['number'], p['name'], p['first_sector'], p['last_sector']) for p in expected}
    if len(disk.partitions) != len(expected) or actual != wanted:
        raise LayoutError('实际分区表与已确认的多系统方案不一致。')


def layout_payload(planned, userdata_info):
    """Strict data-only format consumed by the device-side partition writer."""
    validate_names(p['name'] for p in planned['prefix'] + planned['partitions'])
    guid = r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}'
    fields = {}
    for key, pattern in (('typecode', r'^Partition GUID code:\s*('+guid+r')'),
                         ('guid', r'^Partition unique GUID:\s*('+guid+r')'),
                         ('attributes', r'^Attribute flags:\s*([0-9A-Fa-f]{16})')):
        match = re.search(pattern, userdata_info, re.M)
        if not match:
            raise LayoutError('无法读取 userdata 的原始分区身份。')
        fields[key] = match[1]
    source = planned['source_userdata']
    lines = ['LIUQIN_LAYOUT_V1', f"{source['number']} {source['first_sector']} {source['last_sector']}"]
    for p in planned['partitions']:
        if p['role'] == 'android':
            kind, guid_value, attributes = fields['typecode'], fields['guid'], fields['attributes']
        else:
            kind, guid_value, attributes = '0FC63DAF-8483-4772-8E79-3D69D8477DE4', p['partuuid'], '0000000000000000'
        lines.append(f"{p['number']} {p['name']} {p['first_sector']} {p['last_sector']} {kind} {guid_value} {attributes}")
    return ('\n'.join(lines)+'\n').encode()
