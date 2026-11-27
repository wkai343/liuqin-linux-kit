/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#define ioctl test_event_ioctl
#define ff_set_dimensions test_set_dimensions
#define ff_v4l2_m2m_codec_reinit test_codec_reinit
#include "libavcodec/v4l2_context.c"
#undef ioctl

static struct v4l2_format driver_format;
static struct v4l2_rect driver_visible;
static int reinitialized;
static int selection_supported = 1;

int test_event_ioctl(int fd, unsigned long request, ...)
{
    va_list ap;
    void *arg;
    assert(fd == 17);
    va_start(ap, request); arg = va_arg(ap, void *); va_end(ap);
    switch (request) {
    case VIDIOC_DQEVENT:
        ((struct v4l2_event *)arg)->type = V4L2_EVENT_SOURCE_CHANGE;
        return 0;
    case VIDIOC_G_FMT:
        *(struct v4l2_format *)arg = driver_format;
        return 0;
    case VIDIOC_G_SELECTION:
        if (!selection_supported) { errno = EINVAL; return -1; }
        ((struct v4l2_selection *)arg)->r = driver_visible;
        return 0;
    case VIDIOC_CROPCAP:
        errno = EINVAL;
        return -1;
    case VIDIOC_DECODER_CMD:
        return 0;
    default:
        assert(0 && "unexpected ioctl");
        return -1;
    }
}

int test_set_dimensions(AVCodecContext *ctx, int width, int height)
{
    ctx->width = width;
    ctx->height = height;
    return 0;
}

int test_codec_reinit(V4L2m2mContext *s)
{
    reinitialized++;
    s->capture.format = driver_format;
    /* Model Iris S_FMT resetting the kernel rectangle to storage dimensions. */
    driver_visible.width = driver_format.fmt.pix_mp.width;
    driver_visible.height = driver_format.fmt.pix_mp.height;
    return 0;
}

int main(void)
{
    V4L2m2mContext s = { 0 };
    AVCodecContext avctx = { 0 };
    s.fd = 17;
    s.avctx = &avctx;
    s.capture.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    s.capture.name = "capture";
    driver_format.type = s.capture.type;
    driver_format.fmt.pix_mp.width = 1280;
    driver_format.fmt.pix_mp.height = 736;
    driver_format.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
    s.capture.format = driver_format;
    s.capture.selection = (struct v4l2_rect) { 0, 0, 320, 240 };
    driver_visible = (struct v4l2_rect) { 0, 0, 1280, 720 };
    assert(v4l2_handle_event(&s.capture) == 0);
    assert(!reinitialized && s.capture.selection.height == 720);
    driver_format.fmt.pix_mp.width = 1920;
    driver_format.fmt.pix_mp.height = 1088;
    driver_visible = (struct v4l2_rect) { 0, 0, 1920, 1080 };
    assert(v4l2_handle_event(&s.capture) == 1);
    assert(reinitialized == 1 && driver_visible.height == 1088);
    assert(s.capture.selection.height == 1080);
    driver_visible.left = -1;
    assert(ff_v4l2_context_get_selection(&s.capture, &driver_format) == AVERROR(EINVAL));
    assert(!s.capture.selection.width);
    selection_supported = 0;
    assert(ff_v4l2_context_get_selection(&s.capture, &driver_format) == 0);
    assert(!s.capture.selection.width);
    puts("PASS: crop-only SOURCE_CHANGE, reconfiguration cache, invalid/unsupported selection");
    return 0;
}
