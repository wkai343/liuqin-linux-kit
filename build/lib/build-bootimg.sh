#!/bin/sh

set -eu

# Running the pinned AOSP scripts must not modify their verified source cache.
export PYTHONDONTWRITEBYTECODE=1

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
kernel_out=${KERNEL_OUT:-"$project_root/out/build/kernel-next-liuqin-next"}
image=${IMAGE:-"$kernel_out/arch/arm64/boot/Image"}
dtb=${DTB:-"$kernel_out/arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dtb"}
abl_overlay=${ABL_OVERLAY:-"$project_root/device/liuqin-abl-boot-overlay.dts"}
ramdisk=${RAMDISK:-"$project_root/out/build/initramfs-liuqin/liuqin-firstboot.cpio.gz"}
mkbootimg_dir=${MKBOOTIMG_DIR:-"$project_root/inputs/aosp-mkbootimg"}
out_dir=${OUT_DIR:-"$project_root/out/boot-liuqin"}
dtc=${DTC:-"$kernel_out/scripts/dtc/dtc"}
fdtoverlay=${FDTOVERLAY:-"$kernel_out/scripts/dtc/fdtoverlay"}
image_gz="$out_dir/Image.gz"
overlay_dtb="$out_dir/liuqin-abl-boot-overlay.dtbo"
boot_dtb="$out_dir/sm8475-xiaomi-liuqin-abl.dtb"
boot_dts="$out_dir/sm8475-xiaomi-liuqin-abl.dts"
bootimg="$out_dir/boot-liuqin-firstboot.img"
info="$out_dir/boot-liuqin-firstboot.info"
partition_size=$((0x0c000000))
# The screen is the only first-boot channel on a retail unit. console=tty0 alone
# is not enough: fbcon only binds at device_initcall, after cmd-db, rpmh-rsc,
# clk-rpmh, rpmhpd, smem and gcc have already probed, so a failure among those
# leaves a blank panel and no evidence. earlycon=simplefb draws onto the same
# bootloader framebuffer from parse_early_param(), before any of them.
#
# Three parts are load-bearing; each silently loses that window on its own. The
# late DRM log console must be named explicitly: registering it is not enough,
# and its first enabled write is what commits the initial atomic modeset.
# console=tty0 follows it so the VT remains /dev/console for userspace. See
# the matching comment in the board DTS, which is where the kernel actually
# reads this from: ABL concatenates its own ~800 characters onto whatever
# /chosen/bootargs already holds, in place and with a single terminating NUL, so
# ours comes first and ABL's follows in the same string. Most images keep the
# header equal to the DT bootargs. A product may deliberately leave the header
# empty so ABL contributes the DT bootargs exactly once; both fields remain
# independently audited.
#
# Three flags are gone on purpose and the DTS says why at length: keep_bootcon
# kept simplefb0 writing into the bootloader framebuffer all session, which a
# desktop compositor cannot draw over, and clk_ignore_unused/pd_ignore_unused
# kept every unclaimed clock and power domain alive, which is why the tablet
# discharges while plugged in. Keep this string equal to the DTS one; the build
# fails below if they drift.
cmdline=${CMDLINE:-"earlycon=simplefb console=drm_log console=tty0 initcall_blacklist=simplefb_driver_init rootwait"}
header_cmdline=${HEADER_CMDLINE-$cmdline}

for input in "$image" "$dtb" "$abl_overlay" "$ramdisk" "$dtc" "$fdtoverlay" \
	"$mkbootimg_dir/mkbootimg.py" "$mkbootimg_dir/unpack_bootimg.py"; do
	if [ ! -r "$input" ]; then
		echo "error: required input is unavailable: $input" >&2
		exit 1
	fi
done

mkdir -p "$out_dir"
gzip -n -9 -c "$image" >"$image_gz"

