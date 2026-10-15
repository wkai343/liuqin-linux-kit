#!/bin/sh
# SPDX-License-Identifier: MIT
# Assemble three files; bundle.json is not needed. Firmware/ROM inputs use
# the same environment variables as build-liuqin-native-boot.sh.
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
root_out=${CUSTOM_ROOT_OUT:?set CUSTOM_ROOT_OUT to build-liuqin-custom-root.py output}
installer_image=${INSTALLER_IMAGE:?set INSTALLER_IMAGE to a reusable installer.img}
dest=${OUT_DIR:-$project/out/releases/custom}
work=${BUILD_WORK_DIR:-$project/out/build/custom-bundle}
die() { printf 'build-liuqin-custom-bundle: %s\n' "$*" >&2; exit 1; }
case $dest in "$project"/out/*) ;; *) die 'output must be below project out/' ;; esac
case $dest in */../*|*/..|*/./*) die 'output path must not contain traversal' ;; esac
case $work in "$project"/out/*) ;; *) die 'work directory must be below project out/' ;; esac
case $work in */../*|*/..|*/./*) die 'work directory must not contain traversal' ;; esac
[ ! -e "$work" ] && [ ! -L "$work" ] || die 'use a fresh build work directory'
[ "$dest" != "$work" ] || die 'package and build directories must differ'
case $dest in "$work"/*) die 'package directory must be outside the build work directory' ;; esac
case $work in "$dest"/*) die 'build work directory must be outside the package directory' ;; esac
[ ! -e "$dest" ] && [ ! -L "$dest" ] || die 'use a fresh output directory'
[ -f "$root_out/rootfs.tar.gz" ] || die 'custom root output incomplete'
profile=native-updatable
[ -f "$root_out/native-root.contract" ] || die 'root contract missing'
[ -f "$installer_image" ] && [ ! -L "$installer_image" ] || die 'reusable installer image missing'
# The source epoch, board firmware checks and ABL-compatible image format are
# shared with the product builder. No fixed desktop or kernel config is used.
if [ -n "${BOOT_IMAGE:-}" ]; then
    [ -f "$BOOT_IMAGE" ] && [ ! -L "$BOOT_IMAGE" ] || die 'target boot image missing'
else
    LIUQIN_ROOT_PROFILE="$profile" \
        INSTALLER_RUNTIME= OUT_DIR="$work/boot" sh "$project/build/scripts/build-liuqin-native-boot.sh"
    BOOT_IMAGE="$work/boot/boot-liuqin-native.img"
fi
mkdir -p "$work/package"
cp "$BOOT_IMAGE" "$work/package/boot.img"
cp "$installer_image" "$work/package/installer.img"
cp "$root_out/rootfs.tar.gz" "$work/package/rootfs.tar.gz"
python3 - "$project/shared" "$work/package" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from liuqin_artifacts import inspect_package
print(inspect_package(Path(sys.argv[2])))
PY
mkdir -p "$(dirname -- "$dest")"
mv "$work/package" "$dest"
printf 'Custom package complete: %s (installer.img, boot.img, rootfs.tar.gz)\n' "$dest"
