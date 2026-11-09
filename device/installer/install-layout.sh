#!/bin/sh
# SPDX-License-Identifier: MIT
# Device-side partition-table operations, intended only for the dedicated
# read-only RAM image. Read the existing layout, save both GPT copies, or
# restore an explicitly supplied backup. Installation does not repartition.
#
# Every helper below runs under `set -e`, so each one ends on a command whose
# success status is the status the caller sees, and each resolution is bound to
# a variable before it is used: a failure inside `$(...)` used as an argument is
# invisible to the caller, and a trailing `command && die ...` reports the
# absence of the failure as a failure of the helper.
set -eu
die() { printf 'liuqin-layout: %s\n' "$*" >&2; exit 1; }
[ "$#" -ge 2 ] || die 'usage: install-layout.sh BOOT_ID VERB [ARGUMENT...]'
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM boot identity changed'
[ "$(cat /etc/liuqin-installer 2>/dev/null)" = liuqin ] || die 'not the installer RAM image'
verb=$2
if [ -f /etc/liuqin-native-recovery.json ] && [ "$verb" = restore-gpt ]; then
    die 'native Recovery requires a separately reviewed recovery procedure for GPT restore'
fi
shift 2

BB=/bin/busybox
SGDISK=/usr/sbin/sgdisk
[ -x "$SGDISK" ] || die 'sgdisk is missing from the installer runtime'

# Scratch space for the few verbs that need a file on disk.  The installer RAM
# image has no /tmp -- it is built from a fixed directory list -- so nothing on
# the device path may assume one: /run is the writable tmpfs the init script
# mounts, and every temporary file this script writes lives in one directory
# below it.  The directory is created here, before any verb runs, so a verb
# never discovers a missing parent halfway through a partition operation, and
# the host stages its blobs into the same directory.
WORK=${LIUQIN_LAYOUT_WORK:-/run/liuqin-layout}
mkdir -p "$WORK" || die 'cannot create the scratch directory'
[ -d "$WORK" ] && [ -w "$WORK" ] || die 'the scratch directory is not writable'

# Decoded copies are never inputs, so they can go at any exit; the staged
# base64 halves are inputs the host wrote, and only restore-gpt removes them.
cleanup() { rm -f "$WORK"/liuqin-gpt-read.bin "$WORK"/liuqin-gpt-head.bin \
	"$WORK"/liuqin-gpt-tail.bin 2>/dev/null || :; }
trap cleanup EXIT HUP INT TERM

# Resolve a partition by PARTNAME.  /dev/disk/by-partlabel is built once at
# boot from a fixed name list, so it cannot describe a table this script is
# about to change; the sysfs uevent files always can.
find_part() {
	found=
	for info in /sys/class/block/sd*/uevent; do
		"$BB" grep -qx "PARTNAME=$1" "$info" || continue
		[ -z "$found" ] || die "duplicate partition name: $1"
		node=${info%/uevent}
		found=/dev/${node##*/}
	done
	[ -n "$found" ] && [ -b "$found" ] || die "partition is missing: $1"
	printf '%s' "$found"
}

parent_disk() {
	node=${1##*/}
	[ -n "$node" ] || die 'parent disk is unavailable'
	path=$("$BB" readlink -f "/sys/class/block/$node/.." 2>/dev/null || true)
	[ -n "$path" ] || die 'parent disk is unavailable'
	disk=/dev/${path##*/}
	[ -b "$disk" ] || die 'parent disk is missing'
	printf '%s' "$disk"
}

sector_size() {
	value=$(cat "/sys/class/block/${1##*/}/queue/logical_block_size" 2>/dev/null || true)
	case $value in ''|*[!0-9]*) die 'logical sector size is unavailable' ;; esac
	[ "$value" -gt 0 ] || die 'logical sector size is unavailable'
	printf '%s' "$value"
}

# /sys/class/block/*/size counts 512-byte units whatever the logical size is.
sector_count() {
	value=$(cat "/sys/class/block/${1##*/}/size" 2>/dev/null || true)
	case $value in ''|*[!0-9]*) die 'disk size is unavailable' ;; esac
	unit=$(sector_size "$1")
	printf '%s' "$(( value * 512 / unit ))"
}

# True only when nothing on the target disk is mounted.  The awk probe exits
# non-zero for the ordinary case -- no mount found -- so it is consumed by an
# `if`, never left as the status of the loop or of the function.
assert_idle() {
	for info in /sys/class/block/"${1##*/}"*/dev; do
		[ -f "$info" ] || continue
		number=$(cat "$info")
		if awk -v device="$number" '$3 == device {found=1} END {exit !found}' \
			/proc/self/mountinfo; then
			die 'a partition of the target disk is mounted'
		fi
	done
	return 0
}

# Restore the fail-closed posture the RAM image boots with: the whole disk and
# every partition read-only again, including partitions the kernel has just
# re-created from the new table.
seal() {
	for info in /sys/class/block/"${1##*/}"*/dev; do
		[ -f "$info" ] || continue
		node=${info%/dev}
		"$BB" blockdev --setro "/dev/${node##*/}" || die 'cannot restore the read-only flag'
	done
	"$BB" blockdev --setro "$1" || die 'cannot restore the read-only flag'
	return 0
}

case $verb in
disk)
	[ "$#" = 1 ] || die 'usage: disk PARTNAME'
	part=$(find_part "$1")
	parent_disk "$part"
	printf '\n'
	;;