"$dtc" -@ -I dts -O dtb -o "$overlay_dtb" "$abl_overlay"
"$fdtoverlay" -i "$dtb" -o "$boot_dtb" "$overlay_dtb"

# Experiment D proved by construction that the exact ABL rejects our DTB but
# accepts the stock vendor_boot dtb-02 in the very same boot image, and the
# public reference code accepts both, so the rejection is Xiaomi-private and
# cannot be located offline. ABL_DTB02_IDS=1 sidesteps it: every root identity
# property the DT-selection path reads is mirrored byte-for-byte from the
# accepted DTB, so private checks see exactly what they demonstrably accepted.
if [ "${ABL_DTB02_IDS:-0}" = 1 ]; then
	stock_soc_dtb=${STOCK_SOC_DTB:-"$project_root/inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/analysis/vendor_boot/dtbs/dtb-02.dtb"}
	stock_soc_dtb_sha256=527344cd52497d0b8b9311f1ab82ac3418b86b95c25e7786890f38f40054e467
	if ! printf '%s  %s\n' "$stock_soc_dtb_sha256" "$stock_soc_dtb" | sha256sum -c --quiet -; then
		echo "error: stock dtb-02.dtb does not match the pinned hash" >&2
		exit 1
	fi
	python3 "$project_root/build/lib/abl-dtb-ids.py" \
		--boot-dtb "$boot_dtb" \
		--stock-dtb "$stock_soc_dtb" \
		--output "$boot_dtb.stockids" \
		--dtc "$dtc"
	mv -f "$boot_dtb.stockids" "$boot_dtb"
fi

# The bootloader always applies a stock liuqin DTBO overlay to whatever DTB
# this image carries, and aborts the boot outright when that apply fails. It
# fails on a mainline DTB for want of a __symbols__ node. Setting
# ABL_OVERLAY_SINK=1 adds that node plus an inert sink so the overlay applies
# and directs the overlay writes into that node.
#
# Which of the 44 DTBO entries ABL selects is not ours to decide, and
# ufdt_apply_multi_overlay may apply more than one, so the symbols of every
# entry are exported -- 237 distinct across the table, against 43..126 per
# entry. Building from one entry only would work if and only if ABL happened to
# pick that entry.
#
# The DTBO table is not the whole overlay set, and assuming it was cost a failed
# boot. Differencing the tree ABL really handed to a kernel here against
# dtb-02 + the stock DTBO leaves /hypervisor, /soc/timer always-on and an
# updated /firmware/qcom_scm -- a runtime Gunyah RM DTBO that lives in no
# partition we can enumerate. Its fixups resolve against the base's __symbols__
# like any other overlay, and a base exporting only the DTBO union aborts on the
# first label it names. So seed the union from every stock base DTB as well:
# any overlay ABL applies was authored against one of those.
if [ "${ABL_OVERLAY_SINK:-1}" = 1 ]; then
	stock_overlay_dir=${STOCK_OVERLAY_DIR:-"$project_root/inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/analysis/dtbo"}
	stock_base_dir=${STOCK_BASE_DIR:-"$project_root/inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/analysis/vendor_boot/dtbs"}
	stock_overlay_count=$(find "$stock_overlay_dir" -maxdepth 1 -type f \
		-regex '.*/entry\.[0-9]+' 2>/dev/null | wc -l)
	if [ "$stock_overlay_count" -ne 44 ]; then
		echo "error: expected 44 stock liuqin DTBO entries in $stock_overlay_dir, found $stock_overlay_count" >&2
		exit 1
	fi
	stock_base_count=$(find "$stock_base_dir" -maxdepth 1 -type f \
		-regex '.*/dtb-[0-9][0-9]\.dtb' 2>/dev/null | wc -l)
	if [ "$stock_base_count" -ne 14 ]; then
		echo "error: expected 14 stock base DTBs in $stock_base_dir, found $stock_base_count" >&2
		exit 1
	fi
	set -- --boot-dtb "$boot_dtb" --overlay-dir "$stock_overlay_dir"
	for base in "$stock_base_dir"/dtb-[0-9][0-9].dtb; do
		set -- "$@" --symbols-from-dtb "$base"
	done
	# No --rename-node here on purpose. ABL does look up /soc/cache-controller,
	# which its prefix match would never find under the upstream name
	# system-cache-controller, but that lookup is unreachable: the SCT-support
	# predicate it hangs off is a two-instruction constant-false stub in the
	# exact binary, and the tree ABL really produced on this device carries the
	# node without qcom,sct-config. Renaming would be invisible to Linux but is
	# dead weight in a first-boot image.
	python3 "$project_root/build/lib/abl-symbols.py" "$@" \
		--output "$boot_dtb.symbols" \
		--dtc "$dtc" \
		--expect-symbols 1781 \
		--sink-phandle
	mv -f "$boot_dtb.symbols" "$boot_dtb"
