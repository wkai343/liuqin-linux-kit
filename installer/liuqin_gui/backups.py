# SPDX-License-Identifier: MIT
"""Immutable recovery backups, separate from mutable installation receipts."""
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime, timezone

IMAGES = ('boot_a.img', 'boot_b.img', 'persist.img')
GPT_FILES = ('gpt/sda-head.bin', 'gpt/sda-tail.bin', 'gpt/manifest.json', 'gpt/sgdisk-p.txt')
FILES = IMAGES + GPT_FILES


class BackupError(RuntimeError):
    pass


def sha(path, check=None):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(4 * 1024**2):
            if check:
                check()
            digest.update(chunk)
    return digest.hexdigest()


def geometry(disk):
    # Bootloader slot changes alter type GUIDs and attributes, not this topology.
    return dict(sector_size=disk.sector_size, sectors=disk.total_sectors,
                first_usable=disk.first_usable, last_usable=disk.last_usable,
                entry_count=disk.entry_count,
                partitions=[[p.number, p.first_sector, p.last_sector, p.name] for p in disk.partitions])


def json_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise BackupError('备份描述文件缺失或无效：' + path.name)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise BackupError('备份描述文件必须是对象：' + path.name)
    return data


def load(directory, serial, simulation, layout, check=None):
    """Validate new manifests and legacy CLI / v0.1 backups without modifying them."""
    root = Path(directory).expanduser().resolve()
    for name in FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise BackupError('备份不完整或包含外部链接：' + name)
    gpt = json_file(root / 'gpt/manifest.json')
    if gpt.get('serial') != serial:
        raise BackupError('备份序列号与所选平板不一致。')
    disk = layout.parse_print((root / 'gpt/sgdisk-p.txt').read_text(encoding='utf-8'))
    layout.check_geometry(disk)
    if gpt.get('sector_size') != disk.sector_size or gpt.get('sectors') != disk.total_sectors:
        raise BackupError('备份的 GPT 几何信息不一致。')
    if disk.sector_size != 4096 or gpt.get('disk') != '/dev/sda':
        raise BackupError('备份不是受支持的 UFS 布局。')
    manifest_path = root / 'backup.json'
    manifest = json_file(manifest_path) if manifest_path.exists() else None
    if manifest:
        if (manifest.get('schema_version') != 1 or manifest.get('complete') is not True or
                manifest.get('serial') != serial or manifest.get('device') != 'liuqin' or
                set(manifest.get('files', {})) != set(FILES)):
            raise BackupError('备份未完成或描述信息不正确。')
        expected = manifest['files']
        is_demo = manifest.get('simulation') is True
        if manifest.get('geometry') != geometry(disk):
            raise BackupError('备份描述与分区报告不一致。')
    else:
        sums_path = root / 'SHA256SUMS'
        if sums_path.is_symlink() or not sums_path.is_file():
            raise BackupError('旧备份缺少 SHA256SUMS。')
        expected = {}
        for line in sums_path.read_text(encoding='utf-8').splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] in IMAGES:
                if fields[1] in expected:
                    raise BackupError('旧备份存在重复校验项。')
                expected[fields[1]] = fields[0]
        if set(expected) != set(IMAGES) or set(gpt.get('files', {})) != {'sda-head.bin', 'sda-tail.bin'}:
            raise BackupError('旧备份校验信息不完整。')
        expected.update({'gpt/' + name: value for name, value in gpt['files'].items()})
        # The original CLI did not pin its report and metadata separately.
        # Bind their current validated contents into the selected backup ID.
        expected.update({name: sha(root / name, check) for name in GPT_FILES if name not in expected})
        is_demo = (root / 'DEMO-ONLY.txt').exists()
        if (root / 'receipt.json').is_file():
            is_demo = is_demo or json_file(root / 'receipt.json').get('simulation') is True
    if is_demo != simulation:
        raise BackupError('演示备份不能用于真机，真机备份也不用于演示。')
    for name, expected_hash in expected.items():
        if not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_hash) or sha(root / name, check) != expected_hash:
            raise BackupError('备份校验失败：' + name)
    for name in ('gpt/sda-head.bin', 'gpt/sda-tail.bin'):
        if (root / name).stat().st_size != 6 * disk.sector_size:
            raise BackupError('GPT 备份大小不正确。')
    if not all((root / name).stat().st_size > 0 for name in IMAGES):
        raise BackupError('启动或校准备份为空。')
    identity = dict(serial=serial, simulation=simulation, files=expected, geometry=geometry(disk))
    backup_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(path=str(root), serial=serial, simulation=simulation, files=expected,
                sizes={name: (root / name).stat().st_size for name in FILES},
                geometry=geometry(disk), backup_id=backup_id, valid_local=True,
                created_at=manifest.get('created_at', '') if manifest else
                    datetime.fromtimestamp((root / 'gpt/manifest.json').stat().st_mtime, timezone.utc).isoformat(),
                legacy=manifest is None)


def publish(directory, serial, simulation, layout):
    record = load(directory, serial, simulation, layout)
    manifest = dict(schema_version=1, device='liuqin', serial=serial, simulation=simulation,
                    complete=True, created_at=datetime.now(timezone.utc).isoformat(),
                    files=record['files'], geometry=record['geometry'])
    temporary = Path(directory) / 'backup.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    temporary.replace(Path(directory) / 'backup.json')
    return load(directory, serial, simulation, layout)


def assert_compatible(record, device, report, persist_sha256, layout):
    if record['serial'] != device['serial'] or geometry(layout.parse_print(report)) != record['geometry']:
        raise BackupError('已有备份不匹配当前设备或分区边界，请单独新建备份。')
    if any(record['sizes'][name] != device['boot_bytes'] for name in ('boot_a.img', 'boot_b.img')):
        raise BackupError('已有备份的引导分区大小不匹配。')
    if not isinstance(persist_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', persist_sha256):
        raise BackupError('无法取得当前 persist 分区的有效校验值。')
    matched = record['files']['persist.img'] == persist_sha256
    return dict(persist_snapshot_matches=matched, current_persist_sha256=persist_sha256,
                backup_persist_sha256=record['files']['persist.img'],
                warnings=[] if matched else [
                    '当前设备状态与原始恢复备份不完全相同。原始备份仍有效并保留；本次使用已检查的当前设备校准数据。'])
