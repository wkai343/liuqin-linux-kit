#!/bin/sh
# Host-side execution tests for the GNOME storage guard.
#
# The guard is the stage-2 counterpart of the initramfs UFS seal: it runs once
# from the real root and must keep exactly the root mount's parent disk and
# source node read-write -- plus, in the split layout, the /home mount's node
# -- while sealing every other sd* node again.  The first split-layout boot
# re-sealed linux_home because the guard only knew the root mount; six seconds
# later systemd logged
#   mount: /home: WARNING: source write-protected, mounted read-only
# and gnome-initial-setup could not create the user's home.  This suite runs
# the real script under BusyBox ash (fallback: sh) against a synthetic mounts
# table and a state-tracking blockdev stub, for both layouts and every invalid
# /home shape.  An invalid /home is not admitted and is sealed with the
# siblings; it must never take the root down, because /home is nofail by
# design and the initramfs deliberately leaves an unverified linux_home sealed.  The script's device paths stay at their shipped defaults; the
# fixture reaches in only through the LIUQIN_GNOME_GUARD_TEST_* overrides.

set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
guard_source=${LIUQIN_GUARD_SCRIPT:-$project_root/device/board-overlay/usr/local/sbin/liuqin-gnome-storage-guard}
guard_unit=$project_root/device/board-overlay/etc/systemd/system/liuqin-gnome-storage-guard.service
real_marker=$project_root/device/board-overlay/etc/liuqin-gnome-root
for input in "$guard_source" "$guard_unit" "$real_marker"; do
	[ -f "$input" ] ||
		{ printf '%s\n' "test-liuqin-guard: missing: $input" >&2; exit 1; }
done

host_busybox=${HOST_BUSYBOX:-$(command -v busybox 2>/dev/null || true)}
if [ -n "$host_busybox" ] && [ -x "$host_busybox" ]; then
	shell=$host_busybox
	shell_argument=sh
else
	printf '%s\n' 'test-liuqin-guard: BusyBox is unavailable, falling back to sh' >&2
	host_busybox=
	shell=$(command -v sh)
	shell_argument=
fi

test_root=$(mktemp -d)
cleanup() { rm -rf "$test_root"; }
trap cleanup EXIT HUP INT TERM

fail() { printf 'test-liuqin-guard: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Static drift checks.  The guard's root authority is the reviewed marker: its
# hardcoded hash must be the real marker's hash.  The unit half of the split
# /home fix is ordering: the guard must evaluate /proc/mounts only after the
# /home mount is final.  A nofail mount is not ordered before local-fs.target
# (systemd.mount(5)), so home.mount has to be named explicitly; the guard also
# stays ahead of basic.target.
# --------------------------------------------------------------------------
marker_sha=$(sha256sum "$real_marker" | cut -d' ' -f1)
grep -q "^marker_sha=$marker_sha\$" "$guard_source" ||
	fail 'the guard no longer pins the real marker content'
grep -qx 'After=local-fs.target home.mount' "$guard_unit" ||
	fail 'the guard unit is no longer ordered after local-fs.target and home.mount'
grep -qx 'Before=basic.target network-pre.target display-manager.service' "$guard_unit" ||
	fail 'the guard unit lost its early-boot boundary'
grep -qx 'DefaultDependencies=no' "$guard_unit" ||
	fail 'the guard unit lost DefaultDependencies=no'

# --------------------------------------------------------------------------
# Stub commands.  blockdev tracks the read-only flag of each node in a state
# directory; stat answers the marker metadata check from the fixture (the host
# cannot chown to root); mount is the fail() remount.  Everything else defers
# to the host BusyBox, then to the host tool of the same name.
# --------------------------------------------------------------------------
bin=$test_root/bin
mkdir -p "$bin"
cat >"$bin/bb" <<'BB'
#!/bin/sh
set -u
bb_command=$1
shift
printf 'bb %s %s\n' "$bb_command" "$*" >>"$TEST_COMMAND_LOG"
case $bb_command in
blockdev)
	bb_flag=$1
	bb_node=${2##*/}
	case $bb_flag in
	--setro) printf '1\n' >"$TEST_STATE/$bb_node" ;;
	--setrw) printf '0\n' >"$TEST_STATE/$bb_node" ;;
	--getro) cat "$TEST_STATE/$bb_node" ;;
	*) exit 2 ;;
	esac
	exit 0
	;;
