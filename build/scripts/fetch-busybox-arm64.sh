#!/bin/sh

set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
download_dir="$project_root/inputs/downloads"
extract_dir="$project_root/inputs/busybox-arm64"
package=busybox-static_1.36.1-6ubuntu3.1_arm64.deb
url="http://ports.ubuntu.com/pool/main/b/busybox/$package"
package_sha256=d96535e0402c011e0ee43449799df2f4504d44b842e4f2b3a6cbc845508eaafc
binary_sha256=52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933

mkdir -p "$download_dir" "$extract_dir"

if [ ! -f "$download_dir/$package" ] || \
	! echo "$package_sha256  $download_dir/$package" | sha256sum --check --status; then
	curl --fail --location "$url" --output "$download_dir/$package.part"
	mv "$download_dir/$package.part" "$download_dir/$package"
fi

echo "$package_sha256  $download_dir/$package" | sha256sum --check
dpkg-deb --extract "$download_dir/$package" "$extract_dir"
echo "$binary_sha256  $extract_dir/usr/bin/busybox" | sha256sum --check

dpkg-deb --field "$download_dir/$package" Package Version Architecture
file "$extract_dir/usr/bin/busybox"
