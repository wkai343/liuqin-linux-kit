#!/bin/sh
# SPDX-License-Identifier: MIT
# Build the exact matching module tree for the full Ubuntu root.  Boot-critical
# liuqin drivers stay built in; ordinary distro consumers no longer become
# silent no-ops merely because a generic arm64 defconfig selected =m.
set -eu

project=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
kernel=${KERNEL_DIR:-$project/../linux-sm8450-liuqin}
out=${KERNEL_OUT:-$project/out/build/kernel}
dest=${OUT_DIR:-$project/out/build/kernel-modules}
cross=${CROSS_COMPILE:-aarch64-linux-gnu-}
jobs=${JOBS:-$(getconf _NPROCESSORS_ONLN)}
objcopy=${OBJCOPY:-${cross}objcopy}
rebuild_modules=${REBUILD_MODULES:-0}
expected_commit=${KERNEL_COMMIT:-}
canonical_prefix=${KBUILD_CANONICAL_PREFIX:-}

die() { echo "build-liuqin-kernel-modules: $*" >&2; exit 1; }
case $dest in "$project"/out/*|/tmp/*) ;; *) die 'OUT_DIR must be below out/ or /tmp' ;; esac
[ -f "$out/.config" ] || die 'kernel output config is unavailable'
[ -f "$out/arch/arm64/boot/Image" ] || die 'matching Image is unavailable'
command -v "$objcopy" >/dev/null || die "objcopy is unavailable: $objcopy"

case $rebuild_modules in 0|1) ;; *) die 'REBUILD_MODULES must be 0 or 1' ;; esac
commit=$(git -C "$kernel" rev-parse HEAD) || die 'kernel source commit is unavailable'
case $commit in *[!0-9a-f]*|'') die 'unsafe kernel commit identity' ;; esac
if [ -n "$expected_commit" ] && [ "$commit" != "$expected_commit" ]; then
	die "kernel source commit differs: $commit != $expected_commit"
fi
[ -f "$out/include/config/kernel.release" ] || die 'generated kernel release is unavailable'
release=$(cat "$out/include/config/kernel.release")
case $release in *[!A-Za-z0-9._+-]*|'') die 'unsafe kernel release' ;; esac

# A packaging pass must not mutate the candidate OUT.  Calling `make modules`
# with a different KBUILD identity or prefix-map changes saved command lines,
# rewrites compile.h and can rebuild most of the tree after Image was hashed.
# Consume the already-complete module graph by default.  An explicit rebuild
# is admitted only with the complete deterministic identity and one canonical
# debug prefix, which makes the state transition intentional and reproducible.
if [ "$rebuild_modules" = 1 ]; then
	[ -n "${KBUILD_BUILD_TIMESTAMP:-}" ] && [ -n "${KBUILD_BUILD_USER:-}" ] &&
		[ -n "${KBUILD_BUILD_HOST:-}" ] && [ -n "${KBUILD_BUILD_VERSION:-}" ] &&
		[ -n "$canonical_prefix" ] ||
		die 'REBUILD_MODULES=1 requires KBUILD_BUILD_{TIMESTAMP,USER,HOST,VERSION} and KBUILD_CANONICAL_PREFIX'
	case $canonical_prefix in /*) ;; *) die 'KBUILD_CANONICAL_PREFIX must be absolute' ;; esac
	debug_map=-fdebug-prefix-map=$out=$canonical_prefix
	make -C "$kernel" O="$out" ARCH=arm64 CROSS_COMPILE="$cross" -j"$jobs" \
		KCFLAGS="$debug_map" KAFLAGS="$debug_map" \
		CFLAGS_.vmlinux.export.o="$debug_map" CFLAGS_.module-common.o="$debug_map" modules
fi
[ -f "$out/modules.order" ] && [ ! -L "$out/modules.order" ] || die 'kernel modules.order is unavailable'
awk '
	$0 == "" || $0 ~ /^\// || $0 ~ /^\.\.\// || $0 ~ /\/\.\.\// ||
	$0 ~ /\/\.\.$/ || $0 ~ /\/\// || $0 !~ /[.](o|ko)$/ || seen[$0]++ { bad=1 }
	END { exit bad }
' "$out/modules.order" || die 'kernel modules.order contains an unsafe or duplicate path'
source_count=$(wc -l <"$out/modules.order" | tr -d ' ')
missing=0
while IFS= read -r object; do
	case $object in *.ko) module=$object ;; *.o) module=${object%.o}.ko ;; *) die "unsafe modules.order entry: $object" ;; esac
	[ -f "$out/$module" ] && [ ! -L "$out/$module" ] || missing=$((missing + 1))
done <"$out/modules.order"
[ "$missing" = 0 ] || die "kernel OUT lacks $missing linked modules; finish the deterministic kernel build first"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM
make -C "$kernel" O="$out" ARCH=arm64 CROSS_COMPILE="$cross" \
	INSTALL_MOD_PATH="$stage/usr" INSTALL_MOD_STRIP=1 modules_install
tree=$stage/usr/lib/modules/$release
[ -d "$tree" ] || die 'modules_install produced no release tree'
rm -f "$tree/build" "$tree/source"
"${DEPMOD:-depmod}" -b "$stage/usr" "$release"
# The pre-strip objects contain their OUT_DIR in debug metadata. GNU ld hashes
# that metadata into .note.gnu.build-id, then modules_install strips the debug
# bytes but preserves the now path-dependent 20-byte note. The kernel loader
# does not consume it; remove it so two otherwise-identical stripped module
# trees are byte reproducible. The artifact manifest remains the identity.
find "$tree" -type f -name '*.ko' -exec \
	"$objcopy" --remove-section=.note.gnu.build-id {} \;
# modules_install inherits the developer's collaborative umask.  Product bytes
# must not alternate between 0755 and 0775 directories across hosts.
find "$stage/usr/lib/modules" -type d -exec chmod 0755 {} +
find "$stage/usr/lib/modules" -type f -exec chmod 0644 {} +
count=$(find "$tree" -type f -name '*.ko' | wc -l | tr -d ' ')
[ "$count" = "$source_count" ] ||
	die "installed module count differs from source order: $count != $source_count"

rm -rf "$dest"
mkdir -p "$dest"
chmod 0755 "$dest"
(cd "$stage" && find usr/lib/modules -type f -print0 | LC_ALL=C sort -z |
	xargs -0 sha256sum) >"$dest/modules.manifest"
tar -C "$stage" --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
	-cf "$dest/modules.tar" usr/lib/modules
printf '%s\n' "$release" >"$dest/kernel.release"
printf '%s\n' "$commit" >"$dest/kernel.commit"
python3 - "$out" "$dest" "$commit" "$release" <<'PY'
import hashlib, json, sys
from pathlib import Path
out, dest = map(Path, sys.argv[1:3])
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
identity = dict(commit=sys.argv[3], release=sys.argv[4],
                image_sha256=sha(out / 'arch/arm64/boot/Image'),
                config_sha256=sha(out / '.config'),
                archive_sha256=sha(dest / 'modules.tar'),
                manifest_sha256=sha(dest / 'modules.manifest'))
(dest / 'kernel.build.json').write_text(json.dumps(identity, indent=2) + '\n')
PY
chmod 0644 "$dest/modules.tar" "$dest/modules.manifest" \
	"$dest/kernel.release" "$dest/kernel.commit" "$dest/kernel.build.json"
sha256sum "$dest/modules.tar" "$dest/modules.manifest" "$dest/kernel.release" "$dest/kernel.commit"
printf 'kernel modules: release=%s count=%s\n' "$release" "$count"
