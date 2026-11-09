# SPDX-License-Identifier: MIT
"""Deterministic simulation; no USB, socket or Fastboot operations."""
import hashlib
import json
import gzip
import io
import tarfile
import shutil
from pathlib import Path
import uuid

from .engine import Engine, InstallerError, PHASES, cli
from . import backups


def demo_image(records, kernel=b'DEMO ONLY KERNEL'):
    """Make a synthetic Android image for the offline demo and protocol tests."""
    cpio = bytearray()
    for inode, (name, data) in enumerate([*records.items(), ('TRAILER!!!', b'')]):
        fields = [inode, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name.encode()) + 1, 0]
        cpio.extend(('070701' + ''.join(f'{number:08x}' for number in fields)).encode())
        cpio.extend(name.encode() + b'\0'); cpio.extend(b'\0' * (-len(cpio) % 4))
        cpio.extend(data); cpio.extend(b'\0' * (-len(cpio) % 4))
    ramdisk, dtb = gzip.compress(cpio, mtime=0), b'xiaomi,liuqin\0'
    header = bytearray(4096); header[:8] = b'ANDROID!'
    for offset, number in ((8, len(kernel)), (16, len(ramdisk)), (36, 4096), (40, 2), (1644, 1660), (1648, len(dtb))):
        header[offset:offset + 4] = number.to_bytes(4, 'little')
    return bytes(header) + kernel + b'\0' * (-len(kernel) % 4096) + ramdisk + b'\0' * (-len(ramdisk) % 4096) + dtb


def demo_report(capacity=256):
    disk = cli.layout.reference_stock_disk()
    disk.total_sectors = disk.total_sectors * capacity // 256
    disk.last_usable = disk.total_sectors - 6
    disk.by_name('userdata').last_sector = disk.last_usable
    lines = [f'Disk /dev/sda: {disk.total_sectors} sectors',
             f'Sector size (logical/physical): {disk.sector_size}/{disk.sector_size} bytes',
             f'First usable sector is {disk.first_usable}, last usable sector is {disk.last_usable}',
             f'Partition table holds up to {disk.entry_count} entries',
             'Number  Start (sector) End (sector) Size Code Name']
    lines += [f'{p.number} {p.first_sector} {p.last_sector} {p.size / cli.layout.GIB:.3f} GiB 8300 {p.name}'
              for p in disk.partitions]
    return '\n'.join(lines) + '\n'


