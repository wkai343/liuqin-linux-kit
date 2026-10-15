#!/bin/sh
# SPDX-License-Identifier: MIT

set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
exec python3 "$project_root/build/lib/aosp_mkbootimg.py"
