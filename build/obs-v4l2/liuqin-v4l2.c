// SPDX-License-Identifier: GPL-2.0-or-later
/* OBS encoder adapter for the stateful FFmpeg V4L2 encoders.
 * Use with the Iris stride/packet lifetime fixes documented in ../ffmpeg/.
 */
#include <obs-module.h>
#include <obs-avc.h>
#include <obs-hevc.h>
#include <libavcodec/avcodec.h>
#include <libavutil/error.h>
#include <inttypes.h>
#include <limits.h>
#include <string.h>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("liuqin-v4l2", "en-US")
MODULE_EXPORT const char *obs_module_description(void)
{
	return "H.264 and HEVC stateful V4L2 hardware encoders";
}

struct encoder {
	obs_encoder_t *obs;
	AVCodecContext *codec;
	AVFrame *frame;
	AVPacket *packet;
	uint8_t *headers, *sei, *first_packet;
	size_t headers_size, sei_size, first_packet_size;
	bool hevc, opened;
	uint64_t submitted, returned;
};

static bool fail(struct encoder *e, const char *operation, int code)
{
	char message[256], detail[AV_ERROR_MAX_STRING_SIZE];
	av_strerror(code, detail, sizeof(detail));
	snprintf(message, sizeof(message), "V4L2: %s: %s", operation, detail);
	obs_encoder_set_last_error(e->obs, message);
	blog(LOG_ERROR, "[liuqin-v4l2] %s", message);
	return false;
}

static void destroy(void *data)
{
	struct encoder *e = data;
	if (!e)
		return;
	uint64_t drained = 0;
	/* Standard OBS outputs continue encoding until their stop timestamp is
	 * reached. By destroy(), their callbacks are gone: drain only releases
	 * the later pipeline frames, never try to write to a stopped output. */
	if (e->opened && e->submitted && e->packet) {
		int ret;
		do {
			ret = avcodec_send_frame(e->codec, NULL);
			if (ret == AVERROR(EAGAIN)) {
				av_packet_unref(e->packet);
				if (avcodec_receive_packet(e->codec, e->packet) < 0)
					break;
				drained++;
			}
		} while (ret == AVERROR(EAGAIN));
		if (ret >= 0) {
			for (;;) {
				av_packet_unref(e->packet);
				ret = avcodec_receive_packet(e->codec, e->packet);
				if (ret < 0)
					break;
				drained++;
			}
			if (ret != AVERROR_EOF)
				blog(LOG_WARNING, "[liuqin-v4l2] drain ended with %d", ret);
		}
	}
	blog(LOG_INFO, "[liuqin-v4l2] %s submitted=%" PRIu64 " returned=%" PRIu64
		      " drained_after_stop=%" PRIu64,
	     e->hevc ? "hevc" : "h264", e->submitted, e->returned, drained);
	av_packet_free(&e->packet);
	av_frame_free(&e->frame);
	avcodec_free_context(&e->codec);
	bfree(e->headers);
	bfree(e->sei);
	bfree(e->first_packet);
	bfree(e);
}

static void video_info(void *data, struct video_scale_info *info)
{
	(void)data;
	/* Deliberately limited to 8-bit SDR. OBS performs the conversion. */
	info->format = VIDEO_FORMAT_NV12;
}

