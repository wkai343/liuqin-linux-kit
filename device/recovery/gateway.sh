#!/system/bin/sh
# SPDX-License-Identifier: MIT
set -eu
umask 077
BB=/system/bin/liuqin/busybox
ROOT=/tmp/liuqin-runtime
fail() { echo "liuqin-recovery: $*" >&2; exit 1; }
check_ram() {
    # BusyBox dispatches using argv[0]; the executable must keep its basename.
    "$BB" true || fail 'bundled BusyBox cannot execute'
    status=0
    "$BB" awk '$5=="/tmp" && / - tmpfs / {yes=1} END {exit yes ? 0 : 42}' /proc/self/mountinfo || status=$?
    case $status in
        0) ;;
        42) fail '/tmp is not tmpfs' ;;
        *) fail "cannot inspect /tmp mount: awk exited $status" ;;
    esac
}
[ "$(id -u)" = 0 ] || fail 'root Recovery ADB is required'
[ "$(getprop ro.product.vendor.device)" = liuqin ] || [ "$(getprop ro.product.device)" = liuqin ] || fail 'wrong device'
case ${1:-} in
probe)
    serial=$(getprop ro.boot.serialno); [ -n "$serial" ] || serial=$(getprop ro.serialno)
    slot=$(getprop ro.boot.slot_suffix); slot=${slot#_}
    case $serial in ''|*[!A-Za-z0-9._-]*) fail 'invalid serial' ;; esac
    case $slot in a|b) ;; *) fail 'unknown boot slot' ;; esac
    printf '{"serial":"%s","slot":"%s","boot_id":"%s","build_id":"%s","locked":"%s"}\n' "$serial" "$slot" "$(cat /proc/sys/kernel/random/boot_id)" "$(cat /system/etc/liuqin-build-id)" "$(getprop ro.boot.flash.locked)"
    ;;
check)
    check_ram
    printf '{"ram_ready":true}\n'
    ;;
enter)
    [ "$#" = 2 ] && [ "$2" = "$(cat /system/etc/liuqin-build-id)" ] || fail 'build mismatch'
    [ "$(getprop ro.boot.slot_suffix)" = _a ] || fail 'only slot A is qualified'
    # /tmp is verified as RAM before storing any installation executable.
    check_ram
    mkdir -p "$ROOT"
    if [ ! -f "$ROOT/.ready" ]; then
        printf '%s  %s\n' "$(cat /system/etc/liuqin-runtime.sha256)" /system/etc/liuqin-runtime.tar.xz | "$BB" sha256sum -c - >/dev/null
        "$BB" tar -xJf /system/etc/liuqin-runtime.tar.xz -C "$ROOT"
        mkdir -p "$ROOT/proc" "$ROOT/sys" "$ROOT/dev" "$ROOT/run" "$ROOT/tmp"
        mount --bind /proc "$ROOT/proc"; mount --bind /sys "$ROOT/sys"
        # Recovery identity comes from Android properties and boot ID, never a
        # host-supplied model string. Values are validated before interpolation.
        serial=$(getprop ro.boot.serialno); [ -n "$serial" ] || serial=$(getprop ro.serialno)
        case $serial in ''|*[!A-Za-z0-9._-]*) fail 'invalid serial' ;; esac
        printf '{"device":"liuqin","serial":"%s","slot":"a","build_id":"%s","boot_id":"%s"}\n' "$serial" "$2" "$(cat /proc/sys/kernel/random/boot_id)" >"$ROOT/etc/liuqin-recovery-session.json"
        touch "$ROOT/.ready"
    fi
    # Stop the Recovery service, not init/adbd. No TWRP action can race the
    # installation. Recovery returns through a reboot, never a guessed unlock.
    stop recovery
    remaining=20
    while [ "$(getprop init.svc.recovery)" != stopped ]; do
        [ "$remaining" -gt 0 ] || fail 'Recovery UI has not stopped'
        sleep 1; remaining=$((remaining - 1))
    done
    unset LD_LIBRARY_PATH LD_PRELOAD PYTHONPATH PYTHONHOME
    # Use the private loader explicitly for the one pre-chroot operation that
    # must see Recovery's complete mount namespace.
    PYTHONHOME="$ROOT/usr" "$ROOT/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1" --library-path "$ROOT/lib/aarch64-linux-gnu" "$ROOT/usr/bin/python3" -B "$ROOT/usr/lib/liuqin/recovery-session.py" quiesce
    export PATH=/bin:/usr/bin:/usr/sbin:/sbin
    "$BB" chroot "$ROOT" /usr/bin/python3 -I -B /usr/lib/liuqin/recovery-session.py enter
    ;;
leave)
    [ -f "$ROOT/run/maintenance-ready" ] || fail 'cannot verify GPT names; staying in Recovery'
    serial=$(getprop ro.boot.serialno); [ -n "$serial" ] || serial=$(getprop ro.serialno)
    unset LD_LIBRARY_PATH LD_PRELOAD PYTHONPATH PYTHONHOME
    "$BB" chroot "$ROOT" /usr/bin/python3 -I -B /usr/lib/liuqin/android-device.py check-fastboot-names "$(cat /proc/sys/kernel/random/boot_id)" "$serial" >/dev/null
    # Maintenance has stopped Android's storage/security clients. Use the
    # verified static restart2 helper instead of init's service shutdown path.
    (sleep 1; /system/bin/liuqin/reboot bootloader) >/tmp/liuqin-reboot.log 2>&1 &
    ;;
run)
    [ "$#" = 3 ] && [ -f "$ROOT/run/maintenance-ready" ] || fail 'maintenance session not established'
    [ "$2" = "$(cat /proc/sys/kernel/random/boot_id)" ] || fail 'stale Recovery session'
    case $3 in ''|*[!A-Za-z0-9+/=]*) fail 'invalid request encoding' ;; esac
    unset LD_LIBRARY_PATH LD_PRELOAD PYTHONPATH PYTHONHOME
    export PATH=/bin:/usr/bin:/usr/sbin:/sbin
    "$BB" chroot "$ROOT" /usr/bin/python3 -I -B /usr/lib/liuqin/recovery-session.py nodes
    # Commands originate in the trusted desktop implementation, not ROM scripts.
    printf '%s' "$3" | "$BB" base64 -d | "$BB" chroot "$ROOT" /bin/sh
    ;;
*) fail 'usage: probe | check | enter BUILD_ID | run BOOT_ID BASE64 | leave' ;;
esac
