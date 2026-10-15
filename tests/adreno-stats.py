#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile the real read-only Adreno reader and apply the pinned upstream patch."""
import argparse
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'build/lib'))
from fedora_stats import source

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-archive',type=Path)
args=parser.parse_args()
if not shutil.which('c++') or not shutil.which('pkg-config'):
    raise SystemExit('Adreno 检查需要 C++ 编译器和 pkg-config。')
with tempfile.TemporaryDirectory() as directory:
    work=Path(directory)
    flags=subprocess.check_output(['pkg-config','--cflags','--libs','Qt6Core'],text=True)
    subprocess.run(['c++','-std=c++17','-fPIC','-I'+str(PROJECT/'device/ksystemstats'),
        str(PROJECT/'tests/adreno-sysfs.cpp'),'-o',str(work/'reader'),*shlex.split(flags)],check=True)
    subprocess.run([str(work/'reader')],check=True)
    archive=args.source_archive or PROJECT/'inputs/ksystemstats/ksystemstats-6.7.5.tar.xz'
    if archive.is_file():
        source(work,archive)
        print('PASS: pinned KSystemStats source and exact upstream Adreno patch')
    else:
        print('Adreno reader passed; pinned source patch check requires the downloaded archive.')
