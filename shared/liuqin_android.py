# SPDX-License-Identifier: MIT
"""Android ROM admission and preservation rules; no device writes."""
from __future__ import annotations
from liuqin_partition_names import root_name, validate_names
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import uuid
from liuqin_android_images import Image
from liuqin_layout import STOCK_TABLE, check_geometry
from liuqin_multiboot import identifier

CHUNK_BYTES = 32 * 1024**2
BOOT_STORE_CHUNK = 4 * 1024**2
BOOT_STORE_BLOCK = 4096
ANDROID_PARTITIONS = frozenset(('super', 'vendor_boot_a', 'dtbo_a', 'vbmeta_a', 'vbmeta_system_a'))
FORMAT_PARTITIONS = frozenset(('userdata', 'metadata'))
FIRMWARE_PARTITIONS = frozenset(('modem_a', 'bluetooth_a', 'dsp_a'))

FORMAT_OPTIONS = {
    'userdata': ('f2fs', ('-f', '-t', '0', '-g', 'android', '-O',
        'extra_attr,inode_checksum,sb_checksum,compression', '-w', '4096', '-l', 'userdata')),
    'metadata': ('ext4', ('-F', '-b', '4096', '-I', '256', '-m', '0', '-O',
        'none,has_journal,extent,huge_file,flex_bg,metadata_csum,dir_index,extra_isize,uninit_bg', '-L', 'metadata')),
}


def sparse_boot_chunks(chunks):
    offset = 0
    for chunk in chunks:
        data = chunk.rstrip(b'\0')
        if data:
            yield offset, data
        offset += len(chunk)


def sparse_boot_bytes(chunks, block_size=BOOT_STORE_BLOCK):
    return sum((len(data)+block_size-1)//block_size*block_size
               for _, data in sparse_boot_chunks(chunks))


def sparse_boot_file_bytes(path):
    with Path(path).open('rb') as stream:
        return sparse_boot_bytes(iter(lambda: stream.read(BOOT_STORE_CHUNK), b''))


def format_settings(name, filesystem=None):
    if name not in FORMAT_OPTIONS: raise ValueError('禁止格式化此分区。')
    selected, arguments = FORMAT_OPTIONS[name]
    if filesystem is None:filesystem=selected
    if name=='userdata' and filesystem=='ext4':
        arguments=FORMAT_OPTIONS['metadata'][1][:-1]+('userdata',)
    elif filesystem!=selected:raise ValueError('不支持此 Android 数据文件系统。')
    return dict(filesystem=filesystem, mkfs_args=list(arguments))


