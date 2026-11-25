# SPDX-License-Identifier: MIT
"""Build the desktop shutter integration from Fedora's Kamoso source RPM."""
import fnmatch
import json
import tomllib
import shutil

from source_build import PROJECT, sha

RECIPE = PROJECT / 'build/kamoso'


def record():
    return json.loads((RECIPE / 'source.json').read_text())


def nvr():
    info = record()
    return f"kamoso-{info['version']}-{info['release']}.{info['dist']}"


def build(root, archive, output, jobs, chroot, dnf):
    if jobs < 1 or sha(archive) != record()['sha256']:
        raise ValueError('Invalid Kamoso source or job count')
    guest = '/tmp/liuqin-kamoso'
    work = root / guest.lstrip('/')
    shutil.copytree(RECIPE, work)
    shutil.copyfile(archive, work / 'fedora.src.rpm')
    dnf(root, 'install', 'rpm-build', 'redhat-rpm-config', 'cpio', 'dnf5-plugins')
    chroot(root, ['python3', guest + '/prepare.py', guest + '/fedora.src.rpm', guest + '/rpm'])
    spec = guest + '/rpm/SPECS/kamoso.spec'
    dnf(root, 'builddep', spec)
    chroot(root, ['rpmbuild', '-ba', '--define', '_topdir ' + guest + '/rpm',
                  '--define', '_smp_mflags -j' + str(jobs), spec])
    shutil.copyfile(work / 'rpm/RPMS/aarch64' / (nvr() + '.aarch64.rpm'), output / (nvr() + '.aarch64.rpm'))
    shutil.copyfile(work / 'rpm/SRPMS' / (nvr() + '.src.rpm'), output / (nvr() + '.src.rpm'))

def check_image(root, chroot):
    installed = chroot(root, ['rpm', '-q', 'kamoso'], capture_output=True, text=True).stdout.strip()
    if installed != nvr() + '.aarch64':
        raise ValueError('桌面缺少后摄闪光版 Kamoso。')
    locks = tomllib.loads((root/'etc/dnf/versionlock.toml').read_text())
    entries = [e for e in locks['packages'] if fnmatch.fnmatchcase('kamoso', e['name'])]
    condition = [{'key': 'evr', 'comparator': '=', 'value': nvr().removeprefix('kamoso-')}]
    if len(entries) != 1 or entries[0]['name'] != 'kamoso' or entries[0]['conditions'] != condition:
        raise ValueError('镜像 Kamoso 版本锁不匹配。')
