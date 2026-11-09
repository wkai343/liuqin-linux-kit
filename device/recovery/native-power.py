#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Use the same guarded Recovery exit for computer and tablet power actions."""
import importlib.util
import os
from pathlib import Path
import sys

if __name__ == '__main__':
    if os.geteuid() != 0 or os.uname().machine != 'aarch64' or len(sys.argv) != 2:
        raise SystemExit('native ARM64 Recovery root and one power action required')
    path = Path('/usr/lib/liuqin/native-ui-service.py')
    spec = importlib.util.spec_from_file_location('native_power_service', path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    service.Operations().exit_recovery(sys.argv[1])
