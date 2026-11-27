/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed-profile still-image stages. Userspace supplies only NV12 pixels. */
#include "snapshot-fixture.h"
#ifdef ICP_HDR_MAC_FRAME
#include "mac-fixture.h"
#endif
#define PRE_UBWC_BYTES 749568u
#define PRE_PD10_BYTES 49152u
#define PRE_OUTPUT_BYTES (PRE_UBWC_BYTES + PRE_PD10_BYTES)

/* Finish the sensors' delayed power-down and the CCI clock transition before
 * starting a new IPE session. The lens can remain open in a desktop session.
 * Runtime PM rejects a busy sensor or controller and preserves its votes. */
static int quiesce_idle_capture(struct icp *c, bool offline)
{
 static const char * const sensors[] = { "sony,imx596", "samsung,s5kjn1" };
 struct device *controller = NULL;
 int ret = 0;
 for (unsigned int i = 0; i < ARRAY_SIZE(sensors); i++) {
  struct device_node *np = of_find_compatible_node(NULL, NULL, sensors[i]);
  struct i2c_client *client = np ? of_find_i2c_device_by_node(np) : NULL;
  of_node_put(np);
  if (!client) {
   ret = -ENODEV;
   break;
  }
  if (!controller) controller = get_device(client->adapter->dev.parent);
  if (client->adapter->dev.parent != controller) ret = -EXDEV;
  else {
   ret = pm_runtime_suspend(&client->dev);
   if (!offline && (ret == -EAGAIN || ret == -EBUSY)) ret = 0;
  }
  put_device(&client->dev);
  if (ret < 0) break;
 }
 if (ret >= 0 && controller) {
  ret = pm_runtime_suspend(controller);
  if (!offline && (ret == -EAGAIN || ret == -EBUSY)) ret = 0;
 }
 put_device(controller);
 if (ret < 0)
  dev_warn(c->dev, "capture power handoff failed: %d\n", ret);
 return ret < 0 ? ret : 0;
}
static size_t snapshot_image_bytes(const struct icp *c)
{
 return c->native ? c->native->width * c->native->height * 3 / 2 : IMAGE_BYTES;
}
#ifdef ICP_HDR_NATIVE
#include "native-fixture.h"
/* Changing geometry invalidates all retained planes, including failed or
 * partially uploaded legs. Native pixels are released when the tool leaves. */
static void snapshot_geometry_set(struct icp *c, const struct snapshot_native *p)
{
 /* These host copies are never the DMA surfaces of the video session. */
 for (unsigned int i = 0; i < ARRAY_SIZE(c->legs); i++) { vfree(c->legs[i]); c->legs[i] = NULL; }
 vfree(c->input); c->input = NULL;
 vfree(c->pre_output); c->pre_output = NULL;
 vfree(c->p010); c->p010 = NULL;
 c->input_ready = c->pre_valid = c->p010_valid = false;
 c->input_written = c->nlegs = 0;
 vfree(c->live_inputs); c->live_inputs = NULL; c->live_written = 0;
 c->native = p;
}
static ssize_t snapshot_geometry_store(struct device *dev, struct device_attribute *attr,
 const char *buf, size_t count)
{
 struct icp *c = dev_get_drvdata(dev);
 const struct snapshot_native *p;
 if (sysfs_streq(buf, "2592x1952")) p = &front_native;
 else if (sysfs_streq(buf, "4080x3060")) p = &rear_native;
#ifdef ICP_HDR_WIDE
 else if (sysfs_streq(buf, "4080x2296")) p = &rear_wide_native;
 else if (sysfs_streq(buf, "3840x2160")) p = &rear_uhd_native;
#endif
 else if (sysfs_streq(buf, "640x480")) p = NULL;
 else return -EINVAL;
 mutex_lock(&c->lock);
 if (c->unsafe) { mutex_unlock(&c->lock); return -EIO; }
#ifdef ICP_HDR_WIDE
 if (c->hdr_owner) { mutex_unlock(&c->lock); return -EBUSY; }
#endif
 snapshot_geometry_set(c, p);
 mutex_unlock(&c->lock);
 return count;
}
static DEVICE_ATTR_WO(snapshot_geometry);
#endif

/* Only kernel-owned pages reach the firmware. A recording leg can be copied
 * and sealed with its expected CRC before it becomes the active PRE input. */
struct snapshot_leg {
 struct snapshot_surface *surface;
 const char __user *pixels;
 u32 crc;
};

static int snapshot_load_leg(const struct icp *c, struct snapshot_leg *leg)
{
 size_t bytes = snapshot_image_bytes(c);
 if (!leg->surface->data || leg->surface->mapped != PAGE_ALIGN(bytes)) return -EINVAL;
 if (copy_from_user(leg->surface->data, leg->pixels, bytes)) return -EFAULT;
 leg->crc = crc32_le(0, leg->surface->data, bytes);
 return 0;
}

