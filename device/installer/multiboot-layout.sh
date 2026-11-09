#!/bin/sh
# SPDX-License-Identifier: MIT
# First-install partition writer. Accepts a typed, hashed plan, never sgdisk options.
set -eu
export LC_ALL=C
BB=/bin/busybox
SG=/usr/sbin/sgdisk
die() { printf 'liuqin-multiboot: %s\n' "$*" >&2; exit 1; }
[ ! -f /etc/liuqin-native-recovery.json ] || die 'use the confirmed native partition workflow'
[ "$#" = 6 ] || die 'usage: BOOT_ID PLAN SHA256 REPORT_SHA256 USERDATA_INFO_SHA256 ERASE-LIUQIN-USERDATA'
[ "$6" = ERASE-LIUQIN-USERDATA ] || die 'erasure not confirmed'
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM identity changed'
[ "$(cat /etc/liuqin-installer)" = liuqin ] && [ "$(cat /etc/liuqin-multiboot)" = 1 ] || die 'unsupported RAM installer'
plan=$2
case $plan in /run/*) ;; *) die 'plan must be in RAM' ;; esac
[ -f "$plan" ] && [ ! -L "$plan" ] && [ "$(stat -c %s "$plan")" -lt 16384 ] || die 'invalid plan file'
printf '%s  %s\n' "$3" "$plan" | "$BB" sha256sum -c -
[ "$(cat /sys/class/block/sda/queue/logical_block_size)" = 4096 ] || die 'unsupported UFS geometry'
disk=/dev/sda
[ -b "$disk" ] || die 'missing internal disk'
for entry in /sys/class/block/sda*/dev; do
    number=$(cat "$entry")
    if awk -v device="$number" '$3 == device {found=1} END {exit !found}' /proc/self/mountinfo; then
        die 'target disk is mounted'
    fi
    node=${entry%/dev}; node=/dev/${node##*/}
    [ "$("$BB" blockdev --getro "$node")" = 1 ] || die 'target disk is not sealed'
done
[ "$("$SG" -p "$disk" | "$BB" sha256sum | "$BB" cut -d' ' -f1)" = "$4" ] || die 'GPT changed after confirmation'
exec 3<"$plan"
IFS= read -r magic <&3
[ "$magic" = LIUQIN_LAYOUT_V1 ] || die 'unsupported layout plan'
IFS=' ' read -r old_number old_first old_last extra <&3
[ -z "$extra" ] || die 'invalid source geometry'
for number in "$old_number" "$old_first" "$old_last"; do
    case $number in ''|*[!0-9]*) die 'invalid source geometry' ;; esac
    [ "${#number}" -le 12 ] || die 'invalid source geometry'
