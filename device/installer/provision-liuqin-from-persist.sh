#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Provision liuqin's per-device data straight from the factory persist
# partition (sda21), without any host-side backup.  Runs on the device in
# either the stage-1 rescue shell (busybox) or the booted desktop system.
#
#   provision-liuqin-from-persist.sh [TARGET_ROOT]
#
# TARGET_ROOT defaults to / (the running system); the RAM-install flow passes
# the fresh tree (e.g. /native-root).  persist is only ever mounted read-only
# and is never written.
#
# Provisioned items (consumer in parentheses):
#   persist/wlan/wlan_mac.bin          text "wlan0=AABBCCDDEEFF" ->
#     $TGT/var/lib/liuqin-private/wlan-mac            (ath11k MAC helper)
#   persist/bluetooth/.bt_nv.bin       6 raw bytes, in order ->
#     $TGT/var/lib/liuqin-private/bluetooth-address   (liuqin-bt-public-addr)
#   persist/audio/crus_calr.bin        4 x 4-byte LE, order TL,TR,BL,BR ->
#     $TGT/usr/lib/firmware/cirrus/cs35l41-liuqin-<ch>-calr.bin
#     (cs35l41 per-channel calibration; crus_calr.txt carries the same order)
#   persist/sensors/registry/registry/ ->
#     $TGT/var/lib/liuqin-sensors/registry/ + SHA256SUMS
#     (hexagonrpcd serves it to the SLPI; fastrpc:fastrpc 0640/0750)
#
# The persist sibling sns_reg_version is runtime-consumed by hexagonrpcd only
# when present next to a served registry and is deliberately not provisioned
# (matching the reviewed 2026-09-10 arrangement).
set -eu

tgt=${1:-/}
persist_src=${PERSIST_SRC:-}
part=${PERSIST_PART:-/dev/disk/by-partlabel/persist}

die() { printf 'provision-liuqin-from-persist: %s\n' "$*" >&2; exit 1; }
say() { printf 'provision-liuqin-from-persist: %s\n' "$*"; }

[ "$(id -u)" = 0 ] || die 'root privileges are required'
[ -d "$tgt" ] || die "target root is not a directory: $tgt"
[ -f "$tgt/etc/liuqin-native-root" ] || [ -f "$tgt/etc/os-release" ] ||
	die "target does not look like a liuqin root: $tgt"

mnt=$persist_src
mounted_here=
if [ -z "$mnt" ]; then
	[ -b "$part" ] || die "persist partition is unavailable: $part"
	# Not below a temporary directory: this script also runs inside the RAM
	# images, which are built from a fixed directory list that has none.
	# /run is present and writable in every environment it runs in.
	work=${LIUQIN_PROVISION_WORK:-/run}
	mkdir -p "$work" || die "cannot create the scratch directory: $work"
	mnt=$(mktemp -d "$work/persist-ro.XXXXXX")
	mount -o ro "$part" "$mnt" || { rmdir "$mnt"; die "cannot mount $part read-only"; }
	mounted_here=1
fi
cleanup() {
	[ -z "$mounted_here" ] || { umount "$mnt" 2>/dev/null || :; rmdir "$mnt" 2>/dev/null || :; }
}
trap cleanup EXIT HUP INT TERM

[ -d "$mnt/sensors/registry/registry" ] || die 'persist sensor registry is missing'
[ -f "$mnt/wlan/wlan_mac.bin" ] || die 'persist wlan_mac.bin is missing'
[ -f "$mnt/bluetooth/.bt_nv.bin" ] || die 'persist .bt_nv.bin is missing'
[ -f "$mnt/audio/crus_calr.bin" ] || die 'persist crus_calr.bin is missing'

# --- WLAN MAC (text file, "wlan0=AABBCCDDEEFF") -----------------------------
raw=$(sed -n 's/^wlan0=\([0-9A-Fa-f]\{12\}\).*/\1/p' "$mnt/wlan/wlan_mac.bin" | head -1)
[ -n "$raw" ] || die 'wlan_mac.bin does not carry a wlan0= entry'
wlan_mac=$(printf '%s' "$raw" | tr 'A-F' 'a-f' |
	sed 's/\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)/\1:\2:\3:\4:\5:\6/')