static int snapshot_process(struct icp *c, struct snapshot_leg *following)
{
 u32 cmd[] = {40, 0x101000e, 6, 0x59e, 0, 0, 0, 1, c->handle, 0x804000};
 u32 reply[128];
 int ret, copied = 0;
 dma_wmb();
 ret = send(c, cmd, ARRAY_SIZE(cmd)); if (ret) return ret;
 /* Never return a copy fault with an outstanding frame command. */
 if (following) copied = snapshot_load_leg(c, following);
 ret = receive(c, 0x102000e, reply, ARRAY_SIZE(reply)); if (ret < 0) return ret;
 if (ret < 9 || reply[2] != 6 || reply[3] != cmd[3] || reply[4]) return -EPROTO;
 c->frame_error = reply[7]; c->frame_rc = reply[8];
 if (c->frame_error || c->frame_rc || log_ipe_ack(c))
  dev_info(c->dev, "snapshot stage=%d FRAME_PROCESS error=%u rc=%u\n", c->snapshot_probe, c->frame_error, c->frame_rc);
 if (c->frame_error || c->frame_rc) return -EREMOTEIO;
 dma_rmb();
 return copied;
}

static const unsigned int mac_inputs[] = {IPE_INPUT_FULL, IPE_INPUT2_FULL, IPE_INPUT_FULL_REF};

/* next contains the read-only inputs of the following stage, with a
 * separate guarded producer mapping. Check their contents after each ACK. */