static void *create(obs_data_t *settings, obs_encoder_t *obs, bool hevc)
{
	struct encoder *e = bzalloc(sizeof(*e));
	e->obs = obs;
	e->hevc = hevc;
	const char *name = hevc ? "hevc_v4l2m2m" : "h264_v4l2m2m";
	const AVCodec *codec = avcodec_find_encoder_by_name(name);
	const struct video_output_info *video = video_output_get_info(obs_encoder_video(obs));
	int64_t bitrate = obs_data_get_int(settings, "bitrate");
	int64_t keyint = obs_data_get_int(settings, "keyint_sec");
	const char *rc = obs_data_get_string(settings, "rate_control");
	uint32_t width = obs_encoder_get_width(obs), height = obs_encoder_get_height(obs);
	if (!codec || !video || !video->fps_num || !video->fps_den ||
	    video->fps_num > INT_MAX || video->fps_den > INT_MAX ||
	    (uint64_t)video->fps_num > (uint64_t)video->fps_den * 240 ||
	    !width || !height || width > 4096 || height > 4096 || (width & 1) || (height & 1) ||
	    bitrate < 100 || bitrate > 100000 || keyint < 0 || keyint > 10) {
		fail(e, "unsupported encoder, dimensions, frame rate or bitrate", AVERROR(EINVAL));
		goto error;
	}
	if (*rc && strcmp(rc, "VBR")) {
		fail(e, "only driver-default VBR is supported", AVERROR(ENOSYS));
		goto error;
	}
	if (video->colorspace == VIDEO_CS_2100_PQ || video->colorspace == VIDEO_CS_2100_HLG) {
		fail(e, "select SDR Rec.709 video for this 8-bit encoder", AVERROR(ENOSYS));
		goto error;
	}
	e->codec = avcodec_alloc_context3(codec);
	e->frame = av_frame_alloc();
	e->packet = av_packet_alloc();
	if (!e->codec || !e->frame || !e->packet) {
		fail(e, "allocate encoder", AVERROR(ENOMEM));
		goto error;
	}
	AVCodecContext *c = e->codec;
	c->width = (int)width;
	c->height = (int)height;
	/* OBS PTS uses 1/fps_num units. obs_encoder_video() already includes
	 * the frame-rate divisor in its fps_den; do not apply it twice. */
	c->time_base = (AVRational){1, (int)video->fps_num};
	c->framerate = (AVRational){(int)video->fps_num, (int)video->fps_den};
	c->pix_fmt = AV_PIX_FMT_NV12;
	c->bit_rate = bitrate * 1000;
	c->max_b_frames = 0;
	c->gop_size = (int)((keyint ? keyint : 2) * c->framerate.num / c->framerate.den);
	if (c->gop_size < 1)
		c->gop_size = 1;
	c->profile = hevc ? AV_PROFILE_HEVC_MAIN : AV_PROFILE_H264_HIGH;
	c->color_range = video->range == VIDEO_RANGE_FULL ? AVCOL_RANGE_JPEG : AVCOL_RANGE_MPEG;
	c->color_primaries = AVCOL_PRI_BT709;
	c->color_trc = AVCOL_TRC_BT709;
	c->colorspace = AVCOL_SPC_BT709;
	if (video->colorspace == VIDEO_CS_601) {
		c->color_primaries = AVCOL_PRI_SMPTE170M;
		c->color_trc = AVCOL_TRC_SMPTE170M;
		c->colorspace = AVCOL_SPC_SMPTE170M;
	} else if (video->colorspace == VIDEO_CS_SRGB) {
		c->color_trc = AVCOL_TRC_IEC61966_2_1;
	}
	c->chroma_sample_location = AVCHROMA_LOC_LEFT;
	/* No GLOBAL_HEADER: get the hardware's parameter sets from its first
	 * access unit, before OBS's standard muxer requests get_extra_data(). */
	int ret = avcodec_open2(c, codec, NULL);
	if (ret < 0) {
		fail(e, "open hardware encoder", ret);
		goto error;
	}
	e->opened = true;
	e->frame->format = c->pix_fmt;
	e->frame->width = c->width;
	e->frame->height = c->height;
	e->frame->color_range = c->color_range;
	e->frame->colorspace = c->colorspace;
	e->frame->color_primaries = c->color_primaries;
	e->frame->color_trc = c->color_trc;
	e->frame->chroma_location = c->chroma_sample_location;
	ret = av_frame_get_buffer(e->frame, 32);
	if (ret < 0) {
		fail(e, "allocate NV12 frame", ret);
		goto error;
	}
	blog(LOG_INFO, "[liuqin-v4l2] %s %ux%u %u/%u fps, %" PRId64 " Kbps, GOP %d",
	     name, width, height, (unsigned)c->framerate.num, (unsigned)c->framerate.den, bitrate, c->gop_size);
	return e;
error:
	destroy(e);
	return NULL;
}

