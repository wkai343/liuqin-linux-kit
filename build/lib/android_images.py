# SPDX-License-Identifier: MIT
"""Read stock image files without mounting them or opening block devices.

Format reference: AOSP system/core fs_mgr/liblp metadata_format.h and libsparse.
"""
import bisect
import hashlib
import os
from pathlib import Path
import stat
import struct


class Image:
    def __init__(self, path):
        self.stream = Path(path).open('rb')
        if not stat.S_ISREG(os.fstat(self.stream.fileno()).st_mode):
            self.stream.close(); raise ValueError('只读取普通 ROM 镜像文件。')
        self.file_size = os.fstat(self.stream.fileno()).st_size
        self.ranges = []
        raw = self.stream.read(28)
        if len(raw) >= 28 and struct.unpack_from('<I', raw)[0] == 0xed26ff3a:
            magic, major, minor, header, chunk_header, block, blocks, chunks, checksum = struct.unpack('<IHHHHIIII', raw)
            if major != 1 or header < 28 or chunk_header < 12 or not block or block % 512 or chunks > 1000000:
                raise ValueError('Android sparse 头无效。')
            self.size = blocks * block; offset = header; logical = 0
            for _ in range(chunks):
                self.stream.seek(offset); entry = self.stream.read(12)
                if len(entry) != 12: raise ValueError('sparse chunk 不完整。')
                kind, reserved, count, total = struct.unpack('<HHII', entry)
                length = count * block; payload = offset + chunk_header
                if total < chunk_header or offset + total > self.file_size: raise ValueError('sparse chunk 超出文件。')
                if kind == 0xcac1 and total == chunk_header + length: data = payload
                elif kind == 0xcac2 and total == chunk_header + 4:
                    self.stream.seek(payload); data = self.stream.read(4)
                elif kind == 0xcac3 and total == chunk_header: data = None
                elif kind == 0xcac4 and total == chunk_header + 4 and count == 0:
                    offset += total; continue
                else: raise ValueError('不支持或损坏的 sparse chunk。')
                if length: self.ranges.append((logical, logical + length, data))
                logical += length; offset += total
            if logical != self.size or offset != self.file_size: raise ValueError('sparse 长度不一致。')
        else:
            self.size = self.file_size
            self.ranges.append((0, self.size, 0))
        self.starts = [entry[0] for entry in self.ranges]

    def close(self): self.stream.close()

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size: raise ValueError('镜像读取越界。')
        output = bytearray()
        while length:
            index = bisect.bisect_right(self.starts, offset) - 1
            if index < 0: raise ValueError('sparse 范围缺失。')
            start, end, data = self.ranges[index]; count = min(length, end - offset)
            if count <= 0: raise ValueError('sparse 范围不连续。')
            if data is None: output.extend(bytes(count))
            elif isinstance(data, bytes):
                skip = (offset - start) % 4
                output.extend((data * ((count + skip + 3) // 4))[skip:skip + count])
            else:
                self.stream.seek(data + offset - start); block = self.stream.read(count)
                if len(block) != count: raise ValueError('镜像文件截断。')
                output.extend(block)
            offset += count; length -= count
        return bytes(output)


def vendor_partition(source, destination):
    """Extract only vendor_a from the verified, single-device super image."""
    image = Image(source)
    try:
        geometry = image.read(4096, 52)
        magic, size = struct.unpack_from('<II', geometry)
        if magic != 0x616c4467 or size != 52: raise ValueError('super geometry 无效。')
        candidate = bytearray(geometry); candidate[8:40] = bytes(32)
        if hashlib.sha256(candidate).digest() != geometry[8:40]: raise ValueError('super geometry 校验失败。')
        maximum, slots, block = struct.unpack_from('<III', geometry, 40)
        if not 1 <= slots <= 8 or not 128 <= maximum <= 16 * 1024 * 1024 or maximum % 512 or not 512 <= block <= 65536 or block % 512:
            raise ValueError('super metadata geometry 不支持。')
        metadata = image.read(12288, maximum)
        magic, major, minor, header_size = struct.unpack_from('<IHHI', metadata)
        if magic != 0x414c5030 or major != 10 or minor > 2 or header_size not in (128, 256):
            raise ValueError('super metadata 版本不支持。')
        header = bytearray(metadata[:header_size]); header[12:44] = bytes(32)
        if hashlib.sha256(header).digest() != metadata[12:44]: raise ValueError('super header 校验失败。')
        table_size = struct.unpack_from('<I', metadata, 44)[0]
        if header_size + table_size > maximum: raise ValueError('super table 越界。')
        table = metadata[header_size:header_size + table_size]
        if hashlib.sha256(table).digest() != metadata[48:80]: raise ValueError('super table 校验失败。')
        def entries(offset, expected):
            start, count, width = struct.unpack_from('<III', metadata, offset)
            if width != expected or start + count * width > len(table): raise ValueError('super table descriptor 无效。')
            return [table[start + i * width:start + (i + 1) * width] for i in range(count)]
        partitions, extents, devices = entries(80, 52), entries(92, 24), entries(116, 64)
        if len(devices) != 1: raise ValueError('只支持单文件 super 镜像。')
        disk_size = struct.unpack_from('<Q', devices[0], 16)[0]
        first_sector = struct.unpack_from('<Q', devices[0])[0]
        if disk_size != image.size: raise ValueError('super block device 长度不一致。')
        selected = [entry for entry in partitions if entry[:36].split(b'\0')[0] == b'vendor_a']
        if len(selected) != 1: raise ValueError('vendor_a 缺失或重复。')
        first, count = struct.unpack_from('<II', selected[0], 40)
        if count == 0 or first + count > len(extents): raise ValueError('vendor extents 越界。')
        with Path(destination).open('xb') as output:
            size = 0
            for entry in extents[first:first + count]:
                sectors, kind, sector, device = struct.unpack('<QIQI', entry)
                length = sectors * 512
                if size + length > disk_size: raise ValueError('vendor 逻辑长度超出 super。')
                if device != 0 or kind not in (0, 1): raise ValueError('vendor extent 类型不支持。')
                if kind == 1:
                    output.seek(length, 1); size += length; continue
                if sector < first_sector or sector * 512 + length > disk_size: raise ValueError('vendor extent 超出 super。')
                offset = sector * 512
                while length:
                    block = image.read(offset, min(length, 1024 * 1024))
                    output.write(block); offset += len(block); length -= len(block); size += len(block)
            output.truncate(size)
    finally: image.close()


def dtbs(data, directory):
    directory = Path(directory); directory.mkdir(parents=True)
    offset = 0; index = 0
    while offset < len(data):
        if not any(data[offset:]): break
        # Some vendor_boot packers align consecutive flattened trees with zeroes.
        padding = 0
        while offset < len(data) and data[offset] == 0 and padding < 4096:
            offset += 1; padding += 1
        if len(data) - offset < 40 or data[offset:offset + 4] != b'\xd0\x0d\xfe\xed':
            raise ValueError('vendor_boot DTB 列表无效。')
        size = struct.unpack_from('>I', data, offset + 4)[0]
        if size < 40 or offset + size > len(data): raise ValueError('DTB 长度无效。')
        (directory / f'dtb-{index:02d}.dtb').write_bytes(data[offset:offset + size])
        offset += size; index += 1
    if index < 3: raise ValueError('vendor_boot 缺少所需的基础 DTB。')


def boot_assets(vendor_boot, dtbo, output):
    output = Path(output)
    with Path(vendor_boot).open('rb') as stream:
        header = stream.read(2128)
        if header[:8] != b'VNDRBOOT' or len(header) < 2112: raise ValueError('vendor_boot 头无效。')
        version, page = struct.unpack_from('<II', header, 8)
        ramdisk = struct.unpack_from('<I', header, 24)[0]
        head_size, dtb_size = struct.unpack_from('<II', header, 2096)
        if version not in (3, 4) or page < 4096 or page > 65536 or page & (page - 1) or head_size < 2112 or head_size > page:
            raise ValueError('vendor_boot 布局不支持。')
        align = lambda n: (n + page - 1) // page * page
        stream.seek(align(head_size) + align(ramdisk)); data = stream.read(dtb_size)
        if len(data) != dtb_size: raise ValueError('vendor_boot DTB 截断。')
        dtbs(data, output / 'dtbs')
    with Path(dtbo).open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        header = stream.read(32)
        if len(header) != 32: raise ValueError('DTBO 头截断。')
        magic, total, head, width, count, offset, page, version = struct.unpack('>8I', header)
        if magic != 0xd7b7ab1e or total > size or head < 32 or width < 32 or count < 1 or count > 256 or offset + width * count > total:
            raise ValueError('DTBO table 无效。')
        directory = output / 'dtbo'; directory.mkdir()
        for index in range(count):
            stream.seek(offset + index * width); entry = stream.read(32)
            length, start = struct.unpack_from('>II', entry)
            if start < offset + width * count or length < 40 or start + length > total: raise ValueError('DTBO entry 越界。')
            stream.seek(start); payload = stream.read(length)
            if payload[:4] != b'\xd0\x0d\xfe\xed' or struct.unpack_from('>I', payload, 4)[0] != length:
                raise ValueError('DTBO entry 不是完整 FDT。')
            (directory / f'entry.{index}').write_bytes(payload)
