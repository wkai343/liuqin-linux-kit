# SPDX-License-Identifier: BSD-3-Clause
#
# Xiaomi Pad 6 Pro: protected speakers, onboard capture and DisplayPort.
# The builder selects the firmware's legacy TDM sink (0x0700100e) after
# expansion with linux-msm/audioreach-topology commit 2af1f1e.

include(`util/util.m4') dnl
include(`audioreach/tokens.m4') dnl
include(`audioreach/audioreach.m4') dnl
include(`audioreach/stream-subgraph.m4') dnl
include(`audioreach/device-subgraph.m4') dnl
include(`util/mixer.m4') dnl
include(`util/route.m4') dnl

STREAM_SG_PCM_ADD(`audioreach/subgraph-stream-vol-playback.m4',
	FRONTEND_DAI_MULTIMEDIA1,
	`S24_LE', 48000, 48000, 4, 4,
	0x00004001, 0x00004001, 0x00006001, `110000')

DEVICE_AUDIO_IF_SG_ADD(`audioreach/subgraph-device-audio-if-playback.m4',
	`Tertiary TDM0', TERTIARY_TDM_RX_0,
	`S24_LE', 48000, 48000, 4, 4,
	LPAIF_INTF_TYPE_AUD, AUD_INTF_IDX_3, 0, DATA_FORMAT_FIXED_POINT,
	0x00004002, 0x00004002, 0x00006020, `TERTIARY_TDM_RX_0',
	AUDIO_IF_SYNC_SRC_INTERNAL, AUDIO_IF_CTRL_DATA_OE_DISABLE,
	0x0f, 4, 32,
	AUDIO_IF_INTF_MODE_TDM, AUDIO_IF_FRAME_SYNC_MODE_LONG_SYNC,
	1, 1,
	AUDIO_IF_TYPE_QAIF, AUDIO_IF_LANE_MASK_0, 48000,
	AUDIO_IF_I_BIT_CLK_EN, AUDIO_IF_INT_CLK_INVERT,
	AUDIO_IF_EXT_CLK_NORMAL)

STREAM_DEVICE_PLAYBACK_MIXER(TERTIARY_TDM_RX_0,
	``TERTIARY_TDM_RX_0'', ``MultiMedia1'')

STREAM_DEVICE_PLAYBACK_ROUTE(TERTIARY_TDM_RX_0,
	``TERTIARY_TDM_RX_0 Audio Mixer'',
	``MultiMedia1, stream0.logger1'')
dnl Onboard mic capture: four analog MEMS mics on WCD938x AMIC1/3/4/5,
dnl SoundWire TX into the TX macro, captured by the ADSP via
dnl TX_CODEC_DMA_TX_3.  The codec-DMA source module (0x07001024) has
dnl calibration records in the vendor ACDB, so unlike the TDM sink no
dnl legacy MID rewrite is needed here.
STREAM_SG_PCM_ADD(`audioreach/subgraph-stream-capture.m4',
	FRONTEND_DAI_MULTIMEDIA2,
	`S16_LE', 48000, 48000, 4, 4,
	0x00004011, 0x00004011, 0x00006011, `110000')

DEVICE_SG_ADD(`audioreach/subgraph-device-codec-dma-capture.m4',
	`TX_CODEC_DMA_TX_3', TX_CODEC_DMA_TX_3,
	`S16_LE', 48000, 48000, 4, 4,
	LPAIF_INTF_TYPE_RXTX, CODEC_INTF_IDX_TX3, 0, DATA_FORMAT_FIXED_POINT,
	0x00004012, 0x00004012, 0x00006031)

STREAM_DEVICE_CAPTURE_MIXER(FRONTEND_DAI_MULTIMEDIA2,
	``TX_CODEC_DMA_TX_3'')

STREAM_DEVICE_CAPTURE_ROUTE(FRONTEND_DAI_MULTIMEDIA2,
	``MultiMedia2 Mixer'',
	``TX_CODEC_DMA_TX_3, device120.logger1'')

dnl DisplayPort has its own stereo FE. Do not reuse the four-channel speaker
dnl PCM: the OEM DISPLAY_PORT-RX backend and DP0 machine driver use S16_LE.
STREAM_SG_PCM_ADD(`audioreach/subgraph-stream-vol-playback.m4',
	FRONTEND_DAI_MULTIMEDIA3,
	`S16_LE', 48000, 48000, 2, 2,
	0x00004021, 0x00004021, 0x00006041, `110000')

DEVICE_SG_ADD(`audioreach/subgraph-device-display-port-playback.m4',
	`DISPLAY_PORT_RX_0', DISPLAY_PORT_RX_0,
	`S16_LE', 48000, 48000, 2, 2,
	0, 0, 0, DATA_FORMAT_FIXED_POINT,
	0x00004022, 0x00004022, 0x00006060, `DISPLAY_PORT_RX_0')

STREAM_DEVICE_PLAYBACK_MIXER(DISPLAY_PORT_RX_0,
	``DISPLAY_PORT_RX_0'', ``MultiMedia3'')

STREAM_DEVICE_PLAYBACK_ROUTE(DISPLAY_PORT_RX_0,
	``DISPLAY_PORT_RX_0 Audio Mixer'',
	``MultiMedia3, stream2.logger1'')