static void *create_h264(obs_data_t *settings, obs_encoder_t *obs)
{
	return create(settings, obs, false);
}

static void *create_hevc(obs_data_t *settings, obs_encoder_t *obs)
{
	return create(settings, obs, true);
}

static bool have_headers(const struct encoder *e)
{
	unsigned mask = 0;
	for (size_t i = 0; i + 4 < e->headers_size; i++) {
		if (e->headers[i] || e->headers[i + 1] || e->headers[i + 2] != 1)
			continue;
		unsigned type = e->hevc ? (e->headers[i + 3] >> 1) & 63 : e->headers[i + 3] & 31;
		if (e->hevc && type >= 32 && type <= 34)
			mask |= 1u << (type - 32);
		else if (!e->hevc && (type == 7 || type == 8))
			mask |= 1u << (type - 7);
	}
	return mask == (e->hevc ? 7u : 3u);
}

static bool has_picture(const uint8_t *data, size_t size, bool hevc)
{
	for (size_t i = 0; i + 4 < size; i++) {
		if (data[i] || data[i + 1] || data[i + 2] != 1)
			continue;
		unsigned type = hevc ? (data[i + 3] >> 1) & 63 : data[i + 3] & 31;
		if (hevc ? type <= 31 : (type >= 1 && type <= 5))
			return true;
	}
	return false;
}

static bool export_packet(struct encoder *e, struct encoder_packet *packet, bool *received)
{
	*received = false;
	AVPacket *p = e->packet;
	if (!p->size)
		return fail(e, "empty packet", AVERROR_INVALIDDATA);
	bfree(e->first_packet);
	e->first_packet = NULL;
	if (!e->headers_size) {
		if (e->hevc)
			obs_extract_hevc_headers(p->data, p->size, &e->first_packet, &e->first_packet_size,
					 &e->headers, &e->headers_size, &e->sei, &e->sei_size);
		else
			obs_extract_avc_headers(p->data, p->size, &e->first_packet, &e->first_packet_size,
					&e->headers, &e->headers_size, &e->sei, &e->sei_size);
		blog(LOG_DEBUG, "[liuqin-v4l2] first packet=%d headers=%zu payload=%zu sei=%zu extradata=%d",
		     p->size, e->headers_size, e->first_packet_size, e->sei_size, e->codec->extradata_size);
		if (!have_headers(e))
			return fail(e, "first access unit lacks complete parameter sets", AVERROR_INVALIDDATA);
	}
	/* HEADER_MODE_SEPARATE returns a parameter-set-only packet before the
	 * first picture. It is extradata, not a video frame (nor a timestamp). */
	if (!has_picture(p->data, p->size, e->hevc))
		return true;
	if (p->pts == AV_NOPTS_VALUE || p->dts == AV_NOPTS_VALUE)
		return fail(e, "missing picture timestamp", AVERROR_INVALIDDATA);
	packet->data = e->first_packet ? e->first_packet : p->data;
	packet->size = e->first_packet ? e->first_packet_size : (size_t)p->size;
	packet->pts = p->pts;
	packet->dts = p->dts;
	packet->type = OBS_ENCODER_VIDEO;
	packet->keyframe = e->hevc ? obs_hevc_keyframe(p->data, p->size) : obs_avc_keyframe(p->data, p->size);
	e->returned++;
	blog(LOG_DEBUG, "[liuqin-v4l2] packet pts=%" PRId64 " dts=%" PRId64 " key=%d",
	     p->pts, p->dts, packet->keyframe);
	*received = true;
	return true;
}

