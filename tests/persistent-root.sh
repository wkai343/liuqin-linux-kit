#!/bin/sh
# Host-side fail-closed tests for initramfs persistent-root selection.

set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
init=$project_root/device/boot/initramfs/init
host_busybox=${HOST_BUSYBOX:-$(command -v busybox)}
[ -x "$host_busybox" ] || { printf '%s\n' 'test-liuqin-persistent-root: host BusyBox is required' >&2; exit 1; }
test_root=$(mktemp -d)
cleanup() { rm -rf "$test_root"; }
trap cleanup EXIT HUP INT TERM

fail() { printf 'test-liuqin-persistent-root: %s\n' "$*" >&2; exit 1; }



make_fake_commands() {
	mkdir -p "$test_root/bin"
	cat >"$test_root/bin/findfs" <<'EOF'
#!/bin/sh
case $1 in
LABEL=LIUQIN_ROOT)
	[ "${TEST_FINDFS_FAIL:-0}" != 1 ] || exit 1
	printf '%s\n' "$TEST_LABEL_PATH" ;;
LABEL=LIUQIN_HOME)
	[ -n "${TEST_HOME_LABEL_PATH:-}" ] || exit 1
	printf '%s\n' "$TEST_HOME_LABEL_PATH" ;;
*) exit 2 ;;
esac
EOF
	cat >"$test_root/bin/blockdev" <<'EOF'
#!/bin/sh
set -u
op=$1; node=$2; key=$(basename "$node")
printf '%s %s\n' "$op" "$node" >>"$TEST_COMMAND_LOG"
case "$op:$key" in
	--setrw:$TEST_FAIL_SETRW) exit 1 ;;
	--setro:$TEST_FAIL_SETRO) exit 1 ;;
	--getro:$TEST_FAIL_GETRO) exit 1 ;;
esac
case $op in
	--setro) printf '1\n' >"$TEST_STATE/$key" ;;
	--setrw)
		printf '0\n' >"$TEST_STATE/$key"
		if [ "${TEST_MUTATE_AFTER_SETRW:-none}" != none ] &&
			[ ! -e "$TEST_MUTATED" ]; then
			: >"$TEST_MUTATED"
			case $TEST_MUTATE_AFTER_SETRW in
			add) : >"$TEST_DEV/sdc"; printf '1\n' >"$TEST_STATE/sdc" ;;
			remove) rm -f "$TEST_DEV/sdb" "$TEST_STATE/sdb" ;;
			remove-target) rm -f "$TEST_DEV/sda35" "$TEST_STATE/sda35" ;;
			alias) mv "$TEST_DEV/sdb" "$TEST_DEV/real-sdb"; ln -s real-sdb "$TEST_DEV/sdb" ;;
			esac
		fi
		;;
	--getro) cat "$TEST_STATE/$key" ;;
	*) exit 2 ;;
esac
EOF
	cat >"$test_root/bin/mount" <<'EOF'
#!/bin/sh
set -u
count=0
[ ! -r "$TEST_MOUNT_COUNT" ] || count=$(cat "$TEST_MOUNT_COUNT")
count=$((count + 1)); printf '%s\n' "$count" >"$TEST_MOUNT_COUNT"
printf 'mount%d %s\n' "$count" "$*" >>"$TEST_COMMAND_LOG"
# The current Btrfs kernel parser only accepts rescue=nologreplay. The old
# standalone flag must fail here just as it does on a real filesystem.
for argument in "$@"; do
    case ,$argument, in *,nologreplay,*) exit 1 ;; esac
done
if [ "$count" = "${TEST_MUTATE_AFTER_MOUNT_NUMBER:-0}" ]; then
	case ${TEST_MUTATE_AFTER_MOUNT:-none} in
	add) : >"$TEST_DEV/sdc"; printf '1\n' >"$TEST_STATE/sdc" ;;
	add-newroot) : >"$TEST_NEWROOT/dev/sdc"; printf '1\n' >"$TEST_STATE/sdc" ;;
	remove) rm -f "$TEST_DEV/sdb" "$TEST_STATE/sdb" ;;
	alias) mv "$TEST_DEV/sdb" "$TEST_DEV/real-sdb"; ln -s real-sdb "$TEST_DEV/sdb" ;;
	esac
fi
[ "$count" != "${TEST_FAIL_MOUNT_NUMBER:-0}" ]
EOF
	cat >"$test_root/bin/umount" <<'EOF'
#!/bin/sh
count=0
[ ! -r "$TEST_UMOUNT_COUNT" ] || count=$(cat "$TEST_UMOUNT_COUNT")
count=$((count + 1)); printf '%s\n' "$count" >"$TEST_UMOUNT_COUNT"
printf 'umount %s\n' "$*" >>"$TEST_COMMAND_LOG"
[ "${TEST_FAIL_UMOUNT:-0}" != 1 ] || exit 1
for fail_count in ${TEST_FAIL_UMOUNT_NUMBER:-0}; do
	[ "$count" != "$fail_count" ] || exit 1
done
exit 0
EOF
	cat >"$test_root/bin/chroot" <<'EOF'
#!/bin/sh
count=0
[ ! -r "$TEST_CHROOT_COUNT" ] || count=$(cat "$TEST_CHROOT_COUNT")
count=$((count + 1)); printf '%s\n' "$count" >"$TEST_CHROOT_COUNT"
printf 'chroot %s\n' "$*" >>"$TEST_COMMAND_LOG"
	if [ "$count" = "${TEST_MUTATE_RCS_ON_CHROOT:-0}" ]; then
		case ${TEST_MUTATE_RCS:-none} in
		missing) rm -f "$TEST_NEWROOT/etc/init.d/rcS" ;;
		mode) chmod 0644 "$TEST_NEWROOT/etc/init.d/rcS"; printf '644 0 0\n' >"$TEST_RCS_STAT_FILE" ;;
		stale) printf '#!/bin/sh\necho stale\n' >"$TEST_NEWROOT/etc/init.d/rcS" ;;
		alias) mv "$TEST_NEWROOT/etc/init.d/rcS" "$TEST_NEWROOT/etc/init.d/real-rcS"; ln -s real-rcS "$TEST_NEWROOT/etc/init.d/rcS" ;;
		esac
	fi
	if [ "$count" = "${TEST_MUTATE_PID1_ON_CHROOT:-0}" ]; then
		case ${TEST_MUTATE_PID1:-none} in
		inittab-missing) rm -f "$TEST_NEWROOT/etc/inittab" ;;
		inittab-stale) printf '# changed\n' >"$TEST_NEWROOT/etc/inittab" ;;
		inittab-alias) mv "$TEST_NEWROOT/etc/inittab" "$TEST_NEWROOT/etc/real-inittab"; ln -s real-inittab "$TEST_NEWROOT/etc/inittab" ;;
		busybox-missing) rm -f "$TEST_NEWROOT/usr/local/bin/busybox" ;;
		busybox-stale) printf '# changed\n' >"$TEST_NEWROOT/usr/local/bin/busybox" ;;
		busybox-alias) mv "$TEST_NEWROOT/usr/local/bin/busybox" "$TEST_NEWROOT/usr/local/bin/real-busybox"; ln -s real-busybox "$TEST_NEWROOT/usr/local/bin/busybox" ;;
		sbin-link) rm "$TEST_NEWROOT/sbin"; ln -s usr/local "$TEST_NEWROOT/sbin" ;;
		init-link) rm "$TEST_NEWROOT/usr/sbin/init"; ln -s /bin/sh "$TEST_NEWROOT/usr/sbin/init" ;;
		bash-stale) printf '# changed\n' >"$TEST_NEWROOT/bin/bash" ;;
		loader-stale) printf '# changed\n' >"$TEST_NEWROOT/lib/ld-linux-aarch64.so.1" ;;
		tool-stale) printf '# changed\n' >"$TEST_NEWROOT/usr/bin/stat" ;;
		esac
	fi
	if [ "$count" = "${TEST_MUTATE_GNOME_ON_CHROOT:-0}" ]; then
		case ${TEST_MUTATE_GNOME:-none} in
		marker-missing) rm -f "$TEST_NEWROOT/etc/liuqin-gnome-root" ;;
		file-stale) printf 'changed\n' >"$TEST_NEWROOT/usr/bin/gnome-shell" ;;
		file-symlink) mv "$TEST_NEWROOT/usr/bin/gnome-shell" "$TEST_NEWROOT/usr/bin/real-gnome-shell"; ln -s real-gnome-shell "$TEST_NEWROOT/usr/bin/gnome-shell" ;;
		init-link) rm -f "$TEST_NEWROOT/usr/sbin/init"; ln -s /bin/sh "$TEST_NEWROOT/usr/sbin/init" ;;
		esac
	fi
	if [ "$count" = "${TEST_MUTATE_NATIVE_ON_CHROOT:-0}" ]; then
		case ${TEST_MUTATE_NATIVE:-none} in
		marker-missing) rm -f "$TEST_NEWROOT/etc/liuqin-native-root" ;;
		file-stale) printf 'changed\n' >"$TEST_NEWROOT/usr/libexec/iio-sensor-proxy" ;;
		esac
	fi