static int snapshot_to_internal(struct icp *c, u8 *shared,
                                struct snapshot_surface *next, const u32 *leg_crc,
                                const struct snapshot_leg *input, struct snapshot_leg *following)
{
 struct snapshot_surface *s = c->snapshot_slots;
 bool two = c->snapshot_action == 20;
 bool control = c->snapshot_action >= 19;
 u32 sizes[2], meta[2];
 unsigned int i;
 size_t tp, pd, total;
 const struct snapshot_native *n = c->native;
 struct snapshot_surface *out = next ? next : &s[IPE_OUTPUT_FULL_REF];
 int ret;
 if ((!n && c->scratch_size != (control ? 254352 : 4496)) || !c->scratch_size || c->cdm_size != 524288) return -EPROTO;
 if (snapshot_layout((u32 *)shared + 24 * 11, sizes, meta) != 1) return -EINVAL;
 pd = sizes[0];
 if (snapshot_layout((u32 *)shared + 24 * 10, sizes, meta) != 2) return -EINVAL;
 tp = sizes[0] + sizes[1];
 total = tp + pd;
 if (!s[10].data || !s[11].data || s[10].mapped != PAGE_ALIGN(tp) || s[11].mapped != PAGE_ALIGN(pd))
  return -EINVAL;
 if (!next) {
  if (!c->pre_output) c->pre_output = vzalloc(total);
  if (!c->pre_output) return -ENOMEM;
 }
 BUILD_BUG_ON(sizeof(pre_frame) > 0xc000 || sizeof(fusion_off_frame) > 0xc000 ||
              sizeof(pre_iq) != 6180 || sizeof(fusion_off_iq) != 6180 || sizeof(fusion_two_off_iq) != 6180 ||
              sizeof(pre_stripes) > 0x40000 || sizeof(fusion_off_stripes) > 0x40000 ||
              sizeof(pre_registers) > PAGE_SIZE || sizeof(pre_dmi) > PAGE_SIZE || sizeof(pre_lut) > PAGE_SIZE);
 if (control) {
#ifdef ICP_HDR_MAC_FRAME
  if (c->snapshot_action == 21 || c->snapshot_action == 23) {
   BUILD_BUG_ON(sizeof(mac_frame) != sizeof(fusion_off_frame) || sizeof(mac_iq) != 6180 ||
                sizeof(mac_stripes) > 0x40000 || sizeof(mac_front_registers) != 256 || sizeof(mac_rear_registers) != 256);
   memcpy(shared + 0x10000, n ? n->mac_iq : mac_iq, sizeof(mac_iq));
   memcpy(shared + 0x20000, n ? n->mac_stripes : mac_stripes, n ? n->mac_length : sizeof(mac_stripes));
   memcpy(shared + 0x63000, n && n->mac_registers ? n->mac_registers :
          c->snapshot_action == 23 ? mac_front_registers : mac_rear_registers, 256);
  } else
#endif
  {
   memcpy(shared + 0x10000, two ? fusion_two_off_iq : fusion_off_iq, sizeof(fusion_off_iq));
   memcpy(shared + 0x20000, fusion_off_stripes, sizeof(fusion_off_stripes));
  }
  for (i = 0; i < ARRAY_SIZE(mac_inputs); i++) {
   unsigned int p = mac_inputs[i];
   if (two && p == IPE_INPUT2_FULL) continue;
   if ((!leg_crc && !c->legs[i]) || !s[p].data || s[p].mapped != tp ||
       !s[p+1].data || s[p+1].mapped != pd) return -EINVAL;
   if (!leg_crc) {
    memcpy(s[p].data, c->legs[i], tp);
    memcpy(s[p+1].data, c->legs[i] + tp, pd);
   }
  }
 } else {
  if ((!input && (!c->input_ready || !c->input)) ||
      !s[0].data || s[0].mapped != PAGE_ALIGN(snapshot_image_bytes(c))) return -ENODATA;
  memcpy(shared + 0x10000, n ? n->pre_iq : pre_iq, sizeof(pre_iq));
  memcpy(shared + 0x20000, n ? n->pre_stripes : pre_stripes, n ? n->pre_length : sizeof(pre_stripes));
  if (input) c->input_crc = input->crc;
  else {
   memcpy(s[0].data, c->input, snapshot_image_bytes(c));
   c->input_crc = crc32_le(0, s[0].data, snapshot_image_bytes(c));
  }
 }
 memcpy(shared + 0x60000, pre_registers, sizeof(pre_registers));
 memcpy(shared + 0x61000, pre_dmi, sizeof(pre_dmi));
 memcpy(shared + 0x62000, pre_lut, sizeof(pre_lut));
 memset(out[0].data, 0xa5, tp);
 memset(out[1].data, 0xa5, pd);
 ret = snapshot_process(c, following); if (ret) return ret;
 if (control) {
  for (i = 0; i < ARRAY_SIZE(mac_inputs); i++) {
   unsigned int p = mac_inputs[i];
   if (two && p == IPE_INPUT2_FULL) continue;
   if (leg_crc) {
    if (crc32_le(crc32_le(0, s[p].data, tp), s[p+1].data, pd) != leg_crc[i])
     c->input_changed = 1;
   } else if (memcmp(s[p].data, c->legs[i], tp) ||
              memcmp(s[p+1].data, c->legs[i] + tp, pd)) c->input_changed = 1;
  }
 } else if (input) {
  c->input_changed = crc32_le(0, input->surface->data, snapshot_image_bytes(c)) != c->input_crc;
 } else {
  c->input_changed = memcmp(s[0].data, c->input, snapshot_image_bytes(c)) != 0;
 }
 if (c->input_changed) return -EILSEQ;
 for (i = 0; i < 2; i++) {
  u8 *plane = out[0].data + (i ? sizes[0] : 0);
  if (!memchr_inv(plane, 0xa5, meta[i]) ||
      !memchr_inv(plane + meta[i], 0xa5, sizes[i] - meta[i])) return -ENODATA;
 }
 if (!memchr_inv(out[1].data, 0xa5, pd)) return -ENODATA;
 if (next) {
  c->output_crc = crc32_le(crc32_le(0, out[0].data, tp), out[1].data, pd);
 } else {
  memcpy(c->pre_output, s[10].data, tp);
  memcpy(c->pre_output + tp, s[11].data, pd);
  c->output_crc = crc32_le(0, c->pre_output, total);
 }
 c->completed_frames++;
 c->frame_done = true;
 return 0;
}