done
info=$("$SG" -i "$old_number" "$disk")
[ "$(printf '%s\n' "$info" | "$BB" sha256sum | "$BB" cut -d' ' -f1)" = "$5" ] || die 'userdata identity changed'
printf '%s\n' "$info" | "$BB" grep -qx "Partition name: 'userdata'" || die 'source is not userdata'
[ "$(printf '%s\n' "$info" | awk '/^First sector:/ {print $3}')" = "$old_first" ] || die 'source start mismatch'
[ "$(printf '%s\n' "$info" | awk '/^Last sector:/ {print $3}')" = "$old_last" ] || die 'source end mismatch'
old_type=$(printf '%s\n' "$info" | awk '/^Partition GUID code:/ {print $4}')
old_guid=$(printf '%s\n' "$info" | awk '/^Partition unique GUID:/ {print $4}')
old_attrs=$(printf '%s\n' "$info" | awk '/^Attribute flags:/ {print $3}')
capacity=$("$SG" -p "$disk" | awk '/^Partition table holds up to/ {print $6}')
set -- --set-alignment=1 "--delete=$old_number"
seen_names=' '; seen_numbers=' '; seen_guids=' '; previous=$((old_first-1)); roots=0; stores=0; android_number=
while IFS=' ' read -r number name first last type guid attrs extra <&3; do
    [ -z "$extra" ] && [ -n "$attrs" ] || die 'invalid partition record'
    [ "${#name}" -le 16 ] || die 'GPT partition name exceeds Fastboot 16-byte limit'
    printf '%s\n' "$name" | "$BB" grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_-]{0,15}$' || die 'invalid ASCII partition name'
    for n in "$number" "$first" "$last"; do
        case $n in ''|*[!0-9]*) die 'invalid partition number/range' ;; esac
        [ "${#n}" -le 12 ] || die 'invalid partition range'
    done
    [ "$number" -ge 1 ] && [ "$number" -le "$capacity" ] || die 'partition number outside GPT'
    [ "$first" -gt "$previous" ] && [ "$first" -ge "$old_first" ] && [ "$last" -ge "$first" ] && [ "$last" -le "$old_last" ] || die 'partition leaves source region or overlaps'
    for id in "$type" "$guid"; do
        printf '%s\n' "$id" | "$BB" grep -Eq '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$' || die 'invalid GPT UUID'
    done
    printf '%s\n' "$attrs" | "$BB" grep -Eq '^[0-9A-Fa-f]{16}$' || die 'invalid GPT attributes'
    case $seen_numbers in *" $number "*) die 'duplicate number' ;; esac
    case $seen_names in *" $name "*) die 'duplicate name' ;; esac
    case $seen_guids in *" $guid "*) die 'duplicate GUID' ;; esac
    seen_numbers="$seen_numbers$number "; seen_names="$seen_names$name "; seen_guids="$seen_guids$guid "
    case $name in
    userdata)
        [ "$number" = "$old_number" ] && [ "$first" = "$old_first" ] && [ "$type" = "$old_type" ] && [ "$guid" = "$old_guid" ] && [ "$attrs" = "$old_attrs" ] || die 'userdata identity must be preserved'
        [ "$((last-first+1))" -ge 4194304 ] || die 'userdata smaller than 16 GiB'
        android_number=$number
        ;;
    linux_boot|lqr_*)
        [ "$number" != "$old_number" ] || die 'new partitions cannot reuse userdata entry'
        if "$SG" -i "$number" "$disk" 2>/dev/null | "$BB" grep -q '^Partition unique GUID:'; then die 'GPT entry already occupied'; fi
        [ "$((first % 1024))" = 0 ] && [ "$(((last+1) % 1024))" = 0 ] || die 'new partition is not 4 MiB aligned'
        [ "$type" = 0FC63DAF-8483-4772-8E79-3D69D8477DE4 ] && [ "$attrs" = 0000000000000000 ] || die 'invalid Linux partition type/attributes'
        if [ "$name" = linux_boot ]; then
            stores=$((stores+1)); [ "$((last-first+1))" -ge 524288 ] || die 'boot store smaller than 2 GiB'
        else
            printf '%s\n' "$name" | "$BB" grep -Eq '^lqr_[0-9a-f]{12}$' || die 'invalid root name'
            roots=$((roots+1)); [ "$((last-first+1))" -ge 4194304 ] || die 'root smaller than 16 GiB'
        fi
        ;;
    *) die 'unsupported partition role' ;;
    esac
    set -- "$@" "--new=$number:$first:$last" "--typecode=$number:$type" "--partition-guid=$number:$guid" "--attributes=$number:=:$attrs" "--change-name=$number:$name"
    previous=$last
done
exec 3<&-
[ "$stores" = 1 ] && [ "$roots" -le 16 ] || die 'incomplete layout'
[ "$roots" -gt 0 ] || [ -n "$android_number" ] || die 'no system partitions'
seal() {
    sync
    "$BB" blockdev --setro "$disk" || return 1
    for path in /sys/class/block/sda*/dev; do
        node=${path%/dev}; "$BB" blockdev --setro "/dev/${node##*/}" || return 1
    done
}
refresh_nodes() {
    if [ -f /etc/liuqin-recovery-session.json ]; then
        /usr/bin/python3 -I -B /usr/lib/liuqin/recovery-session.py nodes-after-gpt
    else
        "$BB" mdev -s
    fi
}
trap seal EXIT
trap 'exit 130' HUP INT TERM
"$BB" blockdev --rereadpt "$disk" || die 'kernel cannot reread GPT before writing'
refresh_nodes
seal
"$BB" blockdev --setrw "$disk"
"$SG" "$@" "$disk"
sync
"$BB" blockdev --rereadpt "$disk" || die 'kernel did not reread GPT'
refresh_nodes
"$SG" -v "$disk"
# A reserved Android region is explicitly uninitialized. Remove the old
# filesystem signature; Android Recovery must format it before Android boot.
if [ -n "$android_number" ]; then
    part=/dev/sda$android_number
    [ "$(cat "/sys/class/block/sda$android_number/partition")" = "$android_number" ] || die 'new userdata is not visible'
    "$BB" blockdev --setrw "$part"
    "$BB" dd if=/dev/zero of="$part" bs=1048576 count=16 conv=notrunc
fi
seal
trap - EXIT HUP INT TERM
printf 'liuqin-multiboot: LAYOUT_APPLIED\n'
