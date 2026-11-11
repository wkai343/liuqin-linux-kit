#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Build the liuqin device .deb set from explicit firmware, userspace and
# kernel inputs. Each package owns its installed content.
#
#   firmware        /usr/lib/firmware closure from build-liuqin-firmware-prep.sh
#   device-support  desktop-neutral board-overlay and storage helper
#   sensors         SSC userspace from build-liuqin-sensors-stack.sh
#   kernel          modules tree + /boot payload (input-gated; boot write is C)
#   meta            liuqin-device metapackage depending on all of the above
#   all             everything whose inputs are present (kernel may be gated)
#
# Generic DEB packaging remains available for external system integration.
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
out_dir=${OUT_DIR:-"$project_root/out/build/liuqin-debs"}
version=${LIUQIN_DEB_VERSION:-0.1}
[ "${DEVICE_DESKTOP:-generic}" = generic ] || { printf '%s\n' 'only the generic device profile is supported' >&2; exit 1; }
maintainer='Liuqin Linux Kit contributors'

firmware_tree=${FIRMWARE_TREE:-"$project_root/device/firmware"}
firmware_manifest_sha256=${FIRMWARE_MANIFEST_SHA256:-012c413e0a5d5c3c14631fbdfe50da5c51e95ffc1ae431b8d93f776538b65ba2}
speaker_firmware=${SPEAKER_FIRMWARE_DIR:-}
board_overlay=${BOARD_OVERLAY:-"$project_root/device/board-overlay"}
busybox=${BUSYBOX:-}
busybox_sha256=${BUSYBOX_SHA256:-}
# SENSOR_STACK_SHA256 selects the sensor build to include in this package set.
sensor_stack=${SENSOR_STACK_TAR:-"$project_root/out/liuqin-sensors-stack/artifacts/sensor-stack.tar"}
sensor_stack_sha256=${SENSOR_STACK_SHA256:-9dcb2b8cb3a6539ccd6d2b410476f3e6ebc725072a3b63089a570ebdaeec5701}

kernel_modules_dir=${KERNEL_MODULES_DIR:-}
kernel_image=${KERNEL_IMAGE:-}
kernel_dtb=${KERNEL_DTB:-}
kernel_initramfs=${KERNEL_INITRAMFS:-}

die() { printf 'build-liuqin-debs: %s\n' "$*" >&2; exit 1; }

