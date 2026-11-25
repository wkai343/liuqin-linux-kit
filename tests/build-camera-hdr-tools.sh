#!/bin/sh
# SPDX-License-Identifier: MIT
# Build the tablet's userspace HDR tools against its libcamera and OpenCV.
set -eu
: "${1:?usage: build-camera-hdr-tools.sh OUTPUT_DIRECTORY}"
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
dest=$1
mkdir -p "$dest/camera-icp"
opencv=$(pkg-config --cflags --libs opencv4)
libcamera=$(pkg-config --cflags --libs libcamera)
# Fedora's libyuv-devel installs libyuv.pc.
yuv=$(pkg-config --cflags --libs libyuv)
cc=${CC:-cc}
cxx=${CXX:-c++}
"$cxx" -std=c++17 -O2 "$here/camera-hdr-capture.cpp" -o "$dest/camera-hdr-capture" $libcamera -lcrypto -pthread
"$cxx" -std=c++17 -O2 "$here/camera-hdr-live.cpp" -o "$dest/camera-hdr-live" $libcamera $opencv $yuv -pthread
"$cxx" -std=c++17 -O2 "$here/camera-icp/hdr-motion.cpp" -o "$dest/hdr-motion" $opencv
"$cc" -std=c11 -O2 -ffp-contract=off "$here/camera-icp/hdr-pixels.c" -o "$dest/hdr-pixels" -lm
obj=$(mktemp "$dest/hdr-pixels.XXXXXX.o")
trap 'rm -f "$obj"' EXIT
"$cc" -std=c11 -O3 -ffp-contract=off -DHDR_VIDEO_LIBRARY -fPIC -c "$here/camera-icp/hdr-pixels.c" -o "$obj"
"$cxx" -std=c++17 -O3 -DHDR_VIDEO_LIBRARY -fPIC -shared "$here/camera-icp/hdr-motion.cpp" "$obj" -o "$dest/libliuqin-hdr.so" $opencv -lm
install -m 755 "$here/camera-hdr-photo.py" "$here/camera-hdr-video.py" "$dest"
install -m 644 "$here/camera-hdr-validate.py" "$dest"
install -m 644 "$here/camera-icp/hdr-bundle.py" "$here/camera-icp/check-hdr-photo.py" "$dest/camera-icp"
