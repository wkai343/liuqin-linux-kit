#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare the dedicated Recovery kernel, load shim, DTB and touch module."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT/'shared'))
from liuqin_artifacts import boot_interface

BASE_COMMIT = 'fe81794b5e1b8a3f11a165f3acbfdb8fa671dbb3'
BASE_IMAGE_SHA = 'dcc287f5a8273260613fdcf26447e35e28fd40cec36358615d32c0e9c7abe569'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tool(name):
    path = shutil.which(name) or PROJECT/'.devbox/nix/profile/default/bin'/name
    if not Path(path).is_file():
        raise ValueError('missing tool: '+name)
    return str(path)


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def aligned_kernel(image, output):
    data = Path(image).read_bytes()
    if data[56:60] != b'ARM\x64' or len(data) < 64:
        raise ValueError('expected an ARM64 Linux Image')
    offset, size, flags = struct.unpack_from('<QQQ', data, 8)
    if offset != 0 or not len(data) <= size < 62*1024**2 or flags & 1:
        raise ValueError('unsupported ARM64 image placement')
    output = Path(output)
    with tempfile.TemporaryDirectory(prefix='aligned-recovery-') as work:
        obj = Path(work)/'entry.o'
        elf = Path(work)/'entry.elf'
        run([tool('aarch64-unknown-linux-musl-gcc'), '-c',
             '-DKERNEL_FILE="'+str(Path(image).resolve())+'"',
             '-DKERNEL_IMAGE_SIZE='+str(size), '-DKERNEL_FLAGS='+str(flags),
             PROJECT/'device/recovery/aligned-entry.S', '-o', obj])
        run([tool('aarch64-unknown-linux-musl-ld'), '-Ttext=0', '--entry=_start', obj, '-o', elf])
        run([tool('aarch64-unknown-linux-musl-objcopy'), '-O', 'binary', elf, output])
    wrapped = output.read_bytes()
    if wrapped[4096:4096+len(data)] != data or len(wrapped) != 2*1024**2+(len(data)+15)//16*16:
        raise ValueError('aligned entry payload differs from the original kernel')
    return wrapped


def recovery_dtb(base_image, output):
    blob = base_image.read_bytes()
    kernel_size, ramdisk_size = struct.unpack_from('<I', blob, 8)[0], struct.unpack_from('<I', blob, 16)[0]
    offset = 4096+(kernel_size+4095)//4096*4096+(ramdisk_size+4095)//4096*4096
    length = struct.unpack_from('<I', blob, 1648)[0]
    output.write_bytes(blob[offset:offset+length])
    get, put = tool('fdtget'), tool('fdtput')

    def children(node):
        return subprocess.check_output([get, '-l', str(output), node], text=True).splitlines()

    def disable(node):
        run([put, '-t', 's', output, node, 'status', 'disabled'])
        for name in children(node):
            disable(node.rstrip('/')+'/'+name)

    def remove_property(node, name):
        properties = subprocess.check_output([get, '-p', str(output), node], text=True).splitlines()
        if name in properties:
            run([put, '-d', output, node, name])

    run([put, output, '/', 'liuqin,recovery'])
    run([put, output, '/pmic-glink', 'liuqin,recovery'])
    args = subprocess.check_output([get, '-t', 's', str(output), '/chosen', 'bootargs'], text=True).split()
    args = [arg for arg in args if not arg.startswith(('hung_task_panic=', 'softlockup_panic='))]
    run([put, '-t', 's', output, '/chosen', 'bootargs', ' '.join(args)])

    # The ADSP remains available for PMIC battery/charging messages. Audio
    # services and DSP compute consumers are outside this maintenance image.
    # Preserve all qualified clock providers and power-domain consumers.
    # Even apparently idle multimedia nodes can retain the bootloader display
    # state; pruning them left a valid simplefb buffer but a black LCD.
    disabled = ['/sound', '/audio-codec',
                '/soc@0/remoteproc@3000000/glink-edge/gpr']
    soc_children = children('/soc@0')
    disabled += ['/soc@0/'+name for name in soc_children if name.startswith(('codec@', 'soundwire@'))]
    # Only nodes present in the qualified base are considered.
    for node in disabled:
        result = subprocess.run([get, '-p', str(output), node], capture_output=True)
        if result.returncode == 0:
            disable(node)

    usb = '/soc@0/usb@a6f8800/usb@a600000'
    # MDSS stays disabled as in the qualified base. Its unbound panel node
    # must remain available to the RAM-only touch follower handoff module.
    run([put, output, '/soc@0/phy@88e8000', 'liuqin,recovery'])
    run([put, '-t', 's', output, usb, 'dr_mode', 'peripheral'])
    remove_property(usb, 'usb-role-switch')
    for node in ('/pmic-glink/connector@0', usb+'/ports', '/soc@0/phy@88e8000/ports'):
        run([put, '-r', output, node])
    for name in ('orientation-switch', 'mode-switch'):
        remove_property('/soc@0/phy@88e8000', name)
    # LVGL's maintenance input consumes finger contacts only. Avoid starting
    # the unsupported stylus report path, without changing finger firmware.
    touch='/soc@0/geniqup@9c0000/spi@990000/touchscreen@0'
    remove_property(touch, 'novatek,pen-support')
    return disabled


