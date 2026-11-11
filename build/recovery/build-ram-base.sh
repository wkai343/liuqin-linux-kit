#!/bin/sh
# SPDX-License-Identifier: MIT
# Assemble the intermediate RAM base consumed by build-native.py; no target OS.
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
kernel_source=${RECOVERY_KERNEL_SOURCE:?set RECOVERY_KERNEL_SOURCE to the Recovery kernel source}
kernel_out=${RECOVERY_KERNEL_OUT:?set RECOVERY_KERNEL_OUT to the Recovery kernel build}
runtime=${RECOVERY_RUNTIME:?set RECOVERY_RUNTIME to the prepared ARM64 runtime}
out=${OUT_DIR:-$project/out/build/recovery-base}
# The runtime and firmware are installation tools, independent of the target
# kernel, modules, desktop and native-root manifest.
KERNEL_SOURCE="$kernel_source" KERNEL_OUT="$kernel_out" \
    BUSYBOX="$runtime/bin/busybox" \
    KERNEL_IMAGE="$kernel_out/arch/arm64/boot/Image" \
    KERNEL_DTB="$kernel_out/arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb" \
    LIUQIN_ROOT_PROFILE=installer INSTALLER_RUNTIME="$runtime" \
    OUT_DIR="$out" sh "$project/build/scripts/build-liuqin-native-boot.sh"
mv "$out/boot-liuqin-native.img" "$out/recovery-base.img"
python3 - "$project/shared" "$out/recovery-base.img" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from liuqin_artifacts import boot_interface
print(boot_interface(Path(sys.argv[2]), installer=True))
PY