stat) printf '%s\n' "$TEST_MARKER_STAT"; exit 0 ;;
mount) exit 0 ;;
esac
if [ -n "${TEST_BUSYBOX:-}" ]; then
	exec "$TEST_BUSYBOX" "$bb_command" "$@"
fi
exec "$bb_command" "$@"
BB
chmod 0755 "$bin/bb"

# --------------------------------------------------------------------------
# Fixture.  The namespace mirrors the persistent-root fixture: sda is the UFS
# parent disk, sda35 the root source, sda37 the split layout's linux_home,
# sda1/sda36 siblings and sdb another LUN.  The initramfs handoff leaves the
# parent disk and the root source read-write (and linux_home too in the split
# layout); everything else starts sealed.  One sibling is left writable in
# each run so the seal loop has visible work to do.
# --------------------------------------------------------------------------
case_root=$test_root/case
dev=$case_root/dev
state=$case_root/state
mounts=$case_root/mounts
marker=$case_root/marker
kmsg=$case_root/kmsg
command_log=$case_root/commands

reset_fixture() {
	rm -rf "$case_root"
	mkdir -p "$dev" "$state"
	: >"$command_log"
	: >"$kmsg"
	for node in sda sda1 sda35 sda36 sda37 sdb; do
		: >"$dev/$node"
		printf '1\n' >"$state/$node"
	done
	printf '0\n' >"$state/sda"
	printf '0\n' >"$state/sda35"
	printf '0\n' >"$state/sda1"
	cp "$real_marker" "$marker"
	TEST_MARKER_STAT='644 0 0'
	{
		printf 'proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n'
		printf 'sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0\n'
		printf '%s / ext4 rw,noatime 0 0\n' "$dev/sda35"
	} >"$mounts"
}

split_fixture() { # the split-layout handoff: linux_home mounted and opened
	reset_fixture
	printf '%s /home %s %s 0 0\n' "$dev/sda37" ext4 rw,noatime >>"$mounts"
	printf '0\n' >"$state/sda37"
}

run_guard() {
	status=0
	env PATH="$bin:/usr/bin:/bin" \
		LIUQIN_GNOME_GUARD_TEST_BUSYBOX="$bin/bb" \
		LIUQIN_GNOME_GUARD_TEST_MARKER="$marker" \
		LIUQIN_GNOME_GUARD_TEST_DEV_ROOT="$dev" \
		LIUQIN_GNOME_GUARD_TEST_MOUNTS="$mounts" \
		LIUQIN_GNOME_GUARD_TEST_KMSG="$kmsg" \
		TEST_STATE="$state" \
		TEST_COMMAND_LOG="$command_log" \
		TEST_MARKER_STAT="$TEST_MARKER_STAT" \
		TEST_BUSYBOX="$host_busybox" \
		"$shell" $shell_argument "$guard_source" >"$case_root/output" 2>&1 || status=$?
}

expect_pass() { # NAME PASS-LINE
	[ "$status" = 0 ] ||
		fail "$1: the guard failed: $(cat "$case_root/output")"
	grep -qxF "liuqin-gnome-guard: PASS ($2)" "$kmsg" ||
		fail "$1: expected the PASS line '$2', got: $(cat "$kmsg")"
}

expect_fail() { # NAME CRITICAL-MESSAGE
	[ "$status" != 0 ] || fail "$1: the guard passed unexpectedly"
	grep -qxF "liuqin-gnome-guard: critical: $2" "$kmsg" ||
		fail "$1: expected critical '$2', got: $(cat "$kmsg")"
	grep -qx 'bb mount -o remount,ro /' "$command_log" ||
		fail "$1: the root filesystem was not remounted read-only"
}

expect_degraded() { # NAME REASON
	[ "$status" = 0 ] ||
		fail "$1: the guard failed instead of degrading: $(cat "$kmsg")"
	grep -qxF "liuqin-gnome-guard: warning: /home not admitted read-write: $2; sealing it with the siblings" "$kmsg" ||
		fail "$1: expected the /home warning '$2', got: $(cat "$kmsg")"
	grep -qxF 'liuqin-gnome-guard: PASS (6 nodes; root pair rw, all siblings ro)' "$kmsg" ||
		fail "$1: expected the legacy PASS line, got: $(cat "$kmsg")"
	if grep -q 'bb mount' "$command_log"; then
		fail "$1: the root filesystem was remounted"
	fi
	assert_node sda 0 "$1"
	assert_node sda35 0 "$1"
	assert_node sda37 1 "$1"
}

