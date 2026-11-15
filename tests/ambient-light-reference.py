#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify the brightness reference against the exact stock vendor resource APK."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-overlay', type=Path, required=True)
    parser.add_argument('--aapt2', default='aapt2')
    args = parser.parse_args()
    reference = json.loads((PROJECT / 'build/rootfs/liuqin-auto-brightness.json').read_text())
    with args.stock_overlay.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != reference['source']['sha256']:
        raise SystemExit('Stock overlay SHA-256 mismatch; refusing a different ROM resource')
    result = subprocess.run([args.aapt2, 'dump', 'resources', str(args.stock_overlay)],
                            capture_output=True, text=True, check=True, timeout=30)
    for name in ('config_autoBrightnessLevels', 'config_autoBrightnessLcdBacklightValues'):
        match = re.search(r'array/' + name + r'\n\s*\(\) \(array\) size=(\d+)\s*\[([^]]+)\]', result.stdout)
        if match is None:
            raise SystemExit('Missing default stock array: ' + name)
        values = [int(value.strip()) for value in match[2].split(',')]
        if len(values) != int(match[1]) or values != reference[name]:
            raise SystemExit('Stock reference array mismatch: ' + name)
    print('PASS: both brightness arrays match the SHA-256-pinned stock vendor APK')


if __name__ == '__main__':
    main()
