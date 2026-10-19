#!/bin/sh
# SPDX-License-Identifier: MIT
# Assemble a native boot image from kernel build outputs and a root manifest.
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
python3 "$project_root/build/lib/usb_identity.py"
out_dir=${OUT_DIR:-"$project_root/out/build/native-boot"}
kernel_source=${KERNEL_SOURCE:-"$project_root/../linux-sm8450-liuqin"}
kernel_out=${KERNEL_OUT:-"$project_root/out/build/kernel"}
root_profile=${LIUQIN_ROOT_PROFILE:-native-updatable}
audio_topology=${AUDIO_TOPOLOGY:?"set AUDIO_TOPOLOGY to the built AudioReach topology"}
firmware_pool=${FIRMWARE_POOL:?"set FIRMWARE_POOL to the prepared firmware input directory"}
wlan_tuple=${WLAN_HSP2_TUPLE:?"set WLAN_HSP2_TUPLE to the prepared WLAN tuple"}
stock_overlay_dir=${STOCK_OVERLAY_DIR:?"set STOCK_OVERLAY_DIR to the extracted stock DTBO entries"}
stock_base_dir=${STOCK_BASE_DIR:?"set STOCK_BASE_DIR to the extracted stock vendor_boot DTBs"}
mk_dir=${MKBOOTIMG_DIR:-"$project_root/inputs/aosp-mkbootimg"}
slot_cc=${SLOT_SUCCESS_GCC:-aarch64-linux-gnu-gcc}
dtc=$kernel_out/scripts/dtc/dtc
fdtoverlay=$kernel_out/scripts/dtc/fdtoverlay
image=${KERNEL_IMAGE:-$kernel_out/arch/arm64/boot/Image}
raw_dtb=${KERNEL_DTB:-$kernel_out/arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb}

die() { printf 'build-liuqin-native-boot: %s\n' "$*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
case $root_profile in native-updatable|installer) ;; *) die 'unsupported root profile' ;; esac
if [ "$root_profile" = installer ]; then
    [ -n "${INSTALLER_RUNTIME:-}" ] || die 'independent installer requires INSTALLER_RUNTIME'
    cmdline=$(cat "$project_root/device/installer-bootargs.txt")
elif [ -n "${INSTALLER_RUNTIME:-}" ]; then
    die 'build the RAM image separately with build-liuqin-installer.sh'
else
    cmdline=$(cat "$project_root/device/native-bootargs.txt")
fi

