# SPDX-License-Identifier: MIT
# Only set the build environment; entering a shell never builds or downloads inputs.
export LIUQIN_PROJECT_ROOT="$PWD"
export SLOT_SUCCESS_GCC="${CROSS_COMPILE}gcc"
export OBJCOPY="${CROSS_COMPILE}objcopy"
export DEPMOD="$(command -v depmod)"
export HOST_BUSYBOX="$(command -v busybox)"
export CCACHE_DIR="$PWD/out/build/devbox/ccache"
export LC_ALL=C
liuqin_profile=${DEVBOX_PACKAGES_DIR:-"$PWD/.devbox/nix/profile/default"}
export PKG_CONFIG_PATH="$liuqin_profile/lib/pkgconfig:$liuqin_profile/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
unset liuqin_profile
