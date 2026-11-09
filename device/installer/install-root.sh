#!/bin/sh
# SPDX-License-Identifier: MIT
# Device-side installer, intended only for the dedicated read-only RAM image.
set -eu
die() { printf 'liuqin-install: %s\n' "$*" >&2; exit 1; }
phase() { printf 'liuqin-gui: phase=%s\n' "$1"; }
usage='usage: install-root.sh BOOT_ID ROOTFS_URL SHA256 BYTES ERASE-LIUQIN-USERDATA ROOT_PARTNAME HOME_PARTNAME_OR_- [ROOT-CONTRACT RAM_PATH SHA256] [FILESYSTEM ext4|btrfs] [BOOT-TARGET RAM_PATH SHA256] [ROOT-RANGE START_BYTES LENGTH_BYTES]'
[ "$#" -ge 7 ] || die "$usage"
[ "$5" = ERASE-LIUQIN-USERDATA ] || die 'explicit data-erasure acknowledgement required'
root_name=$6
home_name=$7
# Options follow the seven fixed arguments of the single-root interface.
filesystem=ext4
filesystem_given=
target_contract=/etc/liuqin-native-root.contract
target_contract_sha=
boot_target=
boot_target_sha=
expected_start=
expected_bytes=
parse_options() { # the script's own arguments
	shift 7
	while [ "$#" -gt 0 ]; do
        case $1 in
        ROOT-RANGE)
            [ "$#" -ge 3 ] && [ -z "$expected_start" ] || die "$usage"
            expected_start=$2; expected_bytes=$3
            for value in "$expected_start" "$expected_bytes"; do
                case $value in ''|*[!0-9]*) die 'invalid root range' ;; esac
            done
            shift 2
            ;;
        FILESYSTEM)
            [ "$#" -ge 2 ] && [ -z "$filesystem_given" ] || die "$usage"
            filesystem=$2; filesystem_given=1
            case $filesystem in ext4|btrfs) ;; *) die 'unsupported root filesystem' ;; esac
            shift
            ;;
        BOOT-TARGET)
            [ "$#" -ge 3 ] && [ -z "$boot_target" ] || die "$usage"
            boot_target=$2; boot_target_sha=$3
            case $boot_target in /run/*) ;; *) die 'boot target must be in RAM' ;; esac
            case $boot_target in *'/../'*|*'/./'*|*'//'*) die 'invalid boot target path' ;; esac
            case $boot_target_sha in ''|*[!0-9a-f]*) die 'invalid boot target hash' ;; esac
            [ "${#boot_target_sha}" = 64 ] || die 'invalid boot target hash length'
            shift 2
            ;;
        ROOT-CONTRACT)
            [ "$#" -ge 3 ] && [ -z "$target_contract_sha" ] || die "$usage"
            target_contract=$2
            target_contract_sha=$3
            case $target_contract in /run/*) ;; *) die 'target contract must be staged in RAM' ;; esac
            case $target_contract in *'/../'*|*'/./'*|*'//'*) die 'invalid target contract path' ;; esac
            case $target_contract_sha in *[!0-9a-f]*|'') die 'invalid target contract hash' ;; esac
            [ "${#target_contract_sha}" = 64 ] || die 'invalid target contract hash length'
            shift 2
            ;;
		*) die "unsupported option: $1; $usage" ;;
		esac
		shift
	done
}
parse_options "$@"
if [ -z "$boot_target" ]; then
    [ "$root_name:$home_name" = userdata:- ] || die 'unsupported root/home partition combination'
else
    [ "$home_name" = - ] || die 'bound roots do not have a separate home'
fi
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM boot identity changed'
[ "$(cat /etc/liuqin-installer 2>/dev/null)" = liuqin ] || die 'not the installer RAM image'
# The selected target boot supplies the authenticated kernel contract.
[ "$(cat /etc/liuqin-installer-interface 2>/dev/null)" = liuqin-installer-v2 ] || die 'unsupported installer interface'
[ -n "$target_contract_sha" ] || die 'installer requires ROOT-CONTRACT'
[ -f "$target_contract" ] && [ ! -L "$target_contract" ] || die 'target contract is not a regular file'
[ "$(stat -c '%u %g' "$target_contract")" = '0 0' ] || die 'target contract must be root owned'
[ "$(stat -c %s "$target_contract")" -le 32768 ] || die 'target contract is too large'
printf '%s  %s\n' "$target_contract_sha" "$target_contract" | /bin/busybox sha256sum -c -
BB=/bin/busybox
. /usr/lib/liuqin/root-interface-v2.sh
liuqin_contract_v2_parse "$target_contract" || die 'invalid root contract'
root_label=LIUQIN_ROOT
if [ -n "$boot_target" ]; then
    [ "$(cat /etc/liuqin-multiboot)" = 1 ] || die 'installer lacks multiboot capability'
    [ -f "$boot_target" ] && [ ! -L "$boot_target" ] || die 'invalid boot target'
    printf '%s  %s\n' "$boot_target_sha" "$boot_target" | "$BB" sha256sum -c -
    . /usr/lib/liuqin/boot-target-v1.sh
    liuqin_target_parse "$boot_target" || die 'invalid boot target binding'
    [ "$root_name" = "$bt_name" ] && [ "$filesystem" = "$bt_fstype" ] || die 'root target mismatch'
    root_label=LQ_$(printf '%s' "$bt_id" | "$BB" tr -d '-' | "$BB" cut -c1-12)
fi
if [ "$filesystem" = btrfs ]; then
    [ -x /usr/sbin/mkfs.btrfs ] && [ -x /usr/bin/btrfs ] || die 'installer lacks Btrfs tools'
    grep -qx btrfs /etc/liuqin-filesystems || die 'installer kernel lacks builtin Btrfs support'
    grep -qw btrfs /proc/filesystems || die 'running installer kernel cannot mount Btrfs'
fi
ln -sf /proc/self/fd/0 /dev/stdin
# The host checks Fastboot product/serial; boot_id binds this operation to it.
# Partition numbers vary with capacity; resolve userdata by its GPT name.
find_part() {
	found=
	for info in /sys/class/block/sd*/uevent; do
		/bin/busybox grep -qx "PARTNAME=$1" "$info" || continue
		[ -z "$found" ] || die "duplicate partition name: $1"
		node=${info%/uevent}
		found=/dev/${node##*/}
	done
	[ -n "$found" ] && [ -b "$found" ] ||
		die "partition is missing: $1 —— 该设备不是受支持的小米平板 6 Pro（liuqin），或分区步骤未完成"
	printf '%s' "$found"
}
root=$(find_part "$root_name")
node=${root##*/}
if [ -n "$boot_target" ]; then
    if ! "$BB" grep -qx "PARTUUID=$bt_partuuid" "/sys/class/block/$node/uevent"; then
        # Recovery's private aliases are rebuilt from GPT and matched against
        # the live kernel name/range/device number before each command.
        [ -f /etc/liuqin-recovery-session.json ] &&
            [ -L "/dev/disk/by-partuuid/$bt_partuuid" ] &&
            [ "$("$BB" readlink -f "/dev/disk/by-partuuid/$bt_partuuid")" = "$root" ] || die 'root partition identity mismatch'
    fi
fi
size=$(cat "/sys/class/block/$node/size")
case $size in ''|*[!0-9]*) die 'system partition size is unavailable' ;; esac
if [ -n "$expected_start" ]; then
    [ -n "$boot_target" ] || die 'root range requires a bound root'
    [ "$(( $(cat "/sys/class/block/$node/start") * 512 ))" = "$expected_start" ] &&
        [ "$((size * 512))" = "$expected_bytes" ] || die 'root range changed'
