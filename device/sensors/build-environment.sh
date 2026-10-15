# SPDX-License-Identifier: MIT
# Host helpers for the isolated native ARM64 sensor build.

sensor_elf_needed() (
	# Match the ELF tag and brackets, not translated human-readable labels.
	sensor_dynamic=$(LC_ALL=C readelf -d "$1") || exit 1
	printf '%s\n' "$sensor_dynamic" | sed -n '/(NEEDED)/s/.*\[\([^]]*\)\].*/\1/p'
)

run_sensor_chroot() (
	sensor_guest_root=$1
	shift
	# Resolve chroot with the host PATH, then start a clean Ubuntu environment.
	# Keep proxy settings, but discard Nix tool/library/compiler search paths.
	# Do not set LC_ALL: upstream tests override LC_NUMERIC intentionally.
	exec chroot "$sensor_guest_root" /usr/bin/env -i \
		PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
		LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive JOBS="${JOBS:-12}" \
		SENSOR_LIBDIR="${SENSOR_LIBDIR:-lib/aarch64-linux-gnu}" \
		SENSOR_DISTRIBUTION="${SENSOR_DISTRIBUTION:-ubuntu}" \
		http_proxy="${http_proxy:-${HTTP_PROXY:-}}" \
		https_proxy="${https_proxy:-${HTTPS_PROXY:-}}" \
		no_proxy="${no_proxy:-${NO_PROXY:-}}" \
		"$@"
)

sensor_static_qemu() (
	sensor_qemu=${QEMU_AARCH64:-}
	if [ -z "$sensor_qemu" ]; then
		sensor_qemu=$(command -v qemu-aarch64-static || command -v qemu-aarch64) || {
			printf '%s\n' 'sensor build: a static qemu-aarch64 is required' >&2
			exit 1
		}
	fi
	[ -f "$sensor_qemu" ] && [ -x "$sensor_qemu" ] || {
		printf 'sensor build: QEMU is not executable: %s\n' "$sensor_qemu" >&2
		exit 1
	}
	sensor_qemu_headers=$(LC_ALL=C readelf -l "$sensor_qemu") || exit 1
	if printf '%s\n' "$sensor_qemu_headers" | grep -q INTERP; then
		printf 'sensor build: QEMU must be statically linked: %s\n' "$sensor_qemu" >&2
		exit 1
	fi
	printf '%s\n' "$sensor_qemu"
)
