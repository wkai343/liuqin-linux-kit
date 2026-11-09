#!/bin/sh
# SPDX-License-Identifier: MIT
# Trusted parser: target files are data and are never sourced as shell code.
liuqin_target_parse() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    [ "$("$BB" stat -c %s "$1")" -le 512 ] || return 1
    { IFS= read -r bt_header && IFS= read -r bt_id &&
      IFS= read -r bt_partuuid && IFS= read -r bt_fsuuid &&
      IFS= read -r bt_fstype && IFS= read -r bt_path &&
      IFS= read -r bt_generation && ! IFS= read -r bt_extra; } <"$1" || return 1
    [ "$bt_header" = LIUQIN_BOOT_TARGET_V1 ] || return 1
    case $bt_id in os_id=*) bt_id=${bt_id#os_id=} ;; *) return 1 ;; esac
    case $bt_partuuid in root_partuuid=*) bt_partuuid=${bt_partuuid#root_partuuid=} ;; *) return 1 ;; esac
    case $bt_fsuuid in root_fsuuid=*) bt_fsuuid=${bt_fsuuid#root_fsuuid=} ;; *) return 1 ;; esac
    case $bt_fstype in root_fstype=ext4) bt_fstype=ext4 ;; root_fstype=btrfs) bt_fstype=btrfs ;; *) return 1 ;; esac
    [ "$bt_path" = root_path=native-root ] || return 1
    case $bt_generation in boot_generation=*) bt_generation=${bt_generation#boot_generation=} ;; *) return 1 ;; esac
    for bt_uuid in "$bt_id" "$bt_partuuid" "$bt_fsuuid" "$bt_generation"; do
        printf '%s\n' "$bt_uuid" | "$BB" grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || return 1
    done
    bt_name=lqr_$(printf '%s' "$bt_id" | "$BB" tr -d '-' | "$BB" cut -c1-12)
}

liuqin_target_fs_check() {
    case $bt_fstype in ext4) bt_offset=1128 ;; btrfs) bt_offset=65568 ;; *) return 1 ;; esac
    bt_observed=$("$BB" dd if="$1" bs=1 skip="$bt_offset" count=16 2>/dev/null |
        "$BB" od -An -tx1 | "$BB" tr -d ' \n')
    [ "$bt_observed" = "$(printf '%s' "$bt_fsuuid" | "$BB" tr -d '-')" ]
}

# The filesystem identity is stable across boot-image generations. Versioned
# kernel contracts decide which retained kernels/modules may boot this root.
liuqin_target_matches() (
    bt_expected=$bt_id:$bt_partuuid:$bt_fsuuid:$bt_fstype
    liuqin_target_parse "$1" || exit 1
    [ "$bt_id:$bt_partuuid:$bt_fsuuid:$bt_fstype" = "$bt_expected" ]
)
