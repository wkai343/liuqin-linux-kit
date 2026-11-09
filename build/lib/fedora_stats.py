# SPDX-License-Identifier: MIT
"""Build an isolated Adreno GPU plugin for Fedora's official KSystemStats."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from source_build import PROJECT, sha
from rpm_package import build as rpm

BUILD_PACKAGES = ['gcc', 'gcc-c++', 'make', 'cmake', 'ninja-build', 'gettext',
    'extra-cmake-modules', 'qt6-qtbase-devel', 'libksysguard-devel',
    'kf6-kcoreaddons-devel', 'kf6-ki18n-devel', 'kf6-solid-devel', 'kf6-kio-devel',
    'kf6-kcrash-devel', 'kf6-networkmanager-qt-devel', 'lm_sensors-devel',
    'libnl3-devel', 'systemd-devel', 'libcap-devel', 'libdrm-devel']
PLUGIN = 'usr/lib64/liuqin/qt6/plugins/ksystemstats/ksystemstats_plugin_gpu.so'
DROP_IN = 'usr/lib/systemd/user/plasma-ksystemstats.service.d/50-liuqin-gpu.conf'


def plugin_dependencies(path):
    from elftools.elf.elffile import ELFFile
    if path.is_symlink():
        raise ValueError('GPU 插件不能是符号链接。')
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        if (elf.elfclass != 64 or not elf.little_endian or
                elf.header['e_machine'] != 'EM_AARCH64' or elf.header['e_type'] != 'ET_DYN'):
            raise ValueError('GPU 插件必须是 ARM64 ELF 共享库。')
        dynamic = elf.get_section_by_name('.dynamic')
        if dynamic is None:
            raise ValueError('GPU 插件缺少动态依赖。')
        names = sorted({tag.needed for tag in dynamic.iter_tags('DT_NEEDED')})
    if not names or any(not re.fullmatch(r'[A-Za-z0-9_+.-]+\.so(?:\.[A-Za-z0-9_+.-]+)?', name) for name in names):
        raise ValueError('GPU 插件包含无效的共享库依赖。')
    return [name+'()(64bit)' for name in names]


def source(work, archive):
    record = json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
    if sha(archive) != record['sha256']:
        raise ValueError('KSystemStats 源码校验失败。')
    prefix = 'ksystemstats-' + record['version']
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != prefix or '..' in parts or not (member.isfile() or member.isdir()):
                raise ValueError('KSystemStats 源码归档包含不安全文件。')
        tar.extractall(work, filter='data')
    upstream = work/prefix
    adaptation = PROJECT/'device/ksystemstats'
    for name in ('AdrenoSysfs.h', 'LinuxAdrenoGpu.h', 'LinuxAdrenoGpu.cpp'):
        shutil.copyfile(adaptation/name, upstream/'plugins/gpu'/name)
    subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i',
                    str(adaptation/'0001-adreno-platform-gpu.patch')], cwd=upstream, check=True)
    return upstream, record


def build(root, archive, output, jobs, chroot, dnf):
    work = root/'tmp/ksystemstats-build'
    work.mkdir()
    upstream, record = source(work, archive)
    dnf(root, 'install', *BUILD_PACKAGES)
    guest = '/tmp/ksystemstats-build'
    chroot(root, ['cmake', '-S', guest+'/'+upstream.name, '-B', guest+'/build', '-G', 'Ninja',
        '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_TESTING=OFF', '-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON',
        '-DCMAKE_INSTALL_PREFIX=/usr',
        '-DCMAKE_INSTALL_LIBDIR=lib64', '-DCMAKE_INSTALL_LIBEXECDIR=libexec',
        '-DKDE_INSTALL_SYSCONFDIR=/etc', '-DKDE_INSTALL_SYSTEMDUNITDIR=lib/systemd'])
    chroot(root, ['cmake', '--build', guest+'/build', '--target', 'ksystemstats_plugin_gpu',
                  '--parallel', str(jobs)])
    return pack(root, work, output, chroot)


def pack(root, work, output, chroot):
    record = json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
    plugin = work/'build/bin/ksystemstats_plugin_gpu.so'
    dependencies = plugin_dependencies(plugin)
    package = work/'gpu-package'
    package.mkdir()
    target = package/PLUGIN
    target.parent.mkdir(parents=True)
    shutil.copy2(plugin, target)
    drop_in = package/DROP_IN
    drop_in.parent.mkdir(parents=True)
    shutil.copyfile(PROJECT/'device/ksystemstats/50-liuqin-gpu.conf', drop_in)
    package_name = record['package_name']
    docs = package/'usr/share/licenses'/package_name
    docs.mkdir(parents=True)
    upstream = work/('ksystemstats-'+record['version'])
    shutil.copytree(upstream/'LICENSES', docs/'LICENSES')
    shutil.copytree(PROJECT/'device/ksystemstats', docs/'adreno-source')
    for name in ('LICENSE', 'NOTICE', 'CREDITS.md'):
        shutil.copyfile(PROJECT/name, docs/name)
    (docs/'SOURCE.json').write_text(json.dumps(record, indent=2)+'\n')
    source_name = upstream.name+'-liuqin-gpu.tar.xz'
    with tarfile.open(docs/source_name, 'w:xz') as archive:
        archive.add(upstream, arcname=upstream.name)
    for script in ('fedora_stats.py', 'rpm_package.py'):
        shutil.copyfile(PROJECT/'build/lib'/script, docs/script)
    # Qt plugins reject older Qt minor versions. Retain the real build ABI
    # requirements instead of permitting a silently unloadable sensor plugin.
    versions = chroot(root, ['rpm', '-q', '--qf', '%{NAME} >= %{VERSION}\n',
                            'qt6-qtbase', 'libksysguard', 'glibc', 'libstdc++'], capture_output=True, text=True).stdout.splitlines()
    requires = ['ksystemstats >= '+record['version'], *dependencies, *versions]
    (docs/'BUILD.json').write_text(json.dumps(dict(plugin_sha256=sha(plugin),
        plugin_path='/'+PLUGIN, source_archive=source_name, requires=requires), indent=2)+'\n')
    result = rpm(package, package_name, record['version'], output,
        release=record['package_release'], requires=requires,
        license='GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL',
        summary='Adreno GPU statistics plugin for Fedora KSystemStats')
    inventory = chroot(root, ['rpm', '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
                       capture_output=True).stdout
    (Path(output)/'stats-packages.tsv').write_bytes(inventory)
    print('Fedora 独立 GPU 插件 RPM：'+str(result), flush=True)
    return result
