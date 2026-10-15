#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export the desktop installer without build sources, tools, images or device data."""
import argparse
from pathlib import Path
import sys
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'shared'))
from liuqin_identity import PROJECT_NAME, INSTALLER_ARCHIVE_ROOT, UPSTREAM_PROJECT, PROJECT_URL


def package(output):
    output = output.resolve()
    if output.exists():
        raise RuntimeError('输出已存在，未覆盖：' + str(output))
    sources = {}
    # Only source directories belong in the standalone package. In particular,
    # installer/.venv is a local environment, not part of the application.
    for name in ('installer/liuqin_gui', 'shared', 'device/installer'):
        for path in (PROJECT / name).rglob('*'):
            parts = path.relative_to(PROJECT).parts
            if (path.is_file() and not path.is_symlink() and
                not any(part.startswith('.') or part == '__pycache__' for part in parts) and
                path.suffix != '.pyc'):
                sources[path.relative_to(PROJECT).as_posix()] = path
    for name in ('install-gui.py', 'install-liuqin.py', 'package.py', 'README.md',
                 'pyproject.toml', 'uv.lock', '.python-version', 'start-windows.cmd', 'windows-network.ps1'):
        sources['installer/' + name] = PROJECT / 'installer' / name
    for name in ('LICENSE', 'NOTICE', 'CREDITS.md', 'installer/requirements.txt', 'docs/INSTALLER-GUI.md'):
        sources[name] = PROJECT / name
    required = ('installer/install-gui.py', 'installer/install-liuqin.py', 'installer/liuqin_gui/worker.py',
                'installer/liuqin_gui/qml/Main.qml', 'shared/liuqin_artifacts.py', 'shared/liuqin_layout.py',
                'shared/liuqin-rom-images.json', 'device/installer/install-root.sh')
    if any(name not in sources or not sources[name].is_file() for name in required):
        raise RuntimeError('安装器源码缺少必要入口、公共模块或设备脚本。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, source in sorted(sources.items()):
            if name == 'docs/INSTALLER-GUI.md':
                body = source.read_text().replace('[构建指南](BUILD.md)', '完整项目的 docs/BUILD.md')
                archive.writestr(INSTALLER_ARCHIVE_ROOT + '/' + name, body)
            else:
                archive.write(source, INSTALLER_ARCHIVE_ROOT + '/' + name)
        archive.writestr(INSTALLER_ARCHIVE_ROOT + '/README.md',
            '# ' + PROJECT_NAME + ' 安装助手\n\n'
            '[项目仓库](' + PROJECT_URL + ') · [问题反馈](' + PROJECT_URL + '/issues)。\n\n'
            '使用 uv 管理安装器依赖和 Python，首次运行自动准备环境。\n\n'
            '启动：`uv run --project installer installer/install-gui.py`。\n\n'
            'Windows 10/11 x64：双击 `installer/start-windows.cmd`；官方 platform-tools 目录与 installer 并列。\n\n'
            'Windows USB 驱动和网络配置见 docs/INSTALLER-GUI.md；真机流程仍待验收。\n\n'
            '指定镜像目录：`uv run --project installer installer/install-gui.py --bundle /path/to/images`。\n\n'
            '镜像目录只需要 installer.img、boot.img、rootfs.tar.gz。打开界面不会开始写入。\n\n'
            '本包不含镜像、备份或构建工具。镜像构建使用完整项目的 build/ 与 Devbox。\n\n'
            '本项目衍生自 yzddmr6 的 ' + UPSTREAM_PROJECT + '。\n'
            '原作者与第三方署名见 CREDITS.md；原许可证及第三方声明见 LICENSE 和 NOTICE。\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=PROJECT / 'out/releases/liuqin-linux-kit-installer-source.zip')
    args = parser.parse_args()
    print(package(args.out))


if __name__ == '__main__':
    main()
