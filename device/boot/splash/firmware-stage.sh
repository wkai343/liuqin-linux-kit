#!/bin/busybox sh
# Preserve firmware scanout until the Fedora renderer has drawn a frame.
BB=/bin/busybox
state=/run/liuqin-boot-splash
client=/usr/bin/plymouth
module_sysfs=/sys/module
fw_pid=
log() { echo "liuqin-splash: $*" >/dev/kmsg 2>/dev/null || :; }
fw_reap() {
    [ -n "${fw_pid:-}" ] || return 0
    "$BB" timeout 3 "$client" quit --retain-splash >/dev/null 2>&1 || :
    n=0
    while kill -0 "$fw_pid" 2>/dev/null && [ "$n" -lt 20 ]; do
        "$BB" sleep 0.05; n=$((n+1))
    done
    # fw_pid is our unreaped direct child, so cannot be recycled for another task.
    if kill -0 "$fw_pid" 2>/dev/null; then
        log 'firmware-splash: child did not exit on quit; terminating owned child'
        kill -TERM "$fw_pid" 2>/dev/null || :
        "$BB" sleep 0.1
        kill -KILL "$fw_pid" 2>/dev/null || :
    fi
    wait "$fw_pid" 2>/dev/null || :
    fw_pid=
    log 'firmware-splash: renderer released; native takeover may start'
}
firmware_splash() {
    args=" $(cat /proc/cmdline 2>/dev/null) "
    case "$args" in *' liuqin.firmware_splash=1 '*) ;; *) return 0 ;; esac
    case "$args" in
        *' androidboot.mode=charger '*|*' plymouth.enable=0 '*|*' rd.plymouth=0 '*) return 0 ;;
    esac
    case "$args" in *' rhgb '*|*' splash '*) ;; *) return 0 ;; esac
    [ -s /etc/liuqin-splash-backlight-modules.order ] || return 0
    [ ! -e "$module_sysfs/ktz8866" ] && [ ! -e "$module_sysfs/dispcc_sm8450" ] || return 0
    for card in /dev/dri/card*; do [ ! -e "$card" ] || return 0; done
    [ -x /usr/libexec/liuqin-fw-plymouthd ] && [ -x /usr/libexec/liuqin-fb-check ] || return 0
    "$BB" timeout 1 "$client" --ping >/dev/null 2>&1 && return 0
    mkdir -p "$state" /run/plymouth
    (cd / && "$BB" sha256sum -c /etc/liuqin-fb-check.sha256) >"$state/firmware-check.log" 2>&1 || return 0
    before=$("$BB" timeout 2 /usr/libexec/liuqin-fb-check) || {
        log 'firmware-splash: inherited framebuffer unavailable; use native path'; return 0;
    }
    angle=$("$BB" timeout 1 /usr/libexec/liuqin-logo-orientation 2>"$state/orientation.log")
    detected=$?
    [ "$detected" = 0 ] || angle=90
    case "$angle" in 0|90|180|270) ;; *) angle=90; detected=1 ;; esac
    if [ "$detected" = 0 ]; then
        log "firmware-splash: inherited logo direction=$angle"
    else
        log "firmware-splash: logo direction uncertain; fallback=$angle"
    fi
    # Both renderers read this same RAM-only configuration before painting.
    printf '%s\n' "$angle" >"$state/orientation"
    printf '[Daemon]\nTheme=liuqin-rotate-%s\nShowDelay=0\nDeviceTimeout=5\nDeviceScale=2\n' "$angle" >/etc/plymouth/plymouthd.conf
    log 'firmware-splash: inherited logo validated; starting Fedora renderer before backlight ownership'
    /usr/libexec/liuqin-fw-plymouthd >>"$state/firmware-launch.log" 2>&1 &
    fw_pid=$!
    trap 'fw_reap' 0
    trap 'fw_reap; exit 0' HUP INT TERM
    n=0; ready=0
    while [ "$n" -lt 40 ]; do
        if "$BB" timeout 1 "$client" --ping >/dev/null 2>&1; then ready=1; break; fi
        kill -0 "$fw_pid" 2>/dev/null || break
        "$BB" sleep 0.05; n=$((n+1))
    done
    if [ "$ready" = 1 ] && "$BB" timeout 3 "$client" show-splash; then
        n=0; changed=0
        while [ "$n" -lt 20 ]; do
            after=$("$BB" timeout 2 /usr/libexec/liuqin-fb-check)
            result=$?
            case "$result" in 0) ;; 9) "$BB" sleep 0.05; n=$((n+1)); continue ;; *) break ;; esac
            if [ "${before%% *}" != "${after%% *}" ]; then
                changed=1; break
            fi
            "$BB" sleep 0.05; n=$((n+1))
        done
        if [ "$changed" = 1 ]; then
            log 'firmware-splash: framebuffer changed with KTZ and display modules still unloaded'
            : >"$state/firmware-rendered"
        else
            log 'firmware-splash: no confirmed pixel change; continuing native path'
        fi
    else
        log 'firmware-splash: renderer unavailable; continuing native path'
    fi
    fw_reap
    trap - 0
    trap - HUP INT TERM
}
firmware_splash
exit 0