command -v dpkg-deb >/dev/null || die 'dpkg-deb is required'
case $out_dir in
"$project_root"/out/* | /tmp/*) ;;
*) die "refusing to wipe an output directory outside out/ or /tmp: $out_dir" ;;
esac

mkdir -p "$out_dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM

sha_ok() { [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ] || die "$3"; }

# divert_add <pkg> <path>... : preinst body fragment file
write_divert_preinst() {
	pkg=$1; shift
	{
		printf '#!/bin/sh\nset -e\n'
		printf 'case $1 in install|upgrade)\n'
		for p in "$@"; do
			printf '\tdpkg-divert --package %s --add --rename --divert %s.liuqin-orig %s\n' \
				"$pkg" "$p" "$p"
		done
		printf '\t;;\nesac\n'
	} >"$work/$pkg.preinst"
}

write_divert_postrm() {
	pkg=$1; shift
	{
		printf '#!/bin/sh\nset -e\n'
		printf 'if [ "$1" = remove ]; then\n'
		for p in "$@"; do
			printf '\tdpkg-divert --package %s --rename --remove %s\n' "$pkg" "$p"
		done
		printf 'fi\n'
	} >"$work/$pkg.postrm"
}

pack() { # pack <pkg> <arch> <depends> <description> [extra DEBIAN files...]
	pkg=$1; arch=$2; depends=$3; desc=$4
	root=$work/$pkg/root
	[ -d "$root/DEBIAN" ] || mkdir -p "$root/DEBIAN"
	mkdir -p "$root/usr/share/doc/$pkg"
	for document in LICENSE NOTICE CREDITS.md; do cp "$project_root/$document" "$root/usr/share/doc/$pkg/$document"; done
	{
		printf 'Package: %s\nVersion: %s\nArchitecture: %s\n' "$pkg" "$version" "$arch"
		printf 'Maintainer: %s\n' "$maintainer"
		if [ "$pkg" = liuqin-device-support ]; then printf 'Provides: liuqin-storage-policy-ufs (= 1)\n'; fi
		[ -z "$depends" ] || printf 'Depends: %s\n' "$depends"
		printf 'Section: misc\nPriority: optional\n'
		printf 'Description: %s\n' "$desc"
	} >"$root/DEBIAN/control"
	if [ -d "$root/etc" ]; then
		(cd "$root" && find etc -type f -printf '/etc/%P\n' | LC_ALL=C sort) >"$root/DEBIAN/conffiles"
		# A diverted path ships as a plain file: declaring it a conffile would
		# make dpkg prompt about the diverted-away distro conffile on install.
		if [ -f "$work/$pkg.noconffiles" ]; then
			grep -vxF -f "$work/$pkg.noconffiles" "$root/DEBIAN/conffiles" >"$work/conffiles.filtered" ||
				[ ! -s "$work/conffiles.filtered" ]
			mv "$work/conffiles.filtered" "$root/DEBIAN/conffiles"
		fi
		[ -s "$root/DEBIAN/conffiles" ] || rm -f "$root/DEBIAN/conffiles"
	fi
	for script in preinst postinst prerm postrm; do
		[ ! -f "$root/DEBIAN/$script" ] || chmod 0755 "$root/DEBIAN/$script"
	done
	[ ! -f "$root/DEBIAN/conffiles" ] || chmod 0644 "$root/DEBIAN/conffiles"
	dpkg-deb --root-owner-group --build "$root" "$out_dir/${pkg}_${version}_${arch}.deb" >/dev/null
	printf 'built: %s\n' "$out_dir/${pkg}_${version}_${arch}.deb"
}

build_firmware() {
	pkg=liuqin-firmware
	[ -d "$firmware_tree/usr/lib/firmware" ] ||
		die "firmware-prep tree is unavailable: $firmware_tree (run build/scripts/build-liuqin-firmware-prep.sh)"
	[ -f "$firmware_tree/firmware.manifest" ] && [ ! -L "$firmware_tree/firmware.manifest" ] ||
		die 'firmware-prep manifest is unavailable'
	sha_ok "$firmware_tree/firmware.manifest" "$firmware_manifest_sha256" \
		'firmware-prep manifest identity mismatch'
	root=$work/$pkg/root
	mkdir -p "$root"
	cp -a "$firmware_tree/usr" "$root/"
	# Re-hash every copied file against the pinned manifest; a wrong or
	# partial tree fails here, not on the panel.
	sed 's|  /|  |' "$firmware_tree/firmware.manifest" |
		(cd "$root" && sha256sum -c --quiet - >/dev/null) ||
		die 'firmware content does not match the pinned firmware-prep manifest'
	count=$(find "$root/usr/lib/firmware" -type f | wc -l | tr -d ' ')
	[ "$count" = 197 ] || die "firmware closure is not the pinned 197 files: $count"
	if [ -n "$speaker_firmware" ]; then
		python3 "$project_root/build/lib/speaker_firmware.py" "$speaker_firmware" "$root"
	else
		die 'the generic desktop requires SPEAKER_FIRMWARE_DIR from the stock ROM board stage'
	fi
	# The main-tree ath11k blobs (hw2.0 files and the hw2.1 symlinks pointing
	# at them) collide with linux-firmware-qualcomm-wireless at different
	# bytes; divert the distro copies so the pinned fallbacks survive a
	# distro firmware upgrade.
	divert_paths="
/usr/lib/firmware/ath11k/WCN6855/hw2.0/amss.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.0/board-2.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.0/m3.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.0/regdb.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.1/amss.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.1/board-2.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.1/m3.bin.zst
/usr/lib/firmware/ath11k/WCN6855/hw2.1/regdb.bin.zst"
	mkdir -p "$root/DEBIAN"
	# shellcheck disable=SC2086
	write_divert_preinst "$pkg" $divert_paths
	# shellcheck disable=SC2086
	write_divert_postrm "$pkg" $divert_paths
	cp "$work/$pkg.preinst" "$root/DEBIAN/preinst"
	cp "$work/$pkg.postrm" "$root/DEBIAN/postrm"
	pack "$pkg" all '' 'Xiaomi Pad 6 Pro (liuqin) firmware closure, pinned vendor set'
}

stage_device_overlay() {
	python3 "$project_root/build/lib/device_overlay.py" \
		--board "$board_overlay" --out "$root"
}

build_device_support() {
	pkg=liuqin-device-support
	[ -d "$board_overlay" ] && [ -x "$busybox" ] || die 'board overlay or ARM64 BusyBox is missing'
	sha_ok "$busybox" "$busybox_sha256" 'pinned BusyBox hash mismatch'
	root=$work/$pkg/root
	mkdir -p "$root"
	stage_device_overlay
	guard=$root/usr/local/sbin/liuqin-gnome-storage-guard
	grep -qx 'marker=${LIUQIN_GNOME_GUARD_TEST_MARKER:-/etc/liuqin-native-root}' "$guard" || die 'storage guard marker drifted'
	sed -i \
		-e 's|^marker=${LIUQIN_GNOME_GUARD_TEST_MARKER:-/etc/liuqin-native-root}$|marker=/etc/liuqin-native-root|' "$guard"
	install -D -m 0755 "$busybox" "$root/usr/local/bin/busybox"
	python3 "$project_root/build/lib/storage_scope.py" --out "$root/usr/libexec/liuqin-storage-seal" \
		--compiler "${STORAGE_SCOPE_CC:-${CROSS_COMPILE:-aarch64-unknown-linux-musl-}gcc}"
	for required in usr/local/sbin/liuqin-gnome-storage-guard \
		usr/local/sbin/liuqin-wlan-mac usr/local/sbin/liuqin-bt-public-addr usr/local/libexec/liuqin-backlight-default; do
		[ -x "$root/$required" ] || die "generic device package misses $required"
	done
	mkdir -p "$root/DEBIAN"
	{
		printf '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n'
		PYTHONPATH="$project_root/build/lib" python3 -c 'from storage_scope import legacy_cleanup; print(legacy_cleanup())'
		printf 'fi\n'
	} >"$root/DEBIAN/postinst"
	pack "$pkg" arm64 'systemd, udev, python3, chrony, network-manager, bluez, libglib2.0-bin' \
		'Xiaomi Pad 6 Pro (liuqin) desktop-neutral board services and storage protection'
}

build_sensors() {
	pkg=liuqin-sensors
	[ -f "$sensor_stack" ] && [ ! -L "$sensor_stack" ] ||
		die "reviewed sensor stack is unavailable: $sensor_stack (run build/scripts/build-liuqin-sensors-stack.sh)"
	sha_ok "$sensor_stack" "$sensor_stack_sha256" 'sensor stack artifact identity mismatch'
	root=$work/$pkg/root
	mkdir -p "$root"
	tar --extract --file "$sensor_stack" --directory "$root" --no-same-owner
	for required in \
		usr/bin/hexagonrpcd \
		usr/bin/ssccli \
		usr/local/libexec/liuqin-iio-sensor-proxy \
		usr/local/sbin/liuqin-sensor-registry-import \
		etc/udev/rules.d/80-liuqin-fastrpc.rules \
		etc/systemd/system/liuqin-ssc-sample-gate.service; do
		[ -e "$root/$required" ] || die "sensor stack merge missed $required"
	done
	# Runtime libraries come from the distribution via Depends, not from a
	# private copy inside the package.
	# The standalone layer closes dependencies against a minimal build root,
	# so it may contain GUdev/QMI/MBIM/Polkit and other distro DSOs. A .deb must
	# leave all those paths to distribution packages, keeping only our built libraries.
	find "$root/usr/lib/aarch64-linux-gnu" -maxdepth 1 \
		\( -type f -o -type l \) -name '*.so*' \
		! -name 'libhexagonrpc.so.*' ! -name 'libssc.so.*' -delete
	# End the mixed-ownership arrangement: instead of a /usr/local binary plus
	# an ExecStart override, the SSC-enabled proxy replaces the distro binary
	# in place through a diversion.  The drop-in keeps its real payload -- the
	# SSC sample gate and the AF_QIPCRTR sandbox admission -- and loses only
	# the two ExecStart lines that pointed at the /usr/local path.
	mkdir -p "$root/usr/libexec"
	mv "$root/usr/local/libexec/liuqin-iio-sensor-proxy" "$root/usr/libexec/iio-sensor-proxy"
	chmod 0755 "$root/usr/libexec/iio-sensor-proxy"
	dropin=$root/etc/systemd/system/iio-sensor-proxy.service.d/90-liuqin-ssc.conf
	[ -f "$dropin" ] || die 'sensor stack carries no SSC drop-in'
	grep -qx 'ExecStart=/usr/local/libexec/liuqin-iio-sensor-proxy' "$dropin" ||
		die 'SSC drop-in drifted from the reviewed ExecStart override'
	grep -vx -e 'ExecStart=' -e 'ExecStart=/usr/local/libexec/liuqin-iio-sensor-proxy' \
		"$dropin" >"$work/dropin.tmp"
	mv "$work/dropin.tmp" "$dropin"
	chmod 0644 "$dropin"
	mkdir -p "$root/DEBIAN"
	write_divert_preinst "$pkg" /usr/libexec/iio-sensor-proxy
	write_divert_postrm "$pkg" /usr/libexec/iio-sensor-proxy
	{
		printf '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n'
		printf '\tif [ -d /run/systemd/system ]; then systemctl daemon-reload || :; fi\n'
		printf '\tif command -v systemd-sysusers >/dev/null; then systemd-sysusers || :; fi\n'
		printf '\tif command -v systemd-tmpfiles >/dev/null; then systemd-tmpfiles --create || :; fi\n'
		printf 'fi\n'
	} >"$root/DEBIAN/postinst"
	cp "$work/$pkg.preinst" "$root/DEBIAN/preinst"
	cp "$work/$pkg.postrm" "$root/DEBIAN/postrm"
	pack "$pkg" arm64 'libc6, libglib2.0-0t64 (>= 2.76), libgudev-1.0-0, libpolkit-gobject-1-0, libqmi-glib5, libqrtr-glib0, libqrtr1, libprotobuf-c1, iio-sensor-proxy' \
		'Xiaomi Pad 6 Pro (liuqin) SSC sensor stack (hexagonrpcd, libssc, SSC iio-sensor-proxy)'
}

build_kernel() {
	pkg=liuqin-kernel
	[ -n "$kernel_modules_dir" ] ||
		die 'liuqin-kernel requires KERNEL_MODULES_DIR (build-liuqin-kernel-modules.sh output)'
	[ -n "$kernel_image" ] && [ -f "$kernel_image" ] ||
		die 'liuqin-kernel requires KERNEL_IMAGE (matching kernel OUT Image)'
	[ -n "$kernel_dtb" ] && [ -f "$kernel_dtb" ] ||
		die 'liuqin-kernel requires KERNEL_DTB (matching device.dtb)'
	# The boot builder produces its initramfs after root assembly.
	modules_tar=$kernel_modules_dir/modules.tar
	modules_manifest=$kernel_modules_dir/modules.manifest
	release_file=$kernel_modules_dir/kernel.release
	for f in "$modules_tar" "$modules_manifest" "$release_file"; do
		[ -f "$f" ] && [ ! -L "$f" ] || die "kernel-module input is unavailable: $f"
	done
	release=$(cat "$release_file")
	case $release in '' | *[!A-Za-z0-9._+-]*) die "unsafe kernel release: $release" ;; esac
	root=$work/$pkg/root
	mkdir -p "$root/boot"
	tar --extract --file "$modules_tar" --directory "$root" --no-same-owner
	[ -d "$root/usr/lib/modules/$release" ] ||
		die "module tree does not contain the pinned release $release"
	[ -f "$root/usr/lib/modules/$release/modules.dep" ] ||
		die 'module tree carries no dependency index'
	install -D -m 0644 "$kernel_image" "$root/boot/vmlinuz-$release"
	if [ -n "$kernel_initramfs" ]; then
		[ -f "$kernel_initramfs" ] || die "KERNEL_INITRAMFS is set but unavailable: $kernel_initramfs"
		install -D -m 0644 "$kernel_initramfs" "$root/boot/initrd.img-$release"
	fi
	install -D -m 0644 "$kernel_dtb" "$root/boot/dtb-$release"
	install -D -m 0644 "$modules_manifest" "$root/usr/share/liuqin/kernel-modules.manifest"
	printf '%s\n' "$release" >"$work/kernel.release"
	install -D -m 0644 "$work/kernel.release" "$root/usr/share/liuqin/kernel.release"
	mkdir -p "$root/DEBIAN"
	# The installer owns boot partition updates; this package only supplies files.
	pack "$pkg" arm64 '' \
		'Xiaomi Pad 6 Pro (liuqin) kernel modules and boot payload'
}

build_meta() {
	pkg=liuqin-device-base
	root=$work/$pkg/root
	mkdir -p "$root"
	pack "$pkg" arm64 \
		"liuqin-firmware (= $version), liuqin-device-support (= $version), liuqin-sensors (= $version)" \
		'Xiaomi Pad 6 Pro (liuqin) device support metapackage'
}

case ${1:-all} in
firmware) build_firmware ;;
device-support) build_device_support ;;
sensors) build_sensors ;;
kernel) build_kernel ;;
meta) build_meta ;;
all)
	build_firmware
	build_device_support
	build_sensors
	if [ -n "$kernel_modules_dir" ]; then build_kernel; else
		printf 'build-liuqin-debs: skipping liuqin-kernel (no KERNEL_MODULES_DIR)\n' >&2
	fi
	build_meta
	;;
*) die 'usage: build-liuqin-debs.sh all|firmware|device-support|sensors|kernel|meta' ;;
esac
