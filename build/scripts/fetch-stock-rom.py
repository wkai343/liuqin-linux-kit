#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""获取固定 OS2.0.6.0 Fastboot ROM，只提取构建所需镜像。"""
import argparse
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from stock_rom import fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=PROJECT / 'inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images')
    parser.add_argument('--cache', type=Path, default=PROJECT / 'inputs/downloads/stock-rom')
    parser.add_argument('--archive', type=Path, help='使用已下载的同一份完整 ROM；不重新下载')
    args = parser.parse_args()
    record = json.loads((PROJECT / 'build/data/stock-rom-source.json').read_text())
    reference = json.loads((PROJECT / 'shared/liuqin-rom-images.json').read_text())
    fetch(args.out, args.cache, record, reference, archive=args.archive)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('原厂 ROM 准备停止：' + str(error), file=sys.stderr)
        sys.exit(1)
