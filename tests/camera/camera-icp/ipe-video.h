/* SPDX-License-Identifier: GPL-2.0-only */
/* Optional fixed-profile, copy-backed V4L2 M2M bridge. Included by the core.
 * Imported DMA-BUFs are CPU mapped by vb2-vmalloc. Firmware only sees our
 * separately owned pages, never userspace-supplied command or DMA addresses.
 */
#include <linux/dma-buf.h>
#include <linux/kthread.h>
#include <linux/topology.h>
#include <media/media-device.h>
#include <media/v4l2-device.h>
#include <media/v4l2-ioctl.h>
#include <media/v4l2-mem2mem.h>
#include <media/videobuf2-vmalloc.h>
#include "frame-layout.h"

#define VIDEO_INPUT_IOVA 0x14000000u
#define VIDEO_OUTPUT_IOVA 0x16000000u
#define VIDEO_SLOT_SIZE SZ_32M
static_assert(VIDEO_INPUT_IOVA + VIDEO_SLOT_SIZE <= VIDEO_OUTPUT_IOVA);
struct ipe_profile {
 u32 width, height;
 const u8 *iq, *stripes;
 size_t stripe_size;
 /* Optional stock TF21 first/reference-frame firmware and CDM programs. */
 const u8 *iq_next, *stripes_next, *cdm, *cdm_next;
};
#include "video-fixture.h"
struct ipe_context;
struct ipe_video {
 struct kthread_worker *worker;
 cpumask_t worker_cpus, uhd_cpus;
 struct icp *core;
 struct media_device media;
 struct v4l2_device v4l2;
 struct video_device vdev;
 struct v4l2_m2m_dev *m2m;
 struct mutex lock; /* V4L2 queue/ioctl lock; worker uses core->lock only. */
 struct ipe_context *owner;
 struct page **pages;
 unsigned int allocated, mapped, slots;
 size_t slot_size;
 void *pixels;
 u32 sequence;
};
struct ipe_context {
 struct v4l2_fh fh;
 struct ipe_video *video;
 const struct ipe_profile *profile;
 struct kthread_work work;
 bool abort, failed;
};
static bool video_timeout_once;
module_param(video_timeout_once, bool, 0600);
MODULE_PARM_DESC(video_timeout_once, "One-shot timeout after submitting an IPE frame (recovery test)");
static struct ipe_context *ipe_ctx(void *priv)
{
 return container_of(priv, struct ipe_context, fh);
}
static void video_pixels_free(struct ipe_video *v)
{
 unsigned int i;
 if (v->pixels) vunmap(v->pixels);
 v->pixels = NULL;
 for (i = 0; i < v->mapped; i++) {
  u32 slot = i / (v->slot_size / PAGE_SIZE);
  u32 offset = i % (v->slot_size / PAGE_SIZE) * PAGE_SIZE;
  iommu_unmap(v->core->domain, (VIDEO_INPUT_IOVA + slot * VIDEO_SLOT_SIZE) + offset, PAGE_SIZE);
 }
 v->mapped = 0;
 for (i = 0; i < v->allocated; i++) __free_page(v->pages[i]);
 v->allocated = 0;
 kvfree(v->pages); v->pages = NULL;
}
static int video_pixels_alloc(struct ipe_video *v)
{
 unsigned int i;
 int ret;
 v->slot_size = PAGE_ALIGN(v->core->width * v->core->height * 3 / 2 + PAGE_SIZE);
 if (v->slot_size > VIDEO_SLOT_SIZE) return -E2BIG;
 v->slots = v->core->temporal ? 4 : 2;
 v->pages = kvcalloc(v->slots * v->slot_size / PAGE_SIZE, sizeof(*v->pages), GFP_KERNEL);
 if (!v->pages) return -ENOMEM;
 for (i = 0; i < v->slots * v->slot_size / PAGE_SIZE; i++) {
  struct page *p = alloc_page(GFP_KERNEL | __GFP_ZERO);
  u32 slot = i / (v->slot_size / PAGE_SIZE);
  u32 offset = i % (v->slot_size / PAGE_SIZE) * PAGE_SIZE;
  if (!p) return -ENOMEM;
  v->pages[v->allocated++] = p;
  ret = iommu_map(v->core->domain, (VIDEO_INPUT_IOVA + slot * VIDEO_SLOT_SIZE) + offset,
    page_to_phys(p), PAGE_SIZE, IOMMU_READ | IOMMU_WRITE | IOMMU_CACHE, GFP_KERNEL);
  if (ret) return ret;
  v->mapped++;
 }
 v->pixels = vmap(v->pages, v->allocated, VM_MAP, PAGE_KERNEL);
 if (!v->pixels) return -ENOMEM;
 memset(v->pixels, 0x5a, v->slots * v->slot_size);
 return 0;
}
/* PAS shutdown is the quiescence boundary even after a timed-out frame. The
 * owned pixel pages cannot be freed until it succeeds. No external DMA-BUF is
 * reachable by the firmware, so failed user buffers can still be returned.
 */
