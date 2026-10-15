#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
# Build the tablet speaker topology without accessing an audio device.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$(realpath -- "$0")")/../.." && pwd)
src=${AUDIOREACH_TOPOLOGY_DIR:?set AUDIOREACH_TOPOLOGY_DIR to the pinned AudioReach checkout}
expected_commit=2af1f1ebb8d4fd03b5f53891467ddde2e208a8a0
board=$root/device/audio-topology/liuqin-speakers.m4
output=${OUTPUT:?set OUTPUT to an isolated output path}

die() { printf 'build-liuqin-audio-topology: %s\n' "$*" >&2; exit 1; }

[ -f "$board" ] || die "board topology is unavailable: $board"
git -C "$src" rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout: $src"
[ "$(git -C "$src" rev-parse HEAD)" = "$expected_commit" ] ||
	die 'AudioReach source commit mismatch'
[ -z "$(git -C "$src" status --porcelain --untracked-files=no)" ] ||
	die 'AudioReach source is dirty'
command -v m4 >/dev/null 2>&1 || die 'm4 is unavailable'
command -v alsatplg >/dev/null 2>&1 || die 'alsatplg is unavailable'

tmp=$(mktemp -d "${TMPDIR:-/tmp}/liuqin-audio.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

m4 -I "$src" "$board" >"$tmp/control.conf"

# The pinned framework emits one unified Audio-IF sink.  Rewrite only that
# tuple for the device firmware; an upstream layout change must fail closed.
control_mid='                AR_TKN_U32_MODULE_ID "0x0700117C"'
legacy_mid='                AR_TKN_U32_MODULE_ID "0x0700100E"'
[ "$(grep -Fxc "$control_mid" "$tmp/control.conf")" -eq 1 ] ||
	die 'control topology does not contain exactly one Audio-IF sink MID'

awk -v old="$control_mid" -v new="$legacy_mid" '
	$0 == old { print new; changed++; next }
	{ print }
	END { if (changed != 1) exit 42 }
' "$tmp/control.conf" >"$tmp/candidate.conf" ||
	die 'failed to select the legacy TDM sink'
expected_mid=117444622

alsatplg -c "$tmp/candidate.conf" -o "$tmp/candidate.bin"
alsatplg -d "$tmp/candidate.bin" -o "$tmp/candidate.decoded.conf"

for token_value in \
	"formats 'S24_LE'" 'channels_min 4' 'channels_max 4' 'token1 56' \
	'token264 15' 'token265 4' 'token266 32' 'token273 48000'; do
	grep -Fq "$token_value" "$tmp/candidate.decoded.conf" ||
		die "decoded topology lacks $token_value"
done
awk -v expected="$expected_mid" '
	$1 == "token200" && $2 == expected { count++ }
	END { exit count == 1 ? 0 : 1 }
' "$tmp/candidate.decoded.conf" || die 'decoded topology has the wrong endpoint MID'
grep -Fq "'TERTIARY_TDM_RX_0 Audio Mixer, MultiMedia1, stream0.logger1'" \
	"$tmp/candidate.decoded.conf" || die 'route is missing'

# Mic capture path: MultiMedia2 Capture FE, codec-DMA source module
# 0x07001024 (decimal 117444644) on TX_CODEC_DMA_TX_3 (DAI id 120), plus its
# mixer switch and route.  The capture subgraph must not introduce a second
# unified Audio-IF sink; the legacy-rewrite count above already fails closed
# if that ever changes.
capture_mid=117444644
awk -v expected="$capture_mid" '
	$1 == "token200" && $2 == expected { count++ }
	END { exit count == 1 ? 0 : 1 }
' "$tmp/candidate.decoded.conf" ||
	die 'decoded topology lacks exactly one codec-DMA capture MID'
for capture_value in \
	"stream_name 'MultiMedia2 Capture'" \
	"stream_name 'TX_CODEC_DMA_TX_3 Capture'" \
	"'device120.codec_dma_tx1, , TX_CODEC_DMA_TX_3 Capture'" \
	"'MultiMedia2 Mixer, TX_CODEC_DMA_TX_3, device120.logger1'"; do
	grep -Fq "$capture_value" "$tmp/candidate.decoded.conf" ||
		die "decoded topology lacks capture element: $capture_value"
done

mkdir -p "$(dirname -- "$output")"
install -m 0644 "$tmp/candidate.bin" "$output"
printf 'source_commit=%s\n' "$expected_commit"
sha256sum "$board" "$tmp/candidate.conf" "$tmp/candidate.decoded.conf" "$output"