static bool encode(void *data, struct encoder_frame *frame, struct encoder_packet *packet, bool *received)
{
	struct encoder *e = data;
	*received = false;
	if (!frame)
		return fail(e, "unexpected null input frame", AVERROR(EINVAL));
	int ret = av_frame_make_writable(e->frame);
	if (ret < 0)
		return fail(e, "make frame writable", ret);
	for (unsigned plane = 0; plane < 2; plane++) {
		if (!frame->data[plane] || frame->linesize[plane] < (unsigned)e->codec->width)
			return fail(e, "invalid NV12 input stride", AVERROR(EINVAL));
		int rows = e->codec->height >> plane;
		for (int y = 0; y < rows; y++) {
			uint8_t *dst = e->frame->data[plane] + (size_t)y * e->frame->linesize[plane];
			memcpy(dst, frame->data[plane] + (size_t)y * frame->linesize[plane], e->codec->width);
			memset(dst + e->codec->width, 0, e->frame->linesize[plane] - e->codec->width);
		}
	}
	e->frame->pts = frame->pts;
	/* Request the keyframe boundary explicitly instead of relying solely
	 * on firmware GOP scheduling. */
	e->frame->pict_type = e->submitted % e->codec->gop_size == 0 ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
	av_packet_unref(e->packet);
	ret = avcodec_send_frame(e->codec, e->frame);
	bool got = false;
	if (ret == AVERROR(EAGAIN)) {
		ret = avcodec_receive_packet(e->codec, e->packet);
		if (ret < 0)
			return fail(e, "receive pending packet", ret);
		got = true;
		/* Retry this same frame: EAGAIN does not consume it. */
		ret = avcodec_send_frame(e->codec, e->frame);
	}
	if (ret < 0)
		return fail(e, "submit frame", ret);
	e->submitted++;
	if (!got) {
		ret = avcodec_receive_packet(e->codec, e->packet);
		if (ret == AVERROR(EAGAIN))
			return true;
		if (ret < 0)
			return fail(e, "receive encoded packet", ret);
	}
	return export_packet(e, packet, received);
}

static bool extra_data(void *data, uint8_t **bytes, size_t *size)
{
	struct encoder *e = data;
	*bytes = e->headers;
	*size = e->headers_size;
	return *size != 0;
}

static bool sei_data(void *data, uint8_t **bytes, size_t *size)
{
	struct encoder *e = data;
	*bytes = e->sei;
	*size = e->sei_size;
	return *size != 0;
}

static void defaults(obs_data_t *settings)
{
	obs_data_set_default_int(settings, "bitrate", 6000);
	obs_data_set_default_int(settings, "keyint_sec", 2);
	obs_data_set_default_string(settings, "rate_control", "VBR");
}

static obs_properties_t *properties(void *data)
{
	(void)data;
	obs_properties_t *p = obs_properties_create();
	obs_property_t *rc = obs_properties_add_list(p, "rate_control", obs_module_text("RateControl"),
						  OBS_COMBO_TYPE_LIST, OBS_COMBO_FORMAT_STRING);
	obs_property_list_add_string(rc, obs_module_text("RateControl.VBR"), "VBR");
	obs_properties_add_int(p, "bitrate", obs_module_text("Bitrate"), 100, 100000, 100);
	obs_properties_add_int(p, "keyint_sec", obs_module_text("KeyframeInterval"), 0, 10, 1);
	return p;
}

static const char *name_h264(void *data)
{
	(void)data;
	return obs_module_text("Encoder.H264");
}

static const char *name_hevc(void *data)
{
	(void)data;
	return obs_module_text("Encoder.HEVC");
}

bool obs_module_load(void)
{
	struct obs_encoder_info info = {
		.id = "liuqin_v4l2_h264", .type = OBS_ENCODER_VIDEO, .codec = "h264",
		.get_name = name_h264, .create = create_h264, .destroy = destroy,
		.encode = encode, .get_defaults = defaults, .get_properties = properties,
		.get_extra_data = extra_data, .get_sei_data = sei_data, .get_video_info = video_info,
	};
	if (avcodec_find_encoder_by_name("h264_v4l2m2m"))
		obs_register_encoder(&info);
	info.id = "liuqin_v4l2_hevc";
	info.codec = "hevc";
	info.get_name = name_hevc;
	info.create = create_hevc;
	if (avcodec_find_encoder_by_name("hevc_v4l2m2m"))
		obs_register_encoder(&info);
	return true;
}
