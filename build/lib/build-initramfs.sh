#!/bin/sh

set -eu
umask 022
# Tool diagnostics are parsed below; keep their field names locale-independent.
export LC_ALL=C

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
python3 "$project_root/build/lib/usb_identity.py"
busybox=${BUSYBOX:-"$project_root/inputs/busybox-arm64/usr/bin/busybox"}
# Explicitly select a stage-2 image, or ROOTFS=none for a native persistent root.
if [ -z "${ROOTFS+isset}" ]; then
	echo "error: ROOTFS is not set, and it has no default." >&2
	echo "       ROOTFS=out/rootfs/rootfs.squashfs" >&2
	echo "           the two-stage image: initramfs + Ubuntu Base in RAM" >&2
	echo "       ROOTFS=none" >&2
	echo "           no embedded stage-2 image" >&2
	exit 1
fi
rootfs=$ROOTFS
if [ "$rootfs" = none ]; then
	rootfs=
elif [ -z "$rootfs" ]; then
	echo "error: ROOTFS is set but empty, which is the trap this check exists" >&2
	echo "       for. Write ROOTFS=none to ask for the stage-1-only image." >&2
	exit 1
fi
# The touchscreen firmware has to be here and not only in the stage-2 rootfs.
# nvt_ts_probe() queues Boot_Update_Firmware with zero delay, so request_firmware()
# runs while the initramfs is still the root filesystem -- long before the
# squashfs is even attached to a loop device. The retry that would eventually
# find it in the rootfs is the ESD watchdog, and that only fires once the driver
# has decided the controller is alive, which it cannot do if the first download
# never happened. Half a megabyte against a 32 MB initramfs buys certainty.
touch_firmware_dir=${TOUCH_FIRMWARE_DIR:-"$project_root/inputs/firmware-liuqin/touch-nt36532/vendor/firmware"}
touch_firmware_sha256="\
2582cf81d6eeb69b57cb8b18b2afe683e9cd7e20ed0be390e14ed75302b0c584  novatek_nt36532_m81_fw_csot.bin
3d9737da5e0fc3ea64e340c495067d4d6dcbd5ca2a2351fe955ec88fab35da1e  novatek_nt36532_m81_fw_tm.bin"
# The Bluetooth firmware is here for the same reason, arrived at the same way.
# hci_register_dev() queues hdev->power_on, and hci_power_on() calls
# hci_dev_open() straight away, which runs hdev->setup -- qca_setup() -- which
# is where the two request_firmware() calls live. Nothing waits for a user: by
# the time /init has finished counting block devices the requests have already
# been made and answered, or already failed. Ubuntu Base ships no BlueZ, so
# there is no second chance from userspace either: no hciconfig, no btmgmt,
# nothing that could re-open hci0 once the rootfs is up. One megabyte, and it
# is the difference between the driver having a chance and not.
#
# Selection rule and pinning are shared with build/scripts/build-liuqin-firmware-prep.sh;
# the two scripts ship the identical set, from the identical directory, and both
# check it against the same digest.
bt_firmware_dir=${BT_FIRMWARE_DIR:-"$project_root/inputs/firmware-liuqin/bt-qca6490/vendor/bt_firmware/image"}
bt_firmware_pattern='^(hpbtfw2[01]\.tlv|hpnv2[01]g?\.(bin|b[0-9a-f]+))$'
bt_firmware_set_sha256=d31712321a1c15591148a2e6a7ce94a12445558d2de82b48e5815abc581203a0
wlan_board_file=${WLAN_BOARD_FILE:-"$project_root/inputs/firmware-liuqin/wlan-qca6490/vendor/firmware_mnt/image/qca6490/bd_m81.elf"}
wlan_board_sha256=aa8ae92d781e09db8cffa960b00c9606e4bb65d9c7a89f9b32df1bd24aee8573
wlan_firmware_dir=${WLAN_FIRMWARE_DIR:-"$project_root/inputs/firmware-liuqin/wlan-qca6490/upstream/WCN6855/hw2.0"}
wlan_firmware_sha256="\
d94af8648a0347903b68809b4100ea816e76afb6ead967c8b353c6521500f285  amss.bin.zst
ba583c3550d15871dbbbe1349dab64bac5badced5b6bf122aa7263f8d7df76d4  board-2.bin.zst
7769d18f26c025008221702e6884c9225f375db46d3e584e20223e33728376ec  m3.bin.zst
c4b298869269bc55ca73d71beeb756b72a9b19e710cea2af0401accbafa2d3c7  regdb.bin.zst"
# The GNOME product image uses the complete HSP2-amss20 tuple that passed the
# device validation. ath11k probes before
# switch_root, so carrying this only in the stage-2 device layer would leave a
# cold boot on the old HSP1.1 tuple.  Keep all four protocol inputs atomic and
# put raw copies in firmware/updates for both hardware revision request paths.
wlan_hsp2_tuple=${WLAN_HSP2_TUPLE:-"$project_root/out/wlan"}
wlan_hsp2_amss_sha256=cc3e477fa698a28bdb8c8115a071893f9b2f5230de190ad525e74fd69bbb6092
wlan_hsp2_m3_sha256=6938b4bba268a02659ee5e16992971aa0e2fab103a4f60cbccf65e4bd8ac9836
wlan_hsp2_board2_sha256=15811f0b799fc26a881bce02e282834cd41b77c2812443ffd931012552a7c1f9
wlan_hsp2_regdb_sha256=06810e85c94c8f412c4e72cb38546cac90db7fdbadd45aae00f977f862d38a7b
audio_topology=${AUDIO_TOPOLOGY:-"$project_root/out/audio-topology/Xiaomi-Pad-6-Pro-tplg.bin"}
# Historical source-built topology, retained for existing reference inputs.
audio_topology_sha256=9c9f8bfcffde1f52c143d58cba821c5f6b3899553370ab92a88b90d81fef9ee9
regulatory_firmware_dir=${REGULATORY_FIRMWARE_DIR:-"$project_root/inputs/firmware-liuqin/regulatory/upstream"}
regulatory_firmware_sha256="\
92eec693a0a9ec460be6bb3ff9e22b259b2f906eddd2984dd90654b5ecfa0f18  regulatory.db
4242d15defb0608b917a2c3cd0dbea21c026a4acc905093e8947dd494c2e26b1  regulatory.db.p7s"
gpu_firmware_dir=${GPU_FIRMWARE_DIR:-"$project_root/inputs/firmware-liuqin/gpu-adreno730/vendor"}
gpu_firmware_sha256="\
e67d1829f57fc8326d806234b932dc3b78e28a9940df34c70752f17d5f38b7aa  a730_sqe.fw
36296b019753fbb2a4733450a2d0a94e307238227297a747d0ff2811364f5d5f  a730_zap.mbn
b2d3f0e2a98fd6259ab9ffef16df63265542e6cc9a970d53d476e823d432042a  gmu_gen70000.bin"
dsp_firmware_dir=${DSP_FIRMWARE_DIR:-"$project_root/inputs/firmware-liuqin/dsp-sm8475/vendor/qcom/sm8475/liuqin"}
dsp_firmware_count=68
dsp_firmware_set_sha256=7f9b43d3815b6592f541e0b870357010488980bc040b925e93bbf12d01282908
slot_success_source="$project_root/device/boot/liuqin-mark-slot-successful.c"
storage_seal_source="$project_root/device/boot/liuqin-storage-seal.c"
storage_ready_source="$project_root/device/boot/liuqin-storage-ready.c"
slot_success_gcc=$(command -v "${SLOT_SUCCESS_GCC:-aarch64-linux-gnu-gcc}" || true)
charger_mode_source="$project_root/device/charger-mode/liuqin-charger-mode"
charger_key_source="$project_root/device/charger-mode/liuqin-charger-mode-power-key.c"
charger_exit_source="$project_root/device/charger-mode/liuqin-charger-mode-exit.c"
source_dir="$project_root/device/boot/initramfs"
out_dir=${OUT_DIR:-"$project_root/out/build/initramfs-liuqin"}
source_epoch=${SOURCE_DATE_EPOCH:-0}
storage_mode=${LIUQIN_STORAGE_MODE:-readonly}
embed_rootfs=${LIUQIN_EMBED_ROOTFS:-1}
root_profile=${LIUQIN_ROOT_PROFILE:-native-updatable}
native_root_contract=${NATIVE_ROOT_CONTRACT:-}
case $storage_mode in
readonly|persistent) ;;
*)
	echo "error: LIUQIN_STORAGE_MODE must be readonly or persistent, not: $storage_mode" >&2
	exit 1
	;;
