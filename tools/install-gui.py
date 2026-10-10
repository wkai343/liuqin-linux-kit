#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the QML installer, or its isolated JSON worker."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Simulate; never access USB or a tablet')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--screenshot', type=Path, help='Save an initial offscreen preview and exit')
    args = parser.parse_args()
    if args.screenshot and not args.demo:
        parser.error('--screenshot requires --demo')
    if args.worker:
        from liuqin_gui.worker import main as worker_main
        worker_main(args)
    else:
        from liuqin_gui.app import main as app_main
        app_main(args)


if __name__ == '__main__':
    main()
