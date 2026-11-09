#!/bin/sh
# SPDX-License-Identifier: MIT
set -eu
exec /usr/bin/python3 -I -B /usr/lib/liuqin/android-device.py "$@"