esac
case $embed_rootfs in
0|1) ;;
*)
	echo "error: LIUQIN_EMBED_ROOTFS must be 0 or 1, not: $embed_rootfs" >&2
	exit 1
	;;
esac
if [ "$embed_rootfs" = 0 ] && [ "$storage_mode" != persistent ] && [ "$root_profile" != installer ]; then
	echo "error: a rootfs-free initramfs is only valid in persistent mode" >&2
	exit 1
fi
case $root_profile in
native-updatable|installer) ;;
*) echo "error: unsupported LIUQIN_ROOT_PROFILE" >&2; exit 1 ;;
esac
if [ "$root_profile" != installer ] && [ "$storage_mode" != persistent ]; then
	echo "error: the $root_profile root profile requires persistent mode" >&2
	exit 1
fi
if [ "$root_profile" = installer ]; then
    [ "$storage_mode" = readonly ] && [ "$embed_rootfs" = 0 ] && [ -n "${INSTALLER_RUNTIME:-}" ] || {
        echo 'error: independent installer requires a rootfs-free readonly RAM runtime' >&2; exit 1;
    }
    [ -f "${INSTALLER_KERNEL_RELEASE:-}" ] || {
        echo 'error: installer kernel release is missing' >&2; exit 1;
    }
fi
charger_mode=${LIUQIN_CHARGER_MODE:-auto}
case $charger_mode in
auto)
	if [ "$storage_mode" = persistent ]; then
		charger_mode=1
	else
		charger_mode=0
	fi
	;;
