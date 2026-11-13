# SPDX-License-Identifier: MIT
"""Package upstream Mesa libclc release assets for Fedora's Rusticl loader."""
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from source_build import PROJECT, download, sha
from rpm_package import build as rpm

LOCK = PROJECT/'build/rootfs/mesa-libclc-source.json'
LIBDIR = 'usr/lib64/clc22'
SPIRV = ('spirv-mesa3d-.spv', 'spirv64-mesa3d-.spv')


def record():
    return json.loads(LOCK.read_text())


def filename(info):
    return f"{info['package_name']}-{info['version']}-{info['package_release']}.aarch64.rpm"


def sources(info):
    return {name: download(item['url'], PROJECT/'inputs/mesa-libclc'/name, item['sha256'])
            for name, item in info['files'].items()}


def payload(destination, info, files):
    # Check every artifact before creating a payload, including the matching
    # source archive and pkg-config template supplied by the upstream release.
    for name, item in info['files'].items():
        path = Path(files[name])
        if path.is_symlink() or sha(path) != item['sha256']:
            raise ValueError('Mesa libclc 文件校验失败：'+name)
    libs = destination/LIBDIR
    libs.mkdir(parents=True)
    for name in SPIRV:
        data = Path(files[name]).read_bytes()
        if data[:4] != b'\x03\x02\x23\x07' or b'__clc_mesa_libclc_version\0' not in data:
            raise ValueError('Mesa libclc 发布文件不是修正后的 SPIR-V 库：'+name)
        (libs/name).write_bytes(data)
    pc = destination/'usr/share/pkgconfig/mesa-libclc.pc'
    pc.parent.mkdir(parents=True)
    template = Path(files['mesa-libclc.pc.in']).read_text()
    lines = template.splitlines()
    if sum(line.startswith('libexecdir=') for line in lines) != 1:
        raise ValueError('Mesa libclc pkg-config 模板缺少唯一的库目录。')
    pc.write_text('\n'.join('libexecdir=/'+LIBDIR if line.startswith('libexecdir=') else line
                            for line in lines)+'\n')
    docs = destination/'usr/share/licenses'/info['package_name']
    docs.mkdir(parents=True)
    archive_name = 'mesa-libclc-'+info['version']+'.tar.gz'
    archive = Path(files[archive_name])
    with tarfile.open(archive) as source:
        for name in ('LICENSE.TXT', 'CREDITS.TXT', 'README.md'):
            member = source.getmember('mesa-libclc-'+info['commit']+'/'+name)
            if not member.isfile():
                raise ValueError('Mesa libclc 许可文件必须是普通文件。')
            with source.extractfile(member) as stream:
                (docs/name).write_bytes(stream.read())
    shutil.copyfile(archive, docs/archive_name)
    (docs/'SOURCE.json').write_text(json.dumps(info, indent=2)+'\n')
    for name in ('mesa_libclc.py', 'rpm_package.py'):
        shutil.copyfile(PROJECT/'build/lib'/name, docs/name)


def build(output, *, info=None, files=None):
    info = record() if info is None else info
    files = sources(info) if files is None else files
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mesa-libclc-', dir=output.parent) as work:
        root = Path(work)/'payload'
        payload(root, info, files)
        # Fedora's non-SPIR-V bitcode subpackage requires this exact capability.
        # Mesa's fork is based on that same LLVM 22.1.8 release. Keep its actual
        # version visible while retaining the compatible packaged interface.
        return rpm(root, info['package_name'], info['version'], output,
            release=info['package_release'],
            provides=[info['fedora_compatibility'], 'pkgconfig(mesa-libclc) = 0.2.0'],
            license='Apache-2.0 WITH LLVM-exception OR NCSA OR MIT',
            summary='Mesa libclc SPIR-V builtins for Rusticl (LLVM 22.1.8 with Mesa fixes)')
