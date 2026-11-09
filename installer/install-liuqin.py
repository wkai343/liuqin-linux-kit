#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install a verified liuqin bundle over USB networking from Linux or Windows."""
import argparse
import base64
import functools
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
from liuqin_gui import host

# The supported single-root layout boots from slot A.
LINUX_BOOT_SLOT = 'a'
STOCK_BOOT_IMAGE = 'boot.img'
# Every layout operation resolves the disk through a partition that exists in
# every state of this installation: persist is never created, moved or removed.
ANCHOR = 'persist'


# Retained only for the legacy diagnostic CLI; the GUI uses ADB.
def check_installer_support(manifest):
    if host.IS_WINDOWS and manifest.get('installer_usb_network') not in ('rndis','adb'):
        raise RuntimeError('Windows 需要新版 installer.img 的 RNDIS 安装通道，请先重新构建安装镜像。')


def server_address(address):
    if host.IS_WINDOWS and address != '192.168.7.1':
        raise RuntimeError('此旧版 CLI 需要手动配置 USB 网卡；推荐改用 GUI 的 ADB Recovery。')
    return address, 37631 if host.IS_WINDOWS else 0


def load_layout():
    """Load the layout engine from the repository or from the bundle beside us."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / 'shared/liuqin_layout.py',
                      here / 'lib/liuqin_layout.py', here / 'liuqin_layout.py'):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('liuqin_layout', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError('liuqin_layout.py is missing next to this program'
                       ' —— 安装包不完整，请下载同一版本的全部文件')


layout = load_layout()


def load_artifacts():
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / 'shared/liuqin_artifacts.py', here / 'liuqin_artifacts.py'):
        if candidate.is_file():
            sys.path.insert(0, str(candidate.parent))
            spec = importlib.util.spec_from_file_location('liuqin_artifacts', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError('独立安装环境需要配套的 liuqin_artifacts.py。')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def update_zeros(digest, length):
    """Feed `length` zero bytes into `digest`."""
    while length > 0:
        block = min(length, 1 << 20)
        digest.update(bytes(block))
        length -= block
    return digest


def boot_partition_bytes(table):
    """The size of boot_a and boot_b.

    The pinned stock boot.img fills its partition exactly. Compare the
    reported boot_a size with this known geometry before writing.
    """
    return table['images'][STOCK_BOOT_IMAGE]['bytes']


def pad_boot_image(source, expected_sha256, size, directory):
    """Write `source` followed by zeros up to `size` bytes into `directory`.

    The bootloader writes only the bytes it is sent, so flashing the bare
    project image would leave the old tail of the partition (the stock
    image's AVB footer, for one) behind it. Hash the entire padded payload
    so the GUI can compare it with the complete boot_a readback. The zeros
    are a sparse tail (truncate), so the file costs the image's own size on disk.

    Returns the padded file and its sha256.  The source is checked against
    `expected_sha256` while it is copied, so the padded file carries no bytes
    other than those of the verified bundle image.
    """
    target = Path(directory) / 'boot-padded.img'
    image = hashlib.sha256()
    padded = hashlib.sha256()
    with source.open('rb') as stream, target.open('xb') as out:
        while chunk := stream.read(1 << 20):
            image.update(chunk)
            padded.update(chunk)
            out.write(chunk)
        length = out.tell()
        if length > size:
            raise RuntimeError(f'the image is {length} bytes, larger than the {size}-byte partition')
        out.truncate(size)
    if image.hexdigest() != expected_sha256:
        raise RuntimeError('the image changed while it was being padded; nothing was written')
    update_zeros(padded, size - length)
    if target.stat().st_size != size:
        raise RuntimeError(f'the padded image is not {size} bytes')
    return target, padded.hexdigest()


# The RAM shell is BusyBox ash behind telnetd, and every remote command reaches
# it as one input line.  ash's line-editing buffer refuses a line of 1024 bytes
# or more: measured on the RAM image, 1023 bytes including the newline are
# accepted and 1024 are not.  The PTY is not the limit; Linux's canonical
# input limit is 4095 bytes.
RAM_SHELL_LINE_BYTES = 1023  # longest accepted line, newline included


def frame_command(text, start, end):
    """Return the input line that runs `text` between the two marker lines.

    A line the RAM shell would refuse raises before anything is sent.
    """
    line = ("stty -echo; printf '\\n%s\\n' " + shlex.quote(start) +
            '; sh -c ' + shlex.quote(text) +
            "; result=$?; printf '\\n%s %s\\n' " + shlex.quote(end) +
            ' "$result"\n').encode()
    if len(line) > RAM_SHELL_LINE_BYTES:
        raise RuntimeError(f'remote command line is {len(line)} bytes; the RAM shell accepts at most'
                           f' {RAM_SHELL_LINE_BYTES} including the newline, so it was not sent: '
                           + repr(text[:80]))
    return line


def command(address, text, timeout=60, on_output=None, cancel_check=None):
    """Use exact line markers, not command echo, to delimit one shell result."""
    token = 'LIUQIN_' + uuid.uuid4().hex
    start, end = token + '_START', token + '_END'
    line = frame_command(text, start, end)
    if cancel_check:
        cancel_check()
    with socket.create_connection((address, 2323), timeout=10) as connection:
        connection.settimeout(1)
        # BusyBox telnetd announces WILL ECHO / WILL SGA / DO NAWS.
        connection.sendall(b'\xff\xfd\x01\xff\xfd\x03\xff\xfc\x1f')
        connection.sendall(line)
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        pattern = re.compile(rb'(?:^|\n)' + end.encode() + rb' ([0-9]+)\r?\n')
        delivered = None
        while time.monotonic() < deadline:
            if cancel_check:
                cancel_check()
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer.extend(chunk)
            # Backup output can be large; the terminator is always at the tail.
            match = pattern.search(buffer, max(0, len(buffer) - 512))
            if on_output:
                if delivered is None:
                    first = re.search(rb'(?:^|\n)' + start.encode() + rb'\r?\n', buffer)
                    if first:
                        delivered = first.end()
                if delivered is not None:
                    # Hold the incomplete last line so marker fragments never
                    # reach the observer. Observers must not alter framing.
                    end_offset = (match.start() + (buffer[match.start():match.start() + 1] == b'\n')
                                  if match else buffer.rfind(b'\n') + 1)
                    if end_offset > delivered:
                        on_output(bytes(buffer[delivered:end_offset]))
                        delivered = end_offset
            if match:
                first = re.search(rb'(?:^|\n)' + start.encode() + rb'\r?\n', buffer)
                if not first or int(match[1]) != 0:
                    raise RuntimeError(bytes(buffer[-4096:]).decode(errors='replace'))
                return bytes(buffer[first.end():match.start()]).replace(b'\r\n', b'\n').removesuffix(b'\r')
        raise RuntimeError('RAM command timed out or connection closed; installation stopped')


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Verify local files without accessing any device')
    parser.add_argument('--serial')
    parser.add_argument('--filesystem', choices=('ext4','btrfs'), default='ext4', help='Single-root filesystem (default: ext4)')
    parser.add_argument('--host-address', help='Host IPv4 address on the tablet USB network')
    parser.add_argument('--device-address', default='192.168.7.2')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--erase-userdata', action='store_true')
    parser.add_argument('--layout', choices=list(layout.MODES),
                        help='Use all userdata for / and /home without changing GPT')
    parser.add_argument('--restore-partition-table', type=Path, metavar='BACKUP_DIR',
                        help='Restore the partition table saved in an earlier backup directory and stop')
    parser.add_argument('--yes', action='store_true', help='Skip the interactive data-erasure confirmation')
    parser.add_argument('--allow-unverified', action='store_true', help='Explicitly test an offline-only bundle')
    return parser, parser.parse_args(argv)


def reference_plan(parser, args):
    """The local-only example is never used as the actual device's geometry."""
    try:
        return plan_for_disk(args, layout.reference_stock_disk())
    except layout.LayoutError as error:
        parser.error(str(error))


def validate_arguments(parser, args, preview=False):
    if args.restore_partition_table is not None:
        if args.layout:
            parser.error('--restore-partition-table is a separate action; --layout does not apply')
        if not preview and not args.serial:
            parser.error('--restore-partition-table requires --serial')
        return
    if not preview and not args.layout:
        parser.error('--layout linux-single is required')


def plan_for_disk(args, disk):
    """Validate and plan from the selected tablet's own partition report."""
    return layout.plan_layout(disk, args.layout, filesystem=getattr(args, 'filesystem', 'ext4'))


def confirm_installation(parser, args, plan):
    """One destructive confirmation, after reading and backing up the real disk."""
    print('Actual tablet layout (' + args.layout + '):', flush=True)
    print(plan.table(), flush=True)
    slots = boot_slots(args.layout)
    print('Boot image writes: ' + ', '.join('boot_' + slot for slot in slots)
          + '; active slot after installation: ' + slots[0].upper(), flush=True)
    print('The partition table is unchanged. All userdata, including /home, will be erased.', flush=True)
    if not args.yes:
        if not sys.stdin.isatty():
            parser.error('data erasure needs an interactive confirmation; pass --yes to confirm it')
        print(f'即将按上表安装并清空平板 {args.serial} 的对应数据。', flush=True)
        if input('Type YES to continue / 输入 YES 继续: ') != 'YES':
            parser.error('data erasure was not confirmed —— 未确认，已取消')


def load_rom_table(parser):
    """The pinned stock-ROM identities, from the repository or from the bundle."""
    here = Path(__file__).resolve().parent
    pinned = next((item for item in (here.parent / 'shared/liuqin-rom-images.json',
                                     here / 'lib/liuqin-rom-images.json',
                                     here / 'liuqin-rom-images.json') if item.is_file()), None)
    if pinned is None:
        parser.error('liuqin-rom-images.json is missing next to this program'
                     ' —— 安装包不完整，请下载同一版本的全部文件')
    return json.loads(pinned.read_text(encoding='utf-8'))


def main(argv=None):
    parser, args = parse_arguments(argv)
    bundle = args.bundle.resolve()
    artifacts = load_artifacts()
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        path = bundle/name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            parser.error('安装文件缺失、为空或为符号链接：'+name)
    files = {name: sha(bundle/name) for name in ('boot.img', 'installer.img', 'rootfs.tar.gz')}
    interface = artifacts.inspect_package(bundle)
    metadata = {}
    try:
        candidate = json.loads((bundle/'bundle.json').read_text(encoding='utf-8'))
        if isinstance(candidate, dict): metadata = candidate
    except (OSError, ValueError):
        pass
    tested = (metadata.get('device') == 'liuqin' and metadata.get('files') == files
              and metadata.get('kernel_release') == interface['kernel_release']
              and metadata.get('status') == 'DEVICE_TESTED')
    manifest = dict(device='liuqin', files=files, **interface,
                    installer_layouts=['linux-single'], single_root_boot_slot='a',
                    status='DEVICE_TESTED' if tested else 'CUSTOM_UNVERIFIED')
    if 'native_recovery_manifest' in manifest and not args.check:
        parser.error('原生 Recovery 测试版仅供 GUI 只读检查，CLI 不开放安装或分区恢复；未访问设备。')
    if args.filesystem not in manifest['filesystems']:
        parser.error('所选安装包不支持 ' + args.filesystem + '；未访问设备。')
    if not args.check:
        try:
            check_installer_support(manifest)
        except RuntimeError as error:
            parser.error(str(error))
    validate_arguments(parser, args, preview=args.check)
    if args.layout not in (None, 'linux-single'):
        parser.error('当前根系统仅支持 linux-single；未访问设备。')
    if args.check:
        validate_arguments(parser, args, preview=True)
        print('Local bundle checksums verified; no device access')
        if args.layout:
            print('256 GB reference example only; the actual tablet plan is confirmed during installation:')
            print(reference_plan(parser, args).table())
        return
    validate_arguments(parser, args)
    if manifest['status'] != 'DEVICE_TESTED' and not args.allow_unverified:
        parser.error('bundle has not passed device testing; use --allow-unverified only for attended tests'
                     ' —— 该包未通过真机验证，请勿用于正式安装')
    restore = args.restore_partition_table
    if restore is None and not all((args.serial, args.backup, args.erase_userdata)):
        parser.error('--serial, --backup, --erase-userdata and --layout are required')
    if restore is None:
        if args.backup.exists():
            parser.error('--backup must be a new directory')
        args.backup = args.backup.resolve()
        if args.backup == bundle or bundle in args.backup.parents:
            parser.error('private backups must be outside the served bundle directory')
    else:
        restore = restore.resolve()
        if not (restore / 'gpt/manifest.json').is_file():
            parser.error('the backup directory has no saved partition table: ' + str(restore))
    if args.host_address:
        socket.inet_pton(socket.AF_INET, args.host_address)
    socket.inet_pton(socket.AF_INET, args.device_address)

    def fastboot(*arguments):
        result = host.run_process([host.fastboot_path(), '-s', args.serial, *arguments], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
        return result.stdout

    for name, value in (('product', 'liuqin'), ('unlocked', 'yes')):
        if not re.search(r'\b' + name + r':\s*' + value + r'\b', fastboot('getvar', name)):
            parser.error('Fastboot device check failed: ' + name +
                         ' —— 请确认设备是小米平板 6 Pro（liuqin）且已解锁 Bootloader')

    def partition_size(name, required=True):
        match = re.search(r'partition-size:' + re.escape(name) + r':\s*(0x[0-9a-fA-F]+)',
                          fastboot('getvar', 'partition-size:' + name))
        if not match:
            if required:
                raise RuntimeError('Cannot determine partition size: ' + name)
            return None
        return int(match[1], 16)

    if restore is None:
        # The partition table is the authority once the RAM installer is up;
        # this is the cheapest identity check that runs before booting it.
        userdata_size = partition_size('userdata', required=False)
        if userdata_size is not None and userdata_size < 16 * 1024 ** 3:
            parser.error('userdata is smaller than 16 GiB; only Xiaomi Pad 6 Pro (liuqin) is supported'
                         ' —— 请确认设备为小米平板 6 Pro（liuqin），不要用于其他机型')
        # The project boot image is flashed zero-filled to the whole boot
        # partition, so every partition it goes to must be the pinned size,
        # the image must fit it, and the padded image must go over USB in one
        # raw download rather than be re-sparsed by fastboot.
        boot_bytes = boot_partition_bytes(load_rom_table(parser))
        for slot in boot_slots(args.layout):
            reported = partition_size('boot_' + slot)
            if reported != boot_bytes:
                parser.error(f'boot_{slot} is {reported} bytes, not the {boot_bytes} bytes of the pinned'
                             ' layout —— boot 分区大小与本项目验证过的设备不一致')
        largest = max((bundle / name).stat().st_size for name in ('boot.img', 'installer.img'))
        if largest > boot_bytes:
            parser.error('boot image exceeds the reported boot partition size'
                         ' —— boot 镜像大于 boot 分区，包与设备不匹配')
        match = re.search(r'max-download-size:\s*(0x[0-9a-fA-F]+|[0-9]+)',
                          fastboot('getvar', 'max-download-size'))
        if not match or int(match[1], 0) < boot_bytes:
            parser.error('the bootloader cannot receive a whole boot partition in one download'
                         ' (max-download-size ' + (match[1] if match else 'unknown') + ')'
                         ' —— Bootloader 单次下载上限小于 boot 分区')
        print('Layout: ' + args.layout)
        print('The RAM installer will read the actual layout before data-erasure confirmation.')
        if not args.yes and not sys.stdin.isatty():
            parser.error('data erasure needs an interactive confirmation; pass --yes to confirm it'
                         ' —— 非交互环境请显式加 --yes 确认清空 userdata')
    server = None
    scratch = None
    try:
        if restore is None:
            # Built before anything on the tablet changes, so that a host
            # without room for it stops the installation while it is harmless.
            # Host-side scratch, private to this run; the bundle directory is
            # served to the tablet and may be read-only.
            scratch = tempfile.TemporaryDirectory(prefix='liuqin-install-')
            padded_boot = pad_boot_image(bundle / 'boot.img', manifest['files']['boot.img'],
                                         boot_bytes, scratch.name)
            print(f'Project boot image zero-filled to the {boot_bytes}-byte boot partition:'
                  f' sha256 {padded_boot[1]}', flush=True)
        print('Booting the RAM installer...', flush=True)
        fastboot('boot', str(bundle / 'installer.img'))
        deadline = time.monotonic() + 120
        while True:
            try:
                boot_id = command(args.device_address,
                                  'test "$(cat /etc/liuqin-installer)" = liuqin && cat /proc/sys/kernel/random/boot_id').decode().strip()
                uuid.UUID(boot_id)
                break
            except (OSError, RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Installer USB channel did not become ready; no formatting performed')
                time.sleep(2)

        def remote(text, timeout=60):
            guard = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(boot_id)
            return command(args.device_address, guard + ' && ' + text, timeout)

        def device_layout(*arguments, timeout=180):
            return remote(shlex.join(['sh', '/usr/lib/liuqin/install-layout.sh', boot_id, *arguments]),
                          timeout).decode()

        cmdline = shlex.split(remote('cat /proc/cmdline').decode())
        serial = next((item.split('=', 1)[1] for item in cmdline if item.startswith('androidboot.serialno=')), '')
        if serial != args.serial:
            raise RuntimeError('RAM device serial does not match the selected Fastboot device'
                               ' —— 序列号不一致，请检查 --serial 参数')
        release = remote('uname -r').decode().strip()
        if remote('cat /etc/liuqin-installer-interface').decode().strip() != manifest['installer_interface']:
            raise RuntimeError('RAM 安装环境接口不匹配')
        if release != manifest.get('installer_kernel_release', manifest['kernel_release']):
            raise RuntimeError('RAM 内核与选定的 installer.img 不匹配')
        if restore is not None:
            restore_partition_table(remote, device_layout, restore, args.serial)
            remote("(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &")
            print('Stock partition table restored. The tablet is returning to Fastboot.')
            return
        if not args.host_address:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((args.device_address, 2323))
                args.host_address = route.getsockname()[0]
        server = http.server.ThreadingHTTPServer(server_address(args.host_address), bundle_handler(bundle))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        backups = {}
        args.backup.mkdir(mode=0o700, parents=True)
        for name in ('boot_a', 'boot_b', 'persist'):
            backups[name + '.img'] = backup_partition(remote, args.backup, name)
        (args.backup / 'SHA256SUMS').write_text(''.join(f'{h}  {n}\n' for n, h in backups.items()), encoding='utf-8', newline='\n')
        backup_partition_table(device_layout, args.backup, args.serial)
        disk = layout.parse_print(device_layout('report', ANCHOR))
        state = layout.classify(disk)
        print('Partition table state: ' + state)
        plan = plan_for_disk(args, disk)
        confirm_installation(parser, args, plan)
        # Re-read after the user has reviewed the plan. No size is compared to
        # a 256 GB reference; every boundary must still equal the confirmed one.
        current_disk = layout.parse_print(device_layout('report', ANCHOR))
        confirm_plan(plan, plan_for_disk(args, current_disk))
        base = f'http://{args.host_address}:{server.server_port}'
        target_contract = None
        contract = artifacts.boot_interface(padded_boot[0])['contract']
        digest = hashlib.sha256(contract).hexdigest()
        if digest != manifest['root_contract_sha256']:
            raise RuntimeError('target boot contract changed; no formatting performed')
        stage_blob(remote, 'target-root', contract)
        path = LAYOUT_WORK + '/target-root.contract'
        remote('/bin/busybox base64 -d ' + LAYOUT_WORK + '/liuqin-gpt-target-root.b64 >' + path +
               ' && chmod 0644 ' + path)
        target_contract = (path, digest)
        install = install_root_command(boot_id, base, bundle, manifest, args,
                                       target_contract=target_contract)
        print('Installing Linux into userdata.', flush=True)
        result = remote(shlex.join(install), 3600)
        if b'liuqin-install: ROOT_INSTALLED' not in result:
            raise RuntimeError('Device did not confirm root installation')
        remote("(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            devices = host.run_process([host.fastboot_path(), 'devices'], capture_output=True, text=True, timeout=10, check=True).stdout
            if any(line.split()[0] == args.serial for line in devices.splitlines() if line.split()):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Return to Fastboot not observed; boot partition was not flashed')
        finish_in_fastboot(fastboot, args.layout, *padded_boot, boot_bytes)
        print('Installation commands completed. First-boot verification is still required.')
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if scratch:
            scratch.cleanup()


def boot_slots(mode):
    if mode != 'linux-single':
        raise layout.LayoutError('unknown layout mode: ' + str(mode))
    return (LINUX_BOOT_SLOT,)


def backup_partition(remote, directory, name):
    """Save and verify the entire original partition before a possible write."""
    print('Backing up and verifying ' + name + '...', flush=True)
    device = '/dev/disk/by-partlabel/' + name
    content = remote('test -b ' + device + ' && /bin/busybox base64 ' + device, 600)
    target = directory / (name + '.img')
    target.write_bytes(base64.b64decode(content, validate=False))
    target.chmod(0o600)
    expected = remote('/bin/busybox sha256sum ' + device, 120).decode().split()[0]
    if sha(target) != expected:
        raise RuntimeError('Backup verification failed: ' + name)
    return expected


def finish_in_fastboot(fastboot, mode, padded_image, padded_sha256, partition_bytes):
    """Write the verified, zero-filled single-root image and select slot A."""
    slots = boot_slots(mode)
    if padded_image.stat().st_size != partition_bytes or sha(padded_image) != padded_sha256:
        raise RuntimeError('the zero-filled boot image changed before it was flashed; boot_a was not written')
    fastboot('flash', 'boot_' + slots[0], str(padded_image))
    fastboot('--set-active=' + slots[0])
    fastboot('reboot')


def install_root_command(boot_id, base_url, bundle, manifest, args, *, target_contract=None):
    """Build the single-root request for the versioned RAM installer interface."""
    install = ['sh', '/usr/lib/liuqin/install-root.sh', boot_id, base_url + '/rootfs.tar.gz',
               manifest['files']['rootfs.tar.gz'], str((bundle / 'rootfs.tar.gz').stat().st_size),
               'ERASE-LIUQIN-USERDATA', 'userdata', '-']
    if target_contract:
        install += ['ROOT-CONTRACT', *target_contract]
    if getattr(args, 'filesystem', 'ext4') != 'ext4':
        install += ['FILESYSTEM', args.filesystem]
    return install


def bundle_handler(bundle):
    """Serve the selected bundle directory."""
    return functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(bundle))


def confirm_plan(expected, actual):
    """What was confirmed and what will be written must be the same decision.

    Both plans use the tablet's actual geometry. Re-reading after confirmation
    must not change any partition, boundary, action, or disk size.
    """
    def signature(plan):
        disk = plan.disk
        return (plan.mode, disk.sector_size, disk.total_sectors,
                disk.first_usable, disk.last_usable, disk.entry_count,
                tuple((p.number, p.first_sector, p.last_sector, p.name) for p in disk.partitions),
                tuple((e.number, e.name, e.start, e.end, e.action) for e in plan.entries))
    if signature(expected) != signature(actual):
        raise RuntimeError('the tablet partition table or plan changed after confirmation; '
                           'nothing was written —— 确认后分区表或安装方案发生变化，未写入')


def backup_partition_table(device_layout, directory, serial):
    """Save both GPT copies of the disk before any edit, with a hash manifest."""
    print('Backing up the partition table...', flush=True)
    target = directory / 'gpt'
    target.mkdir(mode=0o700)
    report = device_layout('report', ANCHOR)
    (target / 'sgdisk-p.txt').write_text(report, encoding='utf-8', newline='\n')
    geometry = dict(line.split(None, 1) for line in device_layout('geometry', ANCHOR).splitlines() if line)
    copies = {}
    for half in ('head', 'tail'):
        data = base64.b64decode(device_layout('backup-gpt', ANCHOR, half), validate=False)
        expected = 6 * int(geometry['sector'])
        if len(data) != expected:
            raise RuntimeError(f'the {half} GPT copy is {len(data)} bytes, not {expected}')
        path = target / f'sda-{half}.bin'
        path.write_bytes(data)
        path.chmod(0o600)
        copies[path.name] = hashlib.sha256(data).hexdigest()
    saved = {'serial': serial, 'disk': geometry['disk'], 'sector_size': int(geometry['sector']),
             'sectors': int(geometry['sectors']), 'files': copies}
    (target / 'manifest.json').write_text(json.dumps(saved, indent=2) + '\n', encoding='utf-8', newline='\n')
    print('Partition table saved to ' + str(target))
    return target


# The installer RAM image is built from a fixed directory list that has no
# /tmp, so anything the host writes on the device goes to the same scratch
# directory device/installer/install-layout.sh reads from, and the host creates it
# before writing into it rather than assuming it is already there.
LAYOUT_WORK = '/run/liuqin-layout'


def stage_blob(remote, name, data):
    """Push a small verified blob to the RAM image in base64 chunks."""
    remote('mkdir -p ' + shlex.quote(LAYOUT_WORK))
    path = LAYOUT_WORK + '/liuqin-gpt-' + name + '.b64'
    text = base64.b64encode(data).decode()
    remote(':>' + path)
    # Each chunk is one RAM shell line, which command() bounds by ash's
    # line-editing buffer (RAM_SHELL_LINE_BYTES).  500 base64 characters plus
    # the path, the boot_id guard and the markers stay well inside it.
    for index in range(0, len(text), 500):
        remote('printf %s ' + shlex.quote(text[index:index + 500]) + ' >>' + path)
    remote('printf "\\n" >>' + path)


def restore_partition_table(remote, device_layout, directory, serial):
    """Restore the saved stock table, refusing any dump that is not the saved one."""
    saved = json.loads((directory / 'gpt/manifest.json').read_text(encoding='utf-8'))
    if saved.get('serial') != serial:
        raise RuntimeError('this backup was taken from serial ' + str(saved.get('serial')) +
                           ', not ' + serial + ' —— 拒绝把其他设备的分区表写入本机')
    geometry = dict(line.split(None, 1) for line in device_layout('geometry', ANCHOR).splitlines() if line)
    if int(geometry['sectors']) != saved['sectors'] or int(geometry['sector']) != saved['sector_size']:
        raise RuntimeError('the tablet geometry differs from the saved partition table')
    for name, digest in saved['files'].items():
        path = directory / 'gpt' / name
        if not path.is_file() or sha(path) != digest:
            raise RuntimeError('the saved partition table does not match its manifest: ' + name +
                               ' —— 备份已损坏或被改动，拒绝写入')
        stage_blob(remote, name.split('-')[1].split('.')[0], path.read_bytes())
    print('Restoring the saved partition table...', flush=True)
    if 'GPT_RESTORED' not in device_layout('restore-gpt', ANCHOR):
        raise RuntimeError('the device did not confirm the partition-table restore')


if __name__ == '__main__':
    host.configure_stdio()
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, layout.LayoutError) as error:
        sys.exit('Installation stopped: ' + str(error))