0|1) ;;
*) echo "error: LIUQIN_CHARGER_MODE must be auto, 0 or 1" >&2; exit 1 ;;
esac
# The explicit installer branch switches the final image to read-only RAM mode.
if [ "$root_profile" != installer ] && [ "$charger_mode" != 1 ] && [ -z "${INSTALLER_RUNTIME:-}" ]; then
	echo "error: a persistent GNOME/native image must include charger-mode hold" >&2
	exit 1
fi
if [ "$root_profile" = native-updatable ] || [ "$root_profile" = installer ]; then
	[ -d "$wlan_hsp2_tuple/hw2.0" ] && [ -d "$wlan_hsp2_tuple/hw2.1" ] || {
		echo "error: the GNOME image requires the reviewed HSP2 WLAN tuple: $wlan_hsp2_tuple" >&2
		exit 1
	}
	for wlan_item in \
		"amss.bin:$wlan_hsp2_amss_sha256" \
		"m3.bin:$wlan_hsp2_m3_sha256" \
		"board-2.bin:$wlan_hsp2_board2_sha256" \
		"regdb.bin:$wlan_hsp2_regdb_sha256"; do
		wlan_name=${wlan_item%%:*}
		wlan_hash=${wlan_item#*:}
		for wlan_revision in hw2.0 hw2.1; do
			[ -f "$wlan_hsp2_tuple/$wlan_revision/$wlan_name" ] || {
				echo "error: HSP2 WLAN tuple lacks $wlan_revision/$wlan_name" >&2
				exit 1
			}
			[ "$(sha256sum "$wlan_hsp2_tuple/$wlan_revision/$wlan_name" | cut -d' ' -f1)" = "$wlan_hash" ] || {
				echo "error: HSP2 WLAN tuple hash mismatch: $wlan_revision/$wlan_name" >&2
				exit 1
			}
		done
	done
	[ -f "$audio_topology" ] && [ ! -L "$audio_topology" ] || {
		echo "error: AudioReach topology identity mismatch: $audio_topology" >&2
		exit 1
	}
	case $root_profile in
	installer|native-updatable)
		# The official-root flow uses the canonical repository firmware tree.
		# Read its identity from the same manifest as board/rootfs preparation;
		# retain the separately pinned source-built input for compatibility.
		audio_topology_repository_sha256=$(awk '
			$2 == "/usr/lib/firmware/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin" {
				if (length($1) != 64 || $1 ~ /[^0-9a-f]/) exit 1
				identity = $1; count++
			}
			END { if (count != 1) exit 1; print identity }
		' "$project_root/device/firmware/firmware.manifest") || {
			echo 'error: AudioReach topology firmware manifest entry is missing, duplicate or invalid' >&2
			exit 1
		}
		;;
	*) audio_topology_repository_sha256=$audio_topology_sha256 ;;
	esac
	audio_topology_actual_sha256=$(sha256sum "$audio_topology" | cut -d' ' -f1)
	if [ "$audio_topology_actual_sha256" != "$audio_topology_repository_sha256" ] && \
		[ "$audio_topology_actual_sha256" != "$audio_topology_sha256" ]; then
		echo "error: AudioReach topology identity mismatch: $audio_topology" >&2
		echo "       actual: $audio_topology_actual_sha256" >&2
		echo "       expected repository: $audio_topology_repository_sha256" >&2
		echo "       expected source-built: $audio_topology_sha256" >&2
		exit 1
	fi