fi

"$dtc" -I dtb -O dts -o "$boot_dts" "$boot_dtb" 2>/dev/null

# The board-selection metadata is load-bearing either way: losing it silently
# must be a build failure, not a surprise on the device. In stock-ids mode the
# boot DTB must carry exactly what the accepted dtb-02 carries -- including the
# *absence* of xiaomi,miboard-id and chassis-type -- and in the default mode it
# must carry our declared liuqin identity.
if [ "${ABL_DTB02_IDS:-0}" = 1 ]; then
	if ! grep -q 'qcom,msm-id = <0x213 0x10000>;' "$boot_dts" || \
		! grep -q 'qcom,board-id = <0x00 0x00>;' "$boot_dts" || \
		! grep -q 'compatible = "qcom,capep";' "$boot_dts" || \
		grep -q 'xiaomi,miboard-id' "$boot_dts" || \
		grep -q 'chassis-type' "$boot_dts"; then
		echo "error: the boot DTB does not mirror the accepted dtb-02 identity" >&2
		exit 1
	fi
else
	if ! grep -q 'qcom,msm-id = <0x213 0x10000 0x21c 0x10000 0x212 0x10000>;' "$boot_dts" || \
		! grep -q 'qcom,board-id = <0x10008 0x00>;' "$boot_dts" || \
		! grep -q 'xiaomi,miboard-id = <0x10 0x00>;' "$boot_dts"; then
		echo "error: ABL board-selection metadata is missing from the boot DTB" >&2
		exit 1
	fi
fi

# The kernel reads /chosen/bootargs, not the boot image header. ABL concatenates
# its own ~800 characters onto that property in place -- verified against the
# tree it really produced on this unit, which is one 1201-character string with a
# single NUL whose first 399 characters are the base DTB's own bootargs verbatim.
# So ours is what the kernel parses first, and ABL's follows in the same string.
# Require the final DT to equal the requested DT bootargs; the independently
# audited header may be the deliberate empty product subset.
if ! grep -qF "bootargs = \"$cmdline\";" "$boot_dts"; then
	echo "error: /chosen/bootargs in the boot DTB does not match requested DT bootargs" >&2
	echo "  requested DT: $cmdline" >&2
	echo "  header      : $header_cmdline" >&2
	echo "  dtb   : $(grep -m1 'bootargs = ' "$boot_dts" | sed 's/^\s*//')" >&2
	exit 1
fi

# earlycon=simplefb resolves all of this out of the flat tree, and every piece is
# silently load-bearing: without /chosen's cell counts and ranges the address
# translation returns OF_BAD_ADDR, and the geometry decides where it draws. Three
# separate passes rewrite this DTB after dtc emits it (fdtoverlay, the stock-id
# mirror, the symbol/sink injector), and ABL then merges overlays we cannot
# enumerate, so assert the result rather than assuming it survived.
for required in \
	'compatible = "simple-framebuffer";' \
	'reg = <0x00 0xb8000000 0x00 0x2b00000>;' \
	'width = <0x708>;' \
	'height = <0xb40>;' \
	'stride = <0x1c20>;' \
	'format = "a8r8g8b8";'; do
	if ! grep -qF "$required" "$boot_dts"; then
		echo "error: the boot DTB lost the framebuffer property: $required" >&2
		exit 1
	fi
