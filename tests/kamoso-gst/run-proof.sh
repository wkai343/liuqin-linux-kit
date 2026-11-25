#!/bin/bash
# SPDX-License-Identifier: MIT
# Run in the tablet's graphical user session (directly or over user SSH).
set -euo pipefail
: "${1:?usage: run-proof.sh EXTRACTED_OFFICIAL_KAMOSO PLUGIN_DIRECTORY PIPEWIRE_NODE NEW_OUTPUT}"
test "$#" = 4
exe=$(realpath -e "$1")
plugins=$(realpath -e "$2")
node=$3
check=$(realpath -e "$(dirname "$0")/check-shutter.py")
test -x "$exe" && test -r "$plugins/libgstliuqinshutterproof.so"
umask 077
mkdir -m700 "$4"
cd "$4"
mkdir config photos
config="$PWD/config/kamosorc"
kwriteconfig6 --file "$config" --group General --key saveUrl "file://$PWD/photos"
kwriteconfig6 --file "$config" --group WebcamSettings --key deviceObjectId liuqin-shutter-proof
unit="kamoso-shutter-proof-$$"
cleanup() { systemctl --user stop "$unit" 2>/dev/null || :; }
trap cleanup EXIT
systemd-run --user --collect --unit="$unit" -p RuntimeMaxSec=60 \
    -p "StandardError=file:$PWD/plugin.log" -E "XDG_CONFIG_HOME=$PWD/config" \
    -E "GST_PLUGIN_PATH_1_0=$plugins" -E "GST_REGISTRY_1_0=$PWD/registry.bin" \
    -E "LIUQIN_PROOF_NODE=$node" -E LIUQIN_PROOF_DELAY=1 \
    -E QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 "$exe"
for _ in {1..40}; do
    pid=$(systemctl --user show "$unit" -p MainPID --value)
    if [[ $(readlink "/proc/$pid/exe" || :) == "$exe" ]]; then break; fi
    sleep .05
done
cmp "/proc/$pid/exe" "$exe"
sha256sum "/proc/$pid/exe" > executable.sha256
timeout -k 1 42 python3 "$check" "$pid" "$PWD/photos" "$PWD/plugin.log" | tee results.json