assert_node() { # NODE EXPECTED-FLAG NAME
	[ "$(cat "$state/$1")" = "$2" ] ||
		fail "$3: $1 has read-only flag $(cat "$state/$1"), expected $2"
}

assert_never_sealed() { # NODE NAME
	if grep -q "bb blockdev --setro $1\$" "$command_log"; then
		fail "$2: $1 was sealed"
	fi
}

# --- Legacy layout: no /home mount anywhere --------------------------------
# Byte-for-byte the pre-split behaviour: the root pair stays read-write, every
# other node is sealed, and the PASS line is the historical one.
reset_fixture
run_guard
expect_pass legacy-valid '6 nodes; root pair rw, all siblings ro'
assert_node sda 0 legacy-valid
assert_node sda35 0 legacy-valid
assert_node sda1 1 legacy-valid
assert_node sda36 1 legacy-valid
assert_node sda37 1 legacy-valid
assert_node sdb 1 legacy-valid
grep -q "bb blockdev --setro $dev/sda1\$" "$command_log" ||
	fail 'legacy-valid: the seal loop did no work'

# --- Split layout: /home on its own node ------------------------------------
split_fixture
run_guard
expect_pass split-valid '6 nodes; root and home pairs rw, all siblings ro'
assert_node sda 0 split-valid
assert_node sda35 0 split-valid
assert_node sda37 0 split-valid
assert_node sda1 1 split-valid
assert_node sda36 1 split-valid
assert_node sdb 1 split-valid
grep -q "bb blockdev --getro $dev/sda37\$" "$command_log" ||
	fail 'split-valid: linux_home was never verified read-write'
assert_never_sealed "$dev/sda37" split-valid
assert_never_sealed "$dev/sda35" split-valid

# --- A /home that is present but invalid is sealed, never fatal -----------
# Each case starts from the split handoff (linux_home read-write), so the seal
# of sda37 is visible work, and the root pair must stay read-write.
split_fixture
sed -i 's| /home ext4 rw,noatime | /home ext4 ro,noatime |' "$mounts"
run_guard
expect_degraded home-read-only 'it is mounted read-only'

split_fixture
sed -i "s|$dev/sda37 /home|$dev/sdb1 /home|" "$mounts"
run_guard
expect_degraded home-wrong-lun "unexpected source $dev/sdb1"

split_fixture
sed -i 's| /home ext4 | /home xfs |' "$mounts"
run_guard
expect_degraded home-wrong-fstype 'unexpected filesystem xfs'

split_fixture
printf '%s /home ext4 rw,noatime 0 0\n' "$dev/sda36" >>"$mounts"
run_guard
expect_degraded home-duplicated 'the /home mount is duplicated'

split_fixture
sed -i "s|$dev/sda37 /home|$dev/sda35 /home|" "$mounts"
run_guard
expect_degraded home-shares-root-source 'it shares the root source'

# --- Root anomalies still fail closed ----------------------------------------
split_fixture
sed -i "s|$dev/sda35 / ext4 rw,noatime|$dev/sda35 / ext4 ro,noatime|" "$mounts"
run_guard
expect_fail root-read-only 'root is not read-write'

reset_fixture
sed -i "s|$dev/sda35 / |$dev/sdb / |" "$mounts"
run_guard
expect_fail root-wrong-lun 'unexpected root source'

# --- The root authority is still the marker ----------------------------------
reset_fixture
rm "$marker"
run_guard
expect_fail marker-missing 'root marker missing'

reset_fixture
sed -i 's| / ext4 | / btrfs |' "$mounts"
printf 'btrfs\n' >"${marker%/*}/liuqin-root-filesystem"
run_guard
expect_pass btrfs-single-root '6 nodes; root pair rw, all siblings ro'
reset_fixture
sed -i 's| / ext4 | / btrfs |' "$mounts"
rm -f "${marker%/*}/liuqin-root-filesystem"
run_guard
expect_fail btrfs-marker-missing 'Btrfs root lacks its installation marker'

printf '%s\n' 'liuqin GNOME storage guard execution tests: PASS'