done
if ! awk '/^\tchosen \{/,/^\t\};/' "$boot_dts" | grep -qF '#address-cells = <0x02>;' || \
	! awk '/^\tchosen \{/,/^\t\};/' "$boot_dts" | grep -qF '#size-cells = <0x02>;' || \
	! awk '/^\tchosen \{/,/^\t\};/' "$boot_dts" | grep -qF 'ranges;'; then
	echo "error: /chosen lost the cell counts or ranges the earlycon needs" >&2
	exit 1
fi

image_gz_size=$(stat -c %s "$image_gz")
boot_dtb_size=$(stat -c %s "$boot_dtb")
ramdisk_size=$(stat -c %s "$ramdisk")

python3 -B "$mkbootimg_dir/mkbootimg.py" \
	--header_version 2 \
	--pagesize 4096 \
	--base 0 \
	--kernel_offset 0x00008000 \
	--ramdisk_offset 0x01000000 \
	--tags_offset 0x00000100 \
	--dtb_offset 0x01f00000 \
	--kernel "$image_gz" \
	--ramdisk "$ramdisk" \
	--dtb "$boot_dtb" \
	--cmdline "$header_cmdline" \
	--output "$bootimg"

bootimg_size=$(stat -c %s "$bootimg")
if [ "$bootimg_size" -gt "$partition_size" ]; then
	echo "error: boot image exceeds the 192 MiB boot partition" >&2
	exit 1
fi

unpack_dir=$(mktemp -d "$out_dir/.unpack.XXXXXX")
cleanup() {
	rm -rf "$unpack_dir"
}
trap cleanup EXIT HUP INT TERM

python3 -B "$mkbootimg_dir/unpack_bootimg.py" \
	--boot_img "$bootimg" --out "$unpack_dir" --format info >"$info"

cmp "$image_gz" "$unpack_dir/kernel"
gzip -dc "$unpack_dir/kernel" >"$unpack_dir/Image"
cmp "$image" "$unpack_dir/Image"
cmp "$boot_dtb" "$unpack_dir/dtb"
cmp "$ramdisk" "$unpack_dir/ramdisk"

if ! grep -q '^boot image header version: 2$' "$info"; then
	echo "error: unpacked image is not boot header v2" >&2
	exit 1
fi
if ! grep -Fqx "command line args: $header_cmdline" "$info" || \
	! grep -qx 'additional command line args: ' "$info"; then
	echo "error: unpacked boot header cmdline does not match requested header field" >&2
	exit 1
fi
if ! grep -q "^kernel_size: $image_gz_size\$" "$info" || \
	! grep -q "^ramdisk size: $ramdisk_size\$" "$info" || \
	! grep -q "^dtb size: $boot_dtb_size\$" "$info" || \
	! grep -q '^dtb address: 0x0000000001f00000$' "$info"; then
	echo "error: unpacked boot image layout does not match the liuqin v2 contract" >&2
	exit 1
fi

{
	printf 'ABL_OVERLAY_SINK: %s\n' "${ABL_OVERLAY_SINK:-1}"
	printf 'ABL_DTB02_IDS: %s\n' "${ABL_DTB02_IDS:-0}"
	printf 'DT_BOOTARGS: %s\n' "$cmdline"
	printf 'HEADER_CMDLINE: %s\n' "$header_cmdline"
} >>"$info"

sha256sum "$image_gz" "$boot_dtb" "$ramdisk" "$bootimg"
printf 'boot image size: %s / %s bytes\n' "$bootimg_size" "$partition_size"
printf 'boot image info: %s\n' "$info"
