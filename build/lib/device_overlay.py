# SPDX-License-Identifier: MIT
"""Stage desktop-neutral board files for device packages."""
import argparse
from pathlib import Path
import shutil

PROJECT = Path(__file__).resolve().parents[2]


def overlay_files(source):
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f'overlay directory is unavailable: {source}')
    for path in sorted(source.rglob('*')):
        # Keep package activation unchanged: builders add any required links.
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(source)
        if (path.name == 'README.md' or '__pycache__' in relative.parts
                or path.suffix in ('.pyc', '.pyo')):
            continue
        name = relative.as_posix()
        if any(name == prefix or name.startswith(prefix + '/')
               for prefix in ('usr/lib/firmware',)):
            continue
        yield name, path


def stage_overlay(root, *, board_overlay):
    root = Path(root)
    files = {}
    # Validate the complete payload before copying any files.
    for name, path in overlay_files(board_overlay):
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
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        stage_overlay(args.out, board_overlay=args.board)
    except (OSError, ValueError) as error:
        parser.exit(1, f'device overlay: {error}\n')


if __name__ == '__main__':
    main()