def linux_userdata_signature(header):
    # Android metadata-encrypted data is opaque. A two-byte ext4 magic alone
    # can occur by chance, so require plausible filesystem fields as well.
    if header[:6] == b'LUKS\xba\xbe' or header[65600:65608] == b'_BHRfS_M': return True
    return (len(header) >= 1114 and header[1080:1082] == b'\x53\xef' and
            struct.unpack_from('<I', header, 1048)[0] <= 6 and
            struct.unpack_from('<H', header, 1112)[0] in (128, 256, 512, 1024, 2048, 4096))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha(path, check=lambda: None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(4 * 1024**2):
            check(); digest.update(data)
    return digest.hexdigest()


def load_profile(profile_id='stock'):
    directory = Path(__file__).resolve().parent
    from liuqin_rom_admission import policy
    record=policy(profile_id)
    if record['kind']=='payload-zip-v2':
        profile=json.loads((directory/record['profile']).read_text())
        if (profile.get('id')!=profile_id or profile.get('schema_version')!=1 or profile.get('device')!='liuqin' or
            profile.get('slot')!='a' or profile.get('firmware_policy')!='preserve-existing' or profile.get('shared_firmware')!=[] or
            {profile['images'][name]['partition'] for name in profile['install_images']}!=ANDROID_PARTITIONS or
            set(profile['images'])!={'boot.img','super.img','dtbo.img','vendor_boot.img','vbmeta.img','vbmeta_system.img'} or
            profile['images']['boot.img']['partition']!='boot_a' or
            any(profile[name]!=format_settings(name) for name in FORMAT_PARTITIONS)):
            raise ValueError('OTA 适配描述不符合受控写入策略。')
        return profile
    if profile_id!='stock':raise ValueError('未收录的原厂镜像适配。')
    lock = json.loads((directory/'liuqin-rom-images.json').read_text())
    profile = json.loads((directory/'liuqin-android-profile.json').read_text())
    if (profile['schema_version'] != 1 or profile['device'] != 'liuqin' or
        profile['rom'] != lock['rom'] or profile['anti_version'] != lock['anti_version'] or
        {lock['images'][name]['partition'] for name in profile['install_images']} != ANDROID_PARTITIONS or
        {lock['images'][name]['partition'] for name in profile['shared_firmware']} != FIRMWARE_PARTITIONS or
        profile['super']['source_sha256'] != lock['images']['super.img']['sha256']):
        raise ValueError('Android 适配描述与固定 ROM 清单不一致。')
    if any(profile[name] != format_settings(name) for name in FORMAT_PARTITIONS):
        raise ValueError('Android 格式化参数不匹配受支持的固定策略。')
    return dict(profile, images=lock['images'])


def identify_profile(path):
    """Select a built-in policy; import_rom still verifies every source hash."""
    if not path:return 'stock'
    source=Path(path).expanduser()
    if source.is_symlink():raise ValueError('ROM 材料不能是符号链接。')
    from liuqin_rom_admission import catalog
    candidates=[load_profile(r['id']) for r in catalog()]
    if source.is_dir():
        boot=(source/'images' if (source/'images').is_dir() else source)/'boot.img'
        if boot.is_file() and not boot.is_symlink():
            digest=sha(boot)
            hits=[p.get('id','stock') for p in candidates if boot.stat().st_size==p['images']['boot.img']['bytes'] and digest==p['images']['boot.img']['sha256']]
            if len(hits)==1:return hits[0]
        return 'stock' # Full material verification still rejects unknown directories.
    elif source.suffix.lower()=='.zip':
        candidates=[p for p in candidates if p.get('archive',{}).get('format')=='payload-zip-v2' and source.is_file() and source.stat().st_size==p['archive']['bytes']]
        if candidates:
            digest=sha(source);hits=[p['id'] for p in candidates if digest==p['archive']['sha256']]
            if len(hits)==1:return hits[0]
        raise ValueError('此 OTA ZIP 没有对应的已收录适配。可先使用“ROM 分析与安装预览”，不会按文件名或大小放行写入。')
    return 'stock'


def normalize(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Android 配置版本无效。')
    if value.get('mode') not in ('preserve', 'reinstall') or type(value.get('set_default')) is not bool:
        raise ValueError('请选择保留或重装 Android，以及是否设为默认。')
    source = value.get('rom_path', '')
    if not isinstance(source, str) or '\0' in source:
        raise ValueError('ROM 路径无效。')
    if value['mode'] == 'reinstall' and not source:
        raise ValueError('重装 Android 需要受支持的完整 ROM。')
    return dict(schema_version=1, mode=value['mode'], set_default=value['set_default'], rom_path=source)


def verify_rom(directory, profile, check=lambda: None):
    directory = Path(directory).expanduser().resolve()
    if (directory/'images').is_dir(): directory = directory/'images'
    result = {}
    for name in profile.get('material_images',profile['images']):
        item=profile['images'][name]
        path = directory/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes'] or sha(path, check) != item['sha256']:
            raise ValueError('ROM 镜像缺失或不属于 '+profile['rom']+'：'+name)
        result[name] = str(path)
    return result


def image_plan(path, partition, profile_entry, check=lambda: None):
    image = Image(path)
    try:
        if image.size <= 0 or image.size > partition['bytes'] or partition['bytes'] % 4096:
            raise ValueError('ROM 展开大小超过目标分区或分区未对齐：'+partition['name'])
        chunks = []; whole = hashlib.sha256()
        for offset in range(0, partition['bytes'], CHUNK_BYTES):
            check(); length = min(CHUNK_BYTES, partition['bytes']-offset)
            available = min(length, max(0, image.size-offset))
            data = image.read(offset, available) if available else b''
            data += bytes(length-available)
            whole.update(data)
            chunks.append(dict(offset=offset, bytes=length, sha256=hashlib.sha256(data).hexdigest()))
        return dict(partition=copy.deepcopy(partition), source_sha256=profile_entry['sha256'],
                    source_bytes=profile_entry['bytes'], expanded_bytes=image.size,
                    whole_sha256=whole.hexdigest(), chunks=chunks)
    finally:
        image.close()


def write_partitions(profile):
    from liuqin_ota import full,write_names
    return write_names(profile) if full(profile) else ANDROID_PARTITIONS

def validate_images(plans, profile, inventory):
    allowed=write_partitions(profile)
    if len(plans) != len(allowed) or {p['partition']['name'] for p in plans} != allowed:
        raise ValueError('Android 写入清单必须精确匹配固定白名单。')
    if profile.get('generic_ota_version')==2 and [p['partition']['name'] for p in plans]!=[profile['images'][n]['partition'] for n in profile['install_images']]:raise ValueError('ROM 写入顺序与确认清单不同。')
    records = {p['partition']: p for p in profile['images'].values()}
    for image in plans:
        partition = image['partition']; name = partition['name']
        if partition != inventory.get(name): raise ValueError('目标分区身份发生变化：'+name)
        if image['source_sha256'] != records[name]['sha256'] or image['source_bytes'] != records[name]['bytes']:
            raise ValueError('Android 镜像来源不匹配：'+name)
        limit = image['expanded_bytes']
        if type(limit) is not int or not 0 < limit <= partition['bytes']:
            raise ValueError('Android 镜像范围无效。')
        offset = 0
        for chunk in image['chunks']:
            length = chunk.get('bytes')
            if (type(chunk.get('offset')) is not int or chunk.get('offset') != offset or type(length) is not int or
                not 0 < length <= CHUNK_BYTES or length % 4096 or offset+length > partition['bytes'] or
                not re.fullmatch('[0-9a-f]{64}', chunk.get('sha256', ''))):
                raise ValueError('Android 分块写入范围无效。')
            offset += length
        if offset != partition['bytes'] or not re.fullmatch('[0-9a-f]{64}', image.get('whole_sha256', '')):
            raise ValueError('Android 镜像未覆盖完整目标分区。')


def managed_layout(disk, inventory, scan, serial, require_android=True):
    """Validate a managed layout independently of which systems are installed."""
    check_geometry(disk)
    validate_names(inventory)
    if disk.sector_size != 4096 or 'linux_boot' not in inventory:
        raise ValueError('需要本项目创建的分区和引导存储；不会自动重分区。')
    registry = scan['registry']
    if registry.get('schema_version') != 1 or registry.get('serial') != serial:
        raise ValueError('引导存储不属于当前设备。')
    generation = identifier(registry.get('layout_generation'))
    store = inventory['linux_boot']
    store_id = uuid.uuid5(uuid.UUID(generation), 'boot-store')
    if (store['parent'] != 'sda' or store.get('filesystem') != 'ext4' or
        store['partuuid'] != str(uuid.uuid5(store_id, 'partition')) or
        scan['store_fsuuid'] != str(uuid.uuid5(store_id, generation+'/filesystem'))):
        raise ValueError('引导存储的分区或文件系统身份不匹配。')
    userdata = inventory.get('userdata')
    if require_android and userdata is None:
        raise ValueError('未配置 Android userdata，不能安装 Android。')
    if userdata and (userdata['bytes'] < 16*1024**3 or userdata['parent'] != 'sda'):
        raise ValueError('Android userdata 位置或容量无效。')
    systems = registry.get('systems')
    if not isinstance(systems, list) or len(systems) > 17:
        raise ValueError('已有系统清单无效。')
    ids = set(); linux = []; android = []
    for system in systems:
        os_id = identifier(system.get('id'))
        if os_id in ids: raise ValueError('已有系统身份重复。')
        ids.add(os_id)
        image_path = system.get('image', '')
        if not re.fullmatch('systems/'+re.escape(os_id)+r'/images/[0-9a-f-]{36}\.img', image_path):
            raise ValueError('已有引导镜像路径无效。')
        entry = scan['files'].get(image_path)
        if not entry or entry['sha256'] != system.get('boot_sha256'):
            raise ValueError('已有引导镜像校验失败：'+system.get('name', os_id))
        if system.get('type') == 'android':
            android.append(system)
        elif system.get('type') == 'linux':
            linux.append(system)
        else: raise ValueError('已有系统类型不受支持。')
    if len(android) > 1 or (android and userdata is None) or registry.get('default_id') not in ids | {None}:
        raise ValueError('默认引导或 Android 登记无效。')
    # Older stores only describe installed roots. New stores also keep empty
    # slots, so creating partitions never pretends that an OS is installed.
    slots = registry.get('rootfs_slots', [dict(id=s['id'], name=s['name'], partition=s['partition'],
        target=s['target'], status='installed') for s in linux])
    if not isinstance(slots, list) or len(slots) > 16: raise ValueError('rootfs 分区登记无效。')
    seen = set(); roots = []; installed = {s['id']:s for s in linux}
    for slot in slots:
        os_id = identifier(slot.get('id'))
        if os_id in seen or os_id in {s['id'] for s in android}: raise ValueError('rootfs 身份冲突。')
        seen.add(os_id)
        target, saved = slot['target'], slot['partition']
        name = root_name(os_id); actual = inventory.get(name)
        if (not actual or actual['parent'] != 'sda' or saved.get('name') != name or
            target.get('os_id') != os_id or target.get('root_path') != 'native-root' or
            target.get('root_fstype') not in ('ext4', 'btrfs') or
            target.get('root_partuuid') != str(uuid.uuid5(uuid.UUID(os_id), 'partition')) or
            target.get('root_fsuuid') != str(uuid.uuid5(uuid.UUID(os_id), identifier(slot.get('format_generation',generation))+'/filesystem')) or
            actual['partuuid'] != target['root_partuuid'] or actual['bytes'] != saved.get('bytes') or
            actual['start'] != saved.get('start') or slot.get('status') not in ('reserved', 'installed')):
            raise ValueError('rootfs 身份或边界不匹配：'+slot.get('name', os_id))
        if slot['status'] == 'installed':
            system = installed.get(os_id)
            if (not system or system['partition'] != saved or system['target'] != target or
                actual.get('fsuuid') != target['root_fsuuid'] or actual.get('filesystem') != target['root_fstype']):
                raise ValueError('已安装 Linux rootfs 身份不匹配。')
        elif os_id in installed:
            raise ValueError('已安装系统不能标记为空闲 rootfs。')
        roots.append(copy.deepcopy(actual))
    if set(installed)-seen: raise ValueError('Linux 缺少分区登记。')
    prefix = {row[3] for row in STOCK_TABLE if row[3] != 'userdata'}
    allowed = prefix | {'linux_boot'} | ({'userdata'} if userdata else set()) | {p['name'] for p in roots}
    if {p.name for p in disk.partitions} != allowed or len(disk.partitions) != len(allowed):
        raise ValueError('存在未登记分区，不能自动接管。')
    for part in disk.partitions:
        actual = inventory.get(part.name)
        if not actual or actual['parent'] != 'sda' or actual['start'] != part.start or actual['bytes'] != part.size or actual['number'] != part.number:
            raise ValueError('GPT 与内核分区视图不一致：'+part.name)
    start = max(inventory[name]['start']+inventory[name]['bytes'] for name in prefix)
    if any(p['start'] < start for p in roots+[store]+([userdata] if userdata else [])):
        raise ValueError('系统分区范围不符合管理布局。')
    saved_layout = registry.get('partition_layout')
    if saved_layout is not None:
        names = {'linux_boot'} | ({'userdata'} if userdata else set()) | {p['name'] for p in roots}
        if not isinstance(saved_layout, list) or len(saved_layout) != len(names) or {p['name'] for p in saved_layout} != names:
            raise ValueError('分区规划登记不完整。')
        for saved in saved_layout:
            actual = inventory[saved['name']]
            if any(actual[key] != saved[key] for key in ('number','start','bytes')):
                raise ValueError('实际分区与最初规划不符。')
    if require_android and scan['userdata_linux_signature']:
        raise ValueError('userdata 中发现 Linux 文件系统，拒绝把它当作 Android 数据分区。')
    return dict(store=copy.deepcopy(store), protected=roots+[copy.deepcopy(store)],
                linux=[copy.deepcopy(inventory[s['partition']['name']]) for s in linux], slots=copy.deepcopy(slots),
                android=android[0] if android else None,
                userdata=copy.deepcopy(userdata), metadata=copy.deepcopy(inventory['metadata']))


def next_registry(old, android, transaction, mode, set_default):
    result = copy.deepcopy(old)
    result['systems'] = [s for s in result['systems'] if s['type'] != 'android']+[android]
    result['android_transaction'] = transaction
    result['userdata_state'] = 'android-first-boot-pending' if mode == 'reinstall' else 'android-preserved-unverified'
    result['resolved_android_transactions'] = sorted(set(result.get('resolved_android_transactions', [])) | {transaction})
    if set_default:
        result.update(default_id=android['id'], default_generation=android['generation'])
    elif old.get('default_id') == android['id']:
        result['default_generation'] = android['generation']
    return result


def check_registry_transition(old, new, android_id, set_default):
    identifier(android_id)
    old_linux = [s for s in old['systems'] if s['type'] == 'linux']
    new_linux = [s for s in new['systems'] if s['type'] == 'linux']
    if canonical(old_linux) != canonical(new_linux):
        raise ValueError('Android 操作不能修改 Linux 系统登记。')
    if len([s for s in new['systems'] if s['type']=='android']) != 1 or android_id in {s['id'] for s in old_linux}:
        raise ValueError('Android 系统身份冲突。')
    android = next(s for s in new['systems'] if s['type']=='android')
    generation = identifier(android.get('generation'))
    if android.get('id') != android_id or android.get('image') != 'systems/'+android_id+'/images/'+generation+'.img':
        raise ValueError('Android 登记路径或身份不匹配。')
    if set_default and (new.get('default_id') != android_id or new.get('default_generation') != generation):
        raise ValueError('默认引导必须指向本次 Android 版本。')
    if not set_default and old.get('default_id') != android_id and old.get('default_generation') != new.get('default_generation'):
        raise ValueError('不能改变现有 Linux 默认引导版本。')
    mutable = {'systems', 'android_transaction', 'userdata_state', 'resolved_android_transactions', 'default_id', 'default_generation'}
    if {k:v for k,v in old.items() if k not in mutable} != {k:v for k,v in new.items() if k not in mutable}:
        raise ValueError('Android 操作改变了受保护的引导存储元数据。')
    if not set_default and old.get('default_id') != new.get('default_id'):
        raise ValueError('未选择修改默认引导，不能切换默认系统。')
