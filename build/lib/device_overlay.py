# SPDX-License-Identifier: MIT
"""Compose board files and optional GNOME integration for device packages."""
import argparse
from pathlib import Path
import shutil

PROJECT = Path(__file__).resolve().parents[2]

# These legacy Ubuntu integrations are still shipped by the GNOME DEB profile.
# They do not depend on GNOME, but the current generic profiles do not enable
# Snap, flash-kernel or the old image-switching installation layout.
LEGACY_UBUNTU_FILES = (
    'etc/flash-kernel',
    'etc/liuqin-gnome-root',
    'etc/systemd/system/liuqin-snap-root-admission.service',
    'usr/local/sbin/liuqin-snap-root-admission',
    'usr/local/sbin/liuqin-switch',
    'usr/local/share/applications/liuqin-reboot-to-android.desktop',
    'usr/share/polkit-1/actions/io.github.liuqin.switch.policy',
)


def overlay_files(source, excluded=()):
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f'overlay directory is unavailable: {source}')
    for path in sorted(source.rglob('*')):
        # Keep package activation unchanged: builders add any required links.
        # In particular, dormant legacy apt masks are not package payloads.
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(source)
        if (path.name == 'README.md' or '__pycache__' in relative.parts
                or path.suffix in ('.pyc', '.pyo')):
            continue
        name = relative.as_posix()
        if any(name == prefix or name.startswith(prefix + '/')
               for prefix in ('usr/lib/firmware', *excluded)):
            continue
        yield name, path


def stage_overlay(root, *, board_overlay, gnome_overlay=None, desktop='generic'):
    if desktop not in ('generic', 'gnome'):
        raise ValueError(f'unsupported desktop profile: {desktop}')
    layers = [(board_overlay, LEGACY_UBUNTU_FILES if desktop == 'generic' else ())]
    if desktop == 'gnome':
        if gnome_overlay is None:
            raise ValueError('GNOME overlay is required for the GNOME profile')
        layers.append((gnome_overlay, ('etc/gdm3/custom.conf',)))

    root = Path(root)
    files = {}
    # Validate the entire composition before copying: one layer must not
    # silently replace a board policy supplied by another layer.
    for source, excluded in layers:
        for name, path in overlay_files(source, excluded):
            if name in files:
                raise ValueError(f'overlapping overlay path: {name}')
            target = root / name
            if target.exists() or target.is_symlink():
                raise ValueError(f'overlay target already exists: {target}')
            files[name] = path

    for name in files:
        for parent in Path(name).parents:
            if parent == Path('.'):
                continue
            if parent.as_posix() in files:
                raise ValueError(f'overlapping overlay path: {parent}')
            target = root / parent
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(f'overlay parent is not a directory: {target}')

    for name, source in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', type=Path, default=PROJECT / 'device/board-overlay')
    parser.add_argument('--gnome', type=Path, default=PROJECT / 'device/gnome-overlay')
    parser.add_argument('--desktop', choices=('generic', 'gnome'), default='generic')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        stage_overlay(args.out, board_overlay=args.board,
                      gnome_overlay=args.gnome, desktop=args.desktop)
    except (OSError, ValueError) as error:
        parser.exit(1, f'device overlay: {error}\n')


if __name__ == '__main__':
    main()