fi
if [ ! -x "$busybox" ]; then
	echo "error: static ARM64 BusyBox is unavailable: $busybox" >&2
	exit 1
fi

if ! readelf -h "$busybox" | grep -q 'Machine:.*AArch64'; then
	echo "error: BusyBox is not an AArch64 executable: $busybox" >&2
	exit 1
fi

if readelf -l "$busybox" | grep -q 'INTERP'; then
	echo "error: BusyBox is dynamically linked: $busybox" >&2
	exit 1
fi

if ! (cd "$touch_firmware_dir" && printf '%s\n' "$touch_firmware_sha256" | sha256sum -c --quiet -); then
	echo "error: touchscreen firmware does not match the pinned hashes: $touch_firmware_dir" >&2
	echo "       see inputs/firmware-liuqin/MANIFEST.sha256" >&2
	exit 1
fi

if [ ! -d "$bt_firmware_dir" ]; then
	echo "error: Bluetooth firmware directory is unavailable: $bt_firmware_dir" >&2
	echo "       provide the firmware files matching the expected hashes" >&2
	exit 1
fi
bt_firmware_names=$(
	cd "$bt_firmware_dir" && ls -1 | LC_ALL=C sort | grep -E "$bt_firmware_pattern"
)
bt_firmware_seen=$(
	cd "$bt_firmware_dir" && printf '%s\n' "$bt_firmware_names" | \
		xargs sha256sum | sha256sum | cut -d' ' -f1
)
if [ "$bt_firmware_seen" != "$bt_firmware_set_sha256" ]; then
	echo "error: the Bluetooth firmware set does not match its pinned digest" >&2
	echo "       directory: $bt_firmware_dir" >&2
	echo "       expected:  $bt_firmware_set_sha256" >&2
	echo "       found:     $bt_firmware_seen ($(printf '%s\n' "$bt_firmware_names" | wc -l) files)" >&2
	echo "       see inputs/firmware-liuqin/MANIFEST.sha256" >&2
	exit 1
fi

if ! printf '%s  %s\n' "$wlan_board_sha256" "$wlan_board_file" | sha256sum -c --quiet -; then
	echo "error: WLAN board data does not match the pinned hash: $wlan_board_file" >&2
	exit 1
fi
if ! (cd "$wlan_firmware_dir" && printf '%s\n' "$wlan_firmware_sha256" | sha256sum -c --quiet -); then
	echo "error: upstream WCN6855 firmware does not match the pinned hashes: $wlan_firmware_dir" >&2
	exit 1
fi
if ! (cd "$regulatory_firmware_dir" && printf '%s\n' "$regulatory_firmware_sha256" | sha256sum -c --quiet -); then
	echo "error: wireless regulatory database does not match the pinned hashes: $regulatory_firmware_dir" >&2
	exit 1
fi
if ! (cd "$gpu_firmware_dir" && printf '%s\n' "$gpu_firmware_sha256" | sha256sum -c --quiet -); then
	echo "error: Adreno 730 firmware does not match the pinned hashes: $gpu_firmware_dir" >&2
	exit 1
fi
dsp_firmware_seen=$(
	cd "$dsp_firmware_dir" && find . -maxdepth 1 -type f -printf '%P\n' | \
		LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1
)
if [ "$(find "$dsp_firmware_dir" -maxdepth 1 -type f | wc -l)" -ne "$dsp_firmware_count" ] || \
	[ "$dsp_firmware_seen" != "$dsp_firmware_set_sha256" ]; then
	echo "error: SM8475 DSP firmware set does not match its pinned membership and digest" >&2
	exit 1
fi