case $out_dir in
"$project_root"/out/*) ;;
*) die "output must be below $project_root/out" ;;
esac
case $out_dir in */../*|*/..|*/./*) die 'output path must not contain traversal' ;; esac
[ ! -e "$out_dir" ] && [ ! -L "$out_dir" ] || die "output already exists: $out_dir"
for input in "$image" "$raw_dtb" "$audio_topology" "$dtc" "$fdtoverlay"; do
	[ -f "$input" ] || die "input missing: $input"
done
[ -d "$firmware_pool" ] && [ -d "$wlan_tuple" ] &&
	[ -d "$stock_overlay_dir" ] && [ -d "$stock_base_dir" ] || die 'firmware/ROM input directory missing'
[ "$(sha "$mk_dir/unpack_bootimg.py")" = 06b54dd9a07c5281778e29e234e76f6e3faee8bf0c904a5ef88fdee30eeed12e ] ||
	die 'unpack_bootimg.py identity mismatch'
[ "$(sha "$mk_dir/mkbootimg.py")" = 37d84b3d162e0bc62e36c1f4e1c63c85ea0caa9f29be023eb2f8efe006ad948c ] ||
	die 'mkbootimg.py identity mismatch'

python3 - "$kernel_source" "$kernel_out" <<'PYTHON'
import hashlib, json, re, subprocess, sys
from pathlib import Path
source, out = map(Path, sys.argv[1:3])
commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain']):
    raise SystemExit('kernel source is dirty')
config_hash = hashlib.sha256((out / '.config').read_bytes()).hexdigest()
info = json.loads((out / 'build-info.json').read_text())
if info['commit'] != commit or info['config_sha256'] != config_hash:
    raise SystemExit('kernel output differs from its own build identity')
subprocess.run(['sha256sum', '-c', '--quiet', 'SHA256SUMS'], cwd=out, check=True)
release = (out / 'include/config/kernel.release').read_bytes()
if not re.fullmatch(rb'[A-Za-z0-9._+-]{1,255}\n', release):
    raise SystemExit('kernel release is invalid')
PYTHON
source_epoch=$(git -C "$kernel_source" show -s --format=%ct HEAD)
if [ "$root_profile" = native-updatable ] && [ -n "${BOOT_SPLASH_RUNTIME:-}" ]; then
    cmdline=$(python3 "$project_root/build/lib/splash_assets.py" --runtime "$BOOT_SPLASH_RUNTIME" \
        --kernel-config "$kernel_out/.config" --bootargs="$cmdline")
fi
charger_mode=auto
storage_mode=persistent
if [ "$root_profile" = installer ]; then charger_mode=0; storage_mode=readonly; fi
ramdisk_compression=gzip
ramdisk_suffix=gz
if [ "$root_profile" = native-updatable ] && grep -qx 'CONFIG_RD_ZSTD=y' "$kernel_out/.config"; then
    ramdisk_compression=zstd
    ramdisk_suffix=zst
fi
mkdir -p "$out_dir"
case ${LIUQIN_RECLAIM_MTE:-0} in
0) ;;
1)
    [ "$root_profile" != installer ] || die 'target memory cleanup does not belong in the independent installer'
    python3 "$project_root/build/lib/reclaim-memory.py" --input "$raw_dtb" \
        --output "$out_dir/mte-clean.dtb" --fdtput "${FDTPUT:-fdtput}"
    raw_dtb=$out_dir/mte-clean.dtb
    ;;
*) die 'LIUQIN_RECLAIM_MTE must be 0 or 1' ;;
esac

if [ "$root_profile" = native-updatable ]; then
python3 - "$project_root/shared" "$kernel_out" "${KERNEL_MODULES_DIR:?set KERNEL_MODULES_DIR}" "$out_dir/native-root.contract" "$image" <<'CONTRACT'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from liuqin_kernel import build_contract, parse_contract, digest
content = build_contract(Path(sys.argv[2]), Path(sys.argv[3]))
if digest(sys.argv[5]) != parse_contract(content)['image_sha256']:
    raise SystemExit('selected Image differs from the module build')
Path(sys.argv[4]).write_bytes(content)
CONTRACT

fi

LIUQIN_STORAGE_MODE="$storage_mode" LIUQIN_ROOT_PROFILE="$root_profile" LIUQIN_EMBED_ROOTFS=0 \
	LIUQIN_INITRAMFS_COMPRESSION="$ramdisk_compression" \
	LIUQIN_CHARGER_MODE="$charger_mode" \
	INSTALLER_KERNEL_RELEASE="$kernel_out/include/config/kernel.release" \
	KERNEL_CONFIG="$kernel_out/.config" \
	ROOTFS=none SOURCE_DATE_EPOCH="$source_epoch" SLOT_SUCCESS_GCC="$slot_cc" \
	TOUCH_FIRMWARE_DIR="$firmware_pool/touch-nt36532/vendor/firmware" \
	BT_FIRMWARE_DIR="$firmware_pool/bt-qca6490/vendor/bt_firmware/image" \
	WLAN_BOARD_FILE="$firmware_pool/wlan-qca6490/vendor/firmware_mnt/image/qca6490/bd_m81.elf" \
	WLAN_FIRMWARE_DIR="$firmware_pool/wlan-qca6490/upstream/WCN6855/hw2.0" \
	WLAN_HSP2_TUPLE="$wlan_tuple" \
	REGULATORY_FIRMWARE_DIR="$firmware_pool/regulatory/upstream" \
	GPU_FIRMWARE_DIR="$firmware_pool/gpu-adreno730/vendor" \
	DSP_FIRMWARE_DIR="$firmware_pool/dsp-sm8475/vendor/qcom/sm8475/liuqin" \
	AUDIO_TOPOLOGY="$audio_topology" \
	NATIVE_ROOT_CONTRACT="$out_dir/native-root.contract" \
	BOOT_SPLASH_RUNTIME="${BOOT_SPLASH_RUNTIME:-}" SPLASH_KERNEL_MODULES_DIR="${KERNEL_MODULES_DIR:-}" \
	OUT_DIR="$out_dir/initramfs" sh "$project_root/build/lib/build-initramfs.sh"

ramdisk="$out_dir/liuqin-firstboot.cpio.$ramdisk_suffix"
cp "$out_dir/initramfs/liuqin-firstboot.cpio.$ramdisk_suffix" "$ramdisk"

# ABL takes the kernel command line from /chosen; keep the boot header empty.
python3 - "$cmdline" "$out_dir/cmdline-overlay.dts" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[2]).write_text('/dts-v1/;\n/plugin/;\n/ { fragment@0 { target-path = "/chosen"; '
                           '__overlay__ { bootargs = ' + json.dumps(sys.argv[1]) + '; }; }; };\n')
PY
"$dtc" -@ -q -I dts -O dtb -o "$out_dir/cmdline-overlay.dtbo" "$out_dir/cmdline-overlay.dts"
"$fdtoverlay" -i "$raw_dtb" -o "$out_dir/input.dtb" "$out_dir/cmdline-overlay.dtbo"

KERNEL_OUT="$kernel_out" IMAGE="$image" DTB="$out_dir/input.dtb" \
	RAMDISK="$ramdisk" DTC="$dtc" FDTOVERLAY="$fdtoverlay" \
	MKBOOTIMG_DIR="$mk_dir" STOCK_OVERLAY_DIR="$stock_overlay_dir" STOCK_BASE_DIR="$stock_base_dir" \
	ABL_OVERLAY_SINK=1 ABL_DTB02_IDS=0 CMDLINE="$cmdline" HEADER_CMDLINE= \
	OUT_DIR="$out_dir/boot" sh "$project_root/build/lib/build-bootimg.sh"
mv "$out_dir/boot/boot-liuqin-firstboot.img" "$out_dir/boot-liuqin-native.img"

{
	printf 'status=OFFLINE_BUILT\n'
	printf 'kernel_commit=%s\n' "$(git -C "$kernel_source" rev-parse HEAD)"
	printf 'source_date_epoch=%s\n' "$source_epoch"
	printf 'image_sha256=%s\n' "$(sha "$image")"
	printf 'raw_dtb_sha256=%s\n' "$(sha "$raw_dtb")"
	if [ "$root_profile" = installer ]; then
		printf 'installer_interface=liuqin-installer-v2\n'
		printf 'installer_kernel_release=%s\n' "$(cat "$kernel_out/include/config/kernel.release")"
	else
		printf 'contract_sha256=%s\n' "$(sha "$out_dir/native-root.contract")"
	fi
	printf 'ramdisk_compression=%s\n' "$ramdisk_compression"
	printf 'ramdisk_sha256=%s\n' "$(sha "$ramdisk")"
	printf 'boot_sha256=%s\n' "$(sha "$out_dir/boot-liuqin-native.img")"
} >"$out_dir/native-boot.identity"
cat "$out_dir/native-boot.identity"
