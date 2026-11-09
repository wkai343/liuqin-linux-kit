# SPDX-License-Identifier: MIT
"""Bounded raw/Android sparse image reader shared by the builder and installer.

Format reference: AOSP system/core/libsparse/sparse_format.h.
"""
import bisect
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
