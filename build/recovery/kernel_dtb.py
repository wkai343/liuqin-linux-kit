# SPDX-License-Identifier: MIT
"""Construct the maintenance DTB from compiled sources and pinned ROM metadata."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

from kernel_recipe import PROJECT, load, project_file, sha
sys.path.insert(0, str(PROJECT / 'build/lib'))
from android_images import boot_assets


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def build(raw, images, output, work, lock):
    record = load(lock)
    images, output, work = map(Path, (images, output, work))
    if work.exists():
        raise ValueError('DTB work directory must be new')
    work.mkdir(parents=True)
    reference = json.loads((PROJECT / 'shared/liuqin-rom-images.json').read_text())
    for name in ('vendor_boot.img', 'dtbo.img'):
        if sha(images / name) != reference['images'][name]['sha256']:
            raise ValueError('Recovery DTB needs the pinned ROM: ' + name)
    boot_assets(images / 'vendor_boot.img', images / 'dtbo.img', work)
    dtc, overlay = shutil.which('dtc'), shutil.which('fdtoverlay')
    if not dtc or not overlay:
        raise ValueError('use the Devbox device tree tools')

    def run(args):
        subprocess.run(list(map(str, args)), check=True)

    def apply(source, base, target):
        blob = target.with_suffix('.dtbo')
        run([dtc, '-q', '-@', '-I', 'dts', '-O', 'dtb', '-o', blob, source])
        run([overlay, '-i', base, '-o', target, blob])

    command = work / 'command.dts'
    command.write_text('/dts-v1/;\n/plugin/;\n/ { fragment@0 { target-path = "/chosen"; '
                       '__overlay__ { bootargs = ' + json.dumps(record['bootargs']) + '; }; }; };\n')
    apply(command, raw, work / 'command.dtb')
    apply(project_file('device/liuqin-abl-boot-overlay.dts'), work / 'command.dtb', work / 'abl.dtb')
    args = [sys.executable, PROJECT / 'build/lib/abl-symbols.py', '--boot-dtb', work / 'abl.dtb',
            '--overlay-dir', work / 'dtbo', '--output', work / 'symbols.dtb', '--dtc', dtc,
            '--expect-symbols', '1781', '--sink-phandle']
    for base in sorted((work / 'dtbs').glob('dtb-*.dtb')):
        args += ['--symbols-from-dtb', base]
    run(args)
    apply(project_file('device/recovery/native-display-overlay.dts'), work / 'symbols.dtb', output)
    maintenance = module('recovery_maintenance_dtb', PROJECT / 'build/recovery/prepare-native-kernel.py')
    maintenance.configure_recovery_dtb(output)
    return output