[ "${TEST_FAIL_CHROOT:-0}" != 1 ] &&
	[ "$count" != "${TEST_FAIL_CHROOT_NUMBER:-0}" ]
EOF
	cat >"$test_root/bin/switch_root" <<'EOF'
#!/bin/sh
printf 'switch_root %s\n' "$*" >>"$TEST_COMMAND_LOG"
exit "${TEST_SWITCH_ROOT_STATUS:-1}"
EOF
	cat >"$test_root/bin/stat" <<'EOF'
#!/bin/sh
# Only the GNOME per-file authority check uses the bare owner format; host
# fixtures cannot chown to root, so answer it from the fixture instead.
if [ "${2:-}" = '%u %g' ]; then
	printf '%s\n' "${TEST_GNOME_OWNER:-0 0}"
	exit 0
fi
if [ "${2:-}" = '%a' ]; then
	exec /usr/bin/stat "$@"
fi
if [ "${3##*/}" = liuqin-root-profile ] && [ "${2:-}" = '%a %u %g' ]; then
    printf '%s\n' "$TEST_PROFILE_STAT"; exit 0
fi
if [ "${TEST_V2_OWNER_MODEL:-0}" = 1 ] && [ "${2:-}" = '%a %u %g' ]; then
    printf '%s %s\n' "$(/usr/bin/stat -c %a "$3")" "${TEST_GNOME_OWNER:-0 0}"
    exit 0
fi
case ${3:-} in
	*/etc/liuqin-native-root) printf '%s\n' "${TEST_NATIVE_MARKER_STAT:-644 0 0 22}" ;;
	*/etc/liuqin-gnome-root) printf '%s\n' "${TEST_GNOME_MARKER_STAT:-644 0 0 21}" ;;
	*/liuqin-root-profile) printf '%s\n' "${TEST_PROFILE_STAT:-644 0 0}" ;;
	*/etc/liuqin-persistent-root) printf '%s\n' "$TEST_MARKER_STAT" ;;
	*/etc/init.d/rcS) cat "$TEST_RCS_STAT_FILE" ;;
	*/liuqin-persistent-root.contract) printf '%s\n' "$TEST_ROOT_CONTRACT_STAT" ;;
	*/etc/inittab) printf '%s\n' "$TEST_INITTAB_STAT" ;;
	*/usr/local/bin/busybox) printf '%s\n' "$TEST_BUSYBOX_STAT" ;;
	*/usr/sbin/init) printf '%s\n' "$TEST_INIT_STAT" ;;
	*/sbin) printf '%s\n' "$TEST_SBIN_STAT" ;;
	*) exec /usr/bin/stat "$@" ;;
esac
EOF
	cat >"$test_root/bin/busybox" <<'EOF'
#!/bin/sh
command=$1
shift
printf 'busybox %s\n' "$command $*" >>"$TEST_COMMAND_LOG"
exec "$TEST_FAKE_BIN/$command" "$@"
EOF
	for applet in cut grep head readlink realpath sha256sum cat uname awk dd od tr mkdir; do
		cat >"$test_root/bin/$applet" <<EOF
