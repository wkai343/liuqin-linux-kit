#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Device-side installer step for the dual layout: put the two boot images that
# liuqin-switch rotates through boot_a into its store on the freshly installed
# Ubuntu root, /var/lib/liuqin/switch/{ubuntu,android}.  Runs inside the
# installer RAM image, from install-root.sh, while the root is mounted.
#
#   install-switch-store.sh --check ubuntu URL SHA256 BYTES android URL SHA256 BYTES
#   install-switch-store.sh ROOT_DIR ubuntu URL SHA256 BYTES android URL SHA256 BYTES
#
# --check validates the arguments and touches nothing; install-root.sh runs it
# before it formats anything.  Each image is downloaded into a staging
# directory beside its final place, checked for size, digest and header kind,
# and only then moved into place with its SHA256SUMS -- the same layout and
# the same header rule liuqin-switch applies, so a store written here is one
# the switcher accepts.  /var/lib/liuqin/switch is not part of the native-root
# contract, so nothing here changes what stage 1 admits.
set -eu

BB=/bin/busybox
PARTITION_BYTES=201326592
STORE_REL=var/lib/liuqin/switch

die() { printf 'liuqin-install: %s\n' "$*" >&2; exit 1; }

# Little-endian u32 at a byte offset, in decimal.
u32_at() { # <file> <offset>
	# shellcheck disable=SC2046
	set -- $(dd if="$1" bs=1 skip="$2" count=4 2>/dev/null | od -An -tu1)
	[ "$#" = 4 ] || return 1
	printf '%s\n' "$(($1 + $2 * 256 + $3 * 65536 + $4 * 16777216))"
}

classify() { # <image or device>
	cl_magic=$(dd if="$1" bs=8 count=1 2>/dev/null | tr -d '\000')
	[ "$cl_magic" = 'ANDROID!' ] || { echo unknown; return 0; }
	cl_version=$(u32_at "$1" 40) || { echo unknown; return 0; }
	cl_kernel=$(u32_at "$1" 8) || { echo unknown; return 0; }
	[ "$cl_kernel" -gt 0 ] || { echo unknown; return 0; }
	case $cl_version in
	2)
		cl_page=$(u32_at "$1" 36) || cl_page=
		cl_header=$(u32_at "$1" 1644) || cl_header=
		cl_dtb=$(u32_at "$1" 1648) || cl_dtb=0
		if [ "$cl_page" = 4096 ] && [ "$cl_header" = 1660 ] && [ "$cl_dtb" -gt 0 ]; then
			echo ubuntu
			return 0
		fi
		;;
	3|4)
		cl_header=$(u32_at "$1" 20) || cl_header=
		if { [ "$cl_version" = 3 ] && [ "$cl_header" = 1580 ]; } ||
			{ [ "$cl_version" = 4 ] && [ "$cl_header" = 1584 ]; }; then
			echo android
			return 0
		fi
		;;
	esac
	echo unknown
}

check_entry() { # <expected set> <set> <url> <sha256> <bytes>
	[ "$2" = "$1" ] || die "switch store: expected the $1 image, got '$2'"
	case $3 in http://*) ;; *) die "switch store: $1 URL is not http: $3" ;; esac
	case $4 in *[!0-9a-f]*|'') die "switch store: invalid $1 sha256" ;; esac
	[ "${#4}" = 64 ] || die "switch store: invalid $1 sha256 length"
	case $5 in ''|*[!0-9]*) die "switch store: invalid $1 size" ;; esac
	[ "$5" -gt 0 ] && [ "$5" -le "$PARTITION_BYTES" ] ||
		die "switch store: the $1 image ($5 bytes) does not fit the 192 MiB boot_a"
}

check_arguments() { # ubuntu URL SHA BYTES android URL SHA BYTES
	[ "$#" = 8 ] || die 'usage: install-switch-store.sh ROOT_DIR|--check ubuntu URL SHA256 BYTES android URL SHA256 BYTES'
	check_entry ubuntu "$1" "$2" "$3" "$4"
	check_entry android "$5" "$6" "$7" "$8"
}

fetch_set() { # <store> <set> <url> <sha256> <bytes>
	fs_final=$1/$2
	fs_stage=$1/.$2.new
	$BB rm -rf "$fs_stage"
	$BB mkdir -p "$fs_stage"
	$BB wget -O "$fs_stage/boot.img" "$3"
	[ "$($BB stat -c %s "$fs_stage/boot.img")" = "$5" ] || die "switch store: the $2 image size does not match"
	[ "$($BB sha256sum "$fs_stage/boot.img" | $BB cut -d' ' -f1)" = "$4" ] ||
		die "switch store: the $2 image checksum does not match"
	[ "$(classify "$fs_stage/boot.img")" = "$2" ] ||
		die "switch store: the $2 image does not identify as a $2 boot image"
	printf '%s  boot.img\n' "$4" >"$fs_stage/SHA256SUMS"
	{
		printf '{\n'
		printf '  "set": "%s",\n' "$2"
		printf '  "source": "installer",\n'
		printf '  "created": "%s"\n' "$($BB date -u '+%Y-%m-%dT%H:%M:%SZ')"
		printf '}\n'
	} >"$fs_stage/meta.json"
	$BB chmod 0644 "$fs_stage/boot.img" "$fs_stage/SHA256SUMS" "$fs_stage/meta.json"
	$BB sync
	$BB rm -rf "$fs_final"
	$BB mv "$fs_stage" "$fs_final"
}

[ "$#" -ge 1 ] || check_arguments
target=$1
shift
check_arguments "$@"
[ "$target" != --check ] || exit 0
case $target in /*) ;; *) die 'switch store: the root directory must be absolute' ;; esac
[ -d "$target/etc" ] && [ -d "$target/var" ] || die "switch store: $target is not an installed root"

umask 022
store=$target/$STORE_REL
$BB mkdir -p "$store"
fetch_set "$store" ubuntu "$2" "$3" "$4"
fetch_set "$store" android "$6" "$7" "$8"
# boot_a receives the Ubuntu image when the host returns to fastboot.
{
	printf '{\n'
	printf '  "installed": "ubuntu",\n'
	printf '  "partition": "boot_a",\n'
	printf '  "running_slot": "installer",\n'
	printf '  "updated": "%s",\n' "$($BB date -u '+%Y-%m-%dT%H:%M:%SZ')"
	printf '  "boot_sha256": "%s"\n' "$3"
	printf '}\n'
} >"$store/state.json"
$BB chmod 0644 "$store/state.json"
$BB sync
printf 'liuqin-install: SWITCH_STORE_READY\n'
