# SPDX-License-Identifier: MIT
"""Qt-free installation engine with a deliberate prepare/commit boundary."""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from . import artifacts, backups, host

INSTALLER_DIR = Path(__file__).resolve().parents[1]
DEVICE_SCRIPTS = INSTALLER_DIR.parent / 'device/installer'
spec = importlib.util.spec_from_file_location('liuqin_cli', INSTALLER_DIR / 'install-liuqin.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
from liuqin_filesystems import validate as validate_filesystem

FILES = ('installer.img', 'boot.img', 'rootfs.tar.gz')
PHASES = {
    'validate_bundle': '校验安装包', 'probe_device': '识别平板',
    'boot_installer': '临时启动安装环境', 'check_hardware': '读取真实存储与硬件信息',
    'backup': '备份并核验原始分区', 'verify_backup': '核验已有设备备份',
    'revalidate': '复核已确认的方案',
    'transfer': '向平板发送系统归档', 'verify_archive': '校验平板上的系统归档',
    'format_root': '格式化 Linux 根分区', 'extract_root': '解压系统文件',
    'configure_device': '配置本机固件与校准信息', 'verify_root': '检查系统启动文件',
    'sync_root': '同步并卸载文件系统', 'return_fastboot': '返回 Fastboot',
    'flash_boot': '写入 A 槽引导', 'verify_boot': '回读并校验引导分区',
    'reboot': '选择 A 槽并启动 Linux', 'cancel': '结束准备并返回 Fastboot',
}


class InstallerError(RuntimeError):
    pass


class Cancelled(InstallerError):
    pass


class Parser:
    def error(self, message):
        raise InstallerError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest_file(path, observe=None, check=None):
    digest, count = hashlib.sha256(), 0
    with Path(path).open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            if check:
                check()
            digest.update(chunk)
            count += len(chunk)
            if observe:
                observe(count)
    return digest.hexdigest()


def quiet(function, *args):
    # Legacy helpers print human-readable messages. Worker events use a saved
    # stream, so redirecting these messages cannot mix them into JSON stdout.
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*args)