#!/bin/sh
exec /usr/bin/$applet "\$@"
EOF
	done
	chmod 0755 "$test_root/bin"/*
}


reset_base_fixture() {
    TEST_ROOT_FS=ext4
    export TEST_ROOT_FS
    unset LIUQIN_STORAGE_FS_SUPPORT
	rm -rf "$test_root/case"
	case_root=$test_root/case
	dev=$case_root/dev
	sys=$case_root/sys/class/block
	probe=$case_root/probe
	newroot=$case_root/newroot
	state=$case_root/state
	mkdir -p "$dev" "$sys/sda/queue" "$sys/sda35" "$probe/etc" \
		"$probe/usr/local/bin" "$probe/usr/sbin" "$probe/bin" "$probe/lib" \
		"$probe/usr/bin" "$newroot/etc" "$newroot/usr/local/bin" \
		"$newroot/usr/sbin" "$newroot/bin" "$newroot/lib" "$newroot/usr/bin" \
		"$newroot/dev" "$state"
	for node in sda sda1 sda35 sdb; do
		: >"$dev/$node"
		: >"$newroot/dev/$node"
		printf '1\n' >"$state/$node"
	done
	printf '493854720\n' >"$sys/sda/size"
	printf '4096\n' >"$sys/sda/queue/logical_block_size"
	printf '35\n' >"$sys/sda35/partition"
	printf '22065152\n' >"$sys/sda35/start"
	printf '471789528\n' >"$sys/sda35/size"
	printf 'PARTNAME=userdata\n' >"$sys/sda35/uevent"
	: >"$case_root/mounts"
	for root in "$probe" "$newroot"; do
		printf 'LIUQIN_PERSISTENT_ROOT_V1\n' >"$root/etc/liuqin-persistent-root"
		printf '# inittab\n' >"$root/etc/inittab"
		mkdir -p "$root/etc/init.d"
		printf '#!/usr/local/bin/busybox sh\nexit 0\n' >"$root/etc/init.d/rcS"
		chmod 0755 "$root/etc/init.d/rcS"
		printf '#!/bin/sh\nexit 0\n' >"$root/usr/local/bin/busybox"
		chmod 0755 "$root/usr/local/bin/busybox"
		printf '#!/bin/sh\nexit 0\n' >"$root/bin/bash"
		printf 'loader\n' >"$root/lib/ld-linux-aarch64.so.1"
		printf 'tool\n' >"$root/usr/bin/stat"
		ln -s usr/sbin "$root/sbin"
		ln -s /usr/local/bin/busybox "$root/usr/sbin/init"
	done
	command_log=$case_root/commands
	mount_count=$case_root/mount-count
	umount_count=$case_root/umount-count
	chroot_count=$case_root/chroot-count
	root_contract=$case_root/liuqin-persistent-root.contract
	{
		printf '%s\n' 'LIUQIN_PERSISTENT_ROOT_CONTRACT_V2'
		printf 'rootfs_sha256=%064d\n' 0
		printf 'rcs_sha256=%s\n' "$(sha256sum "$probe/etc/init.d/rcS" | cut -d' ' -f1)"
		printf 'inittab_sha256=%s\n' "$(sha256sum "$probe/etc/inittab" | cut -d' ' -f1)"
		printf 'busybox_sha256=%s\n' "$(sha256sum "$probe/usr/local/bin/busybox" | cut -d' ' -f1)"
		printf '%s\n' 'sbin_link=usr/sbin'
		printf '%s\n' 'usr_sbin_init_link=/usr/local/bin/busybox'
		printf '%s\n' 'rcs_interpreter=/usr/local/bin/busybox sh'
	} >"$root_contract"
	rcs_stat=$case_root/rcs-stat
	printf '755 0 0\n' >"$rcs_stat"
	mutated=$case_root/mutated
	init_log=$case_root/init-log
	: >"$init_log"
	: >"$command_log"
	# The nodes every abort path must have put back into the sealed baseline.
	# A split-layout fixture adds its linux_home node to this list.
	ro_nodes='sda sda1 sda35 sdb'
	TEST_HOME_LABEL_PATH=
	TEST_LABEL_PATH=$dev/sda35
	TEST_MARKER_STAT='644 0 0 26'
	TEST_FINDFS_FAIL=0 TEST_FAIL_SETRW=none TEST_FAIL_SETRO=none \
		TEST_FAIL_GETRO=none TEST_FAIL_MOUNT_NUMBER=0 TEST_FAIL_UMOUNT=0 \
		TEST_FAIL_UMOUNT_NUMBER=0 TEST_FAIL_CHROOT=0 TEST_FAIL_CHROOT_NUMBER=0 \
		TEST_MUTATE_AFTER_SETRW=none TEST_MUTATE_AFTER_MOUNT_NUMBER=0 \
		TEST_MUTATE_AFTER_MOUNT=none TEST_MUTATE_RCS_ON_CHROOT=0 TEST_MUTATE_RCS=none \
		TEST_MUTATE_PID1_ON_CHROOT=0 TEST_MUTATE_PID1=none \
		TEST_STORAGE_PHASE=prepare TEST_SWITCH_ROOT_STATUS=1
	TEST_ROOT_CONTRACT_STAT='644 0 0 453'
	TEST_INITTAB_STAT='644 0 0' TEST_BUSYBOX_STAT='755 0 0'
	TEST_SBIN_STAT='777 0 0' TEST_INIT_STAT='777 0 0'
	TEST_PROFILE_STAT='644 0 0' TEST_GNOME_MARKER_STAT='644 0 0 21'
	TEST_GNOME_OWNER='0 0' TEST_MUTATE_GNOME_ON_CHROOT=0 TEST_MUTATE_GNOME=none
	TEST_NATIVE_MARKER_STAT='644 0 0 22'
	TEST_MUTATE_NATIVE_ON_CHROOT=0 TEST_MUTATE_NATIVE=none
	LIUQIN_STORAGE_PROFILE_FILE= LIUQIN_STORAGE_GNOME_CONTRACT= LIUQIN_STORAGE_NATIVE_CONTRACT=
	LIUQIN_STORAGE_SPLASH_STATE=$case_root/splash-state
	LIUQIN_STORAGE_SPLASH_HELPER=$case_root/splash-helper
	export case_root dev sys probe newroot state command_log mount_count umount_count
	export init_log ro_nodes TEST_HOME_LABEL_PATH
	export chroot_count
	export root_contract rcs_stat mutated
	export TEST_LABEL_PATH TEST_MARKER_STAT TEST_FINDFS_FAIL TEST_FAIL_SETRW
	export TEST_FAIL_SETRO TEST_FAIL_GETRO TEST_FAIL_MOUNT_NUMBER
	export TEST_FAIL_UMOUNT TEST_FAIL_UMOUNT_NUMBER TEST_FAIL_CHROOT
	export TEST_FAIL_CHROOT_NUMBER
	export TEST_MUTATE_AFTER_SETRW TEST_MUTATE_AFTER_MOUNT_NUMBER TEST_MUTATE_AFTER_MOUNT
	export TEST_MUTATE_RCS_ON_CHROOT TEST_MUTATE_RCS TEST_MUTATE_PID1_ON_CHROOT TEST_MUTATE_PID1
	export TEST_STORAGE_PHASE TEST_SWITCH_ROOT_STATUS
	export TEST_ROOT_CONTRACT_STAT TEST_INITTAB_STAT TEST_BUSYBOX_STAT TEST_SBIN_STAT TEST_INIT_STAT
	export TEST_PROFILE_STAT TEST_GNOME_MARKER_STAT TEST_GNOME_OWNER
	export TEST_MUTATE_GNOME_ON_CHROOT TEST_MUTATE_GNOME
	export LIUQIN_STORAGE_PROFILE_FILE LIUQIN_STORAGE_GNOME_CONTRACT
	export TEST_NATIVE_MARKER_STAT TEST_MUTATE_NATIVE_ON_CHROOT TEST_MUTATE_NATIVE
	export LIUQIN_STORAGE_NATIVE_CONTRACT
	export LIUQIN_STORAGE_SPLASH_STATE LIUQIN_STORAGE_SPLASH_HELPER
}


run_init() {
	env \
		LIUQIN_INIT_STORAGE_TEST_ONLY=1 \
		LIUQIN_INIT_STORAGE_TEST_PHASE="$TEST_STORAGE_PHASE" \
		LIUQIN_INIT_TEST_PATH="$test_root/bin:/usr/bin:/bin" \
		LIUQIN_INIT_TEST_BUSYBOX="$test_root/bin/busybox" \
		LIUQIN_INIT_TEST_LOG="$init_log" \
		TEST_HOME_LABEL_PATH="${TEST_HOME_LABEL_PATH:-}" \
		LIUQIN_STORAGE_DEV_ROOT="$dev" \
		LIUQIN_STORAGE_SYS_BLOCK="$sys" \
		LIUQIN_STORAGE_MOUNTS="$case_root/mounts" \
		LIUQIN_STORAGE_PROBE_ROOT="$probe" \
		LIUQIN_STORAGE_NEWROOT="$newroot" \
		LIUQIN_STORAGE_ROOT_CONTRACT="$root_contract" \
		LIUQIN_STORAGE_PROFILE_FILE="${LIUQIN_STORAGE_PROFILE_FILE:-}" \
		LIUQIN_STORAGE_GNOME_CONTRACT="${LIUQIN_STORAGE_GNOME_CONTRACT:-}" \
		LIUQIN_STORAGE_NATIVE_CONTRACT="${LIUQIN_STORAGE_NATIVE_CONTRACT:-}" \
		LIUQIN_STORAGE_SPLASH_STATE="$LIUQIN_STORAGE_SPLASH_STATE" \
		LIUQIN_STORAGE_SPLASH_HELPER="$LIUQIN_STORAGE_SPLASH_HELPER" \
		TEST_NATIVE_MARKER_STAT="$TEST_NATIVE_MARKER_STAT" \
		TEST_MUTATE_NATIVE_ON_CHROOT="$TEST_MUTATE_NATIVE_ON_CHROOT" \
		TEST_MUTATE_NATIVE="$TEST_MUTATE_NATIVE" \
		TEST_PROFILE_STAT="$TEST_PROFILE_STAT" \
		TEST_GNOME_MARKER_STAT="$TEST_GNOME_MARKER_STAT" \
		TEST_GNOME_OWNER="$TEST_GNOME_OWNER" \
		TEST_MUTATE_GNOME_ON_CHROOT="$TEST_MUTATE_GNOME_ON_CHROOT" \
		TEST_MUTATE_GNOME="$TEST_MUTATE_GNOME" \
		TEST_LABEL_PATH="$TEST_LABEL_PATH" TEST_MARKER_STAT="$TEST_MARKER_STAT" \
		TEST_FINDFS_FAIL="$TEST_FINDFS_FAIL" TEST_FAIL_SETRW="$TEST_FAIL_SETRW" \
		TEST_FAIL_SETRO="$TEST_FAIL_SETRO" TEST_FAIL_GETRO="$TEST_FAIL_GETRO" \
		TEST_FAIL_MOUNT_NUMBER="$TEST_FAIL_MOUNT_NUMBER" \
		TEST_FAIL_UMOUNT="$TEST_FAIL_UMOUNT" \
		TEST_FAIL_UMOUNT_NUMBER="$TEST_FAIL_UMOUNT_NUMBER" \
		TEST_FAIL_CHROOT="$TEST_FAIL_CHROOT" \
		TEST_FAIL_CHROOT_NUMBER="$TEST_FAIL_CHROOT_NUMBER" \
		TEST_STATE="$state" TEST_COMMAND_LOG="$command_log" \
		TEST_MOUNT_COUNT="$mount_count" TEST_UMOUNT_COUNT="$umount_count" \
		TEST_CHROOT_COUNT="$chroot_count" \
		TEST_SWITCH_ROOT_STATUS="$TEST_SWITCH_ROOT_STATUS" \
		TEST_DEV="$dev" TEST_MUTATED="$mutated" TEST_NEWROOT="$newroot" \
		TEST_FAKE_BIN="$test_root/bin" \
		TEST_RCS_STAT_FILE="$rcs_stat" \
		sh "$init" >/dev/null 2>&1
}


assert_all_ro() {
	for node in ${ro_nodes:-sda sda1 sda35 sdb}; do
		[ "$(cat "$state/$node")" = 1 ] || fail "$1 left $node writable"
	done
}


expect_rejected() {
	name=$1
	if run_init; then fail "$name: mutation was accepted"; fi
	assert_all_ro "$name"
}


expect_rejected_before_open() {
	name=$1
	expect_rejected "$name"
	if grep -q -- '--setrw' "$command_log"; then
		fail "$name: target identity/root failure opened a block device"
	fi
}


expect_critical() {
	name=$1
	if run_init; then
		fail "$name: critical recovery failure was accepted"
	else
		rc=$?
	fi
	[ "$rc" -eq 125 ] || fail "$name: expected unsafe-fallback status 125, got $rc"
	assert_all_ro "$name"
}


expect_critical_node_set_changed() {
	name=$1
	if run_init; then
		fail "$name: node-set mutation was accepted"
	else
		rc=$?
	fi
	[ "$rc" -eq 125 ] || fail "$name: expected unsafe-fallback status 125, got $rc"
	for node in sda sda1 sda35; do
		[ -e "$dev/$node" ] || continue
		[ "$(cat "$state/$node")" = 1 ] || fail "$name left $node writable"
	done
}


expect_empty_sd_namespace_critical() {
	name=$1
	if run_init; then
		fail "$name: empty /dev/sd* namespace was accepted as a safe fallback"
	else
		rc=$?
	fi
	[ "$rc" -eq 125 ] || fail "$name: expected unsafe-fallback status 125, got $rc"
	if grep -q -- '--setrw' "$command_log"; then
		fail "$name: empty namespace opened a block device"
	fi
}


make_native_tree() {
	tree=$1
	mkdir -p "$tree/etc/dconf/db/local.d/locks" "$tree/etc/udev/rules.d" \
		"$tree/etc/systemd/system/basic.target.requires" \
		"$tree/etc/systemd/system/multi-user.target.wants" \
		"$tree/etc/systemd/system/graphical.target.wants" \
		"$tree/etc/systemd/system/bluetooth.service.d" \
		"$tree/etc/systemd/system/NetworkManager.service.d" \
		"$tree/etc/systemd/system/liuqin-power-keyd.service.d" \
		"$tree/usr/bin" "$tree/usr/sbin" "$tree/usr/local/bin" \
		"$tree/usr/local/libexec" "$tree/usr/local/sbin" \
		"$tree/usr/libexec" "$tree/usr/lib/systemd/system" \
		"$tree/usr/lib/firmware/novatek/liuqin" \
		"$tree/usr/lib/firmware/qcom/sm8450" \
		"$tree/usr/lib/firmware/updates/qcom" \
		"$tree/usr/share/qcom/sm8450/Xiaomi/liuqin/sensors" \
		"$tree/usr/share/liuqin/power"
	printf 'liuqin-native-root-v1\n' >"$tree/etc/liuqin-native-root"
	printf '#!/bin/sh\nexit 0\n' >"$tree/usr/lib/systemd/systemd"
	printf 'gnome-shell\n' >"$tree/usr/bin/gnome-shell"
	printf 'hexagonrpcd\n' >"$tree/usr/bin/hexagonrpcd"
	printf 'gdm3\n' >"$tree/usr/sbin/gdm3"
	printf 'busybox\n' >"$tree/usr/local/bin/busybox"
	printf 'liuqin-shell\n' >"$tree/usr/local/bin/liuqin-shell"
	printf 'storage-guard\n' >"$tree/usr/local/sbin/liuqin-gnome-storage-guard"
	printf 'usb-rescue\n' >"$tree/usr/local/sbin/liuqin-gnome-usb-rescue"
	printf 'power-keyd\n' >"$tree/usr/local/libexec/liuqin-power-keyd"
	printf 'power-key-action\n' >"$tree/usr/local/libexec/liuqin-power-key-action"
	printf 'power-menu\n' >"$tree/usr/local/libexec/liuqin-power-menu"
	printf 'iio-sensor-proxy\n' >"$tree/usr/libexec/iio-sensor-proxy"
	printf 'snap-root-admission\n' >"$tree/usr/local/sbin/liuqin-snap-root-admission"
	printf 'bt-public-addr\n' >"$tree/usr/local/sbin/liuqin-bt-public-addr"
	printf 'wlan-mac\n' >"$tree/usr/local/sbin/liuqin-wlan-mac"
	printf 'ssc-sample-gate\n' >"$tree/usr/libexec/liuqin-ssc-sample-gate"
	printf 'touch-csot\n' >"$tree/usr/lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_csot.bin"
	printf 'touch-tm\n' >"$tree/usr/lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_tm.bin"
	printf 'r04-audio-topology\n' >"$tree/usr/lib/firmware/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin"
	printf 'sqe\n' >"$tree/usr/lib/firmware/updates/qcom/a730_sqe.fw"
	printf 'gmu\n' >"$tree/usr/lib/firmware/updates/qcom/gmu_gen70000.bin"
	printf 'version=12\n' >"$tree/usr/share/qcom/sm8450/Xiaomi/liuqin/sensors/sns_reg_version"
	printf '6.17.0-rc1-gfixture\n' >"$tree/usr/share/liuqin/kernel.release"
	printf 'fixture  usr/lib/modules/6.17.0-rc1-gfixture/kernel/fixture.ko\n' \
		>"$tree/usr/share/liuqin/kernel-modules.manifest"
	printf 'gschema\n' >"$tree/usr/share/liuqin/power/io.github.liuqin.power.gschema.xml"
	printf 'compiled\n' >"$tree/usr/share/liuqin/power/gschemas.compiled"
	printf '/org/gnome/settings-daemon/plugins/power/power-button-action\n' \
		>"$tree/etc/dconf/db/local.d/locks/00-liuqin-power"
	printf '[Service]\nExecStart=/usr/local/sbin/liuqin-gnome-storage-guard\n' \
		>"$tree/etc/systemd/system/liuqin-gnome-storage-guard.service"
	printf '[Service]\nExecStart=/usr/local/sbin/liuqin-gnome-usb-rescue\n' \
		>"$tree/etc/systemd/system/liuqin-gnome-usb-rescue.service"
	printf '[Service]\nExecStart=/usr/local/libexec/liuqin-backlight-default\n' \
		>"$tree/etc/systemd/system/liuqin-backlight-default.service"
	printf '[Unit]\nRequires=liuqin-bt-preconfigure.service\nAfter=liuqin-bt-preconfigure.service\n' \
		>"$tree/etc/systemd/system/bluetooth.service.d/20-liuqin-public-address.conf"
	printf '[Unit]\nBefore=bluetooth.service\nPartOf=bluetooth.service\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/liuqin-bt-public-addr\nRemainAfterExit=yes\n' \
		>"$tree/etc/systemd/system/liuqin-bt-preconfigure.service"
	printf '[Service]\nRuntimeDirectory=liuqin-power-keyd\n' \
		>"$tree/etc/systemd/system/liuqin-power-keyd.service.d/20-liuqin-session-runtime.conf"
	for native_unit in \
		liuqin-power-keyd.service liuqin-snap-root-admission.service \
		liuqin-hexagonrpcd-sdsp.service \
		liuqin-ssc-sample-gate.service liuqin-sensor-stack.target \
		liuqin-wlan-mac.service; do
		printf '[Unit]\nDescription=%s\n' "$native_unit" \
			>"$tree/etc/systemd/system/$native_unit"
	done
	printf '[Unit]\nRequires=liuqin-wlan-mac.service\n' \
		>"$tree/etc/systemd/system/NetworkManager.service.d/20-liuqin-wlan-mac.conf"
	printf 'SUBSYSTEM=="misc", KERNEL=="fastrpc-sdsp"\n' \
		>"$tree/etc/udev/rules.d/80-liuqin-fastrpc.rules"
	chmod 0755 "$tree/usr/lib/systemd/systemd" "$tree/usr/bin/gnome-shell" \
		"$tree/usr/bin/hexagonrpcd" \
		"$tree/usr/sbin/gdm3" "$tree/usr/local/bin/busybox" \
		"$tree/usr/local/bin/liuqin-shell" \
		"$tree/usr/local/sbin/liuqin-gnome-storage-guard" \
		"$tree/usr/local/sbin/liuqin-gnome-usb-rescue" \
		"$tree/usr/local/libexec/liuqin-power-keyd" \
		"$tree/usr/local/libexec/liuqin-power-key-action" \
		"$tree/usr/local/libexec/liuqin-power-menu" \
		"$tree/usr/libexec/iio-sensor-proxy" \
		"$tree/usr/local/sbin/liuqin-snap-root-admission" \
		"$tree/usr/local/sbin/liuqin-bt-public-addr" \
		"$tree/usr/local/sbin/liuqin-wlan-mac" \
		"$tree/usr/libexec/liuqin-ssc-sample-gate"
	chmod 0644 "$tree/etc/liuqin-native-root" \
		"$tree/etc/dconf/db/local.d/locks/00-liuqin-power" \
		"$tree/etc/systemd/system/liuqin-gnome-storage-guard.service" \
		"$tree/etc/systemd/system/liuqin-gnome-usb-rescue.service" \
		"$tree/etc/systemd/system/liuqin-backlight-default.service" \
		"$tree/etc/systemd/system/liuqin-power-keyd.service" \
		"$tree/etc/systemd/system/liuqin-power-keyd.service.d/20-liuqin-session-runtime.conf" \
		"$tree/etc/systemd/system/liuqin-snap-root-admission.service" \
		"$tree/etc/systemd/system/bluetooth.service.d/20-liuqin-public-address.conf" \
		"$tree/etc/systemd/system/liuqin-bt-preconfigure.service" \
		"$tree/etc/systemd/system/liuqin-hexagonrpcd-sdsp.service" \
		"$tree/etc/systemd/system/liuqin-ssc-sample-gate.service" \
		"$tree/etc/systemd/system/liuqin-sensor-stack.target" \
		"$tree/etc/systemd/system/liuqin-wlan-mac.service" \
		"$tree/etc/systemd/system/NetworkManager.service.d/20-liuqin-wlan-mac.conf" \
		"$tree/etc/udev/rules.d/80-liuqin-fastrpc.rules" \
		"$tree/usr/lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_csot.bin" \
		"$tree/usr/lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_tm.bin" \
		"$tree/usr/lib/firmware/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin" \
		"$tree/usr/lib/firmware/updates/qcom/a730_sqe.fw" \
		"$tree/usr/lib/firmware/updates/qcom/gmu_gen70000.bin" \
		"$tree/usr/share/qcom/sm8450/Xiaomi/liuqin/sensors/sns_reg_version" \
		"$tree/usr/share/liuqin/kernel.release" \
		"$tree/usr/share/liuqin/kernel-modules.manifest" \
		"$tree/usr/share/liuqin/power/io.github.liuqin.power.gschema.xml" \
		"$tree/usr/share/liuqin/power/gschemas.compiled"
	ln -s ../liuqin-gnome-storage-guard.service \
		"$tree/etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service"
	ln -s ../liuqin-gnome-usb-rescue.service \
		"$tree/etc/systemd/system/multi-user.target.wants/liuqin-gnome-usb-rescue.service"
	ln -s ../liuqin-power-keyd.service \
		"$tree/etc/systemd/system/multi-user.target.wants/liuqin-power-keyd.service"
	ln -s ../liuqin-backlight-default.service \
		"$tree/etc/systemd/system/graphical.target.wants/liuqin-backlight-default.service"
	rm -f "$tree/usr/sbin/init"
	ln -s ../lib/systemd/systemd "$tree/usr/sbin/init"
	rm -f "$tree/etc/systemd/system/default.target" \
		"$tree/etc/systemd/system/display-manager.service"
	ln -s /usr/lib/systemd/system/graphical.target \
		"$tree/etc/systemd/system/default.target"
	ln -s /lib/systemd/system/gdm3.service \
		"$tree/etc/systemd/system/display-manager.service"
}


native_expect_success() {
	name=$1
    fs=${TEST_ROOT_FS:-ext4}
    ro_opts=ro,noload; rw_opts=rw,noatime
    if [ "$fs" = btrfs ]; then
        ro_opts=ro,rescue=nologreplay,subvolid=5
        rw_opts=rw,noatime,subvolid=5,compress=zstd:3
    fi
	if ! run_init; then cat "$init_log" >&2; fail "$name: valid native root was rejected"; fi
	[ "$(cat "$state/sda")" = 0 ] || fail "$name: parent was not opened"
	[ "$(cat "$state/sda35")" = 0 ] || fail "$name: target was not opened"
	[ "$(cat "$state/sda1")" = 1 ] || fail "$name: sibling became writable"
	grep -q "mount1 -t $fs -o $ro_opts $dev/sda35 $probe" "$command_log" ||
		fail "$name: missing read-only validation mount"
	grep -q "mount2 -t $fs -o $rw_opts $dev/sda35 $probe" "$command_log" ||
		fail "$name: missing read-write volume mount"
	grep -q "mount3 --bind $probe/native-root $newroot" "$command_log" ||
		fail "$name: missing native subroot bind"
	[ "$(grep -c "^umount $probe\$" "$command_log")" = 2 ] ||
		fail "$name: the backing volume was not detached after the bind"
	if grep -q "rw,noatime $dev/sda35 $newroot" "$command_log"; then
		fail "$name: native profile used the legacy whole-volume root mount"
	fi
}



reset_fixture() {
    reset_base_fixture
    make_native_tree "$probe/native-root"
    make_native_tree "$newroot"
    profile_file=$case_root/liuqin-root-profile
    printf 'native-updatable\n' >"$profile_file"
    native_contract=$case_root/native-root.contract
    LIUQIN_STORAGE_PROFILE_FILE=$profile_file
    LIUQIN_STORAGE_NATIVE_CONTRACT=$native_contract
    LIUQIN_STORAGE_V2_TEST_HELPER=$project_root/device/boot/root-interface-v2.sh
    TEST_V2_OWNER_MODEL=1
    export LIUQIN_STORAGE_PROFILE_FILE LIUQIN_STORAGE_NATIVE_CONTRACT LIUQIN_STORAGE_V2_TEST_HELPER TEST_V2_OWNER_MODEL
    kernel_release=$("$host_busybox" uname -r)
    for tree in "$probe/native-root" "$newroot"; do
        rm -f "$tree/usr/sbin/gdm3" "$tree/usr/bin/gnome-shell" \
            "$tree/etc/dconf/db/local.d/locks/00-liuqin-power" \
            "$tree/usr/local/libexec/liuqin-power-keyd" "$tree/usr/local/libexec/liuqin-power-key-action" \
            "$tree/etc/systemd/system/liuqin-power-keyd.service" \
            "$tree/etc/systemd/system/multi-user.target.wants/liuqin-power-keyd.service"
        mkdir -p "$tree/usr/lib/modules/$kernel_release" "$tree/usr/share/liuqin/kernels/$kernel_release"
        printf 'liuqin-root-v2\n' >"$tree/etc/liuqin-root-interface"
        : >"$tree/usr/lib/modules/$kernel_release/modules.dep"
        printf '%s  usr/lib/modules/%s/modules.dep\n' "$(sha256sum "$tree/usr/lib/modules/$kernel_release/modules.dep" | cut -d' ' -f1)" "$kernel_release" \
            >"$tree/usr/share/liuqin/kernels/$kernel_release/modules.manifest"
        { printf 'LIUQIN_ROOT_INTERFACE_V2\nkernel_release=%s\n' "$kernel_release"
          printf 'image_sha256=%064d\n' 0
          printf 'modules_sha256=%s\n' "$(sha256sum "$tree/usr/share/liuqin/kernels/$kernel_release/modules.manifest" | cut -d' ' -f1)"
        } >"$tree/usr/share/liuqin/kernels/$kernel_release/boot.contract"
    done
    cp "$probe/native-root/usr/share/liuqin/kernels/$kernel_release/boot.contract" "$native_contract"
}
make_fake_commands
reset_fixture; native_expect_success valid
reset_fixture; printf '250069680\n' >"$sys/sda/size"; printf '229067376\n' >"$sys/sda35/size"; native_expect_success capacity-128g
reset_fixture; printf '34\n' >"$sys/sda35/partition"; native_expect_success partition-number-dynamic
reset_fixture; printf '512\n' >"$sys/sda/queue/logical_block_size"; expect_rejected_before_open sector-size
reset_fixture; printf '33554431\n' >"$sys/sda35/size"; expect_rejected_before_open partition-floor
reset_fixture; printf 'PARTNAME=super\n' >"$sys/sda35/uevent"; expect_rejected_before_open wrong-partition
reset_fixture; mkdir -p "$sys/sda7"; printf 'PARTNAME=userdata\n' >"$sys/sda7/uevent"; expect_rejected_before_open duplicate-userdata
reset_fixture; TEST_LABEL_PATH=$dev/sda1; export TEST_LABEL_PATH; expect_rejected_before_open wrong-label
reset_fixture; TEST_FINDFS_FAIL=1; export TEST_FINDFS_FAIL; expect_rejected_before_open missing-label
reset_fixture; printf '%s /mnt ext4 rw 0 0\n' "$dev/sda35" >"$case_root/mounts"; expect_rejected_before_open mounted-target
reset_fixture; rm "$profile_file"; expect_rejected_before_open absent-profile
for old_profile in native native-custom gnome unsupported; do
 reset_fixture; printf '%s\n' "$old_profile" >"$profile_file"; expect_rejected_before_open unsupported-profile
 done
reset_fixture; TEST_PROFILE_STAT='600 0 0'; export TEST_PROFILE_STAT; expect_rejected_before_open profile-mode
reset_fixture; TEST_PROFILE_STAT='644 1000 0'; export TEST_PROFILE_STAT; expect_rejected_before_open profile-owner
reset_fixture; mv "$profile_file" "$profile_file.real"; ln -s "$profile_file.real" "$profile_file"; expect_rejected_before_open profile-symlink
reset_fixture; rm "$probe/native-root/etc/liuqin-native-root"; expect_rejected_before_open marker-missing
reset_fixture; printf 'wrong\n' >"$probe/native-root/etc/liuqin-native-root"; expect_rejected_before_open marker-content
reset_fixture; printf 'LIUQIN_NATIVE_ROOT_CONTRACT_V1\n' >"$native_contract"; expect_rejected_before_open unsupported-contract
reset_fixture; rm "$probe/native-root/usr/lib/modules/$kernel_release/modules.dep"; expect_rejected_before_open modules-missing
reset_fixture; printf 'bad\n' >"$probe/native-root/usr/share/liuqin/kernels/$kernel_release/modules.manifest"; expect_rejected_before_open modules-identity
reset_fixture; rm "$probe/native-root/etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service"; expect_rejected_before_open storage-guard-required
reset_fixture; TEST_GNOME_OWNER='1000 0'; export TEST_GNOME_OWNER; expect_rejected_before_open owner-invalid
reset_fixture; printf 'updated implementation\n' >"$probe/native-root/usr/libexec/iio-sensor-proxy"; native_expect_success mutable-device-service
reset_fixture
for tree in "$probe/native-root" "$newroot"; do
 mv "$tree/usr/local/sbin/"* "$tree/usr/local/bin/"; rmdir "$tree/usr/local/sbin"; ln -s bin "$tree/usr/local/sbin"
done
native_expect_success fedora-sbin-merge
reset_fixture; TEST_FAIL_SETRW=sda35; export TEST_FAIL_SETRW; expect_rejected opening-root-fails
reset_fixture; TEST_FAIL_MOUNT_NUMBER=1; export TEST_FAIL_MOUNT_NUMBER; expect_rejected read-only-mount-fails
reset_fixture; TEST_FAIL_MOUNT_NUMBER=2; export TEST_FAIL_MOUNT_NUMBER; expect_rejected read-write-mount-fails
reset_fixture; TEST_FAIL_MOUNT_NUMBER=3; export TEST_FAIL_MOUNT_NUMBER; expect_rejected bind-mount-fails
reset_fixture; TEST_MUTATE_AFTER_SETRW=add; export TEST_MUTATE_AFTER_SETRW; expect_critical_node_set_changed namespace-addition
reset_fixture; TEST_MUTATE_AFTER_MOUNT_NUMBER=2; TEST_MUTATE_AFTER_MOUNT=remove; export TEST_MUTATE_AFTER_MOUNT_NUMBER TEST_MUTATE_AFTER_MOUNT; expect_critical_node_set_changed namespace-removal
reset_fixture; rm -f "$dev"/sd* "$newroot/dev"/sd* "$state"/sd*; expect_empty_sd_namespace_critical empty-namespace
reset_fixture; TEST_STORAGE_PHASE=switch; export TEST_STORAGE_PHASE
if run_init; then fail 'returned switch_root was accepted'; fi
[ "$(cat "$state/sda")" = 0 ] || fail 'handoff lost parent write permission'
[ "$(cat "$state/sda35")" = 0 ] || fail 'handoff lost root write permission'
grep -q "^switch_root $newroot /usr/lib/systemd/systemd$" "$command_log" || fail 'handoff did not execute systemd'
# A returning exec terminates PID 1 on hardware; it cannot reopen a RAM shell.
# The real handoff and slot parsing remain in the exercised init script.
grep -F -q 'exec "$BB" switch_root "$storage_newroot" "$storage_init"' "$init" || fail 'handoff not exec'
grep -q '^storage_init=/usr/lib/systemd/systemd$' "$init" || fail 'systemd handoff changed'

make_splash_fixture() {
	mkdir -p "$LIUQIN_STORAGE_SPLASH_STATE"
	cat >"$LIUQIN_STORAGE_SPLASH_HELPER" <<'SPLASH'
#!/bin/sh
printf 'splash %s\n' "$*" >>"$TEST_COMMAND_LOG"
SPLASH
	chmod 0755 "$LIUQIN_STORAGE_SPLASH_HELPER"
	TEST_STORAGE_PHASE=switch; export TEST_STORAGE_PHASE
}

reset_fixture; make_splash_fixture
if run_init; then fail 'returned splash switch_root was accepted'; fi
grep -q "mount7 -o move /run $newroot/run" "$command_log" || fail 'splash runtime mount was not carried'
grep -q "splash handoff $newroot" "$command_log" || fail 'admitted splash daemon was not handed over'
grep -q "switch_root $newroot /usr/lib/systemd/systemd" "$command_log" || fail 'splash prevented systemd handoff'

reset_fixture; make_splash_fixture
ln -s /tmp "$newroot/run"
expect_rejected splash-runtime-symlink
! grep -q '^splash handoff' "$command_log" || fail 'invalid root received the splash daemon'

reset_fixture; make_splash_fixture
TEST_FAIL_MOUNT_NUMBER=7; export TEST_FAIL_MOUNT_NUMBER
expect_rejected splash-runtime-move-failure
! grep -q '^splash handoff\|^switch_root' "$command_log" || fail 'failed runtime move reached the handoff'
grep -q -- "-o move $newroot/dev /dev" "$command_log" || fail 'kernel mounts were not restored'

reset_fixture; make_splash_fixture
TEST_MUTATE_AFTER_MOUNT_NUMBER=7; TEST_MUTATE_AFTER_MOUNT=add-newroot
export TEST_MUTATE_AFTER_MOUNT_NUMBER TEST_MUTATE_AFTER_MOUNT
expect_rejected splash-last-snapshot-change
! grep -q '^splash handoff\|^switch_root' "$command_log" || fail 'changed UFS namespace received the splash daemon'
grep -q -- "-o move $newroot/run /run" "$command_log" || fail 'splash runtime was not restored on final rejection'

reset_fixture; native_expect_success final-valid


# The slot marked successful is the slot this boot came from, never a constant:
# the dual layout boots Ubuntu from slot A (slot B only on the bootloader's
# fallback copy), the Linux-only layout from slot B.
slot_check_lines=$(grep -n '/bin/liuqin-mark-slot-successful --check "\$slot_suffix"' "$init" | cut -d: -f1)
slot_check_first=$(printf '%s\n' "$slot_check_lines" | sed -n '1p')
slot_check_second=$(printf '%s\n' "$slot_check_lines" | sed -n '2p')
slot_mark_line=$(grep -n '/bin/liuqin-mark-slot-successful --mark "\$slot_suffix"' "$init" | cut -d: -f1)
slot_b_refuse_line=$(grep -n 'refusing a persistent root handoff without a usable androidboot.slot_suffix' "$init" | cut -d: -f1)
! grep -q 'liuqin-mark-slot-successful --mark-a\|liuqin-mark-slot-successful --check-a' "$init" ||
	fail "init still pins slot A in the slot-success gate"
grep -q '^		if slot_from_cmdline "\$(cat /proc/cmdline)"; then$' "$init" ||
	fail "slot-success gate no longer derives the slot from the cmdline"
switch_line=$(grep -n 'elif ! storage_switch_persistent_root; then' "$init" | cut -d: -f1)
case $slot_check_first:$slot_mark_line:$slot_check_second:$slot_b_refuse_line:$switch_line in
	*[!0-9:]*|*::*|:*) fail "persistent slot-success gate is missing" ;;
esac
[ "$(printf '%s\n' "$slot_check_lines" | wc -l | tr -d ' ')" = 2 ] ||
	fail "persistent slot-success gate does not have exactly two checks"
[ "$slot_check_first" -lt "$slot_mark_line" ] &&
	[ "$slot_mark_line" -lt "$slot_check_second" ] &&
	[ "$slot_check_second" -lt "$switch_line" ] &&
	[ "$slot_b_refuse_line" -lt "$switch_line" ] ||
	fail "persistent root can switch before the slot-success gate"

# The split dual-boot layout puts the root in linux_root; a single-system
# install still hands over userdata.  linux_root has to win when both exist.
grep -q '^	for storage_partname in linux_root userdata; do$' "$init" ||
	fail "root resolution no longer prefers PARTNAME=linux_root over userdata"
grep -q '^	storage_partlabel_link=\$storage_dev_root/disk/by-partlabel/linux_root$' "$init" ||
	fail "root resolution no longer cross-checks by-partlabel"
grep -q 'findfs LABEL=LIUQIN_ROOT' "$init" ||
	fail "root resolution no longer confirms LABEL=LIUQIN_ROOT"

# The split layout also has to clear the read-only flag on linux_home, and it
# has to do so in exactly one window: after the root has proven its own
# identity (a partition named by a root we have not admitted is not authority
# enough to unseal anything) and before the handoff (after switch_root the
# initramfs is gone and the flag is whatever it was left as).
home_resolve_def_line=$(grep -cn '^storage_resolve_home() {$' "$init")
[ "$home_resolve_def_line" = 1 ] ||
	fail "init does not define exactly one storage_resolve_home"
grep -q '^	\[ "\$storage_resolved_partname" = linux_root \] || return 1$' "$init" ||
	fail "home resolution is not restricted to the split linux_root layout"
grep -q '^		sda\[0-9\]\*) ;;$' "$init" ||
	fail "home resolution no longer pins the first UFS LUN"
grep -q 'findfs LABEL=LIUQIN_HOME' "$init" ||
	fail "home resolution no longer cross-checks LABEL=LIUQIN_HOME"
root_label_line=$(grep -n 'findfs LABEL=LIUQIN_ROOT' "$init" | cut -d: -f1)
home_call_line=$(grep -n '^		if storage_resolve_home; then$' "$init" | cut -d: -f1)
identity_gate_line=$(grep -n '^	storage_identity_ok || {$' "$init" | cut -d: -f1)
root_setrw_line=$(grep -n 'blockdev --setrw "\$storage_target"' "$init" | cut -d: -f1)
home_setrw_line=$(grep -n 'blockdev --setrw "\$storage_home"' "$init" | cut -d: -f1)
switch_exec_line=$(grep -n '^	exec "\$BB" switch_root ' "$init" | cut -d: -f1)
case $root_label_line:$home_call_line:$identity_gate_line:$root_setrw_line:$home_setrw_line:$switch_exec_line in
	*[!0-9:]*|*::*|:*|*:) fail "the linux_home read-only clear is not where it must be" ;;
esac
[ "$root_label_line" -lt "$home_call_line" ] ||
	fail "home resolution runs before the root confirms LABEL=LIUQIN_ROOT"
[ "$home_call_line" -lt "$identity_gate_line" ] ||
	fail "home resolution is not part of the root identity check"
[ "$identity_gate_line" -lt "$root_setrw_line" ] ||
	fail "the root identity gate no longer precedes the BLKROSET clears"
[ "$root_setrw_line" -lt "$home_setrw_line" ] ||
	fail "linux_home is unsealed before the root partition is"
[ "$home_setrw_line" -lt "$switch_exec_line" ] ||
	fail "linux_home is unsealed after switch_root, where it can no longer help"

# Whatever rejects the boot must put linux_home back with the rest of the LUN.
sed -n '/^storage_abort() {$/,/^}$/p' "$init" >"$test_root/abort-body"
[ -s "$test_root/abort-body" ] || fail "could not extract storage_abort from init"
grep -q '^	storage_home_name=$' "$test_root/abort-body" ||
	fail "storage_abort no longer drops linux_home from the writable set"
grep -q '^	storage_lock_all || storage_lock_ok=false$' "$test_root/abort-body" ||
	fail "storage_abort no longer re-seals every UFS node"

# --------------------------------------------------------------------------
# Installer-mode /dev/disk/by-partlabel: boot_a/boot_b/persist are required,
# userdata/linux_root/linux_home are optional.  A Linux-only install deletes
# userdata and a fresh device has no linux_root, and either case must still get
# a telnet channel -- that is the channel --restore-partition-table runs from.
# --------------------------------------------------------------------------
installer_root=$test_root/installer
sed -n '/^storage_path_is_block() {$/,/^}$/p' "$init" >"$installer_root.fn1"
sed -n '/^storage_installer_required=/,/^}$/p' "$init" >"$installer_root.fn2"
[ -s "$installer_root.fn1" ] && [ -s "$installer_root.fn2" ] ||
	fail "could not extract the installer by-partlabel helpers from init"
grep -q '^storage_installer_required=.boot_a boot_b persist.$' "$init" ||
	fail "installer required partition set changed"
grep -q '^storage_installer_optional=.userdata linux_root linux_home.$' "$init" ||
	fail "installer optional partition set changed"

# $1 case name, $2 writable node suffix or empty, rest: PARTNAME values
installer_run() {
	installer_name=$1; installer_writable=$2; shift 2
	installer_case=$installer_root/$installer_name
	rm -rf "$installer_case"
	mkdir -p "$installer_case/dev" "$installer_case/sys" "$installer_case/etc"
	: >"$installer_case/etc/liuqin-installer"
	installer_index=10
	for installer_label in "$@"; do
		installer_index=$((installer_index + 1))
		mkdir -p "$installer_case/sys/sda$installer_index"
		printf 'PARTNAME=%s\n' "$installer_label" \
			>"$installer_case/sys/sda$installer_index/uevent"
		: >"$installer_case/dev/sda$installer_index"
		[ "$installer_label" != "$installer_writable" ] ||
			printf '%s\n' "sda$installer_index" >"$installer_case/writable"
	done
	cat >"$installer_case/bb" <<'INSTALLER_BB'
#!/bin/sh
installer_bb_cmd=$1; shift
case $installer_bb_cmd in
grep) exec grep "$@" ;;
blockdev)
	installer_bb_node=${2##*/}
	if [ -r "$INSTALLER_WRITABLE_FILE" ] &&
		[ "$(cat "$INSTALLER_WRITABLE_FILE")" = "$installer_bb_node" ]; then
		printf '0\n'
	else
		printf '1\n'
	fi
	;;