fi
[ "$size" -ge 33554432 ] || die 'the system partition is smaller than 16 GiB —— 不支持该布局'
parent_path=$(/bin/busybox readlink -f "/sys/class/block/$node/.." 2>/dev/null || true)
[ -n "$parent_path" ] || die 'the system partition parent disk is unavailable'
parent=/dev/${parent_path##*/}
[ -b "$parent" ] || die 'the system partition parent disk is missing'
[ "$parent" = /dev/sda ] || die 'the root is not on the first UFS LUN'
[ "$(cat /sys/class/block/sda/queue/logical_block_size)" = 4096 ] ||
	die 'the root disk does not report 4096-byte logical sectors'
targets=$root
for target in $targets; do
	device_number=$(cat "/sys/class/block/${target##*/}/dev")
	awk -v device="$device_number" '$3 == device {found=1} END {exit !found}' /proc/self/mountinfo &&
		die 'a target partition is mounted (including through a device alias)'
	[ "$(/bin/busybox blockdev --getro "$target")" = 1 ] || die 'a target partition is not initially read-only'
done
battery=
for supply in /sys/class/power_supply/*; do
	[ "$(cat "$supply/type" 2>/dev/null)" = Battery ] || continue
	battery=$(cat "$supply/capacity")
	break
done
case $battery in ''|*[!0-9]*) die 'battery level is unavailable' ;; esac
[ "$battery" -ge 30 ] || die 'charge the tablet to at least 30 percent before installation'
case $3 in *[!0-9a-f]*|'') die 'invalid rootfs hash' ;; esac
[ "${#3}" = 64 ] || die 'invalid rootfs hash length'
case $4 in ''|*[!0-9]*) die 'invalid archive size' ;; esac
available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
[ "$4" -gt 0 ] && [ "$(( $4 + 536870912 ))" -lt "$((available * 1024))" ] ||
	die 'insufficient RAM to stage this archive safely'
archive=/mnt/install-download/rootfs.tar.gz
mounted=false
opened=false
persist_mounted=false
download_mounted=false
cleanup() {
	cleanup_status=$?
	if [ "$cleanup_status" != 0 ] && [ -f /etc/liuqin-native-recovery.json ]; then
		/usr/bin/python3 -I -B /usr/lib/liuqin/native-root-guard.py failed >/run/liuqin-root-failure.log 2>&1 || true
	fi
	sync
	if [ "$mounted" = true ]; then
		umount /mnt/install || return 1
		mounted=false
	fi
	if [ "$opened" = true ]; then
		/bin/busybox blockdev --setro "$root"
		/bin/busybox blockdev --setro "$parent"
		opened=false
	fi
	if [ "$persist_mounted" = true ]; then
		umount /run/persist || return 1
		persist_mounted=false
	fi
	if [ "$download_mounted" = true ]; then
		umount /mnt/install-download || return 1
		download_mounted=false
	fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM PIPE
# A default /run tmpfs is too small for some complete desktop archives.
mkdir -p /mnt/install-download /run/persist /mnt/install
mount -t tmpfs -o "size=$(( $4 + 16777216 ))" tmpfs /mnt/install-download
download_mounted=true
phase transfer
/bin/busybox wget -O "$archive" "$2"
phase verify_archive
[ "$(stat -c %s "$archive")" = "$4" ] || die 'downloaded archive size mismatch'
printf '%s  %s\n' "$3" "$archive" | /bin/busybox sha256sum -c -
[ "$(cat /proc/sys/kernel/random/boot_id)" = "$1" ] || die 'RAM identity changed before formatting'
[ -b /dev/disk/by-partlabel/persist ] || die 'persist is missing'
mount -t ext4 -o ro,noload /dev/disk/by-partlabel/persist /run/persist
persist_mounted=true
for item in wlan/wlan_mac.bin bluetooth/.bt_nv.bin audio/crus_calr.bin; do
	[ -f "/run/persist/$item" ] || die 'factory data is incomplete'
done
[ -d /run/persist/sensors/registry/registry ] || die 'factory sensor registry is missing'
# Check the target userspace before opening the device write window.
# Published archives use ./ paths. Stop at the requested entry instead of
# decompressing the whole desktop archive for each tiny metadata file.
archive_metadata() {
    /usr/bin/tar -xOf "$archive" --occurrence=1 "./$1" 2>/dev/null ||
        /usr/bin/tar -xOf "$archive" --occurrence=1 "$1" 2>/dev/null
}
if [ "$filesystem" = btrfs ]; then
    support=$(archive_metadata etc/liuqin-filesystems) || die 'target root lacks filesystem capabilities'
    [ "$support" = "$(printf 'ext4\nbtrfs')" ] || die 'target root does not support Btrfs'
    template=$(archive_metadata etc/liuqin-root-filesystem) || die 'target root lacks a filesystem configuration template'
    case $template in ext4|btrfs) ;; *) die 'target root lacks a filesystem configuration template' ;; esac
fi
if [ -n "$boot_target" ]; then
    [ "$(archive_metadata etc/liuqin-system-id)" = unbound ] &&
    [ "$(archive_metadata etc/liuqin-boot-target)" = unbound ] || die 'root lacks clean binding templates'
fi
phase format_root
if [ -f /etc/liuqin-native-recovery.json ]; then
    # The native guard checks that every UFS partition is unmounted and
    # read-only. Release the calibration mount after validating its files.
    umount /run/persist
    persist_mounted=false
    if [ "$filesystem" = btrfs ]; then
        /usr/bin/python3 -I -B /usr/lib/liuqin/native-root-guard.py "$root_name" "$3" PREPARE-BTRFS
    else
        /usr/bin/python3 -I -B /usr/lib/liuqin/native-root-guard.py "$root_name" "$3"
    fi
    mount -t ext4 -o ro,noload /dev/disk/by-partlabel/persist /run/persist
    persist_mounted=true
fi
/bin/busybox blockdev --setrw "$parent"
opened=true
/bin/busybox blockdev --setrw "$root"
case $filesystem in
ext4)
    if [ -n "$boot_target" ]; then
        /usr/sbin/mkfs.ext4 -F -L "$root_label" -U "$bt_fsuuid" -m 0 "$root"
    else
        /usr/sbin/mkfs.ext4 -F -L LIUQIN_ROOT -m 0 "$root"
    fi
    mount -t ext4 "$root" /mnt/install
    ;;
btrfs)
    # Avoid a synchronous whole-userdata TRIM on the tablet's UFS.
    if [ -n "$boot_target" ]; then
        /usr/sbin/mkfs.btrfs -f -K -L "$root_label" -U "$bt_fsuuid" -d single -m dup -s 4096 "$root"
    else
        /usr/sbin/mkfs.btrfs -f -K -L LIUQIN_ROOT -d single -m dup -s 4096 "$root"
    fi
    mount -t btrfs -o subvolid=5,noatime,compress=zstd:3 "$root" /mnt/install
    ;;
esac
mounted=true
if [ "$filesystem" = btrfs ]; then
    /usr/bin/btrfs subvolume create /mnt/install/native-root
else
    mkdir /mnt/install/native-root
fi
phase extract_root
/usr/bin/tar -xzf "$archive" -C /mnt/install/native-root \
	--numeric-owner --same-owner --same-permissions --acls --xattrs --xattrs-include='*' --warning=no-timestamp
phase configure_device
if [ -n "$boot_target" ]; then
    liuqin_target_fs_check "$root" || die 'formatted filesystem UUID mismatch'
    printf '%s\n' "$bt_id" >/mnt/install/native-root/etc/liuqin-system-id
    cat "$boot_target" >/mnt/install/native-root/etc/liuqin-boot-target
    chmod 0644 /mnt/install/native-root/etc/liuqin-system-id /mnt/install/native-root/etc/liuqin-boot-target
fi
# Overwrite the packaged template in place, retaining its Fedora SELinux xattr.
printf '%s\n' "$filesystem" >/mnt/install/native-root/etc/liuqin-root-filesystem
chmod 0644 /mnt/install/native-root/etc/liuqin-root-filesystem
PERSIST_SRC=/run/persist sh /usr/lib/liuqin/provision.sh /mnt/install/native-root
{
	printf '# Written by the liuqin installer. The root filesystem is mounted by\n'
	printf '# the boot image, using its root binding or the single-root label.\n'
    printf '# Root filesystem: %s.\n' "$filesystem"
	printf '# Single root: /home is a directory on the root filesystem.\n'
} >/mnt/install/native-root/etc/fstab
chmod 0644 /mnt/install/native-root/etc/fstab
if [ -f /mnt/install/native-root/usr/lib/snapd/snap-confine ]; then
	[ -n "$(/usr/sbin/getcap /mnt/install/native-root/usr/lib/snapd/snap-confine)" ] ||
		die 'snap-confine capability was not restored'
fi
# Verify immutable boot-contract files after extraction and provisioning.
phase verify_root
liuqin_root_v2_check /mnt/install/native-root "$target_contract" 1 || die 'root or modules do not match'
phase sync_root
cleanup
trap - EXIT HUP INT TERM PIPE
printf 'liuqin-install: ROOT_INSTALLED\n'
