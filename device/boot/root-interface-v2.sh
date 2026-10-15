#!/bin/sh
# SPDX-License-Identifier: MIT
# Sourced by the trusted initramfs/installer. Never source target-root files.
liuqin_contract_v2_parse() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    [ "$("$BB" stat -c %s "$1")" -le 512 ] || return 1
    { IFS= read -r liuqin_header && IFS= read -r liuqin_release &&
      IFS= read -r liuqin_image && IFS= read -r liuqin_modules &&
      ! IFS= read -r liuqin_extra; } <"$1" || return 1
    [ "$liuqin_header" = LIUQIN_ROOT_INTERFACE_V2 ] || return 1
    case $liuqin_release in kernel_release=*) liuqin_release=${liuqin_release#kernel_release=} ;; *) return 1 ;; esac
    case $liuqin_image in image_sha256=*) liuqin_image=${liuqin_image#image_sha256=} ;; *) return 1 ;; esac
    case $liuqin_modules in modules_sha256=*) liuqin_modules=${liuqin_modules#modules_sha256=} ;; *) return 1 ;; esac
    printf '%s\n' "$liuqin_release" | "$BB" grep -Eq '^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$' || return 1
    printf '%s\n%s\n' "$liuqin_image" "$liuqin_modules" | "$BB" grep -Ev '^[0-9a-f]{64}$' | "$BB" grep -q . && return 1
    [ "${#liuqin_image}" = 64 ] && [ "${#liuqin_modules}" = 64 ]
}

liuqin_v2_regular() {
    [ -f "$1/$2" ] && [ ! -L "$1/$2" ] || return 1
    [ "$("$BB" readlink -f "$1/$2")" = "$("$BB" readlink -f "$1")/$2" ] || return 1
    [ "$("$BB" stat -c '%a %u %g' "$1/$2")" = "$3 0 0" ]
}

liuqin_root_v2_check() {
    liuqin_contract_v2_parse "$2" || return 1
    liuqin_v2_regular "$1" etc/liuqin-root-interface 644 || return 1
    [ "$("$BB" cat "$1/etc/liuqin-root-interface")" = liuqin-root-v2 ] || return 1
    liuqin_identity=usr/share/liuqin/kernels/$liuqin_release
    liuqin_v2_regular "$1" "$liuqin_identity/boot.contract" 644 || return 1
    liuqin_v2_regular "$1" "$liuqin_identity/modules.manifest" 644 || return 1
    [ "$("$BB" sha256sum "$1/$liuqin_identity/boot.contract" | "$BB" cut -d' ' -f1)" = "$("$BB" sha256sum "$2" | "$BB" cut -d' ' -f1)" ] || return 1
    [ "$("$BB" sha256sum "$1/$liuqin_identity/modules.manifest" | "$BB" cut -d' ' -f1)" = "$liuqin_modules" ] || return 1
    liuqin_v2_regular "$1" "usr/lib/modules/$liuqin_release/modules.dep" 644 || return 1
    # Fedora merges local sbin into bin. Admit that one relative directory
    # alias while retaining canonical-path, ownership and mode checks on the
    # real executable; other directory or executable aliases remain invalid.
    if [ -L "$1/usr/local/sbin" ]; then
        [ "$("$BB" readlink "$1/usr/local/sbin")" = bin ] || return 1
        liuqin_v2_regular "$1" usr/local/bin/liuqin-gnome-storage-guard 755 || return 1
    else
        liuqin_v2_regular "$1" usr/local/sbin/liuqin-gnome-storage-guard 755 || return 1
    fi
    liuqin_v2_regular "$1" etc/systemd/system/liuqin-gnome-storage-guard.service 644 || return 1
    [ "$("$BB" readlink "$1/etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service")" = ../liuqin-gnome-storage-guard.service ] || return 1
    if [ "${3:-0}" = 1 ]; then
        "$BB" awk -v prefix="usr/lib/modules/$liuqin_release/" '
          { h=substr($0,1,64); p=substr($0,67)
            if(length(h)!=64 || h ~ /[^0-9a-f]/ || substr($0,65,2)!="  " || index(p,prefix)!=1 ||
               p ~ /[[:space:]]/ || p ~ /(^|\/)\.\.(\/|$)/ || seen[p]++) exit 1 }
          END { if(NR==0) exit 1 }
        ' "$1/$liuqin_identity/modules.manifest" || return 1
        (cd "$1" && "$BB" sha256sum -c "$liuqin_identity/modules.manifest" >/dev/null) || return 1
    fi
}