*) exit 2 ;;
esac
INSTALLER_BB
	chmod 0755 "$installer_case/bb"
	{
		printf 'BB=%s\n' "$installer_case/bb"
		printf 'ufs_safe=true\n'
		printf 'storage_dev_root=%s\n' "$installer_case/dev"
		printf 'storage_sys_block=%s\n' "$installer_case/sys"
		printf 'storage_installer_marker=%s\n' "$installer_case/etc/liuqin-installer"
		printf 'log() { printf "log: %%s\\n" "$*"; }\n'
		printf 'telnetd() { printf "telnetd\\n"; }\n'
		cat "$installer_root.fn1"
		cat "$installer_root.fn2"
		printf 'start_stage1_telnet\n'
	} >"$installer_case/run.sh"
	LIUQIN_INIT_STORAGE_TEST_ONLY=1 \
		INSTALLER_WRITABLE_FILE="$installer_case/writable" \
		"$host_busybox" sh "$installer_case/run.sh" >"$installer_case/out" 2>&1
}

installer_expect_ok() {
	installer_expect_name=$1; shift
	installer_run "$installer_expect_name" '' "$@" ||
		fail "installer/$installer_expect_name: refused a usable layout"
	grep -q '^telnetd$' "$installer_root/$installer_expect_name/out" ||
		fail "installer/$installer_expect_name: telnet channel was not opened"
}

