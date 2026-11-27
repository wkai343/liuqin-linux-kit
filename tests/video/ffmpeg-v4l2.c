/* SPDX-License-Identifier: MIT
 * Compile against a configured FFmpeg source tree with ASan/UBSan.
 * Include the actual implementation, so the regression exercises its copy.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#ifndef FFMPEG_V4L2_SOURCE
#define FFMPEG_V4L2_SOURCE "libavcodec/v4l2_buffers.c"
#endif
#define ioctl test_ioctl
#include FFMPEG_V4L2_SOURCE
#undef ioctl

static int queued_packets;

int test_ioctl(int fd, unsigned long request, ...)
{
    assert(fd == 17 && request == VIDIOC_QBUF);
    queued_packets++;
    return 0;
}

static uint8_t pixel(int plane, int y, int x)
{
    return (17 * plane + 3 * y + x) % 251 + 1;
}

static void check_copy(int width, int height, int stride, int padded_height,
                       int source_padding, int negative, int mplane, int nv21)
{
    V4L2Context context = { 0 };
    V4L2Buffer out = { 0 };
    AVFrame frame = { 0 };
    uint8_t *source[2];
    size_t uv_offset = (size_t)stride * padded_height;
    size_t size = uv_offset + (size_t)stride * ((padded_height + 1) / 2);
    uint8_t *destination = malloc(size + 32);
    int i, y, x;

    assert(destination);
    memset(destination, 0xa5, size + 32);
    context.format.type = mplane ? V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE : V4L2_BUF_TYPE_VIDEO_OUTPUT;
    if (mplane) {
        context.format.fmt.pix_mp.pixelformat = nv21 ? V4L2_PIX_FMT_NV21 : V4L2_PIX_FMT_NV12;
        context.format.fmt.pix_mp.height = padded_height;
    } else {
        context.format.fmt.pix.pixelformat = nv21 ? V4L2_PIX_FMT_NV21 : V4L2_PIX_FMT_NV12;
        context.format.fmt.pix.height = padded_height;
    }
    out.context = &context;
    out.num_planes = 1;
    out.buf.type = context.format.type;
    out.plane_info[0].bytesperline = stride;
    out.plane_info[0].length = size;
    out.plane_info[0].mm_addr = destination;
    frame.format = nv21 ? AV_PIX_FMT_NV21 : AV_PIX_FMT_NV12;
    frame.width = width;
    frame.height = height;
    for (i = 0; i < 2; i++) {
        int rows = i ? (height + 1) / 2 : height;
        int columns = i ? 2 * ((width + 1) / 2) : width;
        int pitch = columns + source_padding;
        source[i] = malloc((size_t)pitch * rows);
        assert(source[i]);
        memset(source[i], 0xcc, (size_t)pitch * rows);
        frame.linesize[i] = negative ? -pitch : pitch;
        frame.data[i] = source[i] + (negative ? (size_t)pitch * (rows - 1) : 0);
        for (y = 0; y < rows; y++)
            for (x = 0; x < columns; x++)
                frame.data[i][(ptrdiff_t)y * frame.linesize[i] + x] = pixel(i, y, x);
    }

    assert(v4l2_buffer_swframe_to_buf(&frame, &out) == 0);
    assert((mplane ? out.planes[0].bytesused : out.buf.bytesused) == size);
    for (i = 0; i < 2; i++) {
        int rows = i ? (height + 1) / 2 : height;
        int padded_rows = i ? (padded_height + 1) / 2 : padded_height;
        int columns = i ? 2 * ((width + 1) / 2) : width;
        uint8_t *plane = destination + (i ? uv_offset : 0);
        for (y = 0; y < padded_rows; y++)
            for (x = 0; x < stride; x++)
                assert(plane[(size_t)y * stride + x] ==
                       (y < rows && x < columns ? pixel(i, y, x) : 0));
    }
    for (x = 0; x < 32; x++)
        assert(destination[size + x] == 0xa5);

    /* Invalid capacity/stride/height must fail before touching the buffer. */
    memset(destination, 0xa5, size + 32);
    out.plane_info[0].length = size - 1;
    assert(v4l2_buffer_swframe_to_buf(&frame, &out) == AVERROR(EINVAL));
    out.plane_info[0].length = size;
    out.plane_info[0].bytesperline = width - 1;
    assert(v4l2_buffer_swframe_to_buf(&frame, &out) == AVERROR(EINVAL));
    out.plane_info[0].bytesperline = stride;
    frame.height = padded_height + 1;
    assert(v4l2_buffer_swframe_to_buf(&frame, &out) == AVERROR(EINVAL));
    assert(v4l2_bufref_to_buf(&out, 0, source[0], 1, size + 1) == AVERROR(EINVAL));
    assert(v4l2_bufref_to_buf(&out, 0, source[0], -1, 0) == AVERROR(EINVAL));
    assert(v4l2_bufref_to_buf(&out, -1, source[0], 1, 0) == AVERROR(EINVAL));
    for (size_t n = 0; n < size + 32; n++)
        assert(destination[n] == 0xa5);
    free(source[0]);
    free(source[1]);
    free(destination);
}