report)
	[ "$#" = 1 ] || die 'usage: report PARTNAME'
	part=$(find_part "$1")
	disk=$(parent_disk "$part")
	"$SGDISK" -p "$disk" || die 'sgdisk cannot read the partition table'
	;;
info)
	[ "$#" = 1 ] || die 'usage: info PARTNAME'
	part=$(find_part "$1")
	disk=$(parent_disk "$part")
	number=$(cat "/sys/class/block/${part##*/}/partition" 2>/dev/null || true)
	case $number in ''|*[!0-9]*) die 'partition number is unavailable' ;; esac
	"$SGDISK" -i "$number" "$disk" || die 'sgdisk cannot read the partition entry'
	;;
geometry)
	[ "$#" = 1 ] || die 'usage: geometry PARTNAME'
	part=$(find_part "$1")
	disk=$(parent_disk "$part")
	sector=$(sector_size "$disk")
	sectors=$(sector_count "$disk")
	printf 'disk %s\nsector %s\nsectors %s\n' "$disk" "$sector" "$sectors"
	;;
backup-gpt)
	# Both GPT copies of the disk: the protective MBR, primary header and
	# primary entry array at the head, the entry array and backup header at
	# the tail.  Six sectors each, as dumped by the P0 inventory.  The copy is
	# read into a file and its length checked before it is encoded, so a short
	# read cannot become a backup that only looks complete.
	[ "$#" = 2 ] || die 'usage: backup-gpt PARTNAME head|tail'
	part=$(find_part "$1")
	disk=$(parent_disk "$part")
	sector=$(sector_size "$disk")
	case $2 in
	head) skip=0 ;;
	tail)
		sectors=$(sector_count "$disk")
		[ "$sectors" -gt 6 ] || die 'disk is too small to hold a GPT'
		skip=$(( sectors - 6 ))
		;;
	*) die 'backup-gpt takes head or tail' ;;
	esac
	"$BB" dd if="$disk" of="$WORK/liuqin-gpt-read.bin" bs="$sector" skip="$skip" \
		count=6 2>/dev/null || die 'reading the GPT failed'
	[ "$(stat -c %s "$WORK/liuqin-gpt-read.bin")" = "$(( sector * 6 ))" ] ||
		die 'the GPT copy is short'
	"$BB" base64 <"$WORK/liuqin-gpt-read.bin" || die 'encoding the GPT copy failed'
	rm -f "$WORK/liuqin-gpt-read.bin" || die 'cannot remove the temporary GPT copy'
	;;
restore-gpt)
	# The host has already staged the two verified copies as base64 files.
	[ "$#" = 1 ] || die 'usage: restore-gpt PARTNAME'
	part=$(find_part "$1")
	disk=$(parent_disk "$part")
	sector=$(sector_size "$disk")
	sectors=$(sector_count "$disk")
	[ "$sectors" -gt 6 ] || die 'disk is too small to hold a GPT'
	assert_idle "$disk"
	for half in head tail; do
		[ -f "$WORK/liuqin-gpt-$half.b64" ] || die "staged GPT copy is missing: $half"
		"$BB" base64 -d <"$WORK/liuqin-gpt-$half.b64" >"$WORK/liuqin-gpt-$half.bin" ||
			die "staged GPT copy does not decode: $half"
		[ "$(stat -c %s "$WORK/liuqin-gpt-$half.bin")" = "$(( sector * 6 ))" ] ||
			die "staged GPT copy has the wrong size: $half"
	done
	"$BB" blockdev --setrw "$disk" || die 'cannot open the disk for writing'
	"$BB" dd if="$WORK/liuqin-gpt-head.bin" of="$disk" bs="$sector" count=6 conv=notrunc 2>/dev/null ||
		die 'writing the primary GPT failed'
	"$BB" dd if="$WORK/liuqin-gpt-tail.bin" of="$disk" bs="$sector" \
		seek="$(( sectors - 6 ))" count=6 conv=notrunc 2>/dev/null ||
		die 'writing the backup GPT failed'
	sync
	"$BB" blockdev --rereadpt "$disk" || true
	seal "$disk"
	"$SGDISK" -v "$disk" || die 'the restored partition table does not verify'
	# The staged halves have served their purpose; leaving them behind would
	# let a later restore-gpt write a table nobody staged for it.
	rm -f "$WORK/liuqin-gpt-head.b64" "$WORK/liuqin-gpt-tail.b64" \
		"$WORK/liuqin-gpt-head.bin" "$WORK/liuqin-gpt-tail.bin" ||
		die 'cannot remove the staged GPT copies'
	printf 'liuqin-layout: GPT_RESTORED\n'
	;;
*)
	die "unknown verb: $verb"
	;;
esac