static int snapshot_post_frame(struct icp *c, u8 *shared, bool staged)
{
 struct snapshot_surface *s = c->snapshot_slots;
 size_t bytes = snapshot_image_bytes(c);
 u32 sizes[2], meta[2];
 size_t tp, pd;
 void **output = &c->output;
 bool direct_output = false;
 const u8 *stripes = post_stripes, *iq = post_iq;
 size_t stripe_bytes = sizeof(post_stripes);
 u32 crc;
 int ret;
#ifdef ICP_HDR_MAC_FRAME
 if (c->snapshot_action == 22) {
  bytes *= 2;
  output = &c->p010;
#ifdef ICP_HDR_WIDE
  direct_output = !!c->hdr_owner;
#endif
  stripes = c->native ? c->native->p010_stripes : p010_stripes;
  iq = c->native ? c->native->p010_iq : p010_iq;
  BUILD_BUG_ON(sizeof(p010_iq) != sizeof(post_iq));
  stripe_bytes = c->native ? c->native->p010_length : sizeof(p010_stripes);
  BUILD_BUG_ON(sizeof(p010_stripes) > 0x40000);
 }
#endif
 if (snapshot_layout((u32 *)shared + 24, sizes, meta) != 1) return -EINVAL;
 pd = sizes[0];
 if (snapshot_layout((u32 *)shared, sizes, meta) != 2) return -EINVAL;
 tp = sizes[0] + sizes[1];
 if ((!c->native && c->scratch_size != 197008) || !c->scratch_size || c->cdm_size != 524288 || (!staged && !c->pre_output)) return -EPROTO;
 if (!s[0].data || s[0].mapped != tp || !s[1].data || s[1].mapped != PAGE_ALIGN(pd) ||
     !s[8].data || s[8].mapped != PAGE_ALIGN(bytes)) return -EINVAL;
 if (!direct_output) {
  if (!*output) *output = vzalloc(bytes);
  if (!*output) return -ENOMEM;
 }
 BUILD_BUG_ON(sizeof(post_frame) > 0xc000 || sizeof(post_iq) != 6180 || sizeof(post_stripes) > 0x40000);
 memcpy(shared + 0x10000, iq, sizeof(post_iq));
 memcpy(shared + 0x20000, stripes, stripe_bytes);
 if (staged) crc = c->output_crc;
 else {
  memcpy(s[0].data, c->pre_output, tp);
  memcpy(s[1].data, c->pre_output + tp, pd);
  crc = crc32_le(0, c->pre_output, tp + pd);
 }
 memset(s[8].data, 0xa5, s[8].mapped);
 ret = snapshot_process(c, NULL); if (ret) return ret;
 c->input_changed = crc32_le(crc32_le(0, s[0].data, tp), s[1].data, pd) != crc;
 c->guard_changed = !!memchr_inv(s[8].data + bytes, 0xa5, s[8].mapped - bytes);
 if (c->input_changed || c->guard_changed) return -EOVERFLOW;
 if (!memchr_inv(s[8].data, 0xa5, bytes * 2 / 3) ||
     !memchr_inv(s[8].data + bytes * 2 / 3, 0xa5, bytes / 3)) return -ENODATA;
 if (!direct_output) memcpy(*output, s[8].data, bytes);
 c->output_crc = crc32_le(0, s[8].data, bytes);
 c->completed_frames++;
 c->frame_done = true;
 return 0;
}

static ssize_t pre_output_read(struct file *file, struct kobject *kobj,
 const struct bin_attribute *attr, char *buf, loff_t off, size_t count)
{
 struct icp *c = dev_get_drvdata(kobj_to_dev(kobj));
 ssize_t n;
 mutex_lock(&c->lock);
 n = !c->native && c->pre_valid ? memory_read_from_buffer(buf, count, &off, c->pre_output, PRE_OUTPUT_BYTES) : -ENODATA;
 mutex_unlock(&c->lock);
 return n;
}
static const struct bin_attribute pre_output_attr = {
 .attr = { .name = "snapshot_pre_output", .mode = 0400 },
 .size = PRE_OUTPUT_BYTES, .read = pre_output_read,
};
#ifdef ICP_HDR_MAC_FRAME
static const void *snapshot_p010_data(struct icp *c)
{
#ifdef ICP_HDR_WIDE
 if (c->hdr_owner) return c->saved_slots[2][IPE_OUTPUT_DISPLAY].data;
#endif
 return c->p010;
}
static ssize_t p010_output_read(struct file *file, struct kobject *kobj,
 const struct bin_attribute *attr, char *buf, loff_t off, size_t count)
{
 struct icp *c = dev_get_drvdata(kobj_to_dev(kobj));
 ssize_t n;
 mutex_lock(&c->lock);
 n = c->p010_valid ? memory_read_from_buffer(buf, count, &off, snapshot_p010_data(c), snapshot_image_bytes(c) * 2) : -ENODATA;
 mutex_unlock(&c->lock);
 return n;
}
static const struct bin_attribute p010_output_attr = {
 .attr = { .name = "snapshot_p010", .mode = 0400 },
 #ifdef ICP_HDR_NATIVE
 .size = 4080 * 3060 * 3,
#else
 .size = IMAGE_BYTES * 2,
#endif
 .read = p010_output_read,
};
#endif

#ifdef ICP_HDR_NATIVE
/* Once every pass has its input mappings, IPE writes UBWC and PD10
 * directly into those same physical pages.
 * Each producer has its own aperture beyond the ordinary snapshot ports.
 * Consumer input IOVAs retain read-only permissions. */
