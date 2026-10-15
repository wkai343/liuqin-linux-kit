#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install the KernelSU loadable kernel module into a stock Android boot image.

This is a host-side, pure-Python reimplementation of what KernelSU's own
``ksud boot-patch`` does in LKM mode.  ``ksud`` is an Android binary and its
image surgery is delegated to the ``android-bootimg`` crate, so neither it nor
``magiskboot`` can be used from a Linux build host; the behaviour is mirrored
here instead, byte layout included.

Upstream sources this mirrors, read at KernelSU tag v3.3.0, commit
932014ab5b2c9b74a3d11e2ec4d17dd10fc9442e:

  * ``userspace/ksud/src/boot_patch.rs``.  In LKM mode, with no optional
    switches, the whole ramdisk change is three operations:

        if !is_kernelsu_patched && cpio.exists("init") { cpio.mv("init", "init.real")?; }
        cpio.add("init", CpioEntry::regular(0o755, ksu_init))?;
        cpio.add("kernelsu.ko", CpioEntry::regular(0o755, kernelsu_ko))?;

    ``ksu_config``, ``adb_debug.prop`` and ``force_debuggable`` are only
    written when the corresponding switches are given, and the stock-image
    backup only exists on Android.  ``skip_init_boot`` is true for an
    ``android12-`` KMI, so ``boot`` is the partition ksud patches — which is
    also the only choice here, as this ROM has no ``init_boot``.

  * ``android-bootimg`` (https://github.com/5ec1cff/android_bootimg, rev
    150425b027c76ea104c82e408571651f2181b2c2), pinned by ksud's Cargo.toml:
    ``src/cpio.rs`` for the newc reader/writer, ``src/compress.rs`` for the
    lz4-legacy block container, and ``src/patcher.rs`` for the repack, which
    copies the header page verbatim, patches only the block sizes in it,
    copies every block it does not replace byte for byte, and relocates the
    trailing AVB vbmeta block while keeping the file its original length.

The KernelSU assets are an upstream input.  They are verified against
build/data/kernelsu-assets.json before use; build/scripts/fetch-kernelsu-assets.sh
downloads them into a local cache.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

PAGE_SIZE = 4096
BOOT_MAGIC = b'ANDROID!'
AVB_FOOTER_MAGIC = b'AVBf'
AVB_FOOTER_SIZE = 64
AVB_MAGIC = b'AVB0'

# cpio newc, as written by android-bootimg's Cpio::dump.
CPIO_MAGIC = b'070701'
TYPE_REGULAR = 0o100000
TRAILER = 'TRAILER!!!'
FIRST_INODE = 300000

LZ4_LEGACY_MAGIC = b'\x02\x21\x4c\x18'
LZ4_BLOCK_SIZE = 0x800000
GZIP_MAGICS = (b'\x1f\x8b', b'\x1f\x9e')


class PatchError(RuntimeError):
    """Anything that must stop the patch before an output file is written."""


