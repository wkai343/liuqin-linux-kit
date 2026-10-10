# SPDX-License-Identifier: MIT
"""Deterministic simulation; no USB, socket or Fastboot operations."""
import hashlib
import json
from pathlib import Path
import uuid

from .engine import Engine, PHASES, cli
from . import backups


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
                    serial=self.serial, boot_bytes=8192)

    def boot_ram(self):
        self.calls.append('boot_ram')
        self.boot_id = str(uuid.uuid4())
        self.engine.pause(.12)

    def preflight(self, require_archive=True):
        return dict(battery=87, available_memory_bytes=6 * 1024**3)

    def report(self):
        return demo_report()

    def backup(self, directory):
        self.calls.append('backup')
        sums = {}
        for name in ('boot_a', 'boot_b', 'persist'):
            self.engine.phase('backup', detail=name)
            data = ('DEMO ONLY / NOT A DEVICE BACKUP / ' + name).encode().ljust(8192 if name.startswith('boot') else 256, b'\0')
            (directory / (name + '.img')).write_bytes(data)
            sums[name + '.img'] = hashlib.sha256(data).hexdigest()
            self.engine.pause(.12)
        (directory / 'DEMO-ONLY.txt').write_text('模拟数据，不能用于恢复任何平板。\n')
        (directory / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sums.items()))
        gpt_dir = directory / 'gpt'
        gpt_dir.mkdir()
        copies = {}
        for name in ('sda-head.bin', 'sda-tail.bin'):
            data = b'DEMO' * (6 * 4096 // 4)
            (gpt_dir / name).write_bytes(data)
            copies[name] = hashlib.sha256(data).hexdigest()
        disk = cli.layout.parse_print(self.report())
        (gpt_dir / 'manifest.json').write_text(json.dumps(dict(serial=self.serial, files=copies,
            disk='/dev/sda', sector_size=disk.sector_size, sectors=disk.total_sectors)))
        (gpt_dir / 'sgdisk-p.txt').write_text(self.report())
        return sums

    def verify_backup(self, record):
        self.calls.append('verify_backup')
        current = hashlib.sha256(('DEMO ONLY / NOT A DEVICE BACKUP / persist').encode().ljust(256, b'\0')).hexdigest()
        result = backups.assert_compatible(record, self.engine.device, self.report(), current, cli.layout)
        result['calibration_usable'] = True
        return result

    def install_root(self, _padded):
        self.calls.append('install_root')
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

    def flash_verify_reboot(self, _padded, _digest, _size):
        self.calls.append('flash_verify_reboot')
        for phase in ('return_fastboot', 'flash_boot', 'verify_boot', 'reboot'):
            self.engine.phase(phase)
            self.engine.pause(.15)
        self.engine.receipt.update(boot_flash_started=True, boot_written=True,
                                   boot_readback_verified=True, active_slot_set=True)

    def cancel_cleanup(self):
        self.calls.append('cancel_cleanup')


class DemoEngine(Engine):
    simulation = True

    def __init__(self, emit, state_dir):
        super().__init__(emit, state_dir, transport_factory=DemoTransport)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.demo_bundle = self.state_dir / 'demo-materials'
        self.demo_bundle.mkdir(mode=0o700, exist_ok=True)
        header = bytearray(4096)
        header[:8] = b'ANDROID!'
        for offset, number in ((8, 1000), (36, 4096), (40, 2), (1644, 1660), (1648, 5000)):
            header[offset:offset + 4] = number.to_bytes(4, 'little')
        files = {}
        for name, data in (('boot.img', bytes(header)), ('installer.img', bytes(header)),
                           ('rootfs.tar.gz', b'DEMO ONLY')):
            (self.demo_bundle / name).write_bytes(data)
            files[name] = hashlib.sha256(data).hexdigest()
        (self.demo_bundle / 'bundle.json').write_text(json.dumps(dict(
            device='liuqin', status='OFFLINE_ASSEMBLED', files=files,
            kernel_release='6.17-demo', installer_layouts=['linux-single'], single_root_boot_slot='a')))

    def list_devices(self):
        self.emit('devices', devices=[dict(serial='DEMO-LIUQIN', description='模拟 256GB 平板')])

    def validate_bundle(self, _directory):
        super().validate_bundle(str(self.demo_bundle))