static int live_forward(struct icp *c, u8 *shared, unsigned int producer,
                        struct snapshot_surface *next)
{
 u32 base = snapshot_base(IPE_SCRATCH + 1 + producer);
 u32 offset = 0;
 const u8 *consumer = producer == 3 ? c->native->p010_cfg :
                       c->native->mac_cfg + 96 * mac_inputs[producer];
 for (unsigned int i = 0; i < 2; i++) {
  unsigned int port = IPE_OUTPUT_FULL_REF + i;
  u32 sizes[2] = {}, meta[2] = {};
  const u32 *desc = (u32 *)shared + 24 * port;
  int planes = snapshot_layout(desc, sizes, meta);
  struct snapshot_surface *alias = &c->producer_alias[producer][i];
  u32 bytes = sizes[0] + sizes[1];
  if (memcmp(desc, consumer + 96*i, 3 * sizeof(u32)) ||
      memcmp(desc + 16, consumer + 96*i + 64, 8 * sizeof(u32)) ||
      planes != 2 - i || !next[i].data || bytes != next[i].mapped ||
      offset > SNAP_SLOT_SIZE - 2 * PAGE_SIZE ||
      bytes > SNAP_SLOT_SIZE - 2 * PAGE_SIZE - offset) return -EINVAL;
  if (!alias->data) {
   int ret = snapshot_map(c->domain, alias, base + offset, bytes,
                          IOMMU_READ | IOMMU_WRITE | IOMMU_CACHE, next[i].data);
   if (ret) return ret;
  } else if (alias->data != next[i].data || alias->mapped != bytes ||
             alias->iova != base + offset) return -EINVAL;
  snapshot_address((u32 *)(shared + 0x4000), port, alias->iova, planes, sizes, meta);
  offset += bytes + PAGE_SIZE;
 }
 return 0;
}
/* POST's input has no owner during the three PRE commands: the preceding
 * POST has completed, and MAC will initialize it before the next POST.
 * Borrow its first NV12-sized pages under a tight read-only mapping. */
static int live_pre_frames(struct icp *c, u8 *shared, u32 *leg_crc, const char __user *pixels)
{
 size_t bytes = snapshot_image_bytes(c);
 struct snapshot_surface *base = &c->snapshot_slots[IPE_INPUT_FULL];
 struct snapshot_surface *post = &c->saved_slots[2][IPE_INPUT_FULL];
 struct snapshot_leg inputs[3];
 void *original_input = c->input;
 u32 sizes[2], meta[2];
 bool overlap = pixels && leg_crc && c->width == 3840 && c->height == 2160 &&
                post->data && post->mapped >= PAGE_ALIGN(bytes);
 int ret = 0;
 if (overlap) {
  if (*(u32 *)shared != 20 || snapshot_layout((u32 *)shared, sizes, meta) != 2 ||
      meta[0] || meta[1] || sizes[0] != c->width * c->height ||
      sizes[1] != bytes - sizes[0]) return -EINVAL;
  if (!c->pre_spare.data) {
   ret = snapshot_map(c->domain, &c->pre_spare, snapshot_base(IPE_SCRATCH + 1 + ARRAY_SIZE(c->producer_alias)),
                      bytes, IOMMU_READ | IOMMU_CACHE, post->data);
   if (ret) return ret;
  } else if (c->pre_spare.data != post->data || c->pre_spare.mapped != PAGE_ALIGN(bytes))
   return -EINVAL;
 }
 if (pixels) {
  for (unsigned int leg = 0; leg < ARRAY_SIZE(inputs); leg++) {
   inputs[leg].pixels = pixels + leg * bytes;
   inputs[leg].surface = (overlap && leg == 1) ? &c->pre_spare : base;
  }
 }
 for (unsigned int leg = 0; leg < ARRAY_SIZE(inputs); leg++) {
  struct snapshot_surface *next = leg_crc ? &c->saved_slots[1][mac_inputs[leg]] : NULL;
  if (next) {
   ret = live_forward(c, shared, leg, next);
   if (ret) break;
  }
  if (!pixels) {
   c->input = c->live_inputs + leg * bytes;
   c->input_ready = true;
  } else {
   if (!overlap || leg == 0) {
    ret = snapshot_load_leg(c, &inputs[leg]);
    if (ret) break;
   }
   if (overlap)
    snapshot_address((u32 *)(shared + 0x4000), IPE_INPUT_FULL, inputs[leg].surface->iova,
                     2, sizes, meta);
  }
  ret = snapshot_to_internal(c, shared, next, NULL, pixels ? &inputs[leg] : NULL,
                             overlap && leg + 1 < ARRAY_SIZE(inputs) ? &inputs[leg + 1] : NULL);
  if (ret) break;
  if (leg_crc) leg_crc[leg] = c->output_crc;
  else swap(c->pre_output, c->legs[leg]);
 }
 c->input = original_input;
 c->input_ready = false;
 return ret;
}
/* Immutable per-handle CONFIG_IO payloads fit between the last fixed table
 * at 0x863000 and the firmware CDM output at 0x880000. The firmware receives
 * one FRAME_PROCESS at a time, so all handles can use the same frame packet. */