installer_expect_refused() {
	installer_expect_name=$1; installer_expect_writable=$2; shift 2
	if installer_run "$installer_expect_name" "$installer_expect_writable" "$@"; then
		fail "installer/$installer_expect_name: accepted a layout it must refuse"
	fi
	! grep -q '^telnetd$' "$installer_root/$installer_expect_name/out" ||
		fail "installer/$installer_expect_name: opened telnet after a refusal"
}

installer_linked() {
	[ -L "$installer_root/$1/dev/disk/by-partlabel/$2" ]
}

# Every optional partition present: all six get a symlink.
installer_expect_ok full boot_a boot_b persist userdata linux_root linux_home
for installer_label in boot_a boot_b persist userdata linux_root linux_home; do
	installer_linked full "$installer_label" ||
		fail "installer/full: $installer_label was not linked"
done
grep -q '^log: installer data partitions linked: userdata linux_root linux_home$' \
	"$installer_root/full/out" || fail "installer/full: optional link log is wrong"

# Bare device, nothing but the required three: still a telnet channel.
installer_expect_ok bare boot_a boot_b persist
for installer_label in boot_a boot_b persist; do
	installer_linked bare "$installer_label" ||
		fail "installer/bare: $installer_label was not linked"
done
for installer_label in userdata linux_root linux_home; do
	! installer_linked bare "$installer_label" ||
		fail "installer/bare: $installer_label was linked but does not exist"
