#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Prepare the liuqin firmware tree from pinned stock and upstream firmware,
# source-built AudioReach topology and build/scripts/build-liuqin-wlan.py output.
#
# Per-device objects are deliberately absent: the CS35L41 cirrus/*-calr.bin
# calibration, the Bluetooth public address, the WLAN MAC and the persist
# sensor registry are provisioned per unit at install/runtime and must never
# enter a generic tree.
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
pool=${FIRMWARE_POOL:-$project_root/inputs/firmware-liuqin}
vpu_blob=${VPU_BLOB:-$project_root/inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/extracted/super-work/vendor-extract/firmware/vpu20_4v.mbn}
vpu_sha256=3567fd4522323b132ae4dd0f94a34782b2bc6a8c5c5fb51edfdbe364450fc118
topology_bin=${TOPOLOGY_BIN:?set TOPOLOGY_BIN to the output of build/scripts/build-liuqin-audio-topology.sh}
topology_sha256=9c9f8bfcffde1f52c143d58cba821c5f6b3899553370ab92a88b90d81fef9ee9
tuple_dir=${HSP2_TUPLE_DIR:?set HSP2_TUPLE_DIR to the output of build/scripts/build-liuqin-wlan.py}
out_dir=${OUT_DIR:?set OUT_DIR to a fresh output directory}
oracle_manifest=${ORACLE_MANIFEST:-}

die() { printf 'build-liuqin-firmware-prep: %s\n' "$*" >&2; exit 1; }

[ -d "$pool" ] || die "firmware pool is unavailable: $pool"
case $out_dir in
"$project_root"/out/* | /tmp/*) ;;
*) die "refusing an output directory outside out/ or /tmp: $out_dir" ;;
esac
case $out_dir in
*/..* | */.) die "refusing a relative traversal in the output directory: $out_dir" ;;
esac
[ ! -e "$out_dir" ] || die "output directory already exists (single writer, fresh dir): $out_dir"

# --- Verify every pinned input before staging anything -----------------------

# Touchscreen (NT36532, both panel-vendor variants; vendor_a.img).
touch_dir=$pool/touch-nt36532/vendor/firmware
touch_sha256="\
2582cf81d6eeb69b57cb8b18b2afe683e9cd7e20ed0be390e14ed75302b0c584  novatek_nt36532_m81_fw_csot.bin
3d9737da5e0fc3ea64e340c495067d4d6dcbd5ca2a2351fe955ec88fab35da1e  novatek_nt36532_m81_fw_tm.bin"
(cd "$touch_dir" && printf '%s\n' "$touch_sha256" | sha256sum -c --quiet -) ||
	die "touchscreen firmware does not match the pinned hashes: $touch_dir"

# Bluetooth (QCA6490; whole constructible btqca name set, from BTFM.bin).
bt_dir=$pool/bt-qca6490/vendor/bt_firmware/image
[ -d "$bt_dir" ] || die "Bluetooth firmware directory is unavailable: $bt_dir"
bt_pattern='^(hpbtfw2[01]\.tlv|hpnv2[01]g?\.(bin|b[0-9a-f]+))$'
bt_names=$(cd "$bt_dir" && ls -1 | LC_ALL=C sort | grep -E "$bt_pattern")
bt_seen=$(cd "$bt_dir" && printf '%s\n' "$bt_names" | xargs sha256sum | sha256sum | cut -d' ' -f1)
[ "$bt_seen" = d31712321a1c15591148a2e6a7ce94a12445558d2de82b48e5815abc581203a0 ] ||
	die "Bluetooth firmware set digest mismatch: $bt_seen"

# WLAN fallback board data (bd_m81.elf) and upstream WCN6855 runtime.
wlan_board=$pool/wlan-qca6490/vendor/firmware_mnt/image/qca6490/bd_m81.elf
printf '%s  %s\n' aa8ae92d781e09db8cffa960b00c9606e4bb65d9c7a89f9b32df1bd24aee8573 "$wlan_board" |
	sha256sum -c --quiet - >/dev/null || die 'WLAN board data hash mismatch'
[ "$(dd if="$wlan_board" bs=4 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')" = 7f454c46 ] ||
	die 'WLAN board data is not an ELF'