class Transport:
    """All hardware access is contained here; the demo never constructs this."""
    def __init__(self, engine, serial):
        self.engine, self.serial, self.boot_id = engine, serial, None
        try:
            self.fastboot_path = host.fastboot_path()
        except OSError as error:
            raise InstallerError(str(error)) from error
        self.ram_active = False

    def fastboot(self, *arguments, timeout=1800):
        result = host.run_process([self.fastboot_path, '-s', self.serial, *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout)
        if result.returncode:
            raise InstallerError('Fastboot 操作失败：' + result.stdout[-2500:])
        return result.stdout

    def getvar(self, name):
        output = self.fastboot('getvar', name, timeout=20)
        match = re.search(r'(?:^|\n)(?:\(bootloader\)\s*)?' + re.escape(name) +
                          r':\s*([^\r\n]+)', output)
        if not match:
            raise InstallerError('无法读取 Fastboot 属性：' + name)
        return match[1].strip()

    def check_device(self):
        values = {name: self.getvar(name) for name in ('product', 'unlocked', 'current-slot')}
        if values['product'] != 'liuqin' or values['unlocked'] != 'yes':
            raise InstallerError('仅支持已解锁的小米平板 6 Pro（liuqin）。')
        if values['current-slot'] not in ('a', 'b'):
            raise InstallerError('无法确认当前启动槽位。')
        size = cli.boot_partition_bytes(quiet(cli.load_rom_table, Parser()))
        if int(self.getvar('partition-size:boot_a'), 0) != size:
            raise InstallerError('boot_a 大小与项目要求不一致。')
        if int(self.getvar('max-download-size'), 0) < size:
            raise InstallerError('Bootloader 单次下载上限不足。')
        return dict(values, serial=self.serial, boot_bytes=size)

    def boot_ram(self):
        self.boot_id = None
        image = self.engine.installer_snapshot
        if digest_file(image) != self.engine.manifest['files']['installer.img']:
            raise InstallerError('临时安装镜像校验不再匹配。')
        self.fastboot('boot', str(image))
        # The boot command changed the device state even if the network fails.
        self.ram_active = True
        deadline = time.monotonic() + 120
        last = ''
        if host.IS_WINDOWS:
            self.engine.emit('notice', message='正在等待平板 USB 网卡；首次使用请点击下方“设置 USB 网络”，允许管理员设置后继续。')
        while time.monotonic() < deadline:
            self.engine.check_cancel()
            try:
                if host.IS_WINDOWS:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                        route.connect(('192.168.7.2', 2323))
                        host.server_address(route.getsockname()[0])
                value = cli.command('192.168.7.2',
                                    'test "$(cat /etc/liuqin-installer)" = liuqin && '
                                    'cat /proc/sys/kernel/random/boot_id', timeout=10,
                                    cancel_check=self.engine.check_cancel).decode().strip()
                uuid.UUID(value)
                self.boot_id = value
                break
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                last = str(exc)
                self.engine.pause(1)
        else:
            raise InstallerError('USB 安装通道未就绪；未格式化。' + last[-300:])
        cmdline = shlex.split(self.remote('cat /proc/cmdline').decode())
        serial = next((p.split('=', 1)[1] for p in cmdline
                       if p.startswith('androidboot.serialno=')), '')
        if serial != self.serial:
            self.boot_id = None  # Never send a cleanup reboot to another tablet.
            raise InstallerError('RAM 环境序列号与选定平板不一致。')
        self.verify_ram_interface()

    def verify_ram_interface(self):
        manifest = self.engine.manifest
        if self.remote('cat /etc/liuqin-installer-interface').decode().strip() != manifest['installer_interface']:
            raise InstallerError('RAM 安装环境接口与选定 installer.img 不匹配。')
        if self.remote('uname -r').decode().strip() != manifest['installer_kernel_release']:
            raise InstallerError('RAM 内核与选定 installer.img 不匹配。')

    def remote(self, text, timeout=60, observe=None):
        if not self.boot_id:
            raise InstallerError('没有经过验证的 RAM 会话。')
        guard = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(self.boot_id)
        return cli.command('192.168.7.2', guard + ' && ' + text, timeout,
                           on_output=observe, cancel_check=self.engine.check_cancel)

    def device_layout(self, *args):
        command = shlex.join(['sh', '/usr/lib/liuqin/install-layout.sh', self.boot_id, *args])
        return self.remote(command, 180).decode()

    def report(self):
        return self.device_layout('report', cli.ANCHOR)

    def preflight(self, require_archive=True):
        battery = self.remote('for p in /sys/class/power_supply/*; do '
                              '[ "$(cat "$p/type" 2>/dev/null)" = Battery ] || continue; '
                              'cat "$p/capacity"; break; done; true').decode().strip()
        available = self.remote("awk '/^MemAvailable:/ {print $2}' /proc/meminfo").decode().strip()
        if not battery.isdigit() or int(battery) < 30:
            raise InstallerError('无法读取电量或电量不足 30%。')
        if not available.isdigit() or (require_archive and int(available) * 1024 <= self.engine.rootfs_bytes + 536870912):
            raise InstallerError('当前可用内存不足以暂存系统归档。')
        checks = ('test "$(cat /sys/class/block/sda/queue/logical_block_size)" = 4096 && '
                  'test -b /dev/disk/by-partlabel/userdata && '
                  'test "$(/bin/busybox blockdev --getro /dev/disk/by-partlabel/userdata)" = 1')
        self.remote(checks)
        return {'battery': int(battery), 'available_memory_bytes': int(available) * 1024}

    def backup(self, directory):
        sums = {}
        for name in ('boot_a', 'boot_b', 'persist'):
            self.engine.check_cancel()
            self.engine.phase('backup', detail=name)
            sums[name + '.img'] = quiet(cli.backup_partition, self.remote, directory, name)
        (directory / 'SHA256SUMS').write_text(''.join(f'{h}  {n}\n' for n, h in sums.items()), encoding='utf-8', newline='\n')
        quiet(cli.backup_partition_table, self.device_layout, directory, self.serial)
        return sums

    def validate_calibration(self):
        """Run the image's own provisioner into RAM, never the installed root."""
        base = '/run/liuqin-gui/calibration-probe-' + uuid.uuid4().hex
        source, target = base + '/persist', base + '/root'
        mounted = False
        try:
            self.remote('test "$(/bin/busybox blockdev --getro /dev/disk/by-partlabel/persist)" = 1 && '
                        'mkdir -p ' + source + ' ' + target + '/etc')
            self.remote('mount -t ext4 -o ro,noload /dev/disk/by-partlabel/persist ' + source)
            mounted = True
            self.remote("printf '%s\\n' 'fastrpc:x:1:1::/:/bin/false' >" + target + "/etc/passwd; "
                        "printf '%s\\n' 'fastrpc:x:1:' >" + target + '/etc/group; '
                        ':>' + target + '/etc/liuqin-native-root')
            # The target's account files are throwaway RAM fixtures. No target
            # or source path here is controlled by the GUI or points at UFS RW.
            result = self.remote('PERSIST_SRC=' + source +
                                 ' sh /usr/lib/liuqin/provision.sh ' + target, 120)
            if b'provision-liuqin-from-persist: PASS' not in result:
                raise InstallerError('当前设备校准数据未通过只读检查。')
            return True
        finally:
            # Never recursively delete a still-mounted source directory.
            if mounted:
                self.remote('umount ' + source)
            self.remote('rm -rf ' + base)

    def verify_backup(self, record):
        current = self.remote('/bin/busybox sha256sum /dev/disk/by-partlabel/persist', 180).decode().split()[0]
        result = backups.assert_compatible(record, self.engine.device, self.report(), current, cli.layout)
        result['calibration_usable'] = self.validate_calibration()
        return result

    def install_root(self, padded_image):
        engine = self.engine
        try:
            contract = artifacts.boot_interface(padded_image)['contract']
        except (ValueError, OSError) as error:
            raise InstallerError('安装前发现目标设备接口发生变化，未执行写入。') from error
        contract_digest = hashlib.sha256(contract).hexdigest()
        if contract_digest != engine.manifest['root_contract_sha256']:
            raise InstallerError('待写入 boot.img 的设备接口发生变化；未格式化。')
        script = (DEVICE_SCRIPTS / 'install-root.sh').read_bytes()
        token = uuid.uuid4().hex
        routes = {'/' + token + '/rootfs.tar.gz': engine.bundle / 'rootfs.tar.gz',
                  '/' + token + '/install-root.sh': script,
                  '/' + token + '/target-root.contract': contract}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(('192.168.7.2', 2323))
            address = route.getsockname()[0]
        if address in ('0.0.0.0', '127.0.0.1'):
            raise InstallerError('没有可用的平板 USB 网络地址。')
        transport = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                item = routes.get(self.path)
                if self.client_address[0] != '192.168.7.2' or item is None:
                    self.send_error(404)
                    return
                size = len(item) if isinstance(item, bytes) else item.stat().st_size
                self.send_response(200)
                self.send_header('Content-Length', str(size))
                self.end_headers()
                stream = io.BytesIO(item) if isinstance(item, bytes) else item.open('rb')
                sent, last = 0, 0.0
                try:
                    with stream:
                        while chunk := stream.read(256 * 1024):
                            self.wfile.write(chunk)
                            sent += len(chunk)
                            now = time.monotonic()
                            if not isinstance(item, bytes) and (now - last > .3 or sent == size):
                                engine.progress('transfer', sent, size, unit='sent_bytes')
                                last = now
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Device-side hash/command result determines success.

        server = http.server.ThreadingHTTPServer(host.server_address(address), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://{address}:{server.server_port}/{token}'
        directory = '/run/liuqin-gui/' + token
        target = directory + '/install-root.sh'
        contract_target = directory + '/target-root.contract'
        try:
            self.remote('mkdir -p ' + directory + ' && /bin/busybox wget -O ' + target +
                        ' ' + shlex.quote(base + '/install-root.sh'), 120)
            self.remote('printf "%s  %s\\n" ' + hashlib.sha256(script).hexdigest() + ' ' +
                        target + ' | /bin/busybox sha256sum -c -')
            self.remote('/bin/busybox wget -O ' + contract_target + ' ' +
                        shlex.quote(base + '/target-root.contract'), 120)
            self.remote('chmod 0644 ' + contract_target + '; printf "%s  %s\\n" ' +
                        contract_digest + ' ' + contract_target + ' | /bin/busybox sha256sum -c -')
            fields = ['sh', target, self.boot_id, base + '/rootfs.tar.gz',
                      engine.manifest['files']['rootfs.tar.gz'], str(engine.rootfs_bytes),
                      'ERASE-LIUQIN-USERDATA', 'userdata', '-',
                      'ROOT-CONTRACT', contract_target, contract_digest]
            fields += ['FILESYSTEM', engine.filesystem]
            pending = bytearray()

            def observe(chunk):
                pending.extend(chunk)
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending[:] = rest
                    match = re.fullmatch(rb'liuqin-gui: phase=([a-z_]+)\r?', line)
                    if match and match[1].decode() in PHASES:
                        engine.phase(match[1].decode())

            # Conservatively record that a destructive operation may run before
            # dispatch, not after receiving an asynchronously buffered marker.
            engine.receipt['write_dispatched'] = True
            engine.save_receipt()
            result = self.remote(shlex.join(fields), 3600, observe=observe)
            if b'liuqin-install: ROOT_INSTALLED' not in result:
                raise InstallerError('平板未确认根系统安装完成。')
        finally:
            server.shutdown()
            server.server_close()

    def return_fastboot(self):
        self.remote('(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.engine.check_cancel()
            result = host.run_process([self.fastboot_path, 'devices'], capture_output=True,
                                    text=True, timeout=10, check=True)
            if any(line.split()[0] == self.serial for line in result.stdout.splitlines() if line.split()):
                self.ram_active = False
                return
            self.engine.pause(1)
        raise InstallerError('未观察到返回 Fastboot，请保持现有分区，查看执行记录。')

    def flash_verify_reboot(self, padded, digest, size):
        engine = self.engine
        if padded.stat().st_size != size or digest_file(padded) != digest:
            raise InstallerError('待写引导镜像发生变化。')
        engine.phase('return_fastboot')
        self.return_fastboot()
        self.check_device()
        engine.phase('flash_boot')
        engine.receipt['boot_flash_started'] = True
        engine.save_receipt()
        self.fastboot('flash', 'boot_a', str(padded))
        engine.receipt['boot_written'] = True
        engine.save_receipt()
        engine.phase('verify_boot')
        self.boot_ram()
        observed = self.remote('/bin/busybox sha256sum /dev/disk/by-partlabel/boot_a', 180).decode().split()[0]
        if observed != digest:
            raise InstallerError('boot_a 回读校验失败；未将首次启动标记为成功。')
        engine.receipt['boot_readback_verified'] = True
        engine.save_receipt()
        self.return_fastboot()
        engine.phase('reboot')
        self.fastboot('--set-active=a')
        engine.receipt['active_slot_set'] = True
        engine.save_receipt()
        self.fastboot('reboot')

    def cancel_cleanup(self):
        if self.boot_id and self.ram_active:
            # Cancellation applies to read-only preparation. Let cleanup use
            # the verified session even though the cancellation flag is set.
            self.engine.cancel_event.clear()
            self.return_fastboot()


class Engine:
    simulation = False
    def __init__(self, emit, state_dir, transport_factory=Transport):
        self.emit_callback, self.state_dir = emit, Path(state_dir).resolve()
        self.transport_factory = transport_factory
        self.state = 'idle'
        self.cancel_event = threading.Event()
        self.committing = False
        self.session_id, self.plan_id = '', ''
        self.bundle = self.manifest = self.transport = self.plan = None
        self.filesystem = 'ext4'
        self.receipt = {}
        self.scratch = None
        self.log_lock = threading.RLock()
        self.request_id = ''
        self.device_lock = None

    def lock_device(self, serial):
        if self.simulation:
            return
        directory = host.device_lock_directory()
        name = hashlib.sha256(serial.encode()).hexdigest() + '.lock'
        try:
            self.device_lock = host.FileLock(directory / name, blocking=False)
        except OSError as error:
            raise InstallerError('另一份安装程序正在操作此平板，请先结束那个会话。') from error

    def release_device(self):
        if self.device_lock is not None:
            self.device_lock.close()
            self.device_lock = None

    def emit(self, event, **values):
        record = dict(type=event, session_id=self.session_id, request_id=self.request_id,
                      simulation=self.simulation, **values)
        with self.log_lock:
            if self.receipt.get('run_dir'):
                path = Path(self.receipt['run_dir']) / 'events.jsonl'
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            self.emit_callback(record)

    def set_state(self, state):
        self.state = state
        self.emit('state', state=state, can_cancel=state in ('preparing', 'backing_up'),
                  can_close=state not in ('preparing', 'backing_up', 'prepared', 'installing'))

    def phase(self, phase, detail=''):
        self.current_phase = phase
        self.emit('phase', phase=phase, message=PHASES[phase], detail=detail, progress=None)
        if self.receipt:
            self.receipt['phase'] = phase
            self.save_receipt()

    def progress(self, phase, completed, total, unit='bytes'):
        self.emit('progress', phase=phase, completed=completed, total=total,
                  progress=completed / total if total else None, unit=unit)

    def save_receipt(self):
        if not self.receipt.get('run_dir'):
            return
        path = Path(self.receipt['run_dir']) / 'receipt.json'
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.receipt, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def check_cancel(self):
        if self.cancel_event.is_set() and not self.committing:
            raise Cancelled('用户取消准备。')

    def pause(self, seconds):
        if self.committing:
            time.sleep(seconds)
        else:
            self.cancel_event.wait(seconds)
            self.check_cancel()

    def list_devices(self):
        try:
            executable = host.fastboot_path()
        except OSError as error:
            raise InstallerError(str(error)) from error
        result = host.run_process([executable, 'devices', '-l'], capture_output=True,
                                text=True, timeout=15, check=True)
        devices = [{'serial': fields[0], 'description': ' '.join(fields[1:])}
                   for line in result.stdout.splitlines()
                   if len(fields := line.split()) >= 2 and fields[1] == 'fastboot']
        self.emit('devices', devices=devices)

    def validate_bundle(self, directory):
        self.phase('validate_bundle')
        bundle = Path(directory).expanduser().resolve()
        manifest_path = bundle / 'bundle.json'
        # Metadata is optional. The three actual artifacts define the package.
        metadata = {}
        if manifest_path.is_file() and not manifest_path.is_symlink() and manifest_path.stat().st_size <= 1024 * 1024:
            try:
                value = json.loads(manifest_path.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    metadata = value
            except (ValueError, UnicodeError):
                pass
        total = 0
        for name in FILES:
            path = bundle / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise InstallerError('安装包文件缺失、为空或为符号链接：' + name)
            total += path.stat().st_size
        completed = 0
        hashes = {}
        for name in FILES:
            path = bundle / name
            last = [0.0]

            def observe(count):
                now = time.monotonic()
                if now - last[0] > .2 or count == path.stat().st_size:
                    self.progress('validate_bundle', completed + count, total)
                    last[0] = now

            hashes[name] = digest_file(path, observe, self.check_cancel)
            completed += path.stat().st_size
        # Reuse structural inspection only while every artifact hash is unchanged.
        cached = getattr(self, '_artifact_cache', None)
        if cached and cached[0] == hashes:
            interface = cached[1]
        else:
            self.emit('notice', message='正在检查内核和根文件系统接口，首次检查可能需要几分钟。')
            try:
                interface = artifacts.inspect_package(bundle, self.check_cancel)
            except (artifacts.ArtifactError, ValueError, OSError, EOFError) as error:
                raise InstallerError(str(error)) from error
            self._artifact_cache = (hashes, interface)
        tested = (metadata.get('device') == 'liuqin' and metadata.get('files') == hashes
                  and metadata.get('kernel_release') == interface['kernel_release']
                  and metadata.get('status') == 'DEVICE_TESTED')
        manifest = dict(device='liuqin', files=hashes, **interface,
                        installer_layouts=['linux-single'], single_root_boot_slot='a',
                        status='DEVICE_TESTED' if tested else 'CUSTOM_UNVERIFIED')
        self.bundle, self.manifest = bundle, manifest
        self.manifest_digest = hashlib.sha256(canonical(manifest)).hexdigest()
        self.rootfs_bytes = (bundle / 'rootfs.tar.gz').stat().st_size
        self.emit('bundle', valid=True, path=str(bundle), kernel=manifest['kernel_release'],
                  filesystems=manifest['filesystems'],
                  host_compatible=not host.IS_WINDOWS or self.simulation or manifest.get('installer_usb_network') == 'rndis',
                  status=manifest.get('status', 'UNKNOWN'), layouts=['linux-single'],
                  experimental=manifest.get('status') != 'DEVICE_TESTED')

    def guard_operation(self):
        if self.state in ('preparing', 'backing_up', 'prepared', 'installing'):
            raise InstallerError('请先结束当前会话。')
        if self.state == 'failed' and self.receipt.get('write_dispatched'):
            raise InstallerError('上次写入未完成，请先检查执行记录和平板状态，再结束此会话。')

    def require_serial(self, payload):
        serial = payload.get('serial', '')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', serial):
            raise InstallerError('请选择有效的设备序列号。')
        return serial

    def load_backup(self, directory, serial):
        try:
            return backups.load(directory, serial, self.simulation, cli.layout, self.check_cancel)
        except (backups.BackupError, OSError, ValueError, cli.layout.LayoutError) as exc:
            raise InstallerError(str(exc)) from exc

    def remember_backup(self, record):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.state_dir / 'backups-index.json'
        with host.FileLock(self.state_dir / 'backups-index.lock'):
            try:
                index = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            except (OSError, ValueError):
                index = {}
            if not isinstance(index, dict):
                index = {}
            index[('demo:' if self.simulation else 'real:') + record['serial']] = record['path']
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
            temporary.replace(path)

    def backup_candidates(self, serial, parent):
        index_path = self.state_dir / 'backups-index.json'
        try:
            index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
        except (OSError, ValueError):
            index = {}
        if not isinstance(index, dict):
            index = {}
        paths = []
        saved = index.get(('demo:' if self.simulation else 'real:') + serial)
        if saved:
            paths.append(Path(saved))
        root = Path(parent).expanduser().resolve()
        if root.is_dir():
            paths.append(root)
            paths += sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:512]
        found, seen = [], set()
        for path in paths:
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            try:
                metadata = backups.json_file(path / 'gpt/manifest.json')
                if metadata.get('serial') == serial:
                    found.append(str(path))
            except (OSError, ValueError, backups.BackupError):
                continue
        return found

    def emit_backup(self, record, source):
        info = {key: record[key] for key in ('path', 'serial', 'simulation', 'backup_id',
                                            'created_at', 'valid_local', 'legacy')}
        self.emit('backup_ready', backup=dict(info, source=source))

    def verify_current_backup(self, record):
        try:
            result = self.transport.verify_backup(record)
            if result.get('calibration_usable') is not True:
                raise InstallerError('当前设备校准数据未通过检查。')
            return result
        except backups.BackupError as exc:
            raise InstallerError(str(exc)) from exc

    def select_backup(self, payload):
        self.guard_operation()
        serial = self.require_serial(payload)
        self.cancel_event.clear()
        self.phase('verify_backup')
        record = self.load_backup(payload['backup_dir'], serial)
        self.remember_backup(record)
        self.emit_backup(record, 'selected')
        return record

    def find_backup(self, payload):
        self.guard_operation()
        serial = self.require_serial(payload)
        self.cancel_event.clear()
        candidates = self.backup_candidates(serial, payload['backup_parent'])
        self.emit('backups', serial=serial, candidates=candidates)
        if candidates:
            self.phase('verify_backup')
            for candidate in candidates:
                try:
                    record = self.load_backup(candidate, serial)
                except InstallerError as exc:
                    self.emit('backup_rejected', message=str(exc))
                    continue
                self.remember_backup(record)
                self.emit_backup(record, 'reused')
                return
            self.emit('backup_unavailable', serial=serial)
        else:
            self.emit('backup_unavailable', serial=serial)

    def begin_run(self, payload, operation):
        self.cancel_event.clear()
        self.committing = False
        self.receipt = {}
        self.session_id = uuid.uuid4().hex
        self.plan_id = ''
        self.transport = None
        self.validate_bundle(payload.get('bundle', ''))
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir == self.bundle or self.bundle in self.state_dir.parents:
            raise InstallerError('运行记录不能存入安装包目录。')
        if shutil.disk_usage(self.state_dir).free < 256 * 1024**2:
            raise InstallerError('运行目录可用空间不足 256MiB。')
        directory = self.state_dir / 'runs' / self.session_id
        directory.mkdir(mode=0o700, parents=True)
        serial = self.require_serial(payload)
        self.receipt = dict(schema_version=2, simulation=self.simulation, serial=serial,
                            session_id=self.session_id, run_dir=str(directory), operation=operation,
                            bundle=str(self.bundle), layout='linux-single', slot='a',
                            created_at=datetime.now(timezone.utc).isoformat(),
                            write_dispatched=False, root_installed=False,
                            boot_flash_started=False, boot_written=False,
                            boot_readback_verified=False, active_slot_set=False,
                            first_boot_verified=False)
        self.save_receipt()
        self.set_state('backing_up' if operation == 'backup' else 'preparing')

    def connect_ram(self, require_archive=True):
        if not self.simulation:
            try:
                host.check_installer_support(self.manifest)
            except RuntimeError as error:
                raise InstallerError(str(error)) from error
        serial = self.receipt['serial']
        self.transport = self.transport_factory(self, serial)
        self.lock_device(serial)
        self.phase('probe_device')
        self.device = self.transport.check_device()
        self.scratch = tempfile.TemporaryDirectory(prefix='run-', dir=self.state_dir)
        self.installer_snapshot = Path(self.scratch.name) / 'installer.img'
        shutil.copyfile(self.bundle / 'installer.img', self.installer_snapshot)
        if digest_file(self.installer_snapshot) != self.manifest['files']['installer.img']:
            raise InstallerError('复制临时安装镜像时校验不匹配。')
        if require_archive:
            self.padded, self.padded_digest = cli.pad_boot_image(
                self.bundle / 'boot.img', self.manifest['files']['boot.img'],
                self.device['boot_bytes'], self.scratch.name)
        self.phase('boot_installer')
        self.transport.boot_ram()
        self.phase('check_hardware')
        self.device.update(self.transport.preflight(require_archive=require_archive))
        self.report_text = self.transport.report()
        disk = cli.layout.parse_print(self.report_text)
        cli.layout.check_geometry(disk)
        self.device['userdata_bytes'] = disk.by_name('userdata').size
        if require_archive and self.manifest['expanded_bytes'] + 512 * 1024 * 1024 > self.device['userdata_bytes']:
            raise InstallerError('根文件系统解压后的大小超过 userdata 可用容量。')
        self.emit('device', device=self.device)
        return disk

    def create_backup(self, payload):
        self.guard_operation()
        serial = self.require_serial(payload)
        if payload.get('force_new') is not True:
            candidates = self.backup_candidates(serial, payload['backup_parent'])
            if candidates:
                self.cancel_event.clear()
                self.phase('verify_backup')
                for candidate in candidates:
                    try:
                        record = self.load_backup(candidate, serial)
                    except InstallerError:
                        continue
                    self.remember_backup(record)
                    self.emit_backup(record, 'reused')
                    return
                raise InstallerError('已有备份均未通过核验。请选择有效备份，或点击“另建备份”；旧文件会保留。')
        self.begin_run(payload, 'backup')
        parent = Path(payload['backup_parent']).expanduser().resolve()
        if parent == self.bundle or self.bundle in parent.parents:
            raise InstallerError('私有备份不能存入安装包目录。')
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(parent).free < 768 * 1024**2:
            raise InstallerError('备份位置可用空间不足 768MiB。')
        self.connect_ram(require_archive=False)
        directory = parent / (serial + '-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + self.session_id[:8])
        directory.mkdir(mode=0o700)
        self.receipt['backup_dir'] = str(directory)
        self.save_receipt()
        self.phase('backup')
        self.transport.backup(directory)
        self.check_cancel()
        self.phase('verify_backup')
        record = backups.publish(directory, serial, self.simulation, cli.layout)
        self.remember_backup(record)
        self.receipt.update(backup_verified=True, backup_id=record['backup_id'])
        self.save_receipt()
        self.phase('return_fastboot')
        self.transport.cancel_cleanup()
        self.clean_scratch()
        self.release_device()
        self.set_state('backup_complete')
        self.emit_backup(record, 'created')

    def prepare(self, payload):
        self.guard_operation()
        try:
            filesystem = validate_filesystem(payload.get('filesystem', 'ext4'))
        except ValueError as error:
            raise InstallerError(str(error)) from error
        serial = self.require_serial(payload)
        if not payload.get('backup_dir'):
            raise InstallerError('请先独立创建设备备份或选择已有备份；准备安装不再执行备份。')
        if payload.get('layout', 'linux-single') != 'linux-single':
            raise InstallerError('当前 GUI 仅支持单根 A 槽安装。')
        self.begin_run(payload, 'install')
        if filesystem not in self.manifest['filesystems']:
            raise InstallerError('所选安装包尚不支持 ' + filesystem + '，请重新构建配套引导、安装镜像和 rootfs。')
        self.filesystem = filesystem
        self.receipt['filesystem'] = filesystem
        self.phase('verify_backup')
        record = self.load_backup(payload['backup_dir'], serial)
        self.receipt['backup_dir'] = record['path']
        self.save_receipt()
        disk = self.connect_ram()
        self.plan = cli.plan_for_disk(SimpleNamespace(layout='linux-single', keep_home=False,
                                        filesystem=filesystem,
                                        android_size=None, root_size=None), disk)
        self.phase('verify_backup')
        compatibility = self.verify_current_backup(record)
        self.backup_record = record
        self.check_cancel()
        self.plan_snapshot = dict(serial=serial, session_id=self.session_id,
                                  boot_id=self.transport.boot_id, manifest=self.manifest_digest,
                                  report=self.report_text, padded_sha256=self.padded_digest,
                                  backup_id=record['backup_id'],
                                  current_persist_sha256=compatibility['current_persist_sha256'],
                                  layout='linux-single', filesystem=filesystem, boot_slots=['a'])
        self.plan_id = hashlib.sha256(canonical(self.plan_snapshot)).hexdigest()
        self.receipt.update(backup_verified=True, backup_reused=True, backup_id=record['backup_id'],
                            plan_id=self.plan_id, boot_a_sha256=self.padded_digest, device=self.device,
                            backup_compatibility=compatibility)
        self.save_receipt()
        self.set_state('prepared')
        self.emit('plan_ready', plan=dict(plan_id=self.plan_id, serial=serial, layout='linux-single',
                  filesystem=filesystem, compression='zstd:3' if filesystem == 'btrfs' else '',
                  root_bytes=self.device['userdata_bytes'], separate_home=False,
                  changes_partition_boundaries=False, boot_slots=['a'], active_slot='a',
                  erases_personal_data=True, backup_dir=record['path'], run_dir=self.receipt['run_dir'],
                  backup_verified=True, backup_reused=True, backup_id=record['backup_id'],
                  warnings=compatibility['warnings'], calibration_usable=True,
                  experimental=self.manifest.get('status') != 'DEVICE_TESTED'))

    def install(self, payload):
        if self.state != 'prepared':
            raise InstallerError('没有可以确认的准备会话。')
        consent = payload.get('confirmation', {})
        if self.filesystem != self.plan_snapshot['filesystem']:
            raise InstallerError('确认后的文件系统选择发生变化，请重新检查安装方案。')
        if (payload.get('session_id') != self.session_id or payload.get('plan_id') != self.plan_id or
                consent.get('serial') != self.device['serial'] or
                consent.get('personal_data_backed_up') is not True or
                consent.get('erase_userdata') is not True):
            raise InstallerError('确认必须绑定当前设备及方案，并明确确认数据备份和清除。')
        if self.manifest.get('status') != 'DEVICE_TESTED' and consent.get('allow_experimental') is not True:
            raise InstallerError('此安装包仍为实验状态，需要在确认页明确接受。')
        self.committing = True
        self.set_state('installing')
        self.phase('revalidate')
        original_manifest = self.manifest_digest
        self.validate_bundle(str(self.bundle))
        if self.manifest_digest != original_manifest:
            raise InstallerError('确认后安装包发生变化，请重新准备。')
        self.transport.preflight()
        if self.transport.boot_id != self.plan_snapshot['boot_id'] or self.transport.report() != self.report_text:
            raise InstallerError('确认后 RAM 会话或分区表发生变化，未下发格式化。')
        record = self.load_backup(self.receipt['backup_dir'], self.device['serial'])
        if record['backup_id'] != self.plan_snapshot['backup_id']:
            raise InstallerError('确认后备份发生变化，未下发格式化。')
        compatibility = self.verify_current_backup(record)
        if compatibility['current_persist_sha256'] != self.plan_snapshot['current_persist_sha256']:
            raise InstallerError('当前设备状态在确认后发生变化，请重新检查方案；未下发格式化。')
        self.receipt['backup_compatibility'] = compatibility
        self.receipt['confirmation'] = dict(consent, plan_id=self.plan_id)
        self.save_receipt()
        self.transport.install_root(self.padded)
        self.receipt['root_installed'] = True
        self.save_receipt()
        self.transport.flash_verify_reboot(self.padded, self.padded_digest, self.device['boot_bytes'])
        self.set_state('awaiting_first_boot')
        self.receipt['installation_commands_completed'] = True
        self.save_receipt()
        self.emit('installation_written', receipt=self.receipt, first_boot_verified=False)
        self.committing = False
        self.clean_scratch()
        self.release_device()

    def request_cancel(self):
        if self.committing:
            raise InstallerError('安装写入期间不能直接停止进程。')
        self.cancel_event.set()

    def cancel_prepared(self):
        if self.state != 'prepared':
            raise InstallerError('当前没有等待确认的会话。')
        self.cancel_event.clear()
        self.finish_cancel()

    def finish_cancel(self):
        self.phase('cancel')
        if self.transport:
            self.transport.cancel_cleanup()
        self.clean_scratch()
        self.release_device()
        self.plan_id = ''
        self.cancel_event.clear()
        self.receipt['cancelled_before_write'] = True
        self.save_receipt()
        self.set_state('cancelled')

    def clean_scratch(self):
        if self.scratch:
            self.scratch.cleanup()
            self.scratch = None

    def verify_first_boot(self, payload):
        if self.state != 'awaiting_first_boot' or payload.get('session_id') != self.session_id:
            raise InstallerError('没有等待验证的安装结果。')
        if payload.get('desktop_and_reboot_verified') is not True:
            raise InstallerError('请先在平板确认桌面和正常重启。')
        self.receipt.update(first_boot_verified=True, verification_source='user_report')
        self.save_receipt()
        self.set_state('verified')
        self.emit('verified', receipt=self.receipt)

    def fail(self, exc):
        if isinstance(exc, Cancelled) and not self.committing:
            try:
                self.finish_cancel()
                return
            except Exception as cleanup_error:
                exc = InstallerError('取消清理未完成，请手动进入 Fastboot：' + str(cleanup_error))
        message = re.sub(r'[A-Za-z0-9+/]{160,}={0,2}', '[原始分区数据已省略]', str(exc))
        message = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', message)
        self.receipt.update(error=message, failed_phase=getattr(self, 'current_phase', 'unknown'))
        try:
            self.save_receipt()
        except OSError as storage_error:
            message += '；执行记录保存失败：' + str(storage_error)
            self.receipt['error'] = message
        self.state = 'failed'
        # A full or unavailable log disk must not hide failure from the UI.
        for kind, values in (
            ('state', dict(state='failed', can_cancel=False, can_close=True)),
            ('error', dict(message=message, phase=getattr(self, 'current_phase', 'unknown'),
                           writes_may_have_started=self.receipt.get('write_dispatched', False), receipt=self.receipt))):
            try:
                self.emit(kind, **values)
            except OSError:
                self.emit_callback(dict(type=kind, session_id=self.session_id, request_id=self.request_id,
                                        simulation=self.simulation, **values))
        self.committing = False
        self.clean_scratch()
        if not self.receipt.get('write_dispatched'):
            self.release_device()

    def close(self):
        self.clean_scratch()
        self.release_device()