#define HDR_STAGE_CONFIG_OFFSET(pass) (0x64000U + (pass) * SZ_16K)

/* A group owns all three sets of mappings until authenticated shutdown.
 * Pack stages independently inside each port aperture with an unmapped guard.
 * Reuse the pre-processing handle for all three input frames. */
static int live_setup_stage(struct icp *c, unsigned int pass, int mac, bool booted, bool retain)
{
 static const int probes[] = { SNAP_PRE, SNAP_THREE, SNAP_POST_P010 };
 struct hdr_stage_context *ctx = retain ? &c->hdr_contexts[pass] : NULL;
 u32 cfg_offset = ctx ? HDR_STAGE_CONFIG_OFFSET(pass) : 0;
 u8 *shared;
 u32 offsets[IPE_SCRATCH + 1] = {};
 int ret;
 c->snapshot_probe = probes[pass];
 c->snapshot_action = pass == 0 ? 16 : pass == 1 ? mac : 22;
 BUILD_BUG_ON(ARRAY_SIZE(probes) != ARRAY_SIZE(c->hdr_contexts) ||
              IPE_CONFIG_BYTES > SZ_16K ||
              HDR_STAGE_CONFIG_OFFSET(ARRAY_SIZE(probes) - 1) + IPE_CONFIG_BYTES > 0x80000);
 if (ctx && ctx->handle) {
  c->handle = ctx->handle;
  c->scratch_size = ctx->scratch_size;
  c->cdm_size = ctx->cdm_size;
 } else {
  ret = (booted || pass) ? create_ipe(c, 5, cfg_offset) : session_start(c, 5, cfg_offset);
  if (ret) return ret;
  if (ctx) {
   ctx->handle = c->handle;
   ctx->scratch_size = c->scratch_size;
   ctx->cdm_size = c->cdm_size;
  }
 }
 shared = c->heap + 6 * SZ_1M;
 /* The host plane-layout helpers use the current stage's descriptors. */
 if (ctx) memcpy(shared, shared + cfg_offset, IPE_CONFIG_BYTES);
 if (!pass) memcpy(shared + 0x4000, pre_frame, sizeof(pre_frame));
 else if (pass == 1) memcpy(shared + 0x4000, mac_frame, sizeof(mac_frame));
 else memcpy(shared + 0x4000, post_frame, sizeof(post_frame));
 for (unsigned int prev = 0; prev < pass; prev++)
  for (unsigned int p = 0; p <= IPE_SCRATCH; p++)
   if (c->saved_slots[prev][p].mapped)
    offsets[p] += c->saved_slots[prev][p].mapped + PAGE_SIZE;
 return snapshot_buffers(c->domain, c->saved_slots[pass], (u32 *)shared, (u32 *)(shared+0x4000),
                         c->scratch_size, offsets);
}

static int live_stage(struct icp *c, unsigned int pass, int mac, bool booted, u32 *leg_crc,
                      const char __user *pixels)
{
 bool retain = pixels && c->width == 3840 && c->height == 2160;
 int ret = live_setup_stage(c, pass, mac, booted, retain);
 u8 *shared;
 if (ret) return ret;
 shared = c->heap + 6 * SZ_1M;
 memcpy(c->snapshot_slots, c->saved_slots[pass], sizeof(c->snapshot_slots));
 memset(c->saved_slots[pass], 0, sizeof(c->snapshot_slots));
 if (!pass) ret = live_pre_frames(c, shared, leg_crc, pixels);
 else if (pass == 1) {
  struct snapshot_surface *next = leg_crc ? c->saved_slots[2] : NULL;
  if (next) ret = live_forward(c, shared, 3, next);
  if (!ret) ret = snapshot_to_internal(c, shared, next, leg_crc, NULL, NULL);
 }
 else ret = snapshot_post_frame(c, shared, !!leg_crc);
 if (ret) return ret;
 if (!retain) {
  ret = session_destroy(c);
  if (ret) return ret;
 }
 memcpy(c->saved_slots[pass], c->snapshot_slots, sizeof(c->snapshot_slots));
 memset(c->snapshot_slots, 0, sizeof(c->snapshot_slots));
 return 0;
}
static int live_group(struct icp *c, int mac, bool booted, const char __user *pixels)
{
 int ret = 0;
 u32 leg_crc[3];
 bool staged = booted && c->saved_slots[2][IPE_INPUT_FULL].data;
 c->live_written = c->nlegs = c->input_written = 0;
 c->input_ready = c->output_valid = c->pre_valid = c->p010_valid = false;
 c->temporal = c->frame_sent = c->frame_done = false;
 c->completed_frames = c->frame_error = c->frame_rc = c->input_changed = c->guard_changed = 0;
 c->width = c->native ? c->native->width : 640;
 c->height = c->native ? c->native->height : 480;
 for (unsigned int pass = 0; pass < 3 && !ret; pass++)
  ret = live_stage(c, pass, mac, booted, staged ? leg_crc : NULL, pixels);
 if (!ret && c->completed_frames != 5) ret = -EPROTO;
 c->snapshot_action = c->snapshot_probe = 0;
 c->p010_valid = !ret;
 return ret;
}

