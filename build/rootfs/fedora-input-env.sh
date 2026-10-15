#!/bin/sh
# SPDX-License-Identifier: MIT
# Plasma sources this for the entire desktop session, not just terminal shells.
export XMODIFIERS=@im=fcitx
# QT_IM_MODULES takes priority over an application's own QT_IM_MODULE.
# Plasma Keyboard selects qtvirtualkeyboard internally; a global fallback
# list overrides it and leaves the visible keyboard unable to send input.
unset QT_IM_MODULES
if [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
    export GTK_IM_MODULE=fcitx
    export QT_IM_MODULE=fcitx
    export SDL_IM_MODULE=fcitx
else
    # Native Wayland clients must keep their text-input connection to KWin:
    # Plasma Keyboard for touch, or Fcitx when explicitly selected in Settings.
    unset GTK_IM_MODULE QT_IM_MODULE SDL_IM_MODULE
fi