def prepare(args):
    source, kernel = args.kernel_source.resolve(), args.kernel_out.resolve()
    out = args.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):
        raise ValueError('use a new output directory below project/out')
    if subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip() != BASE_COMMIT:
        raise ValueError('unsupported kernel base')
    patch = PROJECT/'build/recovery/kernel-log-fixes.patch'
    if subprocess.check_output(['git', '-C', source, 'diff', '--binary']) != patch.read_bytes():
        raise ValueError('kernel source changes differ from the Recovery patch')
    if sha(args.base_image) != BASE_IMAGE_SHA:
        raise ValueError('qualified native base image required')
    boot_interface(args.base_image, installer=True)
    config = (kernel/'.config').read_text()
    if '# CONFIG_SND is not set' not in config:
        raise ValueError('the maintenance kernel must exclude audio')
    out.mkdir(parents=True)
    raw = kernel/'arch/arm64/boot/Image'
    wrapped = aligned_kernel(raw, out/'Image')
    (out/'Image.gz').write_bytes(gzip.compress(wrapped, compresslevel=9, mtime=0))
    recovery_dtb(args.base_image, out/'recovery.dtb')

    # modpost needs the built-in symbol versions for this exact kernel build.
    builtins = kernel/'vmlinux.symvers'
    if builtins.is_file():
        shutil.copyfile(builtins, kernel/'Module.symvers')
    module_dir = out/'touch-build'
    module_dir.mkdir()
    for path in (PROJECT/'device/recovery/touch').iterdir():
        if path.is_file():
            shutil.copy2(path, module_dir/path.name)
    env = dict(os.environ, PATH=str(PROJECT/'.devbox/nix/profile/default/bin')+os.pathsep+os.environ.get('PATH', ''))
    run(['make', '-C', source, 'O='+str(kernel), 'ARCH=arm64',
         'CROSS_COMPILE=aarch64-unknown-linux-musl-', 'M='+str(module_dir), 'modules'], env=env)
    shutil.copy2(module_dir/'liuqin_bootloader_panel.ko', out/'liuqin_bootloader_panel.ko')
    shutil.copy2(kernel/'.config', out/'kernel.config')
    shutil.copy2(patch, out/patch.name)
    inputs = [Path(__file__), PROJECT/'device/recovery/aligned-entry.S', patch]
    inputs += sorted((PROJECT/'device/recovery/touch').glob('*'))
    manifest = dict(schema=1, device='liuqin', base_image_sha256=BASE_IMAGE_SHA,
                    kernel_base_commit=BASE_COMMIT, kernel_release=(kernel/'include/config/kernel.release').read_text().strip(),
                    raw_kernel_sha256=sha(raw), kernel_sha256=hashlib.sha256(wrapped).hexdigest(),
                    dtb_sha256=sha(out/'recovery.dtb'), config_sha256=sha(kernel/'.config'),
                    files={name:sha(out/name) for name in ('Image.gz','recovery.dtb','liuqin_bootloader_panel.ko',
                                                         'kernel.config','kernel-log-fixes.patch')},
                    source_files={str(path.relative_to(PROJECT)):sha(path) for path in inputs if path.is_file()},
                    hardware_boot_tested=False)
    (out/'kernel-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-source', type=Path, required=True)
    parser.add_argument('--kernel-out', type=Path, required=True)
    parser.add_argument('--base-image', type=Path, default=PROJECT/'out/releases/native-recovery/recovery.img')
    parser.add_argument('--out', type=Path, required=True)
    prepare(parser.parse_args())