mkdir -p "$tgt/var/lib/liuqin-private"
chmod 0700 "$tgt/var/lib/liuqin-private"
printf '%s\n' "$wlan_mac" >"$tgt/var/lib/liuqin-private/wlan-mac"
chmod 0600 "$tgt/var/lib/liuqin-private/wlan-mac"
chown 0:0 "$tgt/var/lib/liuqin-private" "$tgt/var/lib/liuqin-private/wlan-mac"

# --- Bluetooth address (6 raw bytes, in order) -------------------------------
bt_hex=$(od -An -tx1 -N6 "$mnt/bluetooth/.bt_nv.bin" | tr -d ' \n')
[ ${#bt_hex} -eq 12 ] || die "bt_nv.bin is not 6 bytes: $bt_hex"
bt_addr=$(printf '%s' "$bt_hex" | sed 's/\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)/\1:\2:\3:\4:\5:\6/')
printf '%s\n' "$bt_addr" >"$tgt/var/lib/liuqin-private/bluetooth-address"
chmod 0600 "$tgt/var/lib/liuqin-private/bluetooth-address"
chown 0:0 "$tgt/var/lib/liuqin-private/bluetooth-address"

# --- Cirrus per-channel calibration (4 x 4 bytes, TL TR BL BR) ---------------
calr_size=$(wc -c <"$mnt/audio/crus_calr.bin" | tr -d ' ')
[ "$calr_size" = 16 ] || die "crus_calr.bin is not 16 bytes: $calr_size"
mkdir -p "$tgt/usr/lib/firmware/cirrus"
chmod 0755 "$tgt/usr/lib/firmware/cirrus"
i=0
for ch in TL TR BL BR; do
	calr="$tgt/usr/lib/firmware/cirrus/cs35l41-liuqin-$ch-calr.bin"
	dd if="$mnt/audio/crus_calr.bin" of="$calr" bs=4 skip=$i count=1 2>/dev/null
	chmod 0600 "$calr"
	chown 0:0 "$calr"
	i=$((i + 1))
done

# --- SSC sensor registry -----------------------------------------------------
nf_uid=$(awk -F: '$1=="fastrpc"{print $3}' "$tgt/etc/passwd")
nf_gid=$(awk -F: '$1=="fastrpc"{print $3}' "$tgt/etc/group")
[ -n "$nf_uid" ] && [ -n "$nf_gid" ] || die 'fastrpc uid/gid not resolvable in the target root'
mkdir -p "$tgt/var/lib/liuqin-sensors/registry"
chmod 0750 "$tgt/var/lib/liuqin-sensors/registry"
chown "$nf_uid:$nf_gid" "$tgt/var/lib/liuqin-sensors/registry"
count=0
for f in "$mnt/sensors/registry/registry/"*; do
	[ -f "$f" ] || continue
	cp "$f" "$tgt/var/lib/liuqin-sensors/registry/"
	chmod 0640 "$tgt/var/lib/liuqin-sensors/registry/$(basename "$f")"
	count=$((count + 1))
done
[ "$count" -gt 100 ] || die "suspiciously few registry files: $count"
( cd "$tgt/var/lib/liuqin-sensors/registry" && find . -maxdepth 1 -type f ! -name SHA256SUMS -print |
	LC_ALL=C sort | xargs sha256sum ) >"$tgt/var/lib/liuqin-sensors/registry/SHA256SUMS"
chown "$nf_uid:$nf_gid" "$tgt/var/lib/liuqin-sensors/registry/"*
chmod 0640 "$tgt/var/lib/liuqin-sensors/registry/SHA256SUMS"

say "wlan=$wlan_mac bt=$bt_addr calr=4ch registry=$count -> $tgt"
say 'PASS'
