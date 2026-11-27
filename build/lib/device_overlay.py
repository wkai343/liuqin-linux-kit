# SPDX-License-Identifier: MIT
"""Stage desktop-neutral board files for device packages."""
import argparse
from pathlib import Path
import shutil

PROJECT = Path(__file__).resolve().parents[2]
SENSOR_CLAIM_RULE = 'etc/polkit-1/rules.d/49-liuqin-sensorproxy.rules'
RETIRED_SENSOR_FILES = (
    'etc/systemd/system/liuqin-sensor-proxy-refresh.service',
    'etc/systemd/system/graphical.target.wants/liuqin-sensor-proxy-refresh.service',
    'etc/systemd/user/liuqin-sensor-proxy-session-refresh.service',
    'etc/systemd/user/graphical-session.target.wants/liuqin-sensor-proxy-session-refresh.service',
    'usr/local/sbin/liuqin-sensor-proxy-refresh',
    'usr/local/sbin/liuqin-sensor-proxy-session-refresh',
    'etc/polkit-1/rules.d/50-liuqin-gnome-sensor-refresh.rules',
)


def remove_sensor_mock(root):
    """Keep libssc's build-time mock server out of runtime payloads and old bundles."""
    root = Path(root).resolve()
    for libdir in ('usr/lib', 'usr/lib64'):
        for layout in ('site-packages', 'dist-packages'):
            for module in (root / libdir).glob(f'python3*/{layout}/ssc_server'):
                if not module.parent.resolve().is_relative_to(root):
                    raise ValueError(f'sensor mock path outside package: {module}')
                if module.is_symlink():
                    module.unlink()
                else:
                    shutil.rmtree(module)


def stage_sensor_policy(root):
    """Strip the old login workaround, including the restart grant in older bundles."""
    root = Path(root).resolve()
    remove_sensor_mock(root)
    for name in (*RETIRED_SENSOR_FILES, SENSOR_CLAIM_RULE):
        path = root / name
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f'sensor policy path outside package: {path}')
        path.unlink(missing_ok=True)
    rule = root / SENSOR_CLAIM_RULE
    rule.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PROJECT / 'device/sensors-overlay' / SENSOR_CLAIM_RULE, rule)


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--board', type=Path, default=PROJECT / 'device/board-overlay')
    mode.add_argument('--sensor-policy', action='store_true')
    mode.add_argument('--remove-sensor-mock', action='store_true')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.remove_sensor_mock:
            remove_sensor_mock(args.out)
        elif args.sensor_policy:
            stage_sensor_policy(args.out)
        else:
            stage_overlay(args.out, board_overlay=args.board)
    except (OSError, ValueError) as error:
        parser.exit(1, f'device overlay: {error}\n')


if __name__ == '__main__':
    main()
