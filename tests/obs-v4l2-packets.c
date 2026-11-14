// SPDX-License-Identifier: GPL-2.0-or-later
/* Exercise the actual packet adapter with separate/combined hardware headers.
 * No video device or active OBS session is used. */
#include "../build/obs-v4l2/liuqin-v4l2.c"
#include <assert.h>

static void exercise(bool hevc, bool combined)
{
	/* Minimal Annex B NAL payloads suffice: the adapter classifies types,
	 * while the muxer/independent software decoder validate the real syntax
	 * in the device recording test. Include both start-code lengths. */
	const uint8_t h264[] = {0,0,0,1,0x67,0x42, 0,0,1,0x68,0x80};
	const uint8_t h265[] = {0,0,0,1,0x40,1,0x80, 0,0,1,0x42,1,0x80,
				 0,0,0,1,0x44,1,0x80};
	uint8_t picture[] = {0,0,1,hevc ? 0x26 : 0x65,1,0x80};
	const uint8_t *head = hevc ? h265 : h264;
	size_t head_size = hevc ? sizeof(h265) : sizeof(h264);
	struct encoder *e = bzalloc(sizeof(*e));
	e->hevc = hevc;
	e->codec = avcodec_alloc_context3(NULL);
	e->packet = av_packet_alloc();
	assert(e->codec && e->packet);
	assert(av_new_packet(e->packet, head_size + (combined ? sizeof(picture) : 0)) == 0);
	memcpy(e->packet->data, head, head_size);
	if (combined)
		memcpy(e->packet->data + head_size, picture, sizeof(picture));
	e->packet->pts = e->packet->dts = combined ? 0 : AV_NOPTS_VALUE;
	struct encoder_packet out = {0};
	bool received = true;
	assert(export_packet(e, &out, &received));
	assert(received == combined);
	assert(have_headers(e));
	assert(e->headers_size == head_size);
	assert(memcmp(e->headers, head, head_size) == 0);
	if (!combined) {
		assert(e->returned == 0);
		av_packet_unref(e->packet);
		assert(av_new_packet(e->packet, sizeof(picture)) == 0);
		memcpy(e->packet->data, picture, sizeof(picture));
		e->packet->pts = e->packet->dts = 0;
		assert(export_packet(e, &out, &received) && received);
	}
	assert(out.pts == 0 && out.dts == 0 && out.keyframe);
	assert(out.size == sizeof(picture) && !memcmp(out.data, picture, sizeof(picture)));
	assert(e->returned == 1);
	/* A later header-only buffer must not invent a duplicate video frame. */
	av_packet_unref(e->packet);
	assert(av_new_packet(e->packet, head_size) == 0);
	memcpy(e->packet->data, head, head_size);
	assert(export_packet(e, &out, &received) && !received);
	assert(e->returned == 1);
	destroy(e);
}

int main(void)
{
	for (unsigned hevc = 0; hevc < 2; hevc++) {
		exercise(hevc, false);
		exercise(hevc, true);
		/* Exact-sized allocations catch reads beyond short/truncated NALs
		 * when this test is compiled with AddressSanitizer. */
		for (size_t size = 0; size < 8; size++) {
			uint8_t *buf = malloc(size ? size : 1);
			memset(buf, 0, size);
			assert(!has_picture(buf, size, hevc));
			struct encoder e = {.hevc = hevc, .headers = buf, .headers_size = size};
			assert(!have_headers(&e));
			free(buf);
		}
	}
	puts("PASS: H.264/HEVC separate and combined headers, repeated headers, timestamps and short buffers");
	return 0;
}