wlan_dir=$pool/wlan-qca6490/upstream/WCN6855/hw2.0
wlan_sha256="\
d94af8648a0347903b68809b4100ea816e76afb6ead967c8b353c6521500f285  amss.bin.zst
ba583c3550d15871dbbbe1349dab64bac5badced5b6bf122aa7263f8d7df76d4  board-2.bin.zst
7769d18f26c025008221702e6884c9225f375db46d3e584e20223e33728376ec  m3.bin.zst
c4b298869269bc55ca73d71beeb756b72a9b19e710cea2af0401accbafa2d3c7  regdb.bin.zst"
(cd "$wlan_dir" && printf '%s\n' "$wlan_sha256" | sha256sum -c --quiet -) ||
	die 'upstream WCN6855 firmware hash mismatch'

# Regulatory database (upstream, signed).
reg_dir=$pool/regulatory/upstream
reg_sha256="\
92eec693a0a9ec460be6bb3ff9e22b259b2f906eddd2984dd90654b5ecfa0f18  regulatory.db
4242d15defb0608b917a2c3cd0dbea21c026a4acc905093e8947dd494c2e26b1  regulatory.db.p7s"
(cd "$reg_dir" && printf '%s\n' "$reg_sha256" | sha256sum -c --quiet -) ||
	die 'regulatory database hash mismatch'

# Adreno 730 (vendor_a.img): zap stays device-keyed under qcom/sm8475/liuqin;
# sqe/gmu go to the loader's updates/ override tree (no dpkg path collision).
gpu_dir=$pool/gpu-adreno730/vendor
gpu_sha256="\
e67d1829f57fc8326d806234b932dc3b78e28a9940df34c70752f17d5f38b7aa  a730_sqe.fw
36296b019753fbb2a4733450a2d0a94e307238227297a747d0ff2811364f5d5f  a730_zap.mbn
b2d3f0e2a98fd6259ab9ffef16df63265542e6cc9a970d53d476e823d432042a  gmu_gen70000.bin"
(cd "$gpu_dir" && printf '%s\n' "$gpu_sha256" | sha256sum -c --quiet -) ||
	die 'Adreno 730 firmware hash mismatch'

# SM8475 DSP (NON-HLOS.bin, .mdt headers renamed to the .mbn names the DTS uses).
dsp_dir=$pool/dsp-sm8475/vendor/qcom/sm8475/liuqin
[ "$(find "$dsp_dir" -maxdepth 1 -type f | wc -l)" = 68 ] || die 'DSP firmware count differs from 68'
dsp_seen=$(cd "$dsp_dir" && find . -maxdepth 1 -type f -printf '%P\n' | LC_ALL=C sort |
	xargs sha256sum | sha256sum | cut -d' ' -f1)
[ "$dsp_seen" = 7f9b43d3815b6592f541e0b870357010488980bc040b925e93bbf12d01282908 ] ||
	die "DSP firmware set digest mismatch: $dsp_seen"

# VPU (Iris decode, vendor_a.img).
printf '%s  %s\n' "$vpu_sha256" "$vpu_blob" | sha256sum -c --quiet - >/dev/null ||
	die "VPU firmware hash mismatch: $vpu_blob"

# AudioReach topology, built from public source by the pinned builder.
[ -f "$topology_bin" ] && [ ! -L "$topology_bin" ] || die "topology is unavailable: $topology_bin"
[ "$(sha256sum "$topology_bin" | cut -d' ' -f1)" = "$topology_sha256" ] ||
	die 'AudioReach topology identity mismatch'

