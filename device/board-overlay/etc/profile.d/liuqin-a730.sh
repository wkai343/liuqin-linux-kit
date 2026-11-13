# Match environment.d for login shells, including SSH logins.
# Fedora Mesa stays unmodified; see docs/GPU.md for the A730 workaround.
case ",${IR3_SHADER_DEBUG-}," in
    *,noearlypreamble,*) ;;
    *) IR3_SHADER_DEBUG="${IR3_SHADER_DEBUG:+${IR3_SHADER_DEBUG},}noearlypreamble" ;;
esac
MESA_SHADER_CACHE_DIR="${XDG_CACHE_HOME:-${HOME}/.cache}/liuqin-a730-noearly-v1"
export IR3_SHADER_DEBUG MESA_SHADER_CACHE_DIR