static int video_shutdown(struct ipe_video *v, bool graceful)
{
 struct icp *c = v->core;
 int ret = 0, clean;
 if (graceful && c->handle) ret = session_destroy(c);
 {
  bool was_unsafe = c->unsafe;
  clean = quiesce(c);
  if (clean) {
   if (!was_unsafe) __module_get(THIS_MODULE);
   return clean;
  }
 }
 video_pixels_free(v);
 memory_free(c);
 clean = 0;
 c->handle = 0;
 c->streaming = false;
 return ret ? ret : clean;
}
static int video_session(struct ipe_context *ctx)
{
 struct ipe_video *v = ctx->video;
 struct icp *c = v->core;
 int ret;
 if (c->unsafe) return -EIO;
 if (c->streaming || v->owner) return -EBUSY;
#ifdef ICP_HDR_STILL_FRAME
 /* libcamera starts the IPE before the next sensor. Finish the previous
  * stream's delayed power transitions while its sensor is idle. */
 ret = quiesce_idle_capture(c, false);
 if (ret) return ret;
#endif
 c->temporal = !!ctx->profile->cdm;
 c->width = ctx->profile->width; c->height = ctx->profile->height;
 c->frame_sent = false; c->frame_done = false; c->output_valid = false;
 c->completed_frames = 0; c->result = 0; c->handle = 0;
 c->frame_error = c->frame_rc = 0;
 v->sequence = 0;
 /* Use a dedicated worker so the submitting application's CPU never sets
  * the placement of two full-frame copies per request. At UHD60 these
  * copies have a 16.7 ms deadline. Other profiles use the default mask. */
 if (c->width == 3840 && c->height == 2160 &&
     cpumask_intersects(&v->uhd_cpus, cpu_online_mask))
  set_cpus_allowed_ptr(v->worker->task, &v->uhd_cpus);
 else
  set_cpus_allowed_ptr(v->worker->task, &v->worker_cpus);
 ret = session_start(c, 5, 0);
 if (!ret && (c->cdm_size != SZ_512K || c->scratch_size > SZ_1M - SCRATCH_OFFSET)) ret = -E2BIG;
 if (!ret) ret = video_pixels_alloc(v);
 if (ret) {
  debug_dump(c);
  video_shutdown(v, false);
  return ret;
 }
 c->streaming = true;
 v->owner = ctx;
 ctx->abort = ctx->failed = false;
 dev_info(c->dev, "IPE video session %ux%u scratch=%u cdm=%u\n", c->width, c->height, c->scratch_size, c->cdm_size);
 return 0;
}
static int video_copy(struct vb2_buffer *vb, void *pixels, u32 ysize, bool to_buffer)
{
 struct dma_buf *db = vb->planes[0].dbuf;
 enum dma_data_direction dir = to_buffer ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
 void *ptr = vb2_plane_vaddr(vb, 0);
 int ret;
 if (!ptr) return -EFAULT;
 if (db) { ret = dma_buf_begin_cpu_access(db, dir); if (ret) return ret; }
 if (to_buffer) memcpy(ptr, pixels, ysize * 3 / 2);
 else memcpy(pixels, ptr + vb->planes[0].data_offset, ysize * 3 / 2);
 if (db) { ret = dma_buf_end_cpu_access(db, dir); if (ret) return ret; }
 return 0;
}

