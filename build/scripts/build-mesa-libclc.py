#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the pinned Mesa libclc RPM without modifying the host or tablet."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
from mesa_libclc import build

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    print(build(args.out))
