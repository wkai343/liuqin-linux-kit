# SPDX-License-Identifier: MIT
"""Record builtin root filesystem drivers in an initramfs, without device access."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
from liuqin_filesystems import kernel_support

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kernel-config', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
try:
    names = kernel_support(args.kernel_config.read_text())
    with args.output.open('xb') as target:
        target.write(('\n'.join(names) + '\n').encode('ascii'))
    args.output.chmod(0o644)
except (ValueError, OSError) as error:
    parser.error(str(error))
