# SPDX-License-Identifier: MIT
"""Install an optional Firefox launcher without changing distribution files."""
from pathlib import Path
import shutil

from source_build import PROJECT

SOURCE = PROJECT/'build/firefox'


def install(root):
    root = Path(root)
    if not all((root/name).is_file() for name in
               ('usr/bin/firefox', 'usr/bin/bwrap', 'usr/lib64/libavcodec.so.63',
                'usr/lib64/libavutil.so.61')):
        return False
    files = (
        ('liuqin-firefox', 'usr/local/libexec/liuqin-firefox', 0o755),
        ('compatibility.json', 'usr/local/share/liuqin-firefox/compatibility.json', 0o644),
        ('README.md', 'usr/local/share/liuqin-firefox/README.md', 0o644),
    )
    entries = ('usr/local/bin/firefox', 'usr/local/bin/liuqin-firefox')
    # An overlay may deliberately supply another launcher. Do not replace it.
    targets = [root/name for _, name, _ in files] + [root/name for name in entries]
    if any(path.exists() or path.is_symlink() for path in targets):
        return False
    mounts = [root/'usr/local/libexec/liuqin-firefox-host'/name for name in ('bin', 'firefox')]
    if any(not path.resolve().is_relative_to(root.resolve()) for path in targets + mounts):
        raise ValueError('Firefox media installation path escapes root')
    for source, destination, mode in files:
        target = root/destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE/source, target)
        target.chmod(mode)
    for path in mounts:
        path.mkdir(parents=True, exist_ok=True)
    for name in entries:
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to('../libexec/liuqin-firefox')
    return True