def align_to(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# ramdisk compression


def detect_format(data):
    if data.startswith(LZ4_LEGACY_MAGIC):
        return 'lz4-legacy'
    if data.startswith(GZIP_MAGICS):
        return 'gzip'
    raise PatchError('unsupported ramdisk compression; the first bytes are '
                     + data[:4].hex())


def decompress(data, fmt):
    if fmt == 'gzip':
        return zlib.decompress(data, wbits=zlib.MAX_WBITS | 16)
    import lz4.block  # noqa: PLC0415 - only the lz4 path needs the module
    out = bytearray()
    offset = len(LZ4_LEGACY_MAGIC)
    while offset + 4 <= len(data):
        (block,) = struct.unpack_from('<I', data, offset)
        offset += 4
        if block == 0 or offset + block > len(data):
            # The LG variant appends the uncompressed total instead of a block.
            break
        out += lz4.block.decompress(data[offset:offset + block],
                                    uncompressed_size=LZ4_BLOCK_SIZE)
        offset += block
    if not out:
        raise PatchError('the lz4-legacy ramdisk decompressed to nothing')
    return bytes(out)


def compress(data, fmt):
    """Mirror android-bootimg's encoders: gzip at best, lz4 HC at level 12."""
    if fmt == 'gzip':
        deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        body = deflate.compress(data) + deflate.flush()
        return (b'\x1f\x8b\x08\x00' + struct.pack('<IBB', 0, 2, 3) + body
                + struct.pack('<II', zlib.crc32(data) & 0xffffffff, len(data) & 0xffffffff))
    import lz4.block  # noqa: PLC0415
    out = bytearray(LZ4_LEGACY_MAGIC)
    for start in range(0, len(data), LZ4_BLOCK_SIZE):
        block = lz4.block.compress(data[start:start + LZ4_BLOCK_SIZE], mode='high_compression',
                                   compression=12, store_size=False)
        out += struct.pack('<I', len(block)) + block
    return bytes(out)


# --------------------------------------------------------------------------
# cpio (newc)


class CpioEntry:
    __slots__ = ('mode', 'uid', 'gid', 'rdev_major', 'rdev_minor', 'data')

    def __init__(self, mode, data=b'', uid=0, gid=0, rdev_major=0, rdev_minor=0):
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.rdev_major = rdev_major
        self.rdev_minor = rdev_minor
        self.data = data

    @classmethod
    def regular(cls, mode, data):
        return cls(mode | TYPE_REGULAR, data)


def norm_path(path):
    return '/'.join(part for part in path.split('/') if part)


class Cpio:
    """A newc archive held exactly the way android-bootimg holds one.

    Entries live in a name-ordered map, so a rewrite always emits them sorted,
    renumbers the inodes from 300000 and zeroes mtime, nlink and the checksum.
    The stock liuqin ramdisk is already in that shape, because AOSP's mkbootfs
    writes it the same way; ``selftest`` asserts the round trip.
    """

    def __init__(self):
        self.entries = {}

    @classmethod
    def load(cls, data):
        cpio = cls()
        offset = 0
        while offset < len(data):
            if data[offset:offset + 6] != CPIO_MAGIC:
                raise PatchError('unsupported cpio header at offset ' + str(offset))
            fields = [int(data[offset + 6 + index * 8:offset + 14 + index * 8], 16)
                      for index in range(13)]
            mode, uid, gid = fields[1], fields[2], fields[3]
            size, rdev_major, rdev_minor, name_len = fields[6], fields[9], fields[10], fields[11]
            raw = data[offset + 110:offset + 110 + name_len]
            if not raw.endswith(b'\0'):
                raise PatchError('cpio entry name was not NUL-terminated')
            name = raw.rstrip(b'\0').decode()
            offset = align_to(offset + 110 + name_len, 4)
            if name in ('.', '..'):
                continue
            if name == TRAILER:
                rest = data.find(CPIO_MAGIC, offset)
                if rest < 0:
                    break
                offset = rest
                continue
            cpio.entries[name] = CpioEntry(mode, data[offset:offset + size], uid, gid,
                                           rdev_major, rdev_minor)
            offset = align_to(offset + size, 4)
        return cpio

    def dump(self):
        out = bytearray()

        def header(inode, entry, name):
            out.extend(CPIO_MAGIC)
            for value in (inode, entry.mode, entry.uid, entry.gid, 1, 0, len(entry.data),
                          0, 0, entry.rdev_major, entry.rdev_minor, len(name) + 1, 0):
                out.extend(b'%08x' % value)

        inode = FIRST_INODE
        for name in self.sorted_names():
            entry = self.entries[name]
            header(inode, entry, name)
            out.extend(name.encode() + b'\0')
            out.extend(bytes(align_to(len(out), 4) - len(out)))
            if entry.data:
                out.extend(entry.data)
                out.extend(bytes(align_to(len(out), 4) - len(out)))
            inode += 1
        header(inode, CpioEntry(0o755, b''), TRAILER)
        out.extend(TRAILER.encode() + b'\0')
        out.extend(bytes(align_to(len(out), 4) - len(out)))
        return bytes(out)

    def sorted_names(self):
        # Rust's BTreeMap<String> orders by UTF-8 bytes, not by code point.
        return sorted(self.entries, key=str.encode)

    def exists(self, path):
        return norm_path(path) in self.entries

    def add(self, path, entry):
        self.entries[norm_path(path)] = entry

    def move(self, source, target):
        self.entries[norm_path(target)] = self.entries.pop(norm_path(source))


# --------------------------------------------------------------------------
# boot image


class BootImage:
    """A header v3/v4 Android boot image, kept as its original bytes.

    Nothing is re-derived that can be copied: the header page and every block
    other than the ramdisk are written back verbatim, so a repack with the
    ramdisk untouched reproduces the input byte for byte.
    """

    FIELDS = (('kernel_size', 8), ('ramdisk_size', 12), ('os_version', 16),
              ('header_size', 20), ('header_version', 40), ('signature_size', 1580))

    def __init__(self, data):
        if not data.startswith(BOOT_MAGIC):
            raise PatchError('not an Android boot image: the ANDROID! magic is missing')
        self.data = data
        self.header_version = struct.unpack_from('<I', data, 40)[0]
        if self.header_version not in (3, 4):
            raise PatchError('only boot header versions 3 and 4 are supported, not '
                             + str(self.header_version))
        self.kernel_size, self.ramdisk_size = struct.unpack_from('<II', data, 8)
        self.os_version, self.header_size = struct.unpack_from('<II', data, 16)
        self.cmdline = data[44:44 + 1536]
        self.signature_size = (struct.unpack_from('<I', data, 1580)[0]
                               if self.header_version >= 4 else 0)
        offset = PAGE_SIZE
        self.kernel = data[offset:offset + self.kernel_size]
        offset += align_to(self.kernel_size, PAGE_SIZE)
        self.ramdisk_offset = offset
        self.ramdisk = data[offset:offset + self.ramdisk_size]
        offset += align_to(self.ramdisk_size, PAGE_SIZE)
        self.signature = data[offset:offset + self.signature_size]
        offset += align_to(self.signature_size, PAGE_SIZE)
        self.payload_end = offset
        self.avb = self._read_avb()

    def header_fields(self):
        return {name: struct.unpack_from('<I', self.data, offset)[0]
                for name, offset in self.FIELDS
                if not (offset == 1580 and self.header_version < 4)}

    def _read_avb(self):
        """The AVB footer, vbmeta block and the gap between payload and vbmeta."""
        footer = self.data[-AVB_FOOTER_SIZE:]
        if len(self.data) < AVB_FOOTER_SIZE or not footer.startswith(AVB_FOOTER_MAGIC):
            return None
        original_size, vbmeta_offset, vbmeta_size = struct.unpack_from('>QQQ', footer, 12)
        header = self.data[vbmeta_offset:vbmeta_offset + vbmeta_size]
        if not header.startswith(AVB_MAGIC):
            raise PatchError('the AVB footer does not point at a vbmeta block')
        if original_size < self.payload_end:
            raise PatchError('the AVB footer claims an image shorter than the boot payload')
        tail = self.data[self.payload_end:original_size]
        return {'footer': footer, 'header': header, 'tail': tail}

    def rebuild(self, ramdisk):
        """Write the image back with a new ramdisk, mirroring android-bootimg."""
        out = bytearray(self.data[:PAGE_SIZE])
        out.extend(self.kernel)
        out.extend(bytes(align_to(len(out), PAGE_SIZE) - len(out)))
        out.extend(ramdisk)
        out.extend(bytes(align_to(len(out), PAGE_SIZE) - len(out)))
        out.extend(self.signature)
        out.extend(bytes(align_to(len(out), PAGE_SIZE) - len(out)))
        struct.pack_into('<II', out, 8, len(self.kernel), len(ramdisk))
        if self.header_version >= 4:
            struct.pack_into('<I', out, 1580, len(self.signature))
        if self.avb is None:
            if len(out) > len(self.data):
                raise PatchError('the patched image is larger than the stock image')
            out.extend(bytes(len(self.data) - len(out)))
            return bytes(out)
        out.extend(self.avb['tail'])
        out.extend(bytes(align_to(len(out), PAGE_SIZE) - len(out)))
        original_size = len(out)
        vbmeta_offset = align_to(len(out), 4096)
        out.extend(bytes(vbmeta_offset - len(out)))
        out.extend(self.avb['header'])
        if len(out) + AVB_FOOTER_SIZE > len(self.data):
            raise PatchError('no room left for the AVB structures: the patched payload is '
                             f'{original_size} bytes in a {len(self.data)}-byte image')
        out.extend(bytes(len(self.data) - AVB_FOOTER_SIZE - len(out)))
        footer = bytearray(self.avb['footer'])
        struct.pack_into('>QQ', footer, 12, original_size, vbmeta_offset)
        out.extend(footer)
        return bytes(out)


# --------------------------------------------------------------------------


def verify_asset(manifest, role, path):
    entry = manifest['assets'][role]
    data = path.read_bytes()
    if len(data) != entry['bytes'] or sha256(data) != entry['sha256']:
        raise PatchError(
            f'{path} is not the pinned KernelSU {manifest["version"]} {role} asset '
            f'({entry["name"]}); run build/scripts/fetch-kernelsu-assets.sh'
            '  —— KernelSU 资源与固定校验值不一致，请重新下载')
    return data


def load_manifest(path):
    if not path.is_file():
        raise PatchError('kernelsu-assets.json is missing: ' + str(path))
    return json.loads(path.read_text())


def patch(boot_path, ksuinit_path, lkm_path, manifest):
    """Return (image bytes, report lines).  Nothing is written by this function."""
    report = []

    def say(line=''):
        report.append(line)
        print(line)

    stock = boot_path.read_bytes()
    ksuinit = verify_asset(manifest, 'ksuinit', ksuinit_path)
    lkm = verify_asset(manifest, 'lkm', lkm_path)
    say('KernelSU release: ' + manifest['version'] + '  (' + manifest['release'] + ')')
    say('KMI:              ' + manifest['kmi'])
    say('')
    say('Inputs')
    say(f'  boot.img      {sha256(stock)}  {len(stock)} bytes')
    say(f'  ksuinit       {sha256(ksuinit)}  {len(ksuinit)} bytes')
    say(f'  kernelsu.ko   {sha256(lkm)}  {len(lkm)} bytes')
    say('')

    image = BootImage(stock)
    before = image.header_fields()
    say('Boot image')
    say(f'  header version {image.header_version}, page size {PAGE_SIZE}, '
        f'os_version field 0x{image.os_version:08x}')
    say(f'  kernel {image.kernel_size} bytes, ramdisk {image.ramdisk_size} bytes, '
        f'boot signature {image.signature_size} bytes')
    say(f'  AVB footer {"present" if image.avb else "absent"}')
    say('')

    # Round-trip proof: repack with the ramdisk untouched and require the exact
    # stock bytes back.  Everything after this point differs only where the
    # ramdisk was deliberately changed.
    roundtrip = image.rebuild(image.ramdisk)
    identical = roundtrip == stock
    say('Round trip (unpatched unpack -> repack)')
    say(f'  output {len(roundtrip)} bytes, sha256 {sha256(roundtrip)}')
    say('  byte-identical to the stock image: ' + ('yes' if identical else 'NO'))
    if not identical:
        raise PatchError('the unpatched round trip is not byte-identical to the stock image;'
                         ' refusing to patch —— 空转重打包与原厂镜像不一致，已拒绝')
    say('')

    fmt = detect_format(image.ramdisk)
    plain = decompress(image.ramdisk, fmt)
    cpio = Cpio.load(plain)
    say('Ramdisk')
    say(f'  compression {fmt}, {image.ramdisk_size} compressed / {len(plain)} plain bytes, '
        f'{len(cpio.entries)} entries')
    cpio_roundtrip = cpio.dump()
    say('  cpio re-dump reproduces the stock archive: '
        + ('yes' if cpio_roundtrip == plain.rstrip(b'\0') else
           'yes (up to trailing zero padding)' if cpio_roundtrip == plain[:len(cpio_roundtrip)]
           else 'no, the archive is rewritten in normalised form'))

    if cpio.exists('kernelsu.ko'):
        raise PatchError('this boot image already carries kernelsu.ko; patch the stock image'
                         ' —— 该镜像已被打过补丁，请使用原厂镜像')
    changes = []
    if cpio.exists('init'):
        original = cpio.entries['init']
        cpio.move('init', 'init.real')
        changes.append(f'init -> init.real  (mode {original.mode & 0o7777:04o} kept, '
                       f'{len(original.data)} bytes)')
    cpio.add('init', CpioEntry.regular(0o755, ksuinit))
    changes.append(f'+ init             (mode 0755, {len(ksuinit)} bytes, ksuinit)')
    cpio.add('kernelsu.ko', CpioEntry.regular(0o755, lkm))
    changes.append(f'+ kernelsu.ko      (mode 0755, {len(lkm)} bytes, LKM)')
    say('  ramdisk changes:')
    for line in changes:
        say('    ' + line)
    say('')

    patched_plain = cpio.dump()
    patched_ramdisk = compress(patched_plain, fmt)
    out = image.rebuild(patched_ramdisk)

    result = BootImage(out)
    after = result.header_fields()
    changed = {name: (before[name], after[name]) for name in before if before[name] != after[name]}
    say('Verification')
    say('  kernel sha256 unchanged:   '
        + ('yes' if sha256(result.kernel) == sha256(image.kernel) else 'NO'))
    say('  cmdline unchanged:         ' + ('yes' if result.cmdline == image.cmdline else 'NO'))
    say('  boot signature unchanged:  '
        + ('yes' if result.signature == image.signature else 'NO'))
    say('  header fields changed:     '
        + (', '.join(f'{name} {old} -> {new}' for name, (old, new) in changed.items())
           or 'none'))
    say(f'  ramdisk {image.ramdisk_size} -> {len(patched_ramdisk)} bytes '
        f'({len(plain)} -> {len(patched_plain)} plain)')
    patched_cpio = Cpio.load(decompress(result.ramdisk, detect_format(result.ramdisk)))
    say('  init is ksuinit:           '
        + ('yes' if patched_cpio.entries['init'].data == ksuinit else 'NO'))
    say('  init mode:                 '
        + f'{patched_cpio.entries["init"].mode & 0o7777:04o}')
    say('  init.real is the stock init: '
        + ('yes' if patched_cpio.entries['init.real'].data == cpio.entries['init.real'].data
           else 'NO'))
    say('  /kernelsu.ko is the LKM:   '
        + ('yes' if patched_cpio.entries['kernelsu.ko'].data == lkm else 'NO'))
    say('  /kernelsu.ko mode:         '
        + f'{patched_cpio.entries["kernelsu.ko"].mode & 0o7777:04o}')
    say(f'  output size:               {len(out)} bytes '
        + ('(same as the stock image)' if len(out) == len(stock) else '(DIFFERENT)'))
    say('  output sha256:             ' + sha256(out))
    if (changed.keys() - {'ramdisk_size'} or sha256(result.kernel) != sha256(image.kernel)
            or result.cmdline != image.cmdline or len(out) != len(stock)
            or patched_cpio.entries['init'].data != ksuinit
            or patched_cpio.entries['kernelsu.ko'].data != lkm):
        raise PatchError('the patched image failed its own verification; nothing was written')
    say('')
    say('AVB note: any change to the ramdisk invalidates both the boot signature and the'
        ' vbmeta hash descriptor for this partition.  The image is therefore only bootable'
        ' on an unlocked bootloader.  待真机核实。')
    return out, report


def selftest():
    """Exercise the cpio and compression paths without a stock image."""
    cpio = Cpio()
    cpio.add('init', CpioEntry.regular(0o750, b'stock init'))
    cpio.add('dev', CpioEntry(0o040755))
    dumped = cpio.dump()
    again = Cpio.load(dumped)
    assert again.dump() == dumped
    assert again.entries['init'].data == b'stock init'
    assert again.entries['init'].mode == (0o750 | TYPE_REGULAR)
    payload = b'liuqin' * 100000
    for fmt in ('gzip', 'lz4-legacy'):
        blob = compress(payload, fmt)
        assert detect_format(blob) == fmt, fmt
        assert decompress(blob, fmt) == payload, fmt
    print('PASS: cpio round trip, gzip and lz4-legacy round trip')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--boot', type=Path, help='stock Android boot.img to patch')
    parser.add_argument('--ksuinit', type=Path, help='ksuinit-aarch64 from the pinned release')
    parser.add_argument('--lkm', type=Path, help='the KMI-matching *_kernelsu.ko')
    parser.add_argument('--out', type=Path, help='patched boot image to write')
    parser.add_argument('--report', type=Path, help='also write the verification report here')
    parser.add_argument('--assets', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'data/kernelsu-assets.json',
                        help='pinned KernelSU asset table (default: build/data/kernelsu-assets.json)')
    parser.add_argument('--selftest', action='store_true',
                        help='run the offline cpio and compression checks and stop')
    args = parser.parse_args(argv)
    if args.selftest:
        selftest()
        return
    missing = [name for name in ('boot', 'ksuinit', 'lkm', 'out') if getattr(args, name) is None]
    if missing:
        parser.error('--' + ', --'.join(missing) + ' ' + ('are' if len(missing) > 1 else 'is')
                     + ' required')
    manifest = load_manifest(args.assets)
    out, report = patch(args.boot, args.ksuinit, args.lkm, manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text('\n'.join(report) + '\n')
    print('Wrote ' + str(args.out))


if __name__ == '__main__':
    try:
        main()
    except (PatchError, OSError, ValueError, KeyError) as error:
        sys.exit('patch-android-boot-ksu: ' + str(error))
