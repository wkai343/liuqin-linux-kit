#!/bin/sh
# SPDX-License-Identifier: MIT
# Offline image assembly must compile the target store without reloading the
# build host's SELinux policy or relabeling shared host device/sysfs nodes.
set -eu
policy=/usr/share/selinux/packages/liuqin-fingerprint.cil
runtime=/usr/local/lib/liuqin-fpc-oem
if [ -d /run/systemd/system ] && [ "$(cat /etc/liuqin-native-root 2>/dev/null)" = liuqin-native-root-v1 ]; then
    semodule -i "$policy"
    restorecon -RF "$runtime"
    for path in /run/liuqin-fpc-oem-runtime /var/lib/liuqin-fingerprint; do
        [ ! -d "$path" ] || restorecon -RF "$path"
    done
    for path in /dev/fpc1020 /dev/tee0 /dev/teepriv0 /dev/bsg/0:0:0:49476 /sys/module/firmware_class/parameters/path; do
        [ ! -e "$path" ] || restorecon "$path"
    done
    systemctl daemon-reload
else
    semodule -n -i "$policy"
fi
authselect check
authselect enable-feature with-fingerprint
