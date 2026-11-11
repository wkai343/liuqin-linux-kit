#!/bin/sh
# Host-side execution tests for the GNOME storage guard.
#
# Exercise the real guard against synthetic mounts and blockdev state.
# Only the root and its parent may remain writable; other mounts cannot
# expand that set. The fixture never accesses a real block device.

set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
guard_source=${LIUQIN_GUARD_SCRIPT:-$project_root/device/board-overlay/usr/local/sbin/liuqin-gnome-storage-guard}
guard_unit=$project_root/device/board-overlay/etc/systemd/system/liuqin-gnome-storage-guard.service
real_marker=$project_root/device/board-overlay/etc/liuqin-native-root
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
# Pin the root marker and the service's early boot boundary.
marker_sha=$(sha256sum "$real_marker" | cut -d ' ' -f1)
grep -q "^marker_sha=$marker_sha\$" "$guard_source" ||
	fail 'the guard no longer pins the real marker content'
grep -qx 'After=local-fs.target' "$guard_unit" ||
	fail 'the guard unit is no longer ordered after local-fs.target'
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
# Fixture: root pair plus siblings and another LUN.
# --------------------------------------------------------------------------
case_root=$test_root/case
dev=$case_root/dev
state=$case_root/state
mounts=$case_root/mounts
marker=$case_root/marker
kmsg=$case_root/kmsg
command_log=$case_root/commands
scope=$case_root/scope

reset_fixture() {
	rm -rf "$case_root"
	mkdir -p "$dev" "$state"
	: >"$command_log"
	cat >"$scope" <<'SCOPE'
#!/bin/sh
case $1 in
--list-ufs) printf 'sda\nsda1\nsda35\nsda36\nsda37\nsdb\n' ;;
--verify-flags)
 printf 'verify %s\n' "$*" >>"$TEST_COMMAND_LOG"
 [ "$4" = ufs ] || exit 2
 root=$5
 shift 5
 [ "$*" = 'sda sda1 sda35 sda36 sda37 sdb' ] || exit 1
 for name do
  expected=1
  case $name in sda|"$root") expected=0 ;; esac
  [ "$(cat "$TEST_STATE/$name")" = "$expected" ] || exit 1
 done
 ;;
*) exit 2 ;;
esac
SCOPE
	chmod 0755 "$scope"
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

run_guard() {
	status=0
	env PATH="$bin:/usr/bin:/bin" \
		LIUQIN_GNOME_GUARD_TEST_BUSYBOX="$bin/bb" \
		LIUQIN_GNOME_GUARD_TEST_MARKER="$marker" \
		LIUQIN_GNOME_GUARD_TEST_DEV_ROOT="$dev" \
		LIUQIN_GNOME_GUARD_TEST_MOUNTS="$mounts" \
		LIUQIN_GNOME_GUARD_TEST_KMSG="$kmsg" \
		LIUQIN_GNOME_GUARD_TEST_SCOPE_HELPER="$scope" \
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

assert_node() { # NODE EXPECTED-FLAG NAME
	[ "$(cat "$state/$1")" = "$2" ] ||
		fail "$3: $1 has read-only flag $(cat "$state/$1"), expected $2"
}

assert_never_sealed() { # NODE NAME
	if grep -q "bb blockdev --setro $1\$" "$command_log"; then
		fail "$2: $1 was sealed"
	fi
}

# --- Single-root layout ---------------------------------------------------
reset_fixture
run_guard
expect_pass single-valid '6 nodes; root pair rw, all UFS siblings ro'
assert_node sda 0 single-valid
assert_node sda35 0 single-valid
assert_node sda1 1 single-valid
assert_node sda36 1 single-valid
assert_node sda37 1 single-valid
assert_node sdb 1 single-valid
grep -q "bb blockdev --setro $dev/sda1\$" "$command_log" ||
	fail 'single-valid: the seal loop did no work'
grep -q '^verify --verify-flags ' "$command_log" || fail 'batch verification was skipped'
[ "$(grep -c 'bb blockdev --getro ' "$command_log")" = 2 ] || fail 'only the immediate root-pair reads should remain per-node'

# A rejected batch cannot fall through to the previous weaker per-node path.
reset_fixture
sed -i '/root=\$5/a\ exit 1' "$scope"
run_guard
expect_fail batch-rejected 'UFS read-only state or device identity changed'

# A separately mounted home cannot grant write access to another partition.
reset_fixture
printf '%s /home ext4 rw,noatime 0 0\n' "$dev/sda37" >>"$mounts"
printf '0\n' >"$state/sda37"
run_guard
expect_pass extra-home '6 nodes; root pair rw, all UFS siblings ro'
assert_node sda37 1 extra-home
assert_never_sealed "$dev/sda35" extra-home

# External disks keep both writable and intentionally read-only flags.
for flag in 0 1; do
 reset_fixture
 for node in sdg sdg1; do : >"$dev/$node"; printf '%s\n' "$flag" >"$state/$node"; done
 run_guard
 expect_pass external-preserved '6 nodes; root pair rw, all UFS siblings ro'
 assert_node sdg "$flag" external-preserved
 assert_node sdg1 "$flag" external-preserved
 if grep -q 'bb blockdev .*sdg' "$command_log"; then fail 'external disk was touched'; fi
done

reset_fixture
printf '#!/bin/sh\nexit 1\n' >"$scope"
run_guard
expect_fail scope-missing 'internal UFS scope is unproven'

# --- Root anomalies still fail closed ----------------------------------------
reset_fixture
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
expect_pass btrfs-single-root '6 nodes; root pair rw, all UFS siblings ro'
reset_fixture
sed -i 's| / ext4 | / btrfs |' "$mounts"
rm -f "${marker%/*}/liuqin-root-filesystem"
run_guard
expect_fail btrfs-marker-missing 'Btrfs root lacks its installation marker'

printf '%s\n' 'liuqin GNOME storage guard execution tests: PASS'