if [ -n "$rootfs" ] && [ ! -r "$rootfs" ]; then
	echo "error: stage-2 root filesystem is unavailable: $rootfs" >&2
	echo "       prepare the required ROOTFS archive, or set ROOTFS=none to" >&2
	echo "       build the stage-1-only diagnostic initramfs." >&2
	exit 1
fi

# Authenticate the selected kernel contract before building a persistent image.
[ -r "$storage_seal_source" ] && [ -r "$storage_ready_source" ] && [ -x "$slot_success_gcc" ] || {
    echo 'error: storage seal source/toolchain is unavailable' >&2; exit 1;
}
if [ "$storage_mode" = persistent ]; then
    [ "$root_profile" = native-updatable ] && [ -n "$native_root_contract" ] || {
        echo 'error: persistent mode requires the current root contract' >&2; exit 1;
    }
    python3 - "$project_root/shared" "$native_root_contract" <<'VALIDATE_CONTRACT'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from liuqin_kernel import parse_contract
parse_contract(Path(sys.argv[2]).read_bytes())
VALIDATE_CONTRACT
    [ -r "$slot_success_source" ] && [ -x "$slot_success_gcc" ] || {
        echo 'error: persistent slot-success source/toolchain is unavailable' >&2; exit 1;
    }
fi
# Naming the file explicitly stops the build from picking a stale rootfs on its
# own; it does not stop a stale path from being copied out of an old shell
# history. So say when a newer one exists. A warning and not an error, because
# rebuilding an older image on purpose is a legitimate thing to want -- the
# failure being guarded against is the one where nobody was thinking about it.
if [ -n "$rootfs" ]; then
	find "$project_root/out" -maxdepth 2 -name rootfs.squashfs -newer "$rootfs" \
		2>/dev/null | LC_ALL=C sort | while read -r newer_rootfs; do
		echo "warning: a newer stage-2 root filesystem exists: $newer_rootfs" >&2
	done
	echo "stage 2: $rootfs" >&2
fi

compression=${LIUQIN_INITRAMFS_COMPRESSION:-gzip}
case $compression in
gzip) archive_suffix=gz ;;
zstd)
    command -v zstd >/dev/null || { echo 'error: Zstd compression requires zstd' >&2; exit 1; }
    archive_suffix=zst ;;
