#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare boot inputs and generic sensor configuration from stock image files."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from source_build import sha
from android_images import boot_assets, vendor_partition
from speaker_firmware import extract as extract_speaker_firmware


def verify_rom(images):
    reference = json.loads((PROJECT / 'shared/liuqin-rom-images.json').read_text())
    for name in ('vendor_boot.img', 'dtbo.img', 'super.img'):
        path = Path(images) / name
        if not path.is_file() or path.is_symlink(): raise ValueError('原厂 ROM 缺少普通镜像文件：' + str(path))
        if sha(path) != reference['images'][name]['sha256']:
            raise ValueError('原厂 ROM 与已支持版本 ' + reference['rom'] + ' 不一致：' + name)
    return reference


def prepare(images, output, *, verified=False):
    images, output = Path(images).resolve(), Path(output).resolve()
    if output.exists(): raise ValueError('板级输出需要新目录。')
    if not verified: verify_rom(images)
    firmware = PROJECT / 'device/firmware'
    # Manifest paths are rooted at /usr; verify against the repository, never the host.
    for row in (firmware / 'firmware.manifest').read_text().splitlines():
        expected, name = row.split('  ', 1)
        path = firmware / name.lstrip('/')
        if not path.is_file() or path.is_symlink() or sha(path) != expected:
            raise ValueError('仓库设备固件校验失败：' + name)
    output.mkdir(parents=True)
    fw = firmware / 'usr/lib/firmware'
    pool = output / 'firmware-pool'
    def copy(source, target):
        target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for path in (fw / 'novatek/liuqin').iterdir(): copy(path, pool / 'touch-nt36532/vendor/firmware' / path.name)
    for path in (fw / 'qca').iterdir(): copy(path, pool / 'bt-qca6490/vendor/bt_firmware/image' / path.name)
    copy(fw / 'ath11k/WCN6855/hw2.0/board.bin', pool / 'wlan-qca6490/vendor/firmware_mnt/image/qca6490/bd_m81.elf')
    for name in ('amss.bin.zst','m3.bin.zst','board-2.bin.zst','regdb.bin.zst'):
        copy(fw / 'ath11k/WCN6855/hw2.0' / name, pool / 'wlan-qca6490/upstream/WCN6855/hw2.0' / name)
    for name in ('regulatory.db','regulatory.db.p7s'): copy(fw / name, pool / 'regulatory/upstream' / name)
    for name in ('a730_sqe.fw','gmu_gen70000.bin'): copy(fw / 'updates/qcom' / name, pool / 'gpu-adreno730/vendor' / name)
    copy(fw / 'qcom/sm8475/liuqin/a730_zap.mbn', pool / 'gpu-adreno730/vendor/a730_zap.mbn')
    for path in (fw / 'qcom/sm8475/liuqin').iterdir():
        if path.name.startswith(('adsp.', 'cdsp.', 'slpi.')):
            copy(path, pool / 'dsp-sm8475/vendor/qcom/sm8475/liuqin' / path.name)
    for rev in ('hw2.0','hw2.1'):
        for name in ('amss.bin','m3.bin','regdb.bin','board-2.bin'):
            copy(fw / 'updates/ath11k/WCN6855' / rev / name, output / 'wlan' / rev / name)
    copy(fw / 'qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin', output / 'topology.bin')
    boot_assets(images / 'vendor_boot.img', images / 'dtbo.img', output)
    vendor = output / 'vendor.img'
    vendor_partition(images / 'super.img', vendor)
    extraction = output / 'vendor-files'
    with vendor.open('rb') as stream:
        stream.seek(1024); magic = stream.read(4)
    if magic == b'\xe2\xe1\xf5\xe0':
        subprocess.run(['fsck.erofs', '--extract=' + str(extraction), str(vendor)], check=True)
    else:
        with vendor.open('rb') as stream: stream.seek(1080); magic = stream.read(2)
        if magic != b'\x53\xef': raise ValueError('vendor 文件系统既不是 EROFS 也不是 ext4。')
        extraction.mkdir()
        # Paths are generated within the project and contain no quotes for debugfs.
        if '"' in str(extraction) or '\n' in str(extraction): raise ValueError('debugfs 输出路径含不支持字符。')
        subprocess.run(['debugfs','-R','rdump / "' + str(extraction) + '"',str(vendor)], check=True)
    sensors = extraction / 'etc/sensors'
    if not (sensors / 'sns_reg_config').is_file() or not (sensors / 'config').is_dir():
        raise ValueError('原厂 vendor 缺少通用传感器配置。')
    shutil.copytree(sensors, output / 'sensors')
    extract_speaker_firmware(extraction, output / 'speaker-firmware')
    # Discard the large logical image and extracted vendor tree after retaining generic inputs.
    shutil.rmtree(extraction); vendor.unlink()
    inputs = dict(FIRMWARE_POOL='firmware-pool', AUDIO_TOPOLOGY='topology.bin',
                  SPEAKER_FIRMWARE_DIR='speaker-firmware',
                  WLAN_HSP2_TUPLE='wlan', STOCK_OVERLAY_DIR='dtbo', STOCK_BASE_DIR='dtbs')
    (output / 'board.json').write_text(json.dumps(inputs, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rom-images', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.rom_images, args.out)
