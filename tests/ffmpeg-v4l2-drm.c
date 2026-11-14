/* SPDX-License-Identifier: MIT
 * Exercise actual DRM export, ownership and PTS helpers with ASan/UBSan.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <fcntl.h>
#include "config.h"
#undef CONFIG_LIBDRM
#define CONFIG_LIBDRM 1
#define ioctl test_ioctl
#include "libavcodec/v4l2_buffers.c"
#undef ioctl

static int exported, queued, fail_plane = -1, last_fd[2], base_fd, destroyed;
int test_ioctl(int fd, unsigned long request, ...)
{
    va_list args;
    assert(fd == 17);
    if (request == VIDIOC_QBUF) { queued++; return 0; }
    assert(request == VIDIOC_EXPBUF);
    va_start(args,request);
    struct v4l2_exportbuffer *exp = va_arg(args,struct v4l2_exportbuffer *);
    va_end(args);
    assert(exp->index == 4 && (exp->flags & O_CLOEXEC));
    if ((int)exp->plane == fail_plane) { errno = EINVAL; return -1; }
    exp->fd = fcntl(base_fd,F_DUPFD_CLOEXEC,0);
    assert(exp->fd >= 0);
    last_fd[exp->plane] = exp->fd;
    exported++;
    return 0;
}

static void destroy(AVRefStructOpaque unused, void *data)
{
    V4L2m2mContext *s=data;
    av_buffer_unref(&s->capture.hw_frames_ref);
    av_free(s->capture.buffers);
    av_free(s->avctx);
    destroyed++;
}

static V4L2m2mContext *setup(int planes, int multi, int nv21)
{
    V4L2m2mContext *s = av_refstruct_alloc_ext(sizeof(*s),0,NULL,destroy);
    assert(s);
    s->self_ref=s;s->fd=17;
    s->avctx=av_mallocz(sizeof(*s->avctx));assert(s->avctx);
    V4L2Context *c=&s->capture;
    c->av_pix_fmt=nv21 ? AV_PIX_FMT_NV21 : AV_PIX_FMT_NV12;
    c->drm_prime=1;c->hw_frames_ref=av_buffer_alloc(1);
    c->type=multi ? V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE : V4L2_BUF_TYPE_VIDEO_CAPTURE;
    c->format.type=c->type;
    if (multi) { c->format.fmt.pix_mp.width=1280;c->format.fmt.pix_mp.height=736; }
    else { c->format.fmt.pix.width=1280;c->format.fmt.pix.height=736; }
    c->selection=(struct v4l2_rect){0,0,1280,720};
    c->buffers=av_mallocz(sizeof(V4L2Buffer));
    V4L2Buffer *b=c->buffers;
    b->context=c;b->num_planes=planes;b->buf.type=c->type;b->buf.index=4;
    for(int i=0;i<planes;i++) {
        b->plane_info[i].bytesperline=1280;
        b->plane_info[i].length=planes == 1 ? 1280*736*3/2 : 1280*(i?368:736)+64;
        b->planes[i].data_offset=planes == 1 ? 0 : 64;
    }
    return s;
}

static void good(int planes,int multi,int nv21,int close_first)
{
    V4L2m2mContext *s=setup(planes,multi,nv21);
    AVFrame *f=av_frame_alloc();
    int q=queued,d=destroyed;
    assert(ff_v4l2_buffer_buf_to_avframe(f,s->capture.buffers)==0);
    AVDRMFrameDescriptor *desc=(void *)f->data[0];
    assert(f->format==AV_PIX_FMT_DRM_PRIME && f->hw_frames_ctx);
    assert(f->height==736 && f->crop_bottom==16);
    assert(desc->nb_objects==planes && desc->nb_layers==1);
    assert(desc->layers[0].format==(nv21 ? DRM_FORMAT_NV21 : DRM_FORMAT_NV12));
    assert(desc->layers[0].planes[1].object_index==(planes==1?0:1));
    assert(desc->layers[0].planes[1].offset==(planes==1?1280*736:64));
    assert(desc->layers[0].planes[1].pitch==1280);
    assert(atomic_load(&s->refcount)==1);
    AVFrame *clone=av_frame_clone(f);assert(clone);
    av_frame_free(&f);
    assert(fcntl(last_fd[0],F_GETFD)>=0 && queued==q);
    if(close_first) { s->self_ref=NULL;av_refstruct_unref(&s);assert(destroyed==d); }
    else s->capture.streamon=1;
    av_frame_free(&clone);
    for(int i=0;i<planes;i++)assert(fcntl(last_fd[i],F_GETFD)==-1 && errno==EBADF);
    if(close_first)assert(destroyed==d+1 && queued==q);
    else { assert(queued==q+1 && atomic_load(&s->refcount)==0);av_refstruct_unref(&s); }
}

static void timestamps(void)
{
    V4L2m2mContext *s=setup(1,1,0);
    V4L2Buffer in={.context=&s->output};
    V4L2Buffer out={.context=&s->capture};
    const int64_t pts[]={-1229,0,AV_NOPTS_VALUE,2560,512,512,INT64_MAX-1};
    struct timeval wire[FF_ARRAY_ELEMS(pts)];
    s->output.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    s->decode=1;
    for(int i=0;i<FF_ARRAY_ELEMS(pts);i++) {
        v4l2_set_pts(&in,pts[i]);wire[i]=in.buf.timestamp;
        assert(wire[i].tv_sec>=0 && wire[i].tv_usec>0 && wire[i].tv_usec<1000000);
    }
    /* Output may reorder, repeat or omit pictures. Recover exact source PTS,
     * without unsigned wrap, rounding, or substituting DTS for missing PTS. */
    for(int i=FF_ARRAY_ELEMS(pts)-1;i>=0;i--) {
        out.buf.timestamp=wire[i];assert(v4l2_get_pts(&out)==pts[i]);
        assert(v4l2_get_pts(&out)==pts[i]);
    }
    for(int i=0;i<300;i++)v4l2_set_pts(&in,i);
    out.buf.timestamp=wire[0];assert(v4l2_get_pts(&out)==AV_NOPTS_VALUE);
    out.buf.timestamp=in.buf.timestamp;assert(v4l2_get_pts(&out)==299);
    s->pts_sequence=999999;v4l2_set_pts(&in,-1);
    assert(in.buf.timestamp.tv_sec==1 && in.buf.timestamp.tv_usec==0);
    out.buf.timestamp=in.buf.timestamp;assert(v4l2_get_pts(&out)==-1);
    av_refstruct_unref(&s);
}

int main(void)
{
    timestamps();
    base_fd=open("/dev/null",O_RDONLY|O_CLOEXEC);assert(base_fd>=0);
    good(1,1,0,0);good(1,0,1,1);good(2,1,0,0);good(2,1,1,1);
    for(int fault=0;fault<4;fault++) {
        V4L2m2mContext *s=setup(2,1,0);V4L2Buffer *b=s->capture.buffers;
        AVFrame *f=av_frame_alloc();int n=exported;
        if(fault==0)b->plane_info[1].length--;
        if(fault==1)b->plane_info[0].bytesperline=1279;
        if(fault==2)b->planes[0].data_offset=UINT_MAX;
        if(fault==3)fail_plane=1;
        assert(ff_v4l2_buffer_buf_to_avframe(f,b)<0);
        assert(!f->buf[0] && !f->hw_frames_ctx && !atomic_load(&s->refcount));
        if(fault==3) { assert(exported==n+1);assert(fcntl(last_fd[0],F_GETFD)==-1); }
        else assert(exported==n);
        fail_plane=-1;av_frame_free(&f);av_refstruct_unref(&s);
    }
    close(base_fd);
    puts("PASS: DRM geometry, crop, retained frames, decoder close, FD cleanup, export failures and reordered PTS");
}