static ssize_t video_fuse_store(struct device *dev, struct device_attribute *attr, const char *buf, size_t count)
{
 struct icp *c = dev_get_drvdata(dev);
 int ret = 0, clean, mac;
 if (sysfs_streq(buf,"rear")) mac = 21;
 else if (sysfs_streq(buf,"front")) mac = 23;
 else return -EINVAL;
 mutex_lock(&c->lock);
 if (c->unsafe || c->streaming) { ret = c->unsafe ? -EIO : -EBUSY; goto out; }
 if (c->native && (c->native == &front_native) != (mac == 23)) { ret = -EINVAL; goto out; }
 if (c->live_written != 3 * snapshot_image_bytes(c)) { ret = -ENODATA; goto out; }
 if (!try_module_get(THIS_MODULE)) { ret = -ENODEV; goto out; }
 ret = live_group(c, mac, false, NULL);
 clean = cleanup(c);
 if (!ret) ret = clean;
 c->result = ret;
 c->p010_valid = !ret;
 if (!c->unsafe) module_put(THIS_MODULE);
out:
 mutex_unlock(&c->lock);
 return ret ? ret : count;
}
static DEVICE_ATTR_WO(video_fuse);
static ssize_t live_input_write(struct file *file, struct kobject *kobj, const struct bin_attribute *attr,
 char *buf, loff_t off, size_t count)
{
 struct icp *c = dev_get_drvdata(kobj_to_dev(kobj));
 ssize_t ret = count;
 mutex_lock(&c->lock);
 if (c->unsafe || c->streaming) { ret = c->unsafe ? -EIO : -EBUSY; goto out; }
 if (!off) c->live_written = 0;
 if (off != c->live_written || count > 3 * snapshot_image_bytes(c) - c->live_written) {
  c->live_written = 0; ret = -EINVAL; goto out;
 }
 if (!c->live_inputs) c->live_inputs = vzalloc(3 * snapshot_image_bytes(c));
 if (!c->live_inputs) { ret = -ENOMEM; goto out; }
 memcpy(c->live_inputs + off, buf, count);
 c->live_written += count;
out:
 mutex_unlock(&c->lock);
 return ret;
}
static const struct bin_attribute live_input_attr = {
 .attr = { .name = "video_inputs", .mode = 0200 }, .size = 4080 * 3060 * 9 / 2, .write = live_input_write,
};
#endif

#ifdef ICP_HDR_WIDE
/* One fd owns a rear wide recording. A write consumes exactly three packed
 * legs of one supported size. Its matching read returns one tight P010 image.
 * Device removal serializes with I/O and release via the parent device lock. */
static int hdr_record_open(struct inode *inode, struct file *file)
{
 struct miscdevice *misc = file->private_data;
 struct icp *c = container_of(misc, struct icp, hdr_recorder);
 int ret = 0;
 if ((file->f_mode & (FMODE_READ | FMODE_WRITE)) != (FMODE_READ | FMODE_WRITE))
  return -EINVAL;
 mutex_lock(&c->lock);
 if (c->unsafe || c->streaming) {
  ret = c->unsafe ? -EIO : -EBUSY;
 } else {
  c->streaming = true;
  c->hdr_owner = file;
  snapshot_geometry_set(c, NULL);
  c->frame_sent = false;
  file->private_data = get_device(c->dev);
 }
 mutex_unlock(&c->lock);
 return ret;
}

static int hdr_record_stop(struct icp *c, bool orderly)
{
 int close_error = 0, ret;
 /* An earlier failed command can have an ACK outstanding. In that case
  * stop the firmware before touching any handle's command or image pages. */
 if (orderly && c->pas) {
  for (int i = ARRAY_SIZE(c->hdr_contexts) - 1; i >= 0; i--) {
   if (!c->hdr_contexts[i].handle) continue;
   c->handle = c->hdr_contexts[i].handle;
   close_error = session_destroy(c);
   if (close_error) break;
  }
 }
 ret = cleanup(c);
 if (!c->unsafe) memset(c->hdr_contexts, 0, sizeof(c->hdr_contexts));
 if (!ret) ret = close_error;
 if (ret) c->result = ret;
 if (ret && c->unsafe) __module_get(THIS_MODULE);
 c->hdr_owner = NULL;
 c->streaming = false;
 c->handle = 0;
 snapshot_geometry_set(c, NULL);
 return ret;
}

