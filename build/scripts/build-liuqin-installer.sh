#!/bin/sh
# SPDX-License-Identifier: MIT
# Build a reusable RAM installer from its own kernel and runtime; no target root.
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
kernel_source=${INSTALLER_KERNEL_SOURCE:?set INSTALLER_KERNEL_SOURCE to the installer kernel source}
kernel_out=${INSTALLER_KERNEL_OUT:?set INSTALLER_KERNEL_OUT to the installer kernel build}
runtime=${INSTALLER_RUNTIME:?set INSTALLER_RUNTIME to the prepared ARM64 runtime}
out=${OUT_DIR:-$project/out/build/installer}
# The runtime and firmware are installation tools, independent of the target
# kernel, modules, desktop and native-root manifest.
KERNEL_SOURCE="$kernel_source" KERNEL_OUT="$kernel_out" \
    KERNEL_IMAGE="$kernel_out/arch/arm64/boot/Image" \
    KERNEL_DTB="$kernel_out/arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb" \
    LIUQIN_ROOT_PROFILE=installer INSTALLER_RUNTIME="$runtime" \
    OUT_DIR="$out" sh "$project/build/scripts/build-liuqin-native-boot.sh"
mv "$out/boot-liuqin-native.img" "$out/installer.img"
python3 - "$project/shared" "$out/installer.img" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from liuqin_artifacts import boot_interface
print(boot_interface(Path(sys.argv[2]), installer=True))
PY
