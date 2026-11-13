#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package optional Liuqin resume compensation without replacing Fedora packages."""
import argparse
from pathlib import Path
import shutil
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from rpm_package import build


def package(output):
    source = PROJECT / 'device/resume-display'
    with tempfile.TemporaryDirectory(prefix='resume-display-') as work:
        root = Path(work)
        for origin, target in (
            ('liuqin-resume-display', 'usr/libexec/liuqin-resume-display'),
            ('liuqin-resume-display.service', 'usr/lib/systemd/user/liuqin-resume-display.service'),
            ('README.md', 'usr/share/doc/liuqin-resume-display/README.md'),
        ):
            destination = root / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / origin, destination)
            destination.chmod(0o755 if origin == 'liuqin-resume-display' else 0o644)
        license_path = root / 'usr/share/licenses/liuqin-resume-display/LICENSE'
        license_path.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT / 'LICENSE', license_path)
        return build(root, 'liuqin-resume-display', '0.1.2', output, architecture='noarch',
                     requires=['python3', 'python3-dbus', 'python3-gobject-base', 'libkscreen', 'systemd'],
                     summary='Bounded input-wake display compensation for Liuqin Plasma')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    print(package(parser.parse_args().out))
