#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Download the pinned upstream KernelSU release assets into a local cache and
# verify every one of them against build/data/kernelsu-assets.json.  The assets
# are an upstream input: they are never redistributed from this repository, and
# an asset whose sha256 differs from the pinned value is deleted, not used.
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
manifest=$project_root/build/data/kernelsu-assets.json

die() { printf 'fetch-kernelsu-assets: %s\n' "$*" >&2; exit 1; }

[ -f "$manifest" ] || die "missing manifest: $manifest"
command -v python3 >/dev/null || die 'python3 is required'
command -v curl >/dev/null || die 'curl is required'
command -v sha256sum >/dev/null || die 'sha256sum is required'

version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$manifest")
out_dir=${KSU_ASSET_DIR:-"$project_root/inputs/downloads/kernelsu/$version"}
mkdir -p "$out_dir"

python3 -c 'import json,sys
table = json.load(open(sys.argv[1]))["assets"]
for role, entry in sorted(table.items()):
    print(role, entry["name"], entry["bytes"], entry["sha256"], entry["url"])' "$manifest" |
while read -r role name bytes digest url; do
	target=$out_dir/$name
	if [ ! -f "$target" ]; then
		printf 'fetch-kernelsu-assets: downloading %s\n' "$name"
		curl -fsSL --retry 3 -o "$target.part" "$url" || die "download failed: $url"
		mv -- "$target.part" "$target"
	fi
	size=$(wc -c <"$target" | tr -d ' ')
	[ "$size" = "$bytes" ] || { rm -f -- "$target"; die "$name is $size bytes, expected $bytes"; }
	actual=$(sha256sum "$target" | cut -d' ' -f1)
	[ "$actual" = "$digest" ] || { rm -f -- "$target"; die "$name has sha256 $actual, expected $digest"; }
	printf '  %s  %s  (%s)\n' "$digest" "$name" "$role"
done

printf 'fetch-kernelsu-assets: %s verified in %s\n' "$version" "$out_dir"
