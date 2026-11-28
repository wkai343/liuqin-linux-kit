#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare the Fedora Kamoso spec and sources in a fresh rpmbuild directory."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def prepare(archive, top):
    recipe = Path(__file__).resolve().parent
    lock = json.loads((recipe / 'source.json').read_text())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']:
        raise ValueError('Kamoso SRPM digest mismatch')
    top.mkdir(parents=True, exist_ok=False)
    sources = top / 'SOURCES'
    sources.mkdir()
    specs = top / 'SPECS'
    specs.mkdir()
    with tempfile.TemporaryFile() as payload:
        subprocess.run(['rpm2cpio', str(archive)], stdout=payload, check=True)
        payload.seek(0)
        subprocess.run(['cpio', '-id', '--quiet', './kamoso.spec', f"./kamoso-{lock['version']}.tar.xz"],
                       stdin=payload, cwd=sources, check=True)
    shutil.copyfile(recipe / 'liuqin-camera-focus', sources / 'liuqin-camera-focus')
    tags = ['Source1: liuqin-camera-focus', 'Requires: pipewire-utils, python3',
            'BuildRequires: pkgconfig(gstreamer-pbutils-1.0)']
    for index, patch in enumerate(lock['patches'], 9000):
        shutil.copyfile(recipe / patch, sources / patch)
        tags.append(f'Patch{index}: {patch}')
    spec = (sources / 'kamoso.spec').read_text()
    original = 'Release: 1%{?dist}'
    if spec.count(original) != 1 or spec.count('%autosetup -p1') != 1:
        raise ValueError('Unexpected Fedora Kamoso spec')
    spec = spec.replace(original, 'Release: ' + lock['release'] + '%{?dist}\n' + '\n'.join(tags))
    spec = spec.replace('%cmake_install\n', '%cmake_install\ninstall -Dm0755 %{SOURCE1} %{buildroot}%{_libexecdir}/kamoso-liuqin-focus\n', 1)
    spec = spec.replace('%{_kf6_bindir}/kamoso\n', '%{_kf6_bindir}/kamoso\n%{_libexecdir}/kamoso-liuqin-focus\n', 1)
    # Qt keys its on-disk QML cache by resource URL and timestamp. RPM gives
    # same-day changelogs the same epoch, so distinguish our package revisions.
    revision = int(lock['release'].split('.liuqin')[1])
    spec = spec.replace('%cmake_build\n',
                        f'export SOURCE_DATE_EPOCH=$((${{SOURCE_DATE_EPOCH:-1795564800}} + {revision}))\n%cmake_build\n', 1)
    downstream = (f"* Sat Nov 28 2026 Liuqin Linux Kit <noreply@localhost> - {lock['version']}-{lock['release']}\n"
                  "- Show the active preview camera when the settings page opens.\n\n"
                  "* Wed Nov 25 2026 Liuqin Linux Kit <noreply@localhost> - 26.08.3-1.liuqin15\n"
                  "- Allow 120 ms of lateness for the onboard rear viewfinder.\n\n"
                  "* Wed Nov 25 2026 Liuqin Linux Kit <noreply@localhost> - 26.08.3-1.liuqin14\n"
                  "- Share the rear recording clock and retain the negotiated native mode.\n\n"
                  "* Wed Nov 25 2026 Liuqin Linux Kit <noreply@localhost> - 26.08.3-1.liuqin13\n"
                  "- Record H.264 through the V4L2 hardware encoder in Matroska.\n\n"
                  "* Wed Nov 25 2026 Liuqin Linux Kit <noreply@localhost> - 26.08.3-1.liuqin11\n"
                  "- Focus the rear lens on the selected preview region.\n\n"
                  "* Wed Nov 25 2026 Liuqin Linux Kit <noreply@localhost> - 26.08.3-1.liuqin8\n"
                  "- Label the rear flash mode On in Simplified Chinese.\n\n"
                  "* Wed Nov 18 2026 Liuqin Linux Kit <noreply@localhost> - 26.08.3-1.liuqin7\n"
                  "- Arm rear flash capture before firing and preserve complete burst frames.\n")
    (specs / 'kamoso.spec').write_text(spec.replace('%changelog\n', '%changelog\n' + downstream + '\n', 1))
    (sources / 'kamoso.spec').unlink()


if __name__ == '__main__':
    prepare(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