*) echo 'error: unsupported initramfs compression' >&2; exit 1 ;;
esac
mkdir -p "$out_dir"
# The cpio below runs inside `cd "$staging"`, so every path it is handed has to
# be absolute. The built-in default already is; a relative OUT_DIR= on the
# command line was not, and failed there with a message about a file it had
# created moments earlier.
out_dir=$(CDPATH= cd -- "$out_dir" && pwd)
archive="$out_dir/liuqin-firstboot.cpio.$archive_suffix"
raw_archive="$out_dir/.liuqin-firstboot.cpio.$$"
staging=$(mktemp -d "$out_dir/.staging.XXXXXX")
cleanup() {
	rm -f "$raw_archive" "$raw_archive.gz" "$raw_archive.zst"
	rm -rf "$staging"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$staging/bin" "$staging/sbin" "$staging/etc" "$staging/proc" \
	"$staging/sys" "$staging/dev" "$staging/run" "$staging/usr/bin" \
	"$staging/usr/sbin"
cp "$busybox" "$staging/bin/busybox"
cp "$source_dir/init" "$staging/init"
mkdir -p "$staging/usr/lib/liuqin"
cp "$project_root/device/boot/root-interface-v2.sh" "$staging/usr/lib/liuqin/root-interface-v2.sh"
chmod 0644 "$staging/usr/lib/liuqin/root-interface-v2.sh"
cp "$source_dir/etc/issue" "$staging/etc/issue"
cp "$source_dir/etc/mdev.conf" "$staging/etc/mdev.conf"
chmod 0644 "$staging/etc/mdev.conf"
chmod 0755 "$staging/init" "$staging/bin/busybox"
"$slot_success_gcc" -static -Os -s -Wall -Wextra -Werror \
    "$storage_seal_source" "$storage_ready_source" -o "$staging/bin/liuqin-storage-seal"
chmod 0755 "$staging/bin/liuqin-storage-seal"
if ! readelf -h "$staging/bin/liuqin-storage-seal" | grep -q 'Machine:.*AArch64' ||
    readelf -l "$staging/bin/liuqin-storage-seal" | grep -q INTERP; then
    echo 'error: storage seal helper is not a static AArch64 ELF' >&2
    exit 1
fi
printf '%s\n' "$storage_mode" >"$staging/etc/liuqin-storage-mode"
chmod 0644 "$staging/etc/liuqin-storage-mode"
if [ "$root_profile" = native-updatable ]; then
    printf '%s\n' "$root_profile" >"$staging/etc/liuqin-root-profile"
    chmod 0644 "$staging/etc/liuqin-root-profile"
fi
if [ "$storage_mode" = persistent ]; then
    cp "$native_root_contract" "$staging/etc/liuqin-native-root.contract"
    chmod 0644 "$staging/etc/liuqin-native-root.contract"
	"$slot_success_gcc" -static -Os -s "$slot_success_source" \
		-o "$staging/bin/liuqin-mark-slot-successful"
	chmod 0755 "$staging/bin/liuqin-mark-slot-successful"
	if ! readelf -h "$staging/bin/liuqin-mark-slot-successful" | grep -q 'Machine:.*AArch64' ||
		readelf -l "$staging/bin/liuqin-mark-slot-successful" | grep -q INTERP; then
		echo "error: slot-success helper is not a static AArch64 ELF" >&2
		exit 1
	fi
fi
if [ "$charger_mode" = 1 ]; then
	printf '%s\n' hold-v1 >"$staging/etc/liuqin-charger-mode"
	chmod 0644 "$staging/etc/liuqin-charger-mode"
	cp "$charger_mode_source" "$staging/bin/liuqin-charger-mode"
	chmod 0755 "$staging/bin/liuqin-charger-mode"
	for charger_helper in key exit; do
		case $charger_helper in
		key) charger_source=$charger_key_source; charger_output=liuqin-charger-mode-power-key ;;
		exit) charger_source=$charger_exit_source; charger_output=liuqin-charger-mode-exit ;;
		esac
		"$slot_success_gcc" -static -Os -s -Wall -Wextra -Werror \
			"$charger_source" -o "$staging/bin/$charger_output"
		chmod 0755 "$staging/bin/$charger_output"
		if ! readelf -h "$staging/bin/$charger_output" | grep -q 'Machine:.*AArch64' ||
			readelf -l "$staging/bin/$charger_output" | grep -q INTERP; then
			echo "error: $charger_output is not a static AArch64 ELF" >&2
			exit 1
		fi
	done
fi

mkdir -p "$staging/lib/firmware/novatek/liuqin"
printf '%s\n' "$touch_firmware_sha256" | while read -r _ firmware_name; do
	cp "$touch_firmware_dir/$firmware_name" \
		"$staging/lib/firmware/novatek/liuqin/$firmware_name"
	chmod 0644 "$staging/lib/firmware/novatek/liuqin/$firmware_name"
done

# btqca.c hardcodes the qca/ prefix into every name it builds, so this directory
# is not a choice.
mkdir -p "$staging/lib/firmware/qca"
printf '%s\n' "$bt_firmware_names" | while read -r firmware_name; do
	cp "$bt_firmware_dir/$firmware_name" "$staging/lib/firmware/qca/$firmware_name"
	chmod 0644 "$staging/lib/firmware/qca/$firmware_name"
done

# ath11k probes and boots MHI before /init can switch_root, so WLAN firmware is
# just as stage-1-critical as the BT blobs above. Keep one physical compressed
# copy under hw2.0 and symlink hw2.1 to it, matching upstream linux-firmware.
for hw_revision in hw2.0 hw2.1; do
	mkdir -p "$staging/lib/firmware/ath11k/WCN6855/$hw_revision"
	cp "$wlan_board_file" \
		"$staging/lib/firmware/ath11k/WCN6855/$hw_revision/board.bin"
	chmod 0644 "$staging/lib/firmware/ath11k/WCN6855/$hw_revision/board.bin"
done
printf '%s\n' "$wlan_firmware_sha256" | while read -r _ firmware_name; do
	cp "$wlan_firmware_dir/$firmware_name" \
		"$staging/lib/firmware/ath11k/WCN6855/hw2.0/$firmware_name"
	chmod 0644 "$staging/lib/firmware/ath11k/WCN6855/hw2.0/$firmware_name"
	ln -s "../hw2.0/$firmware_name" \
		"$staging/lib/firmware/ath11k/WCN6855/hw2.1/$firmware_name"
