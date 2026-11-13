#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared source extraction, C execution and reporting for kernel regressions."""
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(source, name):
    match = re.search(r'^(?:static )?(?:int|void|bool|unsigned (?:long|int))\s+'
                      + re.escape(name) + r'\([^;{]*\)\s*\{', source, re.M)
    if not match:
        raise ValueError(name)
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


def run_c(code, *, flags=(), timeout=15, cc='cc'):
    """Compile actual driver functions with UBSan and return their stdout."""
    with tempfile.TemporaryDirectory(prefix='liuqin-kernel-test-') as temporary:
        source = Path(temporary) / 'test.c'
        binary = source.with_suffix('')
        source.write_text(code)
        subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=undefined', '-fno-sanitize-recover=all',
                        *flags, str(source), '-o', str(binary)], check=True)
        return subprocess.check_output([str(binary)], text=True, timeout=timeout)


def write_report(checks, path=None):
    result = {'status': 'PASS', 'checks': checks, 'hardware_validated': False}
    text = json.dumps(result, indent=2) + '\n'
    if path:
        path.write_text(text)
    print(text, end='')