done
grep -q '^log: installer data partitions linked: none$' "$installer_root/bare/out" ||
	fail "installer/bare: optional link log is wrong"

# A Linux-only install (--android-size 0) has no userdata at all.
installer_expect_ok linux-only boot_a boot_b persist linux_root linux_home
! installer_linked linux-only userdata ||
	fail "installer/linux-only: userdata was linked but does not exist"
installer_linked linux-only linux_root ||
	fail "installer/linux-only: linux_root was not linked"
grep -q '^log: installer data partitions linked: linux_root linux_home$' \
	"$installer_root/linux-only/out" || fail "installer/linux-only: optional link log is wrong"

# The linux_root symlink has to agree with the uevent resolution, because
# storage_resolve_target cross-checks exactly that node.
installer_linked linux-only linux_root &&
	[ "$(readlink "$installer_root/linux-only/dev/disk/by-partlabel/linux_root")" \
		= "$installer_root/linux-only/dev/sda14" ] ||
	fail "installer/linux-only: linux_root points at the wrong node"

# Each required partition is fatal when missing; no optional one ever is.
installer_expect_refused no-boot-a '' boot_b persist userdata
installer_expect_refused no-boot-b '' boot_a persist userdata
installer_expect_refused no-persist '' boot_a boot_b userdata
installer_expect_ok no-userdata boot_a boot_b persist linux_root
installer_expect_ok no-linux-root boot_a boot_b persist userdata
installer_expect_ok no-linux-home boot_a boot_b persist userdata linux_root