done

if [ "$root_profile" = native-updatable ] || [ "$root_profile" = installer ]; then
	for wlan_revision in hw2.0 hw2.1; do
		for wlan_name in amss.bin m3.bin board-2.bin regdb.bin; do
			install -D -m 0644 "$wlan_hsp2_tuple/$wlan_revision/$wlan_name" \
				"$staging/lib/firmware/updates/ath11k/WCN6855/$wlan_revision/$wlan_name"
		done
	done
	install -D -m 0644 "$audio_topology" \
		"$staging/lib/firmware/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin"
fi

# cfg80211 is built in and requests its signed regulatory database while still
# in stage 1. The kernel configuration requires the matching PKCS#7 signature,
# so keep the pair together and pin both bytes.
printf '%s\n' "$regulatory_firmware_sha256" | while read -r _ firmware_name; do
	cp "$regulatory_firmware_dir/$firmware_name" "$staging/lib/firmware/$firmware_name"
	chmod 0644 "$staging/lib/firmware/$firmware_name"
done

# DRM/GPU and all three PAS remoteprocs probe before /init can switch_root.
# Mirror the exact stage-2 layout here so neither side depends on probe timing.
mkdir -p "$staging/lib/firmware/qcom/sm8475/liuqin"
cp "$gpu_firmware_dir/a730_zap.mbn" \
	"$staging/lib/firmware/qcom/sm8475/liuqin/a730_zap.mbn"
cp "$gpu_firmware_dir/a730_sqe.fw" "$gpu_firmware_dir/gmu_gen70000.bin" \
	"$staging/lib/firmware/qcom/"