# HSP2 tuple (vendor amss20/m3/regdb + bdencoder-generated board-2), both
# hardware-revision request paths. Verify the consumed payloads directly;
# archive directory modes are not firmware identity.
[ -d "$tuple_dir/hw2.0" ] && [ -d "$tuple_dir/hw2.1" ] || die "HSP2 tuple is unavailable: $tuple_dir"
for item in \
	"amss.bin:cc3e477fa698a28bdb8c8115a071893f9b2f5230de190ad525e74fd69bbb6092" \
	"m3.bin:6938b4bba268a02659ee5e16992971aa0e2fab103a4f60cbccf65e4bd8ac9836" \
	"board-2.bin:15811f0b799fc26a881bce02e282834cd41b77c2812443ffd931012552a7c1f9" \
	"regdb.bin:06810e85c94c8f412c4e72cb38546cac90db7fdbadd45aae00f977f862d38a7b"; do
	name=${item%%:*}
	hash=${item#*:}
	[ "$(sha256sum "$tuple_dir/hw2.0/$name" | cut -d' ' -f1)" = "$hash" ] ||
		die "HSP2 tuple hash mismatch: $name"
done

# --- Stage the tree ----------------------------------------------------------

fw=$out_dir/usr/lib/firmware
mkdir -p "$fw/novatek/liuqin" "$fw/qca" "$fw/ath11k/WCN6855/hw2.0" "$fw/ath11k/WCN6855/hw2.1" \
	"$fw/qcom/sm8475/liuqin" "$fw/qcom/sm8450" \
	"$fw/updates/qcom/vpu" "$fw/updates/ath11k/WCN6855/hw2.0" "$fw/updates/ath11k/WCN6855/hw2.1"

printf '%s\n' "$touch_sha256" | while read -r _ name; do
	install -m 0644 "$touch_dir/$name" "$fw/novatek/liuqin/$name"
done
printf '%s\n' "$bt_names" | while read -r name; do
	install -m 0644 "$bt_dir/$name" "$fw/qca/$name"
done
for rev in hw2.0 hw2.1; do
	install -m 0644 "$wlan_board" "$fw/ath11k/WCN6855/$rev/board.bin"
done
printf '%s\n' "$wlan_sha256" | while read -r _ name; do
	install -m 0644 "$wlan_dir/$name" "$fw/ath11k/WCN6855/hw2.0/$name"
	ln -s "../hw2.0/$name" "$fw/ath11k/WCN6855/hw2.1/$name"
done
printf '%s\n' "$reg_sha256" | while read -r _ name; do
	install -m 0644 "$reg_dir/$name" "$fw/$name"
done
install -m 0644 "$gpu_dir/a730_zap.mbn" "$fw/qcom/sm8475/liuqin/a730_zap.mbn"
install -m 0644 "$gpu_dir/a730_sqe.fw" "$gpu_dir/gmu_gen70000.bin" "$fw/updates/qcom/"
install -m 0644 "$dsp_dir"/* "$fw/qcom/sm8475/liuqin/"
install -m 0644 "$vpu_blob" "$fw/updates/qcom/vpu/vpu20_4v.mbn"
install -m 0644 "$topology_bin" "$fw/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin"
# The layer ships raw copies in both revision paths (the kernel resolves
# firmware request paths; no userspace symlink repair exists that early).
for rev in hw2.0 hw2.1; do
	for name in amss.bin m3.bin board-2.bin regdb.bin; do
		install -m 0644 "$tuple_dir/hw2.0/$name" "$fw/updates/ath11k/WCN6855/$rev/$name"
	done
done

# --- Manifest and closure gates ----------------------------------------------

manifest=$out_dir/firmware.manifest
(cd "$out_dir" && find usr/lib/firmware -type f -printf '%P\n' | LC_ALL=C sort |
	while IFS= read -r rel; do
		printf '%s  /usr/lib/firmware/%s\n' "$(sha256sum "usr/lib/firmware/$rel" | cut -d' ' -f1)" "$rel"
	done) >"$manifest"
chmod 0644 "$manifest"
file_count=$(wc -l <"$manifest" | tr -d ' ')
[ "$file_count" = 197 ] || die "firmware tree holds $file_count files, expected exactly 197"
[ "$(find "$out_dir/usr/lib/firmware" -type l | wc -l | tr -d ' ')" = 4 ] ||
	die 'firmware tree must hold exactly the four hw2.1 zst symlinks'
grep -q '/cirrus/' "$manifest" &&
	die 'per-device calibration must never enter the generic firmware tree'

# Optional oracle check: a frozen release-layer manifest is a comparison
# oracle only.  Its per-device cirrus entries are excluded; the remaining
# firmware lines must form the same hash+path set as this tree.
if [ -n "$oracle_manifest" ]; then
	[ -f "$oracle_manifest" ] || die "oracle manifest is unavailable: $oracle_manifest"
	grep '  /usr/lib/firmware/' "$oracle_manifest" | grep -v '/cirrus/' |
		LC_ALL=C sort >"$out_dir/.oracle.expected"
	LC_ALL=C sort "$manifest" >"$out_dir/.oracle.actual"
	if ! cmp -s "$out_dir/.oracle.expected" "$out_dir/.oracle.actual"; then
		diff -u "$out_dir/.oracle.expected" "$out_dir/.oracle.actual" >&2 || true
		die 'firmware tree differs from the oracle manifest'
	fi
	rm -f "$out_dir/.oracle.expected" "$out_dir/.oracle.actual"
	printf 'oracle check: %s matches byte-for-byte (%s lines)\n' "$oracle_manifest" "$file_count"
fi

printf 'firmware tree: %s files, %s symlinks\n' "$file_count" 4
printf 'tree:    %s\n' "$out_dir/usr/lib/firmware"
printf 'manifest: %s\n' "$manifest"
sha256sum "$manifest"