static int hdr_record_release(struct inode *inode, struct file *file)
{
 struct device *dev = file->private_data;
 struct icp *c;
 device_lock(dev);
 c = dev_get_drvdata(dev);
 if (c) {
  mutex_lock(&c->lock);
  if (c->hdr_owner == file) hdr_record_stop(c, true);
  mutex_unlock(&c->lock);
 }
 device_unlock(dev);
 put_device(dev);
 return 0;
}

/* Set up the three 4K contexts before capture claims its first bracket. No
 * FRAME_PROCESS is sent and read() only becomes valid after a real write. */
#define LIUQIN_HDR_PREPARE_UHD _IO('H', 0x70)
static long hdr_record_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
 struct device *dev = file->private_data;
 struct icp *c;
 int ret = -ENODEV;
 if (cmd != LIUQIN_HDR_PREPARE_UHD) return -ENOTTY;
 device_lock(dev);
 c = dev_get_drvdata(dev);
 if (!c) goto out;
 mutex_lock(&c->lock);
 if (c->hdr_owner != file) ret = -EIO;
 else if (c->p010_valid) ret = -EBUSY;
 else if (c->native && c->native != &rear_uhd_native) ret = -EINVAL;
 else if (c->pas) ret = 0;
 else {
  c->native = &rear_uhd_native;
  c->width = c->native->width;
  c->height = c->native->height;
  c->completed_frames = c->frame_error = c->frame_rc = c->input_changed = c->guard_changed = 0;
  c->frame_done = false;
  ret = 0;
  for (unsigned int pass = 0; pass < ARRAY_SIZE(c->hdr_contexts) && !ret; pass++)
   ret = live_setup_stage(c, pass, 21, false, true);
  c->snapshot_action = c->snapshot_probe = 0;
  c->result = ret;
  if (ret) hdr_record_stop(c, false);
 }
 mutex_unlock(&c->lock);
out:
 device_unlock(dev);
 return ret;
}

static ssize_t hdr_record_write(struct file *file, const char __user *buf, size_t count, loff_t *pos)
{
 struct device *dev = file->private_data;
 const struct snapshot_native *p;
 struct icp *c;
 int ret = -ENODEV;
 if (count == 4080UL * 2296 * 9 / 2) p = &rear_wide_native;
 else if (count == 3840UL * 2160 * 9 / 2) p = &rear_uhd_native;
 else return -EINVAL;
 device_lock(dev);
 c = dev_get_drvdata(dev);
 if (!c) goto out;
 mutex_lock(&c->lock);
 if (c->hdr_owner != file) { ret = -EIO; goto unlock; }
 if (c->p010_valid) { ret = -EBUSY; goto unlock; }
 if (c->native && c->native != p) { ret = -EINVAL; goto unlock; }
 c->native = p;
 ret = live_group(c, 21, c->pas, buf);
 c->result = ret;
 if (ret) {
  int clean = hdr_record_stop(c, false);
  /* A short user mapping may fault on the second or third leg. Discard
   * the whole group before allowing a corrected write on the same fd. */
  if (ret == -EFAULT && !clean && !c->unsafe) {
   c->hdr_owner = file;
   c->streaming = true;
   c->native = p;
  }
 }
unlock:
 mutex_unlock(&c->lock);
out:
 device_unlock(dev);
 return ret ? ret : count;
}

static ssize_t hdr_record_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
 struct device *dev = file->private_data;
 struct icp *c;
 int ret = -ENODEV;
 device_lock(dev);
 c = dev_get_drvdata(dev);
 if (!c) goto out;
 mutex_lock(&c->lock);
 if (c->hdr_owner != file || !c->p010_valid) ret = -ENODATA;
 else if (count != 2 * snapshot_image_bytes(c)) ret = -EINVAL;
 else if (copy_to_user(buf, snapshot_p010_data(c), count)) ret = -EFAULT;
 else { c->p010_valid = false; ret = 0; }
 mutex_unlock(&c->lock);
out:
 device_unlock(dev);
 return ret ? ret : count;
}

static const struct file_operations hdr_record_ops = {
 .owner = THIS_MODULE, .open = hdr_record_open, .release = hdr_record_release,
 .write = hdr_record_write, .read = hdr_record_read,
 .unlocked_ioctl = hdr_record_ioctl,
#ifdef CONFIG_COMPAT
 .compat_ioctl = hdr_record_ioctl,
#endif
};
#endif
