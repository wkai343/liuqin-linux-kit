#!/bin/busybox sh
# SPDX-License-Identifier: MIT
# Optional graphics-only early boot. Storage admission remains in /init.
BB=/bin/busybox
state=/run/liuqin-boot-splash
client=/usr/bin/plymouth

log() { echo "liuqin-splash: $*" >/dev/kmsg 2>/dev/null || :; }
wait_writable() {
    # Recheck promptly when an asynchronous probe creates the sysfs node,
    # while retaining the existing five-second upper bound.
    "$BB" timeout 5 "$BB" sh -c '
        while [ ! -w "$1" ]; do "$2" sleep 0.05; done
    ' sh "$1" "$BB"
}
stop_udev() {
    [ -f "$state/udev-started" ] || return 0
    "$BB" timeout 5 /usr/bin/udevadm control --exit 2>/dev/null || :
    "$BB" rm -f "$state/udev-started"
}

start() {
    # Charging, RAM installation and an explicit opt-out keep their own paths.
    args=" $(cat /proc/cmdline 2>/dev/null) "
    case "$args" in
        *' androidboot.mode=charger '*|*' plymouth.enable=0 '*|*' rd.plymouth=0 '*) return 0 ;;
    esac
    case "$args" in *' rhgb '*|*' splash '*) ;; *) return 0 ;; esac
    [ ! -e /etc/liuqin-installer ] || return 0
    [ "$(cat /etc/liuqin-storage-mode 2>/dev/null)" = persistent ] || return 0
    [ -x /usr/bin/plymouthd ] && [ -f /etc/liuqin-splash-modules.sha256 ] || return 0
    mkdir -p "$state" /run/plymouth
    log 'starting graphics preparation'
    backlight=/sys/class/backlight/ktz8866-backlight
    if ! wait_writable "$backlight/bl_power" ||
       [ "$(cat "$backlight/max_brightness" 2>/dev/null)" != 2047 ]; then
        log 'backlight not ready; continuing normal boot'; return 0;
    fi
    # Complete an actual backlight update before either display dependency is
    # loaded. This never changes LCD bias supplies or their voltage settings.
    echo 4 >"$backlight/bl_power" || return 0
    log 'backlight blanked before display clock takeover'
    (cd / && "$BB" sha256sum -c /etc/liuqin-splash-modules.sha256) >"$state/modules.log" 2>&1 || {
        log 'early module identity mismatch; continuing normal boot'; return 0;
    }
    while read -r module; do
        case "$module" in ''|*[!a-zA-Z0-9_]*) log 'invalid early module name'; return 0 ;; esac
        [ ! -e "/sys/module/$module" ] || continue
        "$BB" timeout 10 "$BB" insmod "/usr/lib/liuqin-splash/$module.ko" >>"$state/modules.log" 2>&1 || {
            log "could not load $module; continuing normal boot"; return 0;
        }
    done </etc/liuqin-splash-modules.order
    # With the kernel console renderer disabled, nobody has populated the
    # connector's modes yet. Plymouth drops DRM master before querying them,
    # so that query cannot perform the first probe itself. A normal sysfs
    # detect fills the mode list without forcing a mode or enabling the LEDs.
    panel=/sys/class/drm/card0-DSI-1
    if ! wait_writable "$panel/status" || ! "$BB" timeout 5 "$BB" sh -c \
        'printf "detect\n" >"$1/status"' sh "$panel"; then
        log 'panel mode probe unavailable; continuing normal boot'; return 0
    fi
    [ -n "$(cat "$panel/modes" 2>/dev/null)" ] || {
        log 'panel has no usable modes; continuing normal boot'; return 0;
    }
    log 'panel modes detected'
    SYSTEMD_LOG_LEVEL=warning SYSTEMD_LOG_TARGET=kmsg \
        /usr/lib/systemd/systemd-udevd --daemon --resolve-names=never || return 0
    : >"$state/udev-started"
    /usr/bin/udevadm trigger --action=add --subsystem-match=drm \
        --subsystem-match=graphics --subsystem-match=tty --subsystem-match=input || :
    /usr/bin/udevadm settle --timeout=5 || :
    set -- --mode=boot --pid-file=/run/plymouth/pid --tty=/dev/tty1 \
        --ignore-serial-consoles --graphical-boot --no-boot-log --attach-to-session
    case "$args" in *' liuqin.splash_debug=1 '*) set -- "$@" --debug --debug-file="$state/plymouth-debug.log" ;; esac
    "$BB" timeout 10 /usr/bin/plymouthd "$@" || {
        stop_udev; log 'daemon did not start; continuing normal boot'; return 0;
    }
    if "$BB" timeout 10 "$client" show-splash; then
        : >"$state/ready"
        log 'early splash requested'
    else
        "$BB" timeout 5 "$client" quit --retain-splash 2>/dev/null || :
        stop_udev
        log 'splash unavailable; continuing normal boot'
    fi
}

handoff() {
    root=$1
    [ -f "$root$state/ready" ] || return 0
    if [ -x "$root/usr/bin/plymouth" ] && \
       "$BB" timeout 5 "$client" update-root-fs --new-root-dir="$root"; then
        log 'daemon handed to persistent root'
    else
        "$BB" timeout 5 "$client" quit --retain-splash 2>/dev/null || :
        log 'retaining final frame for the persistent root'
    fi
}

case ${1:-} in
    start) start ;;
    stop-udev) stop_udev ;;
    handoff) [ $# = 2 ] && handoff "$2" ;;
esac
# Cosmetic failures never change root admission or its rollback behavior.
exit 0