static void check_crop(int left, int top, int width, int height)
{
    V4L2m2mContext *s = av_refstruct_allocz(sizeof(*s));
    AVCodecContext avctx = { .time_base = { 1, 30 } };
    V4L2Buffer buffer = { 0 };
    AVFrame *frame = av_frame_alloc();
    const int stride = 1280, storage_height = 736;
    uint8_t *pixels = av_malloc((size_t)stride * storage_height * 3 / 2);
    assert(s && frame && pixels);
    s->self_ref = s;
    s->avctx = &avctx;
    s->capture.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    s->capture.av_pix_fmt = AV_PIX_FMT_NV12;
    s->capture.width = stride;
    s->capture.height = storage_height;
    s->capture.format.type = s->capture.type;
    s->capture.format.fmt.pix_mp.width = stride;
    s->capture.format.fmt.pix_mp.height = storage_height;
    s->capture.selection = (struct v4l2_rect) { left, top, width, height };
    buffer.context = &s->capture;
    buffer.buf.type = s->capture.type;
    buffer.num_planes = 1;
    buffer.plane_info[0].bytesperline = stride;
    buffer.plane_info[0].length = (size_t)stride * storage_height * 3 / 2;
    buffer.plane_info[0].mm_addr = pixels;
    assert(ff_v4l2_buffer_buf_to_avframe(frame, &buffer) == 0);
    assert(frame->data[1] == pixels + stride * storage_height);
    assert(frame->crop_left == left && frame->crop_top == top);
    assert(frame->crop_right == stride - left - width);
    assert(frame->crop_bottom == storage_height - top - height);
    assert(av_frame_apply_cropping(frame, AV_FRAME_CROP_UNALIGNED) == 0);
    assert(frame->width == width && frame->height == height);
    assert(frame->data[0] == pixels + stride * top + left);
    assert(frame->data[1] == pixels + stride * storage_height + stride * (top / 2) + left);
    av_frame_free(&frame);
    av_free(pixels);
    av_refstruct_unref(&s);
}

static void check_packet_recycling(void)
{
    V4L2m2mContext *s = av_refstruct_allocz(sizeof(*s));
    AVCodecContext avctx = { .time_base = { 1, 30 } };
    V4L2Buffer buffer = { 0 };
    AVPacket *packets[8];
    uint8_t payload[32];
    int i, j;
    assert(s);
    s->self_ref = s;
    s->avctx = &avctx;
    s->fd = 17;
    s->capture.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    s->capture.streamon = 1;
    buffer.context = &s->capture;
    buffer.buf.type = s->capture.type;
    buffer.buf.m.planes = buffer.planes;
    buffer.num_planes = 1;
    buffer.plane_info[0].mm_addr = payload;
    buffer.plane_info[0].length = sizeof(payload);
    buffer.planes[0].bytesused = 20;
    buffer.planes[0].data_offset = 4;
    for (i = 0; i < 8; i++) {
        packets[i] = av_packet_alloc();
        assert(packets[i]);
        memset(payload, i + 1, sizeof(payload));
        assert(ff_v4l2_buffer_buf_to_avpkt(packets[i], &buffer) == 0);
        assert(buffer.status == V4L2BUF_IN_DRIVER);
        assert(queued_packets == i + 1);
    }
    memset(payload, 0, sizeof(payload));
    for (i = 0; i < 8; i++) {
        assert(packets[i]->size == 16);
        for (j = 0; j < 16; j++) assert(packets[i]->data[j] == i + 1);
        for (j = 0; j < AV_INPUT_BUFFER_PADDING_SIZE; j++) assert(packets[i]->data[16+j] == 0);
        av_packet_free(&packets[i]);
    }
    packets[0] = av_packet_alloc();
    buffer.planes[0].bytesused = 33;
    assert(ff_v4l2_buffer_buf_to_avpkt(packets[0], &buffer) == AVERROR_INVALIDDATA);
    buffer.planes[0].bytesused = 3;
    assert(ff_v4l2_buffer_buf_to_avpkt(packets[0], &buffer) == AVERROR_INVALIDDATA);
    assert(queued_packets == 8);
    av_packet_free(&packets[0]);
    av_refstruct_unref(&s);
}

int main(void)
{
    check_copy(1280, 720, 1280, 736, 0, 0, 1, 0);
    check_copy(1920, 1080, 1920, 1088, 32, 0, 1, 0);
    check_copy(854, 480, 896, 480, 14, 0, 1, 0);
    check_copy(854, 480, 896, 480, 14, 1, 1, 0);
    check_copy(853, 479, 896, 480, 0, 1, 0, 1);
    check_copy(128, 64, 128, 64, 0, 0, 0, 0);
    check_crop(0, 0, 1280, 720);
    check_crop(32, 2, 854, 480);
    check_packet_recycling();
    puts("PASS: padded NV12/NV21, UV offsets, independent/negative strides, bounds, visible crop, retained packets");
    return 0;
}