cp "$dsp_firmware_dir"/* "$staging/lib/firmware/qcom/sm8475/liuqin/"

if [ -n "$rootfs" ] && [ "$embed_rootfs" = 1 ]; then
	mkdir -p "$staging/mnt/lower" "$staging/mnt/rw" "$staging/newroot"
	cp "$rootfs" "$staging/rootfs.squashfs"
	chmod 0644 "$staging/rootfs.squashfs"
fi

# `ln` and `udhcpd` were used by the init script long before they were linked
# here: the Ubuntu BusyBox is built with the standalone shell, so its own `sh`
# dispatches applets it cannot find in PATH. That is a property of one vendor's
# build, not of BusyBox, and nothing outside `sh` gets it -- so link every applet
# the init script names.
for applet in basename blockdev cat chmod chroot cp cttyhack cut dmesg findfs \
	hostname ip kill killall ln losetup ls mkdir mount poweroff readlink reboot \
	setsid sh sha256sum sleep stat switch_root sync telnetd udhcpd umount; do
	ln -s busybox "$staging/bin/$applet"
done
ln -s ../bin/busybox "$staging/sbin/mdev"

if [ -n "${INSTALLER_RUNTIME:-}" ]; then
	[ -x "$INSTALLER_RUNTIME/usr/bin/tar" ] && [ -x "$INSTALLER_RUNTIME/usr/sbin/mkfs.ext4" ] &&
		[ -x "$INSTALLER_RUNTIME/usr/sbin/e2fsck" ] && [ -x "$INSTALLER_RUNTIME/usr/sbin/sgdisk" ] ||
		{ echo 'installer runtime is incomplete' >&2; exit 1; }
	cp -a "$INSTALLER_RUNTIME"/. "$staging"/
	mkdir -p "$staging/usr/lib/liuqin"
	cp "$project_root/device/installer/install-root.sh" "$staging/usr/lib/liuqin/install-root.sh"
	cp "$project_root/device/installer/install-layout.sh" "$staging/usr/lib/liuqin/install-layout.sh"
	cp "$project_root/device/installer/install-switch-store.sh" "$staging/usr/lib/liuqin/install-switch-store.sh"
	cp "$project_root/device/installer/provision-liuqin-from-persist.sh" "$staging/usr/lib/liuqin/provision.sh"
	printf 'readonly\n' >"$staging/etc/liuqin-storage-mode"
	printf 'liuqin\n' >"$staging/etc/liuqin-installer"
	chmod 0644 "$staging/etc/liuqin-storage-mode" "$staging/etc/liuqin-installer"
	if [ "$root_profile" = installer ]; then
		grep -qx 'CONFIG_USB_CONFIGFS_RNDIS=y' "${KERNEL_CONFIG:?set KERNEL_CONFIG}" || {
			echo 'error: Windows-compatible installer requires builtin USB_CONFIGFS_RNDIS' >&2; exit 1;
		}
		printf 'rndis\n' >"$staging/etc/liuqin-installer-usb-network"
		chmod 0644 "$staging/etc/liuqin-installer-usb-network"
		printf 'liuqin-installer-v2\n' >"$staging/etc/liuqin-installer-interface"
		cp "$INSTALLER_KERNEL_RELEASE" "$staging/etc/liuqin-installer-kernel.release"
		chmod 0644 "$staging/etc/liuqin-installer-interface" "$staging/etc/liuqin-installer-kernel.release"
		# A copied runtime must never inject a stale target-specific boot contract.
		rm -f "$staging/etc/liuqin-native-root.contract" "$staging/etc/liuqin-root-profile"
	fi
	# GNU tar invokes gzip externally; do not depend on ash's applet dispatch.
	ln -s busybox "$staging/bin/gzip"
fi

# Keep these build-selected strings outside the reusable runtime payload.
python3 "$project_root/build/lib/usb_identity.py" --output-directory "$staging/etc"
python3 "$project_root/build/lib/root_filesystems.py" --kernel-config "${KERNEL_CONFIG:?set KERNEL_CONFIG}" \
    --output "$staging/etc/liuqin-filesystems"
if [ "$root_profile" = native-updatable ] &&
   grep -qx 'CONFIG_BACKLIGHT_KTZ8866=m' "${KERNEL_CONFIG:?set KERNEL_CONFIG}" &&
   [ -z "${BOOT_SPLASH_RUNTIME:-}" ]; then
    echo 'error: modular Liuqin backlight requires the staged early-graphics runtime' >&2
    exit 1
fi
if [ -n "${BOOT_SPLASH_RUNTIME:-}" ]; then
    [ "$root_profile" = native-updatable ] && [ "$storage_mode" = persistent ] && [ "$embed_rootfs" = 0 ] || {
        echo 'error: boot splash belongs only to a native persistent image' >&2; exit 1;
    }
    python3 "$project_root/build/lib/boot_splash.py" --runtime "$BOOT_SPLASH_RUNTIME" \
        --modules "${SPLASH_KERNEL_MODULES_DIR:?set SPLASH_KERNEL_MODULES_DIR}" \
        --contract "$native_root_contract" --staging "$staging" --kernel-config "$KERNEL_CONFIG" --cc "$slot_success_gcc"
fi
if grep -qx btrfs "$staging/etc/liuqin-filesystems" && [ "$root_profile" = installer ]; then
    [ -x "$staging/usr/sbin/mkfs.btrfs" ] && [ -x "$staging/usr/bin/btrfs" ] || {
        echo 'error: Btrfs-capable installer requires mkfs.btrfs and btrfs tools' >&2; exit 1;
    }
fi
find "$staging" -exec touch -h -d "@$source_epoch" {} +

(
	cd "$staging"
	find . -print0 | LC_ALL=C sort -z | \
		cpio --null --create --format=newc --owner=0:0 --reproducible \
			--file="$raw_archive"
)
case $compression in
gzip)
    gzip -n -9 "$raw_archive"
    mv "$raw_archive.gz" "$archive" ;;
zstd)
    zstd --quiet -10 --single-thread --stdout "$raw_archive" >"$raw_archive.zst"
    mv "$raw_archive.zst" "$archive" ;;
esac

if [ -n "$rootfs" ]; then
	sha256sum "$archive" "$busybox" "$rootfs"
else
	sha256sum "$archive" "$busybox"
fi
printf 'bluetooth firmware: %s files, set sha256 %s\n' \
	"$(printf '%s\n' "$bt_firmware_names" | wc -l)" "$bt_firmware_set_sha256"
printf 'initramfs: %s bytes\n' "$(stat -c %s "$archive")"
printf 'initramfs: %s\n' "$archive"
printf 'initramfs compression: %s\n' "$compression"
printf 'storage mode: %s\n' "$storage_mode"
printf 'embedded rootfs: %s\n' "$embed_rootfs"
printf 'root profile: %s\n' "$root_profile"
printf 'charger mode: %s\n' "$charger_mode"
