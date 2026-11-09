#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read and validate the factory GPT for the userdata single-root installer.

The installation formats userdata as LIUQIN_ROOT without changing its GPT
boundaries. /home stays inside native-root on the same ext4/Btrfs volume.
All planning is pure; device access belongs to the installer transport.
"""
from __future__ import annotations

import re

GIB = 1024 ** 3
MIB = 1024 ** 2

# Only the validated 4096-byte logical-sector geometry is supported.
DEVICE_SECTOR_SIZE = 4096
STOCK_PARTITION_COUNT = 35
ALIGNMENT = 4 * MIB

USERDATA_NAME = 'userdata'
ROOT_LABEL = 'LIUQIN_ROOT'
MIN_ROOT = 16 * GIB
MODES = ('linux-single',)


# The factory partition table of the 256 GB liuqin, taken from a read-only
# inventory of a real tablet: partition numbers, sector bounds and names only.
# No per-device identifier -- disk GUID, partition GUIDs, serial -- is recorded
# here.  It serves the offline layout preview that the confirmation prompt
# prints and the synthetic geometry used by offline tests.
STOCK_TOTAL_SECTORS = 61731840
STOCK_FIRST_USABLE = 6
STOCK_LAST_USABLE = 61731834
STOCK_ENTRIES = 64
STOCK_TABLE = (
    (1, 6, 7, 'switch'),
    (2, 8, 15, 'ssd'),
    (3, 16, 23, 'dbg'),
    (4, 24, 31, 'bk01'),
    (5, 32, 63, 'bk02'),
    (6, 64, 127, 'bk03'),
    (7, 128, 255, 'bk04'),
    (8, 256, 383, 'keystore'),
    (9, 384, 511, 'frp'),
    (10, 512, 1023, 'countrycode'),
    (11, 1024, 2047, 'misc'),
    (12, 2048, 4095, 'bk05'),
    (13, 4096, 6143, 'logfs'),
    (14, 6144, 8191, 'ffu'),
    (15, 8192, 12287, 'oops'),
    (16, 12288, 16383, 'devinfo'),
    (17, 16384, 32767, 'metadata'),
    (18, 32768, 36863, 'bk06'),
    (19, 36864, 45055, 'bk07'),
    (20, 45056, 61439, 'bk08'),
    (21, 61440, 77823, 'persist'),
    (22, 77824, 94207, 'persistbak'),
    (23, 94208, 102399, 'mtdblk'),
    (24, 102400, 117247, 'crash_history'),
    (25, 117248, 141823, 'minidump'),
    (26, 141824, 218623, 'rawdump'),
    (27, 218624, 480767, 'cust'),
    (28, 480768, 2708991, 'super'),
    (29, 2708992, 2709023, 'vbmeta_system_a'),
    (30, 2709024, 2709055, 'vbmeta_system_b'),
    (31, 2709056, 2716159, 'bk010'),
    (32, 2716160, 2717183, 'mem'),
    (33, 2717184, 2725375, 'mbnconfig'),
    (34, 2725376, 2758143, 'rescue'),
    (35, 2758144, 61731834, 'userdata'),
)


def reference_stock_disk(sector_size=DEVICE_SECTOR_SIZE, scale=1):
    """The factory table as a Disk.  ``scale`` restates it in smaller sectors."""
    return Disk(sector_size, STOCK_TOTAL_SECTORS * scale, STOCK_FIRST_USABLE * scale,
                (STOCK_LAST_USABLE + 1) * scale - 1, STOCK_ENTRIES,
                [Partition(number, first * scale, (last + 1) * scale - 1, name, sector_size)
                 for number, first, last, name in STOCK_TABLE])


class LayoutError(ValueError):
    """A layout request that must stop the installation before any write."""


def human(size):
    """Render a byte count the way the plan table and the error texts do."""
    for unit, scale in (('TiB', 1024 ** 4), ('GiB', GIB), ('MiB', MIB), ('KiB', 1024)):
        if size >= scale:
            return f'{size / scale:.1f} {unit}'
    return f'{size} B'


class Partition:
    def __init__(self, number, first_sector, last_sector, name, sector_size):
        self.number = number
        self.first_sector = first_sector
        self.last_sector = last_sector
        self.name = name
        self.sector_size = sector_size

    @property
    def start(self):
        return self.first_sector * self.sector_size

    @property
    def end(self):
        """Exclusive byte offset of the partition end."""
        return (self.last_sector + 1) * self.sector_size

    @property
    def size(self):
        return self.end - self.start


class Disk:
    """The parts of an ``sgdisk -p`` report this engine needs."""

    def __init__(self, sector_size, total_sectors, first_usable, last_usable,
                 entry_count, partitions):
        self.sector_size = sector_size
        self.total_sectors = total_sectors
        self.first_usable = first_usable
        self.last_usable = last_usable
        self.entry_count = entry_count
        self.partitions = partitions

    def by_name(self, name):
        found = [item for item in self.partitions if item.name == name]
        if len(found) > 1:
            raise LayoutError('the partition table has more than one ' + name)
        return found[0] if found else None

    @property
    def usable_end(self):
        """Exclusive byte offset one past the last usable sector."""
        return (self.last_usable + 1) * self.sector_size


PRINT_ROW = re.compile(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+'
                       r'\d+(?:\.\d+)?\s+\S+\s+[0-9A-Fa-f]{4}\s+(\S.*?)\s*$')


def parse_print(text):
    """Parse the output of ``sgdisk -p DEVICE``."""

    def one(pattern, label, group=1):
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            raise LayoutError('cannot read ' + label + ' from the partition table report')
        return int(match[group])

    sector_size = one(r'^Sector size \(logical(?:/physical)?\):\s*(\d+)', 'the sector size')
    total = one(r'^Disk\s+\S+:\s*(\d+)\s+sectors', 'the disk size')
    first = one(r'^First usable sector is (\d+), last usable sector is (\d+)', 'the usable range')
    last = one(r'^First usable sector is (\d+), last usable sector is (\d+)', 'the usable range', 2)
    entries = one(r'^Partition table holds up to (\d+) entries', 'the table capacity')
    partitions = []
    body = text.split('Number  Start (sector)', 1)
    if len(body) == 2:
        for line in body[1].splitlines()[1:]:
            match = PRINT_ROW.match(line)
            if match:
                partitions.append(Partition(int(match[1]), int(match[2]), int(match[3]),
                                            match[4], sector_size))
    partitions.sort(key=lambda item: item.first_sector)
    return Disk(sector_size, total, first, last, entries, partitions)


def classify(disk):
    """Only an unchanged factory table is supported; all other layouts refuse."""
    userdata = disk.by_name(USERDATA_NAME)
    last = disk.partitions[-1] if disk.partitions else None
    if (userdata is not None and last is userdata
            and len(disk.partitions) == STOCK_PARTITION_COUNT
            and {part.name for part in disk.partitions} == {row[3] for row in STOCK_TABLE}
            and userdata.last_sector == disk.last_usable):
        return 'stock'
    return 'unknown'


def check_geometry(disk):
    """Refuse ambiguous, overlapping or out-of-bounds partition reports."""
    if (disk.sector_size <= 0 or disk.first_usable < 0
            or disk.first_usable > disk.last_usable
            or disk.last_usable >= disk.total_sectors):
        raise LayoutError('invalid disk geometry')
    numbers, names = set(), set()
    previous_end = disk.first_usable - 1
    for part in sorted(disk.partitions, key=lambda item: item.first_sector):
        if part.number in numbers or part.name in names:
            raise LayoutError('duplicate partition number or name')
        if not 1 <= part.number <= disk.entry_count:
            raise LayoutError('partition number is outside the table')
        if (part.first_sector <= previous_end or part.last_sector < part.first_sector
                or part.last_sector > disk.last_usable):
            raise LayoutError('overlapping or out-of-bounds partition')
        numbers.add(part.number)
        names.add(part.name)
        previous_end = part.last_sector


def check_stock_device(disk):
    """Hard device-identity checks on top of the Fastboot product check."""
    check_geometry(disk)
    if disk.sector_size != DEVICE_SECTOR_SIZE:
        raise LayoutError(
            f'the disk reports {disk.sector_size}-byte logical sectors, not '
            f'{DEVICE_SECTOR_SIZE} —— 该设备不是受支持的小米平板 6 Pro（liuqin）')
    if len(disk.partitions) != STOCK_PARTITION_COUNT:
        raise LayoutError(
            f'the stock table has {STOCK_PARTITION_COUNT} partitions, this disk has '
            f'{len(disk.partitions)} —— 分区布局已被修改，拒绝继续')
    if {part.name for part in disk.partitions} != {row[3] for row in STOCK_TABLE}:
        raise LayoutError('partition names differ from the factory layout'
                          ' —— 分区布局已被修改，拒绝继续')
    userdata = disk.by_name(USERDATA_NAME)
    if userdata is None or disk.partitions[-1] is not userdata:
        raise LayoutError('userdata is not the last partition on the disk'
                          ' —— 分区布局已被修改，拒绝继续')
    if userdata.last_sector != disk.last_usable:
        raise LayoutError('userdata does not run to the last usable sector'
                          ' —— 分区布局已被修改，拒绝继续')


class Entry:
    def __init__(self, number, name, start, end, action, detail):
        self.number = number
        self.name = name
        self.start = start          # inclusive byte offset
        self.end = end              # exclusive byte offset
        self.action = action
        self.detail = detail

    @property
    def size(self):
        return self.end - self.start

    def first_sector(self, sector_size):
        return self.start // sector_size

    def last_sector(self, sector_size):
        return self.end // sector_size - 1


class Plan:
    def __init__(self, mode, disk, entries, region_start, region_end):
        self.mode = mode
        self.disk = disk
        self.entries = entries
        self.region_start = region_start
        self.region_end = region_end

    def entry(self, name):
        return next((item for item in self.entries if item.name == name), None)

    def table(self):
        """The plan table printed before the confirmation prompt."""
        sector = self.disk.sector_size
        lines = [f'{"Partition":<12} {"Start (sector)":>15} {"Size":>12}  Action',
                 f'{"-" * 12} {"-" * 15} {"-" * 12}  {"-" * 40}']
        for item in self.entries:
            start = str(item.first_sector(sector))
            size = human(item.size)
            lines.append(f'{item.name:<12} {start:>15} {size:>12}  {item.detail}')
        return '\n'.join(lines)


def plan_layout(disk, mode, filesystem='ext4'):
    """Format the whole userdata partition, without changing its boundaries."""
    if mode not in MODES:
        raise LayoutError('unknown layout mode: ' + str(mode))
    if filesystem not in ('ext4', 'btrfs'):
        raise LayoutError('unsupported root filesystem: ' + str(filesystem))
    check_stock_device(disk)
    userdata = disk.by_name(USERDATA_NAME)
    if userdata.size < MIN_ROOT:
        raise LayoutError('the single root is smaller than 16 GiB')
    entry = Entry(userdata.number, USERDATA_NAME, userdata.start, userdata.end, 'format',
                  f'format {filesystem} LABEL={ROOT_LABEL} (system and /home; GPT unchanged)')
    return Plan(mode, disk, [entry], userdata.start, userdata.end)