static int video_frame_process(struct ipe_context *ctx, struct vb2_v4l2_buffer *src,
 struct vb2_v4l2_buffer *dst)
{
 struct ipe_video *v = ctx->video;
 struct icp *c = v->core;
 const struct ipe_profile *p = ctx->profile;
 u8 *shared = c->heap + 6 * SZ_1M;
 u32 ysize = p->width * p->height;
 u32 reply[128], seq = ++v->sequence;
 u32 cmd[] = {40, 0x101000e, 6, seq, 0, 0, 0, 1, c->handle, 0x804000};
 int ret;
 if (!seq) return -EOVERFLOW;
 ret = video_copy(&src->vb2_buf, v->pixels, ysize, false);
 if (ret) return ret;
#ifdef ICP_TEMPORAL_FIXTURE
 memcpy(shared + 0x4000, seq == 1 ? tf_frame_0 : tf_frame_1, sizeof(tf_frame_0));
 /* Keep the preceding FULL_REF output alive until the next completion. */
 {
  u32 out_slot = 2 + ((seq - 1) & 1), in_slot = 2 + (seq & 1);
  u32 ref_in = VIDEO_INPUT_IOVA + in_slot * VIDEO_SLOT_SIZE;
  u32 ref_out = VIDEO_INPUT_IOVA + out_slot * VIDEO_SLOT_SIZE;
  *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_INPUT_FULL_REF, 0, 0)) = ref_in;
  *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_INPUT_FULL_REF, 1, 0)) = ref_in + ysize;
  *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_OUTPUT_FULL_REF, 0, 0)) = ref_out;
  *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_OUTPUT_FULL_REF, 1, 0)) = ref_out + ysize;
 }
 memcpy(shared + 0x60000, seq == 1 ? p->cdm : p->cdm_next, 1680);
#else
 memcpy(shared + 0x4000, video_frame, sizeof(video_frame));
