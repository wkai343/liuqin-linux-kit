#!/bin/sh
# SPDX-License-Identifier: MIT
# Local archive operations only; no device access or partition changes.
set -eu

die() { printf 'rootfs-archive: %s\n' "$*" >&2; exit 1; }
[ "$#" = 3 ] || die 'usage: rootfs-archive.sh pack|extract ROOT ARCHIVE.tar.gz'
action=$1
root=$(realpath -m -- "$2")
archive=$(realpath -m -- "$3")
[ "$(id -u)" = 0 ] || die 'root is required to preserve numeric ownership and capabilities'
tar --version | grep -q 'GNU tar' || die 'GNU tar is required; BusyBox tar is not supported'
[ "$root" != / ] || die 'the host root is not a valid target'

# Keep a standard gzip stream, so the RAM installer needs no new decoder.
# Bound worker count on large hosts; gzip remains a portable fallback.
if command -v pigz >/dev/null 2>&1; then
	workers=$(nproc)
	[ "$workers" -le 8 ] || workers=8
	compressor="pigz -1 -n -p $workers"
else
	compressor='gzip -1 -n'
fi

case $action in
pack)
	[ -d "$root" ] || die 'root directory is missing'
	case $archive in "$root"/*) die 'archive must be outside the input tree' ;; esac
	[ ! -e "$archive" ] && [ ! -L "$archive" ] || die 'archive already exists'
	temporary=$(mktemp "${archive}.XXXXXX")
	trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
	started=$(date +%s)
	printf 'rootfs：压缩归档（%s），保留所有权、ACL 和扩展属性……\n' "$compressor"
	tar --create --use-compress-program="$compressor" --file="$temporary" --directory="$root" \
		--format=pax --numeric-owner --acls --xattrs --xattrs-include='*' \
		--sort=name --pax-option=delete=atime,delete=ctime .
	chmod 0644 "$temporary"
	ln -- "$temporary" "$archive"
	printf 'rootfs：归档完成，耗时 %s 秒。\n' "$(( $(date +%s) - started ))"
	;;
extract)
	[ -f "$archive" ] || die 'archive is missing'
	[ ! -e "$root" ] && [ ! -L "$root" ] || die 'extraction requires a new directory'
	mkdir -- "$root"
	started=$(date +%s)
	printf 'rootfs：解包基础系统……\n'
	tar --extract --use-compress-program="$compressor" --file="$archive" --directory="$root" \
		--numeric-owner --same-owner --same-permissions --acls --xattrs --xattrs-include='*'
	printf 'rootfs：解包完成，耗时 %s 秒。\n' "$(( $(date +%s) - started ))"
	;;
*) die 'expected pack or extract' ;;
esac
