# SPDX-License-Identifier: MIT
"""Build upstream Plasma Setup in a disposable Ubuntu ARM64 root, as a .deb."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from source_build import PROJECT, sha

BUILD_PACKAGES = ['build-essential','cmake','ninja-build','pkg-config','extra-cmake-modules',
                  'gettext','dpkg-dev','qt6-base-dev','qt6-declarative-dev','qt6-svg-dev',
                  'libkf6auth-dev','libkf6coreaddons-dev','libkf6i18n-dev','libkf6package-dev',
                  'libkf6config-dev','libkscreen-dev','plasma-workspace-dev','plasma-desktop']
RUNTIME_PACKAGES = ['sddm','kwin-wayland','plasma-workspace','plasma-desktop','kded6','polkitd','sudo','passwd',
                    'locales','libnss-myhostname','network-manager','wpasupplicant','wireless-regdb','qml6-module-org-kde-kirigami',
                    'qml6-module-org-kde-kirigamiaddons-formcard','qml6-module-org-kde-kcmutils',
                    'qml6-module-org-kde-kirigamiaddons-components','qml6-module-org-kde-kitemmodels',
                    'plasma-nm','qml6-module-qtcore','qml6-module-qtquick','qml6-module-qtquick-window',
                    'qml6-module-org-kde-kirigamiaddons-delegates','qml6-module-qtquick-controls',
                    'qml6-module-qtquick-layouts','qml6-module-qtnetwork','qml6-module-qtlocation',
                    'qml6-module-qtpositioning','qml6-module-org-kde-ksvg','maliit-keyboard']


def source(root, archive, record=None):
    record=record or json.loads((PROJECT/'build/rootfs/plasma-setup-source.json').read_text())
    if sha(archive)!=record['sha256']:raise ValueError('Plasma Setup 源码校验失败。')
    prefix='plasma-setup-'+record['version']
    with tarfile.open(archive) as tar:
        for entry in tar.getmembers():
            parts=Path(entry.name).parts
            if not parts or parts[0]!=prefix or '..' in parts or not (entry.isdir() or entry.isfile()):
                raise ValueError('Plasma Setup 归档包含不安全的文件。')
        tar.extractall(root,filter='data')
    return root/prefix,record


def build(root, archive, output, jobs, chroot):
    run_chroot=chroot
    def chroot(root,command,**kwargs):
        environment=['PATH=/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL=C','HOME=/root',
                     'DEBIAN_FRONTEND=noninteractive','CC=/usr/bin/gcc','CXX=/usr/bin/g++']
        for name in ('http_proxy','https_proxy','no_proxy'):
            if os.environ.get(name):environment.append(name+'='+os.environ[name])
        return run_chroot(root,['env','-i',*environment,*command],**kwargs)
    work=root/'tmp/plasma-setup-build'
    work.mkdir()
    upstream,record=source(work,archive)
    for patch in sorted((PROJECT/'build/rootfs/plasma-setup').glob('*.patch')):
        subprocess.run(['patch','--batch','--forward','-p1','-i',str(patch)],cwd=upstream,check=True)
    chroot(root,['apt-get','install','-y','--no-install-recommends',*BUILD_PACKAGES])
    guest='/tmp/plasma-setup-build'
    chroot(root,['cmake','-S',guest+'/'+upstream.name,'-B',guest+'/build','-G','Ninja',
                 '-DCMAKE_BUILD_TYPE=Release','-DBUILD_TESTING=OFF','-DCMAKE_INSTALL_PREFIX=/usr',
                 '-DCMAKE_INSTALL_LIBDIR=lib/aarch64-linux-gnu','-DCMAKE_INSTALL_LIBEXECDIR=libexec',
                 '-DKDE_INSTALL_SYSCONFDIR=/etc','-DKDE_INSTALL_SYSTEMDUNITDIR=lib/systemd'])
    chroot(root,['cmake','--build',guest+'/build','--parallel',str(jobs)])
    chroot(root,['env','DESTDIR='+guest+'/package','cmake','--install',guest+'/build'])
    build_panel_defaults(root,work,chroot)
    pack_deb(root,work,output,chroot)


def build_panel_defaults(root, work, chroot):
    source=work/'panel-source'
    shutil.copytree(PROJECT/'build/rootfs/plasma-panel',source)
    guest='/tmp/plasma-setup-build'
    chroot(root,['cmake','-S',guest+'/panel-source','-B',guest+'/panel-build','-G','Ninja',
                 '-DCMAKE_BUILD_TYPE=Release','-DCMAKE_INSTALL_PREFIX=/usr'])
    chroot(root,['cmake','--build',guest+'/panel-build','--parallel','2'])
    chroot(root,['env','DESTDIR='+guest+'/package','cmake','--install',guest+'/panel-build'])


def pack_deb(root, work, output, chroot):
    """Audit and package an installed stage without recompiling the upstream source."""
    record=json.loads((PROJECT/'build/rootfs/plasma-setup-source.json').read_text())
    version=record['version']+'-liuqin'+str(record.get('package_revision',1))
    upstream=work/('plasma-setup-'+record['version'])
    guest='/tmp/plasma-setup-build'
    package=work/'package'
    required=('usr/libexec/plasma-setup','usr/libexec/plasma-setup-bootutil',
              'usr/lib/systemd/system/plasma-setup.service','usr/lib/sysusers.d/plasma-setup-sysuser.conf')
    for name in required:
        if not (package/name).is_file():raise ValueError('Plasma Setup 安装文件缺失：'+name)
    # Audit architecture without depending on translated readelf descriptions.
    elf_files=[]
    for path in package.rglob('*'):
        if not path.is_file() or path.is_symlink():continue
        with path.open('rb') as stream:header=stream.read(20)
        if header[:4]!=b'\x7fELF':continue
        if header[4:6]!=b'\x02\x01' or header[18:20]!=b'\xb7\x00':
            raise ValueError('Plasma Setup 产物不是 ARM64 ELF：'+str(path))
        elf_files.append(path)
    # Scope the passwordless OEM actions to the active physical setup session.
    rule=package/'usr/share/polkit-1/rules.d/plasma-setup-polkit.rules'
    text=rule.read_text()
    original='if (subject.user !== "plasma-setup")'
    if text.count(original)!=1:raise ValueError('Plasma Setup 授权规则与固定版本不符。')
    text=text.replace(original,'if (subject.user !== "plasma-setup" || !subject.local || !subject.active)')
    rule.write_text(text)
    # dpkg-shlibdeps supplies the distribution library dependencies. QML and
    # session prerequisites are declared explicitly because they have no ELF edge.
    debian=work/'debian';debian.mkdir()
    (debian/'control').write_text('Source: liuqin-plasma-setup\nSection: admin\nPriority: optional\n'
                                 'Maintainer: Liuqin Linux Kit maintainers\n\n'
                                 'Package: liuqin-plasma-setup\nArchitecture: arm64\nDescription: Plasma first boot setup\n')
    library_dirs=sorted({str(Path(guest)/'package'/p.parent.relative_to(package)) for p in elf_files})
    arguments=['dpkg-shlibdeps','--ignore-missing-info','-O',*['-l'+p for p in library_dirs],
               *['-e'+str(Path(guest)/'package'/p.relative_to(package)) for p in elf_files]]
    command='cd '+guest+' && '+__import__('shlex').join(arguments)
    result=chroot(root,['sh','-c',command],capture_output=True,text=True)
    lines=[line.removeprefix('shlibs:Depends=') for line in result.stdout.splitlines() if line.startswith('shlibs:Depends=')]
    if len(lines)!=1 or not lines[0]:raise ValueError('Plasma Setup 运行库依赖记录为空。')
    dependencies=lines[0]+', '+', '.join(RUNTIME_PACKAGES)
    control=package/'DEBIAN';control.mkdir()
    (control/'control').write_text('Package: liuqin-plasma-setup\nVersion: '+version+'\n'
        'Architecture: arm64\nMaintainer: Liuqin Linux Kit maintainers\nSection: admin\nPriority: optional\n'
        'Provides: plasma-setup\nConflicts: plasma-setup\nReplaces: plasma-setup\nDepends: '+dependencies+'\n'
        'Description: KDE Plasma initial account and system setup for Liuqin\n'
        ' Built from the pinned KDE source; activated only for a fresh first-boot image.\n')
    docs=package/'usr/share/doc/liuqin-plasma-setup';docs.mkdir(parents=True)
    (docs/'copyright').write_text('Upstream: KDE Plasma Setup\nSource: '+record['url']+'\n'
        'Copyright: KDE contributors, including Kristen McWilliam, Carl Schwan and Devin Lin.\n'
        'Licenses: SPDX headers in upstream source; full texts shipped in LICENSES/.\n'
        'Liuqin changes: Ubuntu sudo group configuration, guarded completion and failed-account rollback.\n')
    shutil.copytree(upstream/'LICENSES',docs/'LICENSES')
    destination=output/('liuqin-plasma-setup_'+version+'_arm64.deb')
    subprocess.run(['dpkg-deb','--root-owner-group','--build',str(package),str(destination)],check=True)
    inventory=chroot(root,['dpkg-query','-W','-f=${binary:Package}\t${Version}\t${Architecture}\n'],capture_output=True).stdout
    (output/'setup-packages.tsv').write_bytes(inventory)
    # No development packages or source files enter the target rootfs: only this deb.
    print('Plasma Setup ARM64 软件包：'+str(destination),flush=True)