#endif
 if (p->width == 3840 && p->height == 2160)
  *(u32 *)(shared + 0x4000 + 28) = 16666667; /* frame budget at 60 Hz */
 *(u32 *)(shared + 0x4000 + 168) = seq;
 *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_INPUT_FULL, 0, 0)) = VIDEO_INPUT_IOVA;
 *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_INPUT_FULL, 1, 0)) = VIDEO_INPUT_IOVA + ysize;
 *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_OUTPUT_DISPLAY, 0, 0)) = VIDEO_OUTPUT_IOVA;
 *(u32 *)(shared + 0x4000 + ipe_buffer_offset(0, IPE_OUTPUT_DISPLAY, 1, 0)) = VIDEO_OUTPUT_IOVA + ysize;
 memcpy(shared + 0x10000, c->temporal && seq > 1 ? p->iq_next : p->iq, 6180);
 memcpy(shared + 0x20000, c->temporal && seq > 1 ? p->stripes_next : p->stripes, p->stripe_size);
 memset(c->heap + 5 * SZ_1M + SCRATCH_OFFSET, 0, c->scratch_size);
 dma_wmb();
 ret = send(c, cmd, ARRAY_SIZE(cmd)); if (ret) return ret;
 if (READ_ONCE(video_timeout_once)) {
  WRITE_ONCE(video_timeout_once, false);
  return -ETIMEDOUT;
 }
 ret = receive(c, 0x102000e, reply, ARRAY_SIZE(reply)); if (ret < 0) return ret;
 if (ret < 9 || reply[2] != 6 || reply[3] != seq || reply[4]) return -EPROTO;
 c->frame_error = reply[7]; c->frame_rc = reply[8];
 if (reply[7] || reply[8]) return -EREMOTEIO;
 dma_rmb();
 if (memchr_inv(v->pixels + ysize * 3 / 2, 0x5a, v->slot_size - ysize * 3 / 2) ||
     memchr_inv(v->pixels + v->slot_size + ysize * 3 / 2, 0x5a, v->slot_size - ysize * 3 / 2))
  return -EOVERFLOW;
 for (unsigned int slot = 2; slot < v->slots; slot++)
  if (memchr_inv(v->pixels + slot * v->slot_size + ysize * 3 / 2, 0x5a,
      v->slot_size - ysize * 3 / 2)) return -EOVERFLOW;
 ret = video_copy(&dst->vb2_buf, v->pixels + v->slot_size, ysize, true);
 if (ret) return ret;
 c->completed_frames++;
 c->frame_done = true;
 return 0;
}
static void video_worker(struct kthread_work *work)
{
 struct ipe_context *ctx = container_of(work, struct ipe_context, work);
 struct ipe_video *v = ctx->video;
 struct icp *c = v->core;
 struct vb2_v4l2_buffer *src = v4l2_m2m_next_src_buf(ctx->fh.m2m_ctx);
 struct vb2_v4l2_buffer *dst = v4l2_m2m_next_dst_buf(ctx->fh.m2m_ctx);
 int ret = -ECANCELED;
 mutex_lock(&c->lock);
 if (!READ_ONCE(ctx->abort) && !ctx->failed && v->owner == ctx && c->streaming) {
  ret = video_frame_process(ctx, src, dst);
  if (ret) {
   ctx->failed = true;
   c->result = ret;
   debug_dump(c);
   video_shutdown(v, false);
   vb2_queue_error(v4l2_m2m_get_src_vq(ctx->fh.m2m_ctx));
   vb2_queue_error(v4l2_m2m_get_dst_vq(ctx->fh.m2m_ctx));
  }
 }
 mutex_unlock(&c->lock);
 v4l2_m2m_buf_copy_metadata(src, dst);
 dst->sequence = v->sequence ? v->sequence - 1 : 0;
 src->sequence = dst->sequence;
 vb2_set_plane_payload(&dst->vb2_buf, 0, ctx->profile->width * ctx->profile->height * 3 / 2);
 v4l2_m2m_buf_done_and_job_finish(v->m2m, ctx->fh.m2m_ctx,
   ret || READ_ONCE(ctx->abort) ? VB2_BUF_STATE_ERROR : VB2_BUF_STATE_DONE);
}
static void video_run(void *priv)
{
 struct ipe_context *ctx = priv;
 kthread_queue_work(ctx->video->worker, &ctx->work);
}
static void video_abort(void *priv)
{
 struct ipe_context *ctx = priv;
 /* The worker's bounded HFI wait finishes before m2m releases the buffers. */
 WRITE_ONCE(ctx->abort, true);
}
static const struct v4l2_m2m_ops video_m2m_ops = {
 .device_run = video_run, .job_abort = video_abort,
};
static int video_queue_setup(struct vb2_queue *q, unsigned int *buffers,
 unsigned int *planes, unsigned int sizes[], struct device *alloc_devs[])
{
 struct ipe_context *ctx = vb2_get_drv_priv(q);
 u32 y = ctx->profile->width * ctx->profile->height;
 if (*planes) return *planes == 1 && sizes[0] >= y * 3 / 2 ? 0 : -EINVAL;
 *planes = 1; sizes[0] = y * 3 / 2;
 return 0;
}
static int video_prepare(struct vb2_buffer *vb)
{
 struct ipe_context *ctx = vb2_get_drv_priv(vb->vb2_queue);
 unsigned int i;
 u32 y = ctx->profile->width * ctx->profile->height;
 for (i = 0; i < 1; i++) {
  u32 size = y * 3 / 2, off = vb->planes[i].data_offset;
  if (off > vb2_plane_size(vb, i) || size > vb2_plane_size(vb, i) - off) return -EINVAL;
  if (V4L2_TYPE_IS_OUTPUT(vb->type)) {
   if (vb2_get_plane_payload(vb, i) < off + size) return -EINVAL;
  } else {
   if (off) return -EINVAL;
   vb2_set_plane_payload(vb, i, size);
  }
 }
 return 0;
}
static void video_queue(struct vb2_buffer *vb)
{
 struct ipe_context *ctx = vb2_get_drv_priv(vb->vb2_queue);
 v4l2_m2m_buf_queue(ctx->fh.m2m_ctx, to_vb2_v4l2_buffer(vb));
}
static void video_return(struct ipe_context *ctx, enum v4l2_buf_type type, enum vb2_buffer_state state)
{
 struct vb2_v4l2_buffer *buf;
 while ((buf = V4L2_TYPE_IS_OUTPUT(type) ? v4l2_m2m_src_buf_remove(ctx->fh.m2m_ctx) :
                                         v4l2_m2m_dst_buf_remove(ctx->fh.m2m_ctx)))
  v4l2_m2m_buf_done(buf, state);
}
static int video_start(struct vb2_queue *q, unsigned int count)
{
 struct ipe_context *ctx = vb2_get_drv_priv(q);
 struct vb2_queue *other = V4L2_TYPE_IS_OUTPUT(q->type) ?
  v4l2_m2m_get_dst_vq(ctx->fh.m2m_ctx) : v4l2_m2m_get_src_vq(ctx->fh.m2m_ctx);
 int ret;
 if (!vb2_is_streaming(other)) return 0;
 mutex_lock(&ctx->video->core->lock);
 ret = video_session(ctx);
 mutex_unlock(&ctx->video->core->lock);
 if (ret) video_return(ctx, q->type, VB2_BUF_STATE_QUEUED);
 return ret;
}
static void video_stop(struct vb2_queue *q)
{
 struct ipe_context *ctx = vb2_get_drv_priv(q);
 struct ipe_video *v = ctx->video;
 /* m2m streamoff has already waited for the active job. */
 kthread_flush_work(&ctx->work);
 mutex_lock(&v->core->lock);
 if (v->owner == ctx) {
  int ret = video_shutdown(v, !ctx->failed);
  v->core->result = ret ? ret : v->core->result;
  v->owner = NULL;
 }
 mutex_unlock(&v->core->lock);
 video_return(ctx, q->type, VB2_BUF_STATE_ERROR);
}
static const struct vb2_ops video_queue_ops = {
 .queue_setup = video_queue_setup, .buf_prepare = video_prepare, .buf_queue = video_queue,
 .start_streaming = video_start, .stop_streaming = video_stop,
};
static int video_queue_init(void *priv, struct vb2_queue *src, struct vb2_queue *dst)
{
 struct ipe_context *ctx = priv;
 struct vb2_queue *queues[] = {src, dst};
 unsigned int i;
 for (i = 0; i < 2; i++) {
  struct vb2_queue *q = queues[i];
  int ret;
  q->type = i ? V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE : V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
  q->io_modes = VB2_MMAP | VB2_DMABUF;
  q->drv_priv = ctx;
  q->buf_struct_size = sizeof(struct v4l2_m2m_buffer);
  q->ops = &video_queue_ops; q->mem_ops = &vb2_vmalloc_memops;
  q->timestamp_flags = V4L2_BUF_FLAG_TIMESTAMP_COPY;
  q->lock = &ctx->video->lock;
  q->dev = ctx->video->core->dev;
  ret = vb2_queue_init(q); if (ret) return ret;
 }
 return 0;
}
static int video_querycap(struct file *file, void *priv, struct v4l2_capability *cap)
{
 strscpy(cap->driver, "qcom-ipe", sizeof(cap->driver));
 strscpy(cap->card, "Liuqin IPE", sizeof(cap->card));
 strscpy(cap->bus_info, "platform:liuqin-ipe", sizeof(cap->bus_info));
 return 0;
}
static int video_enum_fmt(struct file *file, void *priv, struct v4l2_fmtdesc *fmt)
{
 if (fmt->index) return -EINVAL;
 fmt->pixelformat = V4L2_PIX_FMT_NV12;
 return 0;
}
static int video_enum_size(struct file *file, void *priv, struct v4l2_frmsizeenum *f)
{
 if (f->pixel_format != V4L2_PIX_FMT_NV12 || f->index >= ARRAY_SIZE(video_profiles)) return -EINVAL;
 f->type = V4L2_FRMSIZE_TYPE_DISCRETE;
 f->discrete.width = video_profiles[f->index].width;
 f->discrete.height = video_profiles[f->index].height;
 return 0;
}
static const struct ipe_profile *video_nearest(u32 width, u32 height)
{
 unsigned int i;
 for (i = 0; i < ARRAY_SIZE(video_profiles); i++)
  if (width <= video_profiles[i].width && height <= video_profiles[i].height)
   return &video_profiles[i];
 return &video_profiles[ARRAY_SIZE(video_profiles) - 1];
}
static void video_format(struct v4l2_format *f, const struct ipe_profile *p)
{
 struct v4l2_pix_format_mplane *pix = &f->fmt.pix_mp;
 memset(pix, 0, sizeof(*pix));
 pix->width = p->width; pix->height = p->height;
 pix->pixelformat = V4L2_PIX_FMT_NV12; pix->field = V4L2_FIELD_NONE;
 pix->colorspace = V4L2_COLORSPACE_JPEG; pix->ycbcr_enc = V4L2_YCBCR_ENC_601;
 pix->quantization = V4L2_QUANTIZATION_FULL_RANGE; pix->xfer_func = V4L2_XFER_FUNC_SRGB;
 pix->num_planes = 1;
 pix->plane_fmt[0].bytesperline = p->width;
 pix->plane_fmt[0].sizeimage = p->width * p->height * 3 / 2;
}
static int video_get_fmt(struct file *file, void *priv, struct v4l2_format *f)
{
 video_format(f, ipe_ctx(file_to_v4l2_fh(file))->profile);
 return 0;
}
static const struct ipe_profile *video_requested_profile(struct ipe_context *ctx,
                                                        const struct v4l2_format *f)
{
 if (V4L2_TYPE_IS_OUTPUT(f->type))
  return video_nearest(f->fmt.pix_mp.width, f->fmt.pix_mp.height);
 return ctx->profile;
}
static int video_try_fmt(struct file *file, void *priv, struct v4l2_format *f)
{
 struct ipe_context *ctx = ipe_ctx(file_to_v4l2_fh(file));
 video_format(f, video_requested_profile(ctx, f));
 return 0;
}
static int video_set_fmt(struct file *file, void *priv, struct v4l2_format *f)
{
 struct ipe_context *ctx = ipe_ctx(file_to_v4l2_fh(file));
 if (vb2_is_busy(v4l2_m2m_get_src_vq(ctx->fh.m2m_ctx)) ||
     vb2_is_busy(v4l2_m2m_get_dst_vq(ctx->fh.m2m_ctx))) return -EBUSY;
 ctx->profile = video_requested_profile(ctx, f);
 video_format(f, ctx->profile);
 return 0;
}
static int video_selection(struct file *file, void *priv, struct v4l2_selection *sel)
{
 const struct ipe_profile *p = ipe_ctx(file_to_v4l2_fh(file))->profile;
 if (!V4L2_TYPE_IS_OUTPUT(sel->type) ||
     (sel->target != V4L2_SEL_TGT_CROP && sel->target != V4L2_SEL_TGT_CROP_DEFAULT &&
      sel->target != V4L2_SEL_TGT_CROP_BOUNDS)) return -EINVAL;
 sel->r = (struct v4l2_rect) {0, 0, p->width, p->height};
 return 0;
}
static const struct v4l2_ioctl_ops video_ioctl_ops = {
 .vidioc_g_selection = video_selection, .vidioc_s_selection = video_selection,
 .vidioc_querycap = video_querycap, .vidioc_enum_fmt_vid_cap = video_enum_fmt,
 .vidioc_enum_fmt_vid_out = video_enum_fmt, .vidioc_enum_framesizes = video_enum_size,
 .vidioc_g_fmt_vid_cap_mplane = video_get_fmt, .vidioc_g_fmt_vid_out_mplane = video_get_fmt,
 .vidioc_try_fmt_vid_cap_mplane = video_try_fmt, .vidioc_try_fmt_vid_out_mplane = video_try_fmt,
 .vidioc_s_fmt_vid_cap_mplane = video_set_fmt, .vidioc_s_fmt_vid_out_mplane = video_set_fmt,
 .vidioc_reqbufs = v4l2_m2m_ioctl_reqbufs, .vidioc_create_bufs = v4l2_m2m_ioctl_create_bufs,
 .vidioc_querybuf = v4l2_m2m_ioctl_querybuf, .vidioc_prepare_buf = v4l2_m2m_ioctl_prepare_buf,
 .vidioc_qbuf = v4l2_m2m_ioctl_qbuf, .vidioc_dqbuf = v4l2_m2m_ioctl_dqbuf,
 .vidioc_expbuf = v4l2_m2m_ioctl_expbuf, .vidioc_streamon = v4l2_m2m_ioctl_streamon,
 .vidioc_streamoff = v4l2_m2m_ioctl_streamoff,
};
static int video_open(struct file *file)
{
 struct ipe_video *v = video_drvdata(file);
 struct ipe_context *ctx = kzalloc(sizeof(*ctx), GFP_KERNEL);
 int ret;
 if (!ctx) return -ENOMEM;
 ctx->video = v; ctx->profile = &video_profiles[0];
 kthread_init_work(&ctx->work, video_worker);
 v4l2_fh_init(&ctx->fh, &v->vdev);
 mutex_lock(&v->lock);
 ctx->fh.m2m_ctx = v4l2_m2m_ctx_init(v->m2m, ctx, video_queue_init);
 if (IS_ERR(ctx->fh.m2m_ctx)) {
  ret = PTR_ERR(ctx->fh.m2m_ctx);
  mutex_unlock(&v->lock); v4l2_fh_exit(&ctx->fh); kfree(ctx);
  return ret;
 }
 file->private_data = &ctx->fh;
 v4l2_fh_add(&ctx->fh, file);
 mutex_unlock(&v->lock);
 return 0;
}
static int video_release(struct file *file)
{
 struct ipe_context *ctx = ipe_ctx(file->private_data);
 mutex_lock(&ctx->video->lock);
 v4l2_m2m_ctx_release(ctx->fh.m2m_ctx);
 kthread_flush_work(&ctx->work);
 v4l2_fh_del(&ctx->fh, file); v4l2_fh_exit(&ctx->fh);
 mutex_unlock(&ctx->video->lock);
 kfree(ctx);
 return 0;
}
static const struct v4l2_file_operations video_fops = {
 .owner = THIS_MODULE, .open = video_open, .release = video_release,
 .unlocked_ioctl = video_ioctl2, .poll = v4l2_m2m_fop_poll, .mmap = v4l2_m2m_fop_mmap,
};
static void video_destroy_worker(void *worker)
{
 kthread_destroy_worker(worker);
}
static int ipe_video_register(struct icp *c)
{
 struct ipe_video *v = devm_kzalloc(c->dev, sizeof(*v), GFP_KERNEL);
 int ret;
 unsigned long minimum = ULONG_MAX;
 int cpu;
 if (!v) return -ENOMEM;
 c->video = v; v->core = c;
 v->worker = kthread_create_worker(0, "liuqin-ipe");
 if (IS_ERR(v->worker)) return PTR_ERR(v->worker);
 ret = devm_add_action_or_reset(c->dev, video_destroy_worker, v->worker);
 if (ret) return ret;
 cpumask_copy(&v->worker_cpus, v->worker->task->cpus_ptr);
 for_each_cpu(cpu, &v->worker_cpus)
  minimum = min(minimum, topology_get_cpu_scale(cpu));
 for_each_cpu(cpu, &v->worker_cpus)
  if (topology_get_cpu_scale(cpu) > minimum)
   cpumask_set_cpu(cpu, &v->uhd_cpus);
 mutex_init(&v->lock);
 media_device_init(&v->media);
 v->media.dev = c->dev;
 strscpy(v->media.model, "qcom-ipe", sizeof(v->media.model));
 strscpy(v->media.bus_info, "platform:liuqin-ipe", sizeof(v->media.bus_info));
 v->v4l2.mdev = &v->media;
 strscpy(v->v4l2.name, "qcom-ipe", sizeof(v->v4l2.name));
 ret = v4l2_device_register(c->dev, &v->v4l2);
 if (ret) goto media;
 v->m2m = v4l2_m2m_init(&video_m2m_ops);
 if (IS_ERR(v->m2m)) { ret = PTR_ERR(v->m2m); goto v4l2; }
 strscpy(v->vdev.name, "qcom-ipe", sizeof(v->vdev.name));
 v->vdev.v4l2_dev = &v->v4l2; v->vdev.fops = &video_fops;
 v->vdev.ioctl_ops = &video_ioctl_ops; v->vdev.release = video_device_release_empty;
 v->vdev.lock = &v->lock; v->vdev.vfl_dir = VFL_DIR_M2M;
 v->vdev.device_caps = V4L2_CAP_VIDEO_M2M_MPLANE | V4L2_CAP_STREAMING;
 video_set_drvdata(&v->vdev, v);
 ret = video_register_device(&v->vdev, VFL_TYPE_VIDEO, -1);
 if (ret) goto m2m;
 ret = v4l2_m2m_register_media_controller(v->m2m, &v->vdev, MEDIA_ENT_F_PROC_VIDEO_PIXEL_FORMATTER);
 if (ret) goto video;
 ret = media_device_register(&v->media);
 if (ret) goto controller;
 dev_info(c->dev, "fixed-profile IPE video registered as /dev/video%d\n", v->vdev.num);
 return 0;
controller: v4l2_m2m_unregister_media_controller(v->m2m);
video: video_unregister_device(&v->vdev);
m2m: v4l2_m2m_release(v->m2m);
v4l2: v4l2_device_unregister(&v->v4l2);
media: media_device_cleanup(&v->media);
 return ret;
}
static void ipe_video_unregister(struct icp *c)
{
 struct ipe_video *v = c->video;
 media_device_unregister(&v->media);
 v4l2_m2m_unregister_media_controller(v->m2m);
 video_unregister_device(&v->vdev);
 v4l2_m2m_release(v->m2m);
 v4l2_device_unregister(&v->v4l2);
 media_device_cleanup(&v->media);
}
MODULE_IMPORT_NS("DMA_BUF");