class DemoTransport:
    def __init__(self, engine, serial):
        self.engine, self.serial, self.boot_id = engine, serial, None
        self.calls = []

    def check_device(self):
        self.calls.append('check_device')
        return dict(product='liuqin', unlocked='yes', **{'current-slot': 'a'},
                    serial=self.serial, boot_bytes=16384, anti_version=1)

    def boot_ram(self):
        self.calls.append('boot_ram')
        self.ram_active = True
        self.boot_id = str(uuid.uuid4())
        self.engine.pause(.12)

    def preflight(self, require_archive=True):
        return dict(battery=87, available_memory_bytes=6 * 1024**3)

    def report(self):
        if hasattr(self.engine, 'demo_partition_report'):
            return self.engine.demo_partition_report
        if hasattr(self.engine,'demo_raw_report'):return self.engine.demo_raw_report
        if self.engine.receipt.get('layout') == 'android-manage':
            from .android_demo import report
            return report(self.engine, cli.layout)
        return getattr(self, 'multiboot_report', demo_report())

    def backup(self, directory):
        self.calls.append('backup')
        sums = {}
        for name in ('boot_a', 'boot_b', 'persist'):
            self.engine.phase('backup', detail=name)
            data = ('DEMO ONLY / NOT A DEVICE BACKUP / ' + name).encode().ljust(16384 if name.startswith('boot') else 256, b'\0')
            (directory / (name + '.img')).write_bytes(data)
            sums[name + '.img'] = hashlib.sha256(data).hexdigest()
            self.engine.pause(.12)
        (directory / 'DEMO-ONLY.txt').write_text('模拟数据，不能用于恢复任何平板。\n', encoding='utf-8', newline='\n')
        (directory / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sums.items()), encoding='utf-8', newline='\n')
        gpt_dir = directory / 'gpt'
        gpt_dir.mkdir()
        copies = {}
        for name in ('sda-head.bin', 'sda-tail.bin'):
            data = b'DEMO' * (6 * 4096 // 4)
            (gpt_dir / name).write_bytes(data)
            copies[name] = hashlib.sha256(data).hexdigest()
        disk = cli.layout.parse_print(self.report())
        (gpt_dir / 'manifest.json').write_text(json.dumps(dict(serial=self.serial, files=copies,
            disk='/dev/sda', sector_size=disk.sector_size, sectors=disk.total_sectors)), encoding='utf-8', newline='\n')
        (gpt_dir / 'sgdisk-p.txt').write_text(self.report(), encoding='utf-8', newline='\n')
        return sums

    def verify_backup(self, record):
        self.calls.append('verify_backup')
        current = hashlib.sha256(('DEMO ONLY / NOT A DEVICE BACKUP / persist').encode().ljust(256, b'\0')).hexdigest()
        result = backups.assert_compatible(record, self.engine.device, self.report(), current, cli.layout)
        result['calibration_usable'] = True
        return result

    def install_root(self, _padded, system=None):
        self.calls.append('install_root:' + system['id'] if system else 'install_root')
        self.engine.receipt['write_dispatched'] = True
        self.engine.save_receipt()
        for phase in ('transfer', 'verify_archive', 'format_root', 'extract_root',
                      'configure_device', 'verify_root', 'sync_root'):
            self.engine.phase(phase)
            if phase == 'transfer':
                for index in range(1, 6):
                    self.engine.progress(phase, index, 5, unit='simulated')
                    self.engine.pause(.08)
            else:
                self.engine.pause(.15)
        if system and self.engine.receipt.get('layout') == 'linux-managed':
            self.engine.demo_android_scan['inventory'][system['partition']['name']].update(
                filesystem=system['target']['root_fstype'],fsuuid=system['target']['root_fsuuid'])

    def flash_verify_reboot(self, _padded, _digest, _size):
        self.calls.append('flash_verify_reboot')
        for phase in ('return_fastboot', 'flash_boot', 'verify_boot', 'reboot'):
            self.engine.phase(phase)
            self.engine.pause(.15)
        self.engine.receipt.update(boot_flash_started=True, boot_written=True,
                                   boot_readback_verified=True, active_slot_set=True)

    def current_boot_digest(self):
        return hashlib.sha256(getattr(self.engine, 'demo_live_boot', b'DEMO CURRENT BOOT'.ljust(16384, b'\0'))).hexdigest()

    def snapshot_boot(self, directory):
        (directory/'boot_a.img').write_bytes(getattr(self.engine, 'demo_live_boot', b'DEMO CURRENT BOOT'.ljust(16384, b'\0')))
        self.calls.append('snapshot_boot')
        return self.current_boot_digest()

    def _switch_plan(self, request):
        from liuqin_multiboot import identifier
        from liuqin_native_install import identity
        from liuqin_partitions import snapshot_id
        from .partition_demo import inspect

        if request.get('serial') != self.serial or not self.boot_id or request.get('boot_id') != self.boot_id:
            raise InstallerError('模拟 Recovery 会话发生变化。')
        target_id = identifier(request.get('system_id'))
        transaction = identifier(request.get('transaction'))
        state = inspect(self.engine)
        registry = state.get('registry') or {}
        systems = {system['id']: system for system in registry.get('systems', [])}
        current = systems.get(registry.get('default_id'))
        target = systems.get(target_id)
        if not target or target == current:
            raise InstallerError('请选择另一个已安装系统。')
        image = target['image']
        data = self.engine.demo_boot_images.get(image)
        if data is None:
            if target['type'] == 'android':
                data = (self.engine.demo_android_rom/'boot.img').read_bytes()
            else:
                # The original demo layout contains synthetic Linux images.
                data = ('DEMO LINUX '+target_id).encode()
        entry = state['files'].get(image, {})
        boot_a = state['inventory']['boot_a']
        if (not data or len(data) != entry.get('bytes') or len(data) > boot_a['bytes'] or
                hashlib.sha256(data).hexdigest() != target['boot_sha256'] or
                entry.get('sha256') != target['boot_sha256']):
            raise InstallerError('模拟系统的引导镜像校验失败。')
        padded = data.ljust(boot_a['bytes'], b'\0')
        plan = dict(serial=self.serial, boot_id=self.boot_id, transaction=transaction, system_id=target_id,
                    target_name=target['name'], previous_name=current['name'] if current else '未设置默认系统',
                    previous_boot_sha256=self.current_boot_digest(),
                    target_boot_sha256=hashlib.sha256(padded).hexdigest(), snapshot_id=snapshot_id(state),
                    parts=[identity(boot_a), identity(state['inventory']['linux_boot'])])
        return plan, target, padded

    def native_rpc(self, action, request):
        self.calls.append('native:'+action)
        if action == 'switch-preview':
            plan, _, _ = self._switch_plan(request)
            return dict(plan=plan)
        if action != 'switch-apply':
            raise InstallerError('未知的模拟 Recovery 操作。')
        plan, target, padded = self._switch_plan(request['plan'])
        if plan != request['plan']:
            raise InstallerError('模拟切换方案已发生变化。')
        generation = target['generation'] if target['type'] == 'android' else target['target']['boot_generation']
        scan = self.engine.demo_android_scan
        scan['registry'].update(default_id=target['id'], default_generation=generation)
        from liuqin_android import canonical
        raw = canonical(scan['registry'])
        scan['registry_sha256'] = hashlib.sha256(raw).hexdigest()
        scan['files']['registry.json'] = dict(bytes=len(raw), sha256=scan['registry_sha256'])
        self.engine.demo_live_boot = padded
        return dict(boot_written=True, boot_readback_verified=True, default_id=target['id'])

    def android_rpc(self, action, request):
        if action.startswith('partition-'):
            from .partition_demo import inspect,apply
            self.calls.append(action)
            if action=='partition-inspect':return inspect(self.engine)
            if action=='partition-edit':
                from .partition_demo import edit
                return edit(self.engine,request['plan'])
            if action in ('partition-resume','partition-resume-check'):
                from .partition_demo import edit
                return edit(self.engine,request['plan'],resume=True,check_only=action=='partition-resume-check')
            return apply(self.engine,request['plan'])
        if action=='android-check':return {'compatible':True}
        if action=='android-direct':
            self.calls.append('android-direct')
            return {'rom_written':True,'android_data_initialized':True}
        from .android_demo import rpc
        result = rpc(self, action, request)
        if action == 'linux-stage':
            self.engine.demo_boot_images[request['plan']['system']['image']] = self.engine.android_boot.read_bytes()
        return result

    def android_default(self, registry):
        self.engine.demo_live_boot = self.engine.android_boot.read_bytes()
        self.flash_verify_reboot(None, None, None)
        self.android_rpc('commit', dict(plan=self.engine.android_plan, registry=registry,
            expected_boot=self.current_boot_digest()))
        self.engine.receipt.update(android_registered=True, default_id=self.engine.android_plan['android_id'])

    def multiboot_info(self):
        return ("Partition GUID code: 0FC63DAF-8483-4772-8E79-3D69D8477DE4 (Linux filesystem)\n"
                "Partition unique GUID: 11111111-1111-4111-8111-111111111111\n"
                "First sector: 2758144\nLast sector: 61731834\n"
                "Attribute flags: 0000000000000000\nPartition name: 'userdata'\n")

    def multiboot_apply(self, plan, _payload, _report, _info):
        self.calls.append('multiboot_apply')
        disk = cli.layout.reference_stock_disk()
        lines = demo_report().split('Number  Start (sector)', 1)[0].splitlines()
        lines.append('Number  Start (sector) End (sector) Size Code Name')
        for entry in plan['prefix'] + plan['partitions']:
            lines.append(f"{entry['number']} {entry['first_sector']} {entry['last_sector']} 1.0 GiB 8300 {entry['name']}")
        self.multiboot_report = '\n'.join(lines) + '\n'
        self.engine.demo_partition_report = self.multiboot_report

    def multiboot_store(self, action, _store, payload=None, digest=None, expected_boot='-'):
        self.calls.append('multiboot_store:' + action)
        if action == 'stage':
            from .android_demo import adopt_layout
            adopt_layout(self.engine, payload, cli.layout)

    def multiboot_default(self, system, store, registry, backup):
        self.calls.append('multiboot_default:' + system['id'])
        self.flash_verify_reboot(None, None, None)

    def cancel_cleanup(self):
        self.calls.append('cancel_cleanup')

    def return_fastboot(self):
        self.ram_active = False
        self.calls.append('return_fastboot')

    def snapshot_recovery(self,directory,name):
        from .engine import digest_file
        source=self.engine.demo_recovery_partitions[name]
        shutil.copyfile(source,directory/(name+'.img'))
        return digest_file(source)

    def fastboot(self, *arguments):
        self.calls.append('fastboot:'+' '.join(arguments))
        if arguments[:1]==('flash',) and arguments[1] in ('recovery_a','recovery_b'):
            shutil.copyfile(arguments[2],self.engine.demo_recovery_partitions[arguments[1]])
        if arguments[:2] == ('flash','boot_a'):
            self.engine.demo_live_boot = Path(arguments[2]).read_bytes()


class DemoEngine(Engine):
    simulation = True

    def __init__(self, emit, state_dir):
        super().__init__(emit, state_dir, transport_factory=DemoTransport)
        self.demo_boot_images = {}
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.demo_bundle = self.state_dir / 'demo-materials'
        self.demo_bundle.mkdir(mode=0o700, exist_ok=True)
        files = {}
        release = '6.17-demo'
        kernel = b'DEMO ONLY KERNEL'
        modules = {f'usr/lib/modules/{release}/modules.dep': b'kernel/demo.ko:\n',
                   f'usr/lib/modules/{release}/kernel/demo.ko': b'DEMO ONLY MODULE'}
        manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n'
                           for name, data in modules.items()).encode()
        contract = (b'LIUQIN_ROOT_INTERFACE_V2\n' + f'kernel_release={release}\n'.encode() +
                    f'image_sha256={hashlib.sha256(kernel).hexdigest()}\n'.encode() +
                    f'modules_sha256={hashlib.sha256(manifest).hexdigest()}\n'.encode())
        contents = dict(modules)
        contents.update({'etc/liuqin-root-interface': b'liuqin-root-v2\n',
                         'etc/liuqin-system-id': b'unbound\n',
                         'etc/liuqin-boot-target': b'unbound\n',
                         'etc/liuqin-filesystems': b'ext4\nbtrfs\n',
                         'etc/liuqin-root-filesystem': b'ext4\n',
                         'etc/liuqin-native-root': b'liuqin-native-root-v1\n',
                         f'usr/share/liuqin/kernels/{release}/boot.contract': contract,
                         f'usr/share/liuqin/kernels/{release}/modules.manifest': manifest,
                         'etc/systemd/system/liuqin-gnome-storage-guard.service': b'DEMO ONLY\n',
                         'usr/local/sbin/liuqin-gnome-storage-guard': b'DEMO ONLY\n'})
        elf = bytearray(20); elf[:6] = b'\x7fELF\x02\x01'; elf[18:20] = (183).to_bytes(2, 'little')
        contents.update({'usr/lib/systemd/systemd': bytes(elf),
                         'etc/passwd': b'fastrpc:x:1:1::/:/bin/false\n', 'etc/group': b'fastrpc:x:1:\n'})
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, data in contents.items():
                entry = tarfile.TarInfo(name); entry.size = len(data)
                entry.mode = 0o755 if name in ('usr/lib/systemd/systemd', 'usr/local/sbin/liuqin-gnome-storage-guard') else 0o644
                archive.addfile(entry, io.BytesIO(data))
            entry = tarfile.TarInfo('etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service')
            entry.type = tarfile.SYMTYPE; entry.linkname = '../liuqin-gnome-storage-guard.service'
            archive.addfile(entry)
        root_archive = stream.getvalue()

        def image(installer):
            records = {'etc/liuqin-root-profile': b'native-updatable\n', 'etc/liuqin-native-root.contract': contract,
                       'etc/liuqin-storage-mode': b'persistent\n'}
            if installer:
                records = {'etc/liuqin-storage-mode': b'readonly\n', 'etc/liuqin-installer': b'liuqin\n',
                           'etc/liuqin-installer-interface': b'liuqin-installer-v2\n',
                           'etc/liuqin-installer-usb-network': b'rndis\n',
                           'etc/liuqin-android-installer': b'1\n',
                           'etc/liuqin-installer-kernel.release': b'6.17-installer-demo\n'}
            kernel = b'DEMO INSTALLER KERNEL' if installer else b'DEMO ONLY KERNEL'
            records['etc/liuqin-filesystems'] = b'ext4\nbtrfs\n'
            records['etc/liuqin-multiboot'] = b'1\n'
            from liuqin_boot_target import HELPER_PATH,current_helper
            records[HELPER_PATH] = current_helper()
            return demo_image(records, kernel)
        for name, data in (('boot.img', image(False)), ('installer.img', image(True)), ('rootfs.tar.gz', root_archive)):
            (self.demo_bundle / name).write_bytes(data)
            files[name] = hashlib.sha256(data).hexdigest()
        (self.demo_bundle / 'bundle.json').write_text(json.dumps(dict(
            device='liuqin', status='OFFLINE_ASSEMBLED', files=files,
            kernel_release='6.17-demo', installer_layouts=['linux-single'], single_root_boot_slot='a')), encoding='utf-8', newline='\n')

    def list_devices(self):
        self.emit('devices', devices=[dict(serial='DEMO-LIUQIN', description='模拟 256GB 平板')])

    def validate_bundle(self, _directory):
        super().validate_bundle(str(self.demo_bundle))

    def validate_installer(self, _directory):
        from .android_demo import initialize
        initialize(self, cli.layout)
        super().validate_installer(str(self.demo_bundle))

    def android_profile_for_run(self):
        from .android_demo import initialize
        initialize(self, cli.layout)
        return self.demo_android_profile

    def android_rom_path(self, _path):
        return str(self.demo_android_rom)