# Duplicates and writable nodes are refused whether the label is required or not.
installer_expect_refused dup-required '' boot_a boot_a boot_b persist
installer_expect_refused dup-optional '' boot_a boot_b persist linux_root linux_root
installer_expect_refused rw-required persist boot_a boot_b persist
installer_expect_refused rw-optional linux_root boot_a boot_b persist linux_root

# Btrfs must skip tree-log replay during the read-only admission probe.
btrfs_fixture() {
    reset_fixture
    TEST_ROOT_FS=btrfs; export TEST_ROOT_FS
    printf '_BHRfS_M' | dd of="$dev/sda35" bs=1 seek=65600 conv=notrunc 2>/dev/null
    printf '\001\000\000\000\000\000\000\000' | dd of="$dev/sda35" bs=1 seek=65672 conv=notrunc 2>/dev/null
    LIUQIN_STORAGE_FS_SUPPORT=$case_root/filesystems
    printf 'ext4\nbtrfs\n' >"$LIUQIN_STORAGE_FS_SUPPORT"
    export LIUQIN_STORAGE_FS_SUPPORT
    printf 'btrfs\n' >"$probe/native-root/etc/liuqin-root-filesystem"
    printf 'btrfs\n' >"$newroot/etc/liuqin-root-filesystem"
}
btrfs_fixture
native_expect_success btrfs-single-root
grep -q 'mount1 -t btrfs -o ro,rescue=nologreplay,subvolid=5' "$command_log" || fail 'Btrfs RO probe can replay logs or use an unsupported option'
grep -q 'mount2 -t btrfs -o rw,noatime,subvolid=5,compress=zstd:3' "$command_log" || fail 'Btrfs compression is not preserved'
btrfs_fixture
printf '\002' | dd of="$dev/sda35" bs=1 seek=65672 conv=notrunc 2>/dev/null
expect_rejected_before_open btrfs-multiple-devices
btrfs_fixture
rm "$LIUQIN_STORAGE_FS_SUPPORT"
expect_rejected_before_open btrfs-unsupported-boot
btrfs_fixture
printf 'ext4\n' >"$probe/native-root/etc/liuqin-root-filesystem"
expect_rejected_before_open btrfs-format-marker-mismatch

printf '%s\n' 'liuqin persistent-root fail-closed tests: PASS'
