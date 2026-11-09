#!/bin/sh
# SPDX-License-Identifier: MIT
set -eu
BB=/bin/busybox
die() { printf 'liuqin-store: %s\n' "$*" >&2; exit 1; }
[ ! -f /etc/liuqin-native-recovery.json ] || die 'use the confirmed native store workflow'
[ "$#" -ge 5 ] || die 'missing store arguments'
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM identity changed'
[ "$(cat /etc/liuqin-installer)" = liuqin ] && [ "$(cat /etc/liuqin-multiboot)" = 1 ] || die 'unsupported RAM environment'
action=$2; partuuid=$3; fsuuid=$4; expected_bytes=$5; shift 5
for id in "$partuuid" "$fsuuid"; do
    printf '%s\n' "$id" | "$BB" grep -Eq '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' || die 'invalid UUID'
done
case $expected_bytes in ''|*[!0-9]*) die 'invalid size' ;; esac
part=
for path in /sys/class/block/sda*/uevent; do
    node=${path%/uevent}; node=${node##*/}
    if ! "$BB" grep -qx "PARTUUID=$partuuid" "$path"; then
        [ -f /etc/liuqin-recovery-session.json ] &&
            [ -L "/dev/disk/by-partuuid/$partuuid" ] &&
            [ "$("$BB" readlink -f "/dev/disk/by-partuuid/$partuuid")" = "/dev/$node" ] || continue
    fi
    "$BB" grep -qx 'PARTNAME=linux_boot' "$path" || die 'unexpected store name'
    [ -z "$part" ] || die 'ambiguous boot store'
    part=/dev/$node
done
[ -n "$part" ] && [ -b "$part" ] || die 'boot store missing'
[ "$(( $(cat "/sys/class/block/$node/size") * 512 ))" = "$expected_bytes" ] || die 'store size changed'
for target in /dev/sda "$part"; do
    number=$(cat "/sys/class/block/${target##*/}/dev")
    if awk -v device="$number" '$3==device {found=1} END {exit !found}' /proc/self/mountinfo; then die 'store or parent is mounted'; fi
    [ "$("$BB" blockdev --getro "$target")" = 1 ] || die 'store is not sealed'
done
work=/run/liuqin-multiboot-store
mkdir -p "$work/mount"
mounted=false; opened=false
cleanup() {
    sync
    if [ "$mounted" = true ]; then umount "$work/mount" || return 1; mounted=false; fi
    if [ "$opened" = true ]; then
        "$BB" blockdev --setro "$part"; "$BB" blockdev --setro /dev/sda; opened=false
    fi
    rm -f "$work/payload"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
case $action in
initialize) [ "$#" = 0 ] || die 'unexpected initialization fields' ;;
stage|publish)
    [ "$#" = 4 ] || die 'missing payload fields'
    url=$1; digest=$2; bytes=$3; expected_boot=$4
    case $bytes in ''|*[!0-9]*) die 'invalid payload size' ;; esac
    available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    [ "$bytes" -gt 0 ] && [ "$((bytes+536870912))" -lt "$((available*1024))" ] || die 'insufficient RAM for store payload'
    "$BB" wget -O "$work/payload" "$url"
    [ "$(stat -c %s "$work/payload")" = "$bytes" ] || die 'payload size mismatch'
    printf '%s  %s\n' "$digest" "$work/payload" | "$BB" sha256sum -c -
    if [ "$action" = publish ]; then
        [ "$("$BB" sha256sum /dev/disk/by-partlabel/boot_a | "$BB" cut -d' ' -f1)" = "$expected_boot" ] || die 'default boot does not match registry'
    fi
    actual=$("$BB" dd if="$part" bs=1 skip=1128 count=16 2>/dev/null | "$BB" od -An -tx1 | "$BB" tr -d ' \n')
    [ "$actual" = "$(printf '%s' "$fsuuid" | "$BB" tr -d '-')" ] || die 'boot store filesystem changed'
    ;;
*) die 'unknown store action' ;;
esac
"$BB" blockdev --setrw /dev/sda; opened=true
"$BB" blockdev --setrw "$part"
if [ "$action" = initialize ]; then
    /usr/sbin/mkfs.ext4 -F -L LQ_BOOT -U "$fsuuid" -m 0 -O '^metadata_csum_seed,^orphan_file' "$part"
else
    mount -t ext4 "$part" "$work/mount"; mounted=true
    if [ "$action" = stage ]; then
        # The host creates this archive solely from verified boot snapshots and
        # generated metadata. Reject unexpected archive paths before extraction.
        /usr/bin/tar -tf "$work/payload" >"$work/entries"
        awk '/^\// || /(^|\/)\.\.($|\/)/ || !/^(SHA256SUMS$|registry.json$|systems\/|transactions\/)/ {exit 1}' "$work/entries" || die 'invalid store archive paths'
        [ ! -e "$work/mount/registry.json" ] || die 'store is already initialized'
        /usr/bin/tar -xf "$work/payload" -C "$work/mount" --no-same-owner
        sync
        (cd "$work/mount" && "$BB" sha256sum -c SHA256SUMS)
    else
        cp "$work/payload" "$work/mount/.registry.pending"
        chmod 0600 "$work/mount/.registry.pending"
        sync
        mv "$work/mount/.registry.pending" "$work/mount/registry.json"
        sync
        [ "$("$BB" sha256sum "$work/mount/registry.json" | "$BB" cut -d' ' -f1)" = "$digest" ] || die 'registry readback failed'
        # SHA256SUMS describes the staged immutable payload; registry.json is
        # mutable transaction state and must not retain its old checksum.
        "$BB" sed '/  registry.json$/d' "$work/mount/SHA256SUMS" >"$work/mount/.sums.pending"
        mv "$work/mount/.sums.pending" "$work/mount/SHA256SUMS"
    fi
fi
cleanup
trap - EXIT HUP INT TERM
printf 'liuqin-store: COMPLETE\n'
