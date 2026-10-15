# SPDX-License-Identifier: MIT
"""Build upstream KSystemStats with read-only Adreno platform sensors."""
import json
from pathlib import Path
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
RUNTIME_PACKAGES = ['dbus-broker', 'libksysguard', 'qt6-qtbase', 'kf6-kcoreaddons',
    'kf6-ki18n', 'kf6-solid', 'kf6-kio-core-libs', 'kf6-kcrash',
    'kf6-networkmanager-qt', 'lm_sensors-libs', 'libnl3', 'systemd-libs', 'libcap',
    'libKSysGuardSystemStats.so.2()(64bit)', 'libprocesscore.so.11()(64bit)']


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
        '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_TESTING=OFF', '-DCMAKE_INSTALL_PREFIX=/usr',
        '-DCMAKE_INSTALL_LIBDIR=lib64', '-DCMAKE_INSTALL_LIBEXECDIR=libexec',
        '-DKDE_INSTALL_SYSCONFDIR=/etc', '-DKDE_INSTALL_SYSTEMDUNITDIR=lib/systemd'])
    chroot(root, ['cmake', '--build', guest+'/build', '--parallel', str(jobs)])
    chroot(root, ['env', 'DESTDIR='+guest+'/package', 'cmake', '--install', guest+'/build'])
    return pack(root, work, output, chroot)


def pack(root, work, output, chroot):
    record = json.loads((PROJECT/'build/rootfs/fedora-stats-source.json').read_text())
    package = work/'package'
    for name in ('usr/bin/ksystemstats', 'usr/lib64/qt6/plugins/ksystemstats/ksystemstats_plugin_gpu.so',
                 'usr/lib/systemd/user/plasma-ksystemstats.service'):
        if not (package/name).is_file():
            raise ValueError('KSystemStats 运行组件缺失：'+name)
    for path in package.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            header = stream.read(20)
        if header[:4] == b'\x7fELF' and (header[4:6] != b'\x02\x01' or header[18:20] != b'\xb7\x00'):
            raise ValueError('KSystemStats 构建产物不是 ARM64 ELF。')
    docs = package/'usr/share/licenses/ksystemstats-liuqin'
    if docs.exists():
        shutil.rmtree(docs)
    docs.mkdir(parents=True)
    shutil.copytree(work/('ksystemstats-'+record['version'])/'LICENSES', docs/'LICENSES')
    shutil.copytree(PROJECT/'device/ksystemstats', docs/'adreno-source')
    for name in ('LICENSE', 'NOTICE', 'CREDITS.md'):
        shutil.copyfile(PROJECT/name, docs/name)
    (docs/'SOURCE.json').write_text(json.dumps(record, indent=2)+'\n')
    # Qt plugins reject older Qt minor versions. Retain the real build ABI
    # requirements instead of permitting a silently unloadable sensor plugin.
    versions = chroot(root, ['rpm', '-q', '--qf', '%{NAME} >= %{VERSION}\n',
                            'qt6-qtbase', 'libksysguard', 'glibc', 'libstdc++'], capture_output=True, text=True).stdout.splitlines()
    requires = [name for name in RUNTIME_PACKAGES if name not in ('qt6-qtbase', 'libksysguard')] + versions
    result = rpm(package, 'ksystemstats', record['version'], output,
        release=record['package_release'], requires=requires,
        license='GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL',
        summary='KDE system statistics with Adreno utilization and frequency sensors')
    inventory = chroot(root, ['rpm', '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
                       capture_output=True).stdout
    (Path(output)/'stats-packages.tsv').write_bytes(inventory)
    print('Fedora Adreno 监测 RPM：'+str(result), flush=True)
    return result
