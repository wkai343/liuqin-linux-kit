// SPDX-License-Identifier: GPL-2.0-only
/* Bounded SM8475 ICP transport and optional fixed-profile V4L2 bringup. */
#include <linux/clk.h>
#include <linux/delay.h>
#include <linux/firmware.h>
#include <linux/firmware/qcom/qcom_pas.h>
#include <linux/interconnect.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/iommu.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/of_address.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/pm_domain.h>
#include <linux/soc/qcom/mdt_loader.h>
#include <linux/vmalloc.h>
#include <linux/crc32.h>
#ifdef ICP_HDR_WIDE
#include <linux/miscdevice.h>
#endif

#ifdef ICP_HDR_STILL_FRAME
#include <linux/i2c.h>
#include <linux/of_platform.h>
#endif
#ifdef ICP_HDR_CONFIG_PROBE
#include "hdr-config.h"
#include "hdr-dma.h"
#endif
#ifdef ICP_FRAME_FIXTURE
#include "frame-fixture.h"
#endif

#define HEAP_BASE 0x13600000u
#define HEAP_SIZE (7 * SZ_1M)
#define GP(n) (0x20 + 4 * (n))
#define PAS_ID 33
#define IMAGE_BYTES (640 * 480 * 3 / 2)
#define IPE_CONFIG_BYTES 8336
#define INPUT_OFFSET 0x10000
#define OUTPUT_OFFSET 0x88000
#define SCRATCH_OFFSET 0xf9000
/* Qualcomm HFI uses a 64-byte lane per field plus a final 60-byte pad. */
struct lane { u32 pad[15]; u32 value; };
struct qhdr { struct lane field[14]; u32 tail[15]; };
struct qtable { u32 version, size, offset, stride, count, active; struct qhdr q[4]; };
enum { Q_STATUS, Q_START, Q_TYPE, Q_SIZE, Q_PACKET, Q_DROPS, Q_RXWM, Q_TXWM, Q_RXREQ, Q_TXREQ,
       Q_RXIRQ, Q_TXIRQ, Q_READ, Q_WRITE };
struct ipe_video;
struct hdr_stage_context {
 u32 handle, scratch_size, cdm_size;
};
struct icp {
 wait_queue_head_t replies;
 struct device *dev, *pd, *bps, *ipe;
 struct qcom_pas_context *pas_ctx;
 void __iomem *csr, *cirq, *wd;
 struct clk_bulk_data *clocks;
 int nclocks, irq;
 struct icc_path *ahb, *sf;
 struct iommu_domain *domain;
 struct page **pages;
 void *heap;
 unsigned int npages, mapped, config_mapped, image_mapped;
 bool attached, qdss, powered, pas, unsafe;
 struct mutex lock;
 struct ipe_video *video;
 u32 width, height;
 bool streaming, temporal;
#ifdef ICP_HDR_CONFIG_PROBE
 int snapshot_probe;
 const struct snapshot_native *native;
#ifdef ICP_HDR_STILL_FRAME
 int snapshot_action;
 unsigned int nlegs;
 bool pre_valid;
#ifdef ICP_HDR_MAC_FRAME
 bool p010_valid;
 void *p010;
#endif
 void *pre_output, *legs[3];
#ifdef ICP_HDR_NATIVE
 void *live_inputs;
 size_t live_written;
#ifdef ICP_HDR_WIDE
 struct miscdevice hdr_recorder;
 struct file *hdr_owner;
#endif
 struct snapshot_surface saved_slots[3][IPE_SCRATCH + 1];
 struct hdr_stage_context hdr_contexts[3];
 struct snapshot_surface producer_alias[4][2];
 struct snapshot_surface pre_spare;
#endif
#endif
 struct snapshot_surface snapshot_slots[IPE_SCRATCH + 1];
#endif
 u32 interrupts, irq_status, version, handle, messages;
 int result;
 const char *stage;
 struct qtable *qt;
 void *output;
 void *input;
 size_t input_written;
 bool input_ready, user_input;
 bool frame_sent, frame_done, output_valid;
 u32 scratch_size, cdm_size, frame_error, frame_rc, output_crc, changed;
 u32 input_crc, input_changed, guard_changed, completed_frames;
};
static irqreturn_t icp_irq(int irq, void *data)
{
 struct icp *c = data;
 u32 status = readl(c->cirq + 0xc);
 writel(status, c->cirq + 4);
 writel(BIT(1), c->cirq + 0x10);
 c->irq_status |= status;
 c->interrupts++;
 wake_up(&c->replies);
 return IRQ_HANDLED;
}
static void memory_free(struct icp *c)
{
 unsigned int i;
#ifdef ICP_HDR_CONFIG_PROBE
#ifdef ICP_HDR_NATIVE
 snapshot_unmap(c->domain, &c->pre_spare);
 for (unsigned int i = 0; i < ARRAY_SIZE(c->producer_alias); i++)
  for (unsigned int j = 0; j < ARRAY_SIZE(c->producer_alias[i]); j++)
   snapshot_unmap(c->domain, &c->producer_alias[i][j]);
#endif
 snapshot_free(c->domain, c->snapshot_slots);
#ifdef ICP_HDR_NATIVE
 for (unsigned int i = 0; i < ARRAY_SIZE(c->saved_slots); i++)
  snapshot_free(c->domain, c->saved_slots[i]);
#endif
#endif
 if (c->heap) vunmap(c->heap);
 c->heap = NULL;
 if (c->qdss) iommu_unmap(c->domain, 0x13d00000, SZ_1M);
 c->qdss = false;
 if (c->config_mapped) iommu_unmap(c->domain, 0x800000, c->config_mapped * PAGE_SIZE);
 c->config_mapped = 0;
 if (c->image_mapped) iommu_unmap(c->domain, 0x13e00000, c->image_mapped * PAGE_SIZE);
 c->image_mapped = 0;
 if (c->mapped) iommu_unmap(c->domain, HEAP_BASE, c->mapped * PAGE_SIZE);
 c->mapped = 0;
 if (c->attached) iommu_detach_device(c->domain, c->dev);
 c->attached = false;
 if (c->domain) iommu_domain_free(c->domain);
 c->domain = NULL;
 for (i = 0; i < c->npages; i++) __free_page(c->pages[i]);
 c->npages = 0;
 kvfree(c->pages);
 c->pages = NULL;
}
static int memory_init(struct icp *c)
{
 int ret;
 c->domain = iommu_paging_domain_alloc(c->dev);
 if (IS_ERR(c->domain)) { ret = PTR_ERR(c->domain); c->domain = NULL; return ret; }
 ret = iommu_attach_device(c->domain, c->dev);
 if (ret) return ret;
 c->attached = true;
 c->pages = kvcalloc(HEAP_SIZE / PAGE_SIZE, sizeof(*c->pages), GFP_KERNEL);
 if (!c->pages) return -ENOMEM;
 while (c->npages < HEAP_SIZE / PAGE_SIZE) {
  struct page *p = alloc_page(GFP_KERNEL | __GFP_ZERO);
  if (!p) return -ENOMEM;
  c->pages[c->npages++] = p;
  ret = iommu_map(c->domain, HEAP_BASE + c->mapped * PAGE_SIZE, page_to_phys(p),
    PAGE_SIZE, IOMMU_READ | IOMMU_WRITE | IOMMU_CACHE, GFP_KERNEL);
  if (ret) return ret;
  c->mapped++;
 }
 c->heap = vmap(c->pages, c->npages, VM_MAP, PAGE_KERNEL);
 if (!c->heap) return -ENOMEM;
 ret = iommu_map(c->domain, 0x13d00000, 0x16790000, SZ_1M,
   IOMMU_READ | IOMMU_WRITE | IOMMU_MMIO, GFP_KERNEL);
 if (ret) return ret;
 c->qdss = true;
 /* Host command buffers live in the advertised shared IOVA window. */
 while (c->config_mapped < SZ_1M / PAGE_SIZE) {
  ret = iommu_map(c->domain, 0x800000 + c->config_mapped * PAGE_SIZE,
    page_to_phys(c->pages[6 * SZ_1M / PAGE_SIZE + c->config_mapped]), PAGE_SIZE,
    IOMMU_READ | IOMMU_WRITE | IOMMU_CACHE, GFP_KERNEL);
  if (ret) return ret;
  c->config_mapped++;
 }
 /* Separate advertised image IOVA window, backed by owned pages. The first
  * 8 KiB of this physical MiB remain reserved for the firmware SFR buffer. */
 while (c->image_mapped < SZ_1M / PAGE_SIZE) {
  ret = iommu_map(c->domain, 0x13e00000 + c->image_mapped * PAGE_SIZE,
    page_to_phys(c->pages[5 * SZ_1M / PAGE_SIZE + c->image_mapped]), PAGE_SIZE,
    IOMMU_READ | IOMMU_WRITE | IOMMU_CACHE, GFP_KERNEL);
  if (ret) return ret;
  c->image_mapped++;
 }
 return 0;
}
static int power_on(struct icp *c)
{
 /* UHD TF21 reads FULL and FULL_REF and writes DISPLAY and FULL_REF.
  * Vote their uncompressed NV12 traffic on the stock CAMNOC SF path.
  * 8,812,800 stripe pixels at 60 Hz fit the stock 600 MHz IPE level. */
 bool uhd_tf = c->temporal && c->width == 3840 && c->height == 2160;
 bool fast_ipe = uhd_tf;
 u32 average = c->width > 640 ? 600000 : 100000;
 u32 peak = c->width > 640 ? 1000000 : 100000;
 int ret;
#ifdef ICP_HDR_WIDE
 /* HDR runs five full-resolution passes per output. Use the same validated
  * clock level and budget for concurrent capture and the MFHDR burst. */
 fast_ipe |= !!c->hdr_owner;
#endif
 if (fast_ipe) {
  average = DIV_ROUND_UP_ULL((u64)c->width * c->height * 6 * 60, 1000);
  peak = average;
 }
 ret = icc_set_bw(c->ahb, 0, 100000); if (ret) return ret;
 ret = icc_set_bw(c->sf, average, peak); if (ret) goto ahb;
 ret = clk_set_rate(c->clocks[5].clk, 400000000); if (ret) goto sf;
 ret = clk_set_rate(c->clocks[8].clk, 200000000); if (ret) goto sf;
 ret = clk_set_rate(c->clocks[13].clk, fast_ipe ? 600000000 : 364000000); if (ret) goto sf;
 ret = clk_bulk_prepare(c->nclocks, c->clocks); if (ret) goto sf;
 c->stage = "power-top";
 ret = pm_runtime_resume_and_get(c->pd); if (ret < 0) goto unprepare;
 c->stage = "power-bps";
 ret = pm_runtime_resume_and_get(c->bps); if (ret < 0) goto top;
 c->stage = "power-ipe";
 ret = pm_runtime_resume_and_get(c->ipe); if (ret < 0) goto bps;
 c->stage = "clocks";
 ret = clk_bulk_enable(c->nclocks, c->clocks); if (ret) goto ipe;
 c->powered = true;
 writel(0, c->cirq); writel(0xffffffff, c->cirq + 4); writel(BIT(1), c->cirq + 0x10);
 enable_irq(c->irq);
 return 0;
ipe: pm_runtime_put_sync(c->ipe);
bps: pm_runtime_put_sync(c->bps);
top: pm_runtime_put_sync(c->pd);
unprepare: clk_bulk_unprepare(c->nclocks, c->clocks);
sf: icc_set_bw(c->sf, 0, 0);
ahb: icc_set_bw(c->ahb, 0, 0);
 return ret;
}
static int quiesce(struct icp *c)
{
 int ret = 0;
 if (c->pas) {
  ret = qcom_pas_shutdown(PAS_ID);
  if (ret) { c->unsafe = true; dev_err(c->dev, "PAS shutdown failed %d; retaining DMA and power\n", ret); return ret; }
  c->pas = false;
 }
 qcom_pas_metadata_release(c->pas_ctx);
 if (c->powered) {
  writel(0, c->cirq);
  disable_irq(c->irq);
  clk_bulk_disable(c->nclocks, c->clocks);
  pm_runtime_put_sync(c->ipe);
  pm_runtime_put_sync(c->bps);
  pm_runtime_put_sync(c->pd);
  clk_bulk_unprepare(c->nclocks, c->clocks);
  icc_set_bw(c->sf, 0, 0);
  icc_set_bw(c->ahb, 0, 0);
  c->powered = false;
 }
 return 0;
}
static int cleanup(struct icp *c)
{
 int ret = quiesce(c);
 if (ret) return ret;
 /* Snapshot only after authenticated firmware shutdown and clocks are off.
  * Even a failed frame remains inspectable, but is never labelled complete. */
 if (c->frame_sent && c->heap && c->output) {
  unsigned int i;
  u8 *io = c->heap + 5 * SZ_1M, *out = io + OUTPUT_OFFSET;
  dma_rmb();
  memcpy(c->output, out, IMAGE_BYTES);
  c->output_crc = crc32_le(0, out, IMAGE_BYTES);
  for (i = 0; i < IMAGE_BYTES; i++) c->changed += out[i] != 0xa5;
  c->input_changed = crc32_le(0, io + INPUT_OFFSET, IMAGE_BYTES) != c->input_crc;
  for (i = 0x2000; i < SZ_1M; i++) {
   if ((i >= INPUT_OFFSET && i < INPUT_OFFSET + IMAGE_BYTES) ||
       (i >= OUTPUT_OFFSET && i < OUTPUT_OFFSET + IMAGE_BYTES) ||
       (i >= SCRATCH_OFFSET && i < SCRATCH_OFFSET + c->scratch_size)) continue;
   c->guard_changed += io[i] != 0x5a;
  }
  c->output_valid = true;
  dev_info(c->dev, "image snapshot complete=%u changed=%u crc=%08x input_changed=%u guard_changed=%u\n",
    c->frame_done, c->changed, c->output_crc, c->input_changed, c->guard_changed);
  if (c->input_changed || c->guard_changed) ret = -EOVERFLOW;
  else if (c->frame_done && !c->changed) ret = -ENODATA;
 }
 memory_free(c);
 return ret;
}
static int boot(struct icp *c)
{
 struct device_node *node;
 struct resource res;
 const struct firmware *fw;
 const char *name;
 ssize_t size;
 int ret;
 c->stage = "pas-supported";
 dev_info(c->dev, "PAS %u supported=%d available=%d\n", PAS_ID, qcom_pas_supported(PAS_ID), qcom_pas_is_available());
 /* Shipping camera.ko calls authenticated load directly; the advisory
  * PAS-supported query returns false on this firmware. Auth remains required. */
 node = of_find_node_by_path("/reserved-memory/memory@9f500000");
 if (!node) return -ENODEV;
 ret = of_address_to_resource(node, 0, &res);
 of_node_put(node);
 if (ret) return ret;
 ret = device_property_read_string(c->dev, "firmware-name", &name);
 if (ret) return ret;
 ret = request_firmware(&fw, name, c->dev);
 if (ret) return ret;
 size = qcom_mdt_get_size(fw);
 if (size <= 0 || size > resource_size(&res)) { ret = -EINVAL; goto release; }
 if (!c->pas_ctx) {
  c->pas_ctx = devm_qcom_pas_context_alloc(c->dev, PAS_ID, res.start, resource_size(&res));
  if (IS_ERR(c->pas_ctx)) { ret = PTR_ERR(c->pas_ctx); c->pas_ctx = NULL; goto release; }
 }
 c->stage = "pas-load";
 dev_info(c->dev, "before MDT PAS load (context metadata retained)\n");
 ret = qcom_mdt_pas_load(c->pas_ctx, fw, name, NULL);
 dev_info(c->dev, "after MDT PAS load ret=%d\n", ret);
 if (ret) goto release;
 c->pas = true;
 c->stage = "pas-auth";
 dev_info(c->dev, "before PAS auth/reset\n");
 ret = qcom_pas_prepare_and_auth_reset(c->pas_ctx);
 dev_info(c->dev, "after PAS auth/reset ret=%d\n", ret);
release:
 release_firmware(fw);
 return ret;
}
static void hfi_memory_init(struct icp *c)
{
 struct qtable *q = c->heap + SZ_1M;
 unsigned int i;
 c->qt = q;
 q->version = ~0u; q->size = sizeof(*q); q->offset = 24;
 q->stride = sizeof(struct qhdr); q->count = 3; q->active = 3;
 for (i = 0; i < 3; i++) {
  q->q[i].field[Q_STATUS].value = 1;
  q->q[i].field[Q_START].value = HEAP_BASE + (2 + i) * SZ_1M;
  q->q[i].field[Q_TYPE].value = i;
  q->q[i].field[Q_SIZE].value = (i == 2 ? 102400 : 8192) / 4;
  q->q[i].field[Q_RXWM].value = 1;
  q->q[i].field[Q_TXWM].value = i == 2 ? 1024 : 1;
  q->q[i].field[Q_RXREQ].value = i == 2 ? 0 : 1;
 }
 *(u32 *)(c->heap + 5 * SZ_1M) = 4096;
 dma_wmb();
}
static int hfi_init(struct icp *c)
{
 u32 status;
 dev_info(c->dev, "HFI CSR programming starts\n");
 writel(HEAP_BASE + SZ_1M, c->csr + GP(6));
 writel(HEAP_BASE + 5 * SZ_1M, c->csr + GP(10));
 writel(0x800000, c->csr + GP(4)); writel(0x12e00000, c->csr + GP(5));
 writel(HEAP_BASE, c->csr + GP(7)); writel(SZ_1M, c->csr + GP(8));
 writel(0x13d00000, c->csr + GP(11)); writel(SZ_1M, c->csr + GP(12));
 writel(0x13e00000, c->csr + GP(13)); writel(0xcc200000, c->csr + GP(14));
 writel(0xe0800000, c->csr + GP(15)); writel(0x1e700000, c->csr + GP(16));
 writel(HEAP_BASE, c->csr + GP(17)); writel(HEAP_SIZE, c->csr + GP(18));
 dev_info(c->dev, "HFI CSR programmed, requesting init\n");
 writel(1, c->csr + GP(2));
 dev_info(c->dev, "HFI init requested, polling response\n");
 if (readl_poll_timeout(c->csr + GP(3), status, status == 1, 1000, 2000000)) {
  dev_err(c->dev, "HFI init response=%08x fw=%08x pfault=%08x\n", status,
    readl(c->csr + GP(1)), readl(c->cirq + 0x128));
  return -ETIMEDOUT;
 }
 c->version = readl(c->csr + GP(1));
 writel(BIT(4) | BIT(2), c->cirq);
 return 0;
}
static int send(struct icp *c, const u32 *data, unsigned int words)
{
 struct qhdr *q = &c->qt->q[0];
 u32 r = READ_ONCE(q->field[Q_READ].value), w = READ_ONCE(q->field[Q_WRITE].value);
 u32 *ring = c->heap + 2 * SZ_1M;
 unsigned int i;
 if (r >= 2048 || w >= 2048 || words >= 2048) return -EPROTO;
 if ((r + 2048 - w - 1) % 2048 < words) return -ENOSPC;
 for (i = 0; i < words; i++) ring[(w + i) % 2048] = data[i];
 dma_wmb();
 WRITE_ONCE(q->field[Q_WRITE].value, (w + words) % 2048);
 writel(1, c->cirq + 0x124);
 return 0;
}
static int receive(struct icp *c, u32 wanted, u32 *reply, unsigned int capacity)
{
 struct qhdr *q = &c->qt->q[1];
 u32 *ring = c->heap + 3 * SZ_1M;
 unsigned long until = jiffies + msecs_to_jiffies(2000);
 do {
  u32 r = READ_ONCE(q->field[Q_READ].value), w = READ_ONCE(q->field[Q_WRITE].value);
  unsigned int i, n, available;
  if (r >= 2048 || w >= 2048) return -EPROTO;
  if (r == w) {
   /* The firmware may coalesce notifications. Recheck the ring even if
    * the interrupt was suppressed; the overall command deadline still applies. */
   wait_event_timeout(c->replies, READ_ONCE(q->field[Q_WRITE].value) != r,
                      msecs_to_jiffies(2));
   continue;
  }
  dma_rmb();
  available = (w + 2048 - r) % 2048;
  n = ring[r] / 4;
  if (ring[r] % 4 || n < 2 || n > capacity || n > available) return -EPROTO;
  for (i = 0; i < n; i++) reply[i] = ring[(r + i) % 2048];
  dma_mb();
  WRITE_ONCE(q->field[Q_READ].value, (r + n) % 2048);
  c->messages++;
  dev_dbg(c->dev, "HFI reply %*phN\n", min(n * 4, 64u), reply);
  if (reply[1] == wanted) return n;
  if (reply[1] == 0x20008) return -EIO;
 } while (time_before(jiffies, until));
 dev_err(c->dev, "reply %08x timed out: cmd=%u/%u msg=%u/%u irq=%08x pfault=%08x\n",
   wanted, c->qt->q[0].field[Q_READ].value, c->qt->q[0].field[Q_WRITE].value,
   q->field[Q_READ].value, q->field[Q_WRITE].value, c->irq_status, readl(c->cirq + 0x128));
 return -ETIMEDOUT;
}
/* Keep the manual bring-up transcript available without writing eleven
 * successful command replies to the journal for every saved video frame. */
#ifdef ICP_HDR_WIDE
static bool hdr_log_acks;
module_param(hdr_log_acks, bool, 0600);
MODULE_PARM_DESC(hdr_log_acks, "Log successful wide HDR recorder command replies");
#endif
static bool log_ipe_ack(const struct icp *c)
{
#ifdef ICP_HDR_WIDE
 return !c->hdr_owner || READ_ONCE(hdr_log_acks);
#else
 return true;
#endif
}
static int config_test(struct icp *c, u32 *reply, unsigned int capacity, u32 cfg_offset)
{
 /* 96-byte NV12 descriptors verified against the shipping HAL packer.
  * 62 image slots; 8336 bytes of config data followed by an 8-byte userArg.
  * No FRAME_PROCESS is issued and no image DMA is submitted. */
 u32 *cfg = c->heap + 6 * SZ_1M + cfg_offset;
 u32 cmd[] = {40, 0x101000e, 5, 0x596, 0, 0, 0, 1, c->handle, 0x800000 + cfg_offset};
 bool failed;
 int ret;
 unsigned int i;
 memset(cfg, 0, cfg_offset ? SZ_16K : SZ_64K);
#ifdef ICP_HDR_CONFIG_PROBE
 if (c->snapshot_probe && c->native) {
  const struct snapshot_native *p = c->native;
  const u8 *desc = c->snapshot_probe == SNAP_PRE ? p->pre_cfg :
                   c->snapshot_probe == SNAP_THREE ? p->mac_cfg : p->p010_cfg;
  memcpy(cfg, desc, IPE_CONFIG_BYTES);
 } else if (c->snapshot_probe)
  snapshot_config(cfg, c->snapshot_probe);
 else
#endif
 for (i = 0; i <= 10; i++) {
  u32 *desc;
  if (i != 0 && i != 8 && !(c->temporal && (i == 4 || i == 10))) continue;
  desc = cfg + 24 * i;
  desc[0] = 20; desc[1] = c->width; desc[2] = c->height;
  desc[16] = c->width; desc[17] = c->height;
  desc[18] = c->width; desc[19] = c->height / 2;
 }
 cfg[5964 / 4] = 65536;
 cfg[5968 / 4] = 1;
 dma_wmb();
 ret = send(c, cmd, ARRAY_SIZE(cmd)); if (ret) return ret;
 ret = receive(c, 0x102000e, reply, capacity); if (ret < 0) return ret;
 if (ret != 46) return -EPROTO;
 failed = reply[2] != 5 || reply[7] || reply[8] || reply[3] != 0x596 || reply[4];
 if (failed || log_ipe_ack(c)) {
  dev_info(c->dev, "CONFIG_IO words=%d opcode=%u error=%u rc=%u scratch=%u cdm=%u\n",
           ret, reply[2], reply[7], reply[8], reply[9], reply[10]);
  dev_info(c->dev, "CONFIG_IO HFI cookie=%08x:%08x\n", reply[4], reply[3]);
 }
 if (failed) return -EREMOTEIO;
 c->scratch_size = reply[9]; c->cdm_size = reply[10];
 return 0;
}
static int frame_test(struct icp *c, u32 *reply, unsigned int capacity, unsigned int index)
{
#ifdef ICP_FRAME_FIXTURE
 u8 *io = c->heap + 5 * SZ_1M, *shared = c->heap + 6 * SZ_1M;
 u32 cmd[] = {40, 0x101000e, 6, 0x597, 0, 0, 0, 1, c->handle, 0x804000};
 unsigned int x, y;
 int ret;
 if (c->scratch_size != 4496 || c->cdm_size != 524288) return -EPROTO;
 BUILD_BUG_ON(sizeof(ipe_frame) > 0xc000);
 BUILD_BUG_ON(sizeof(ipe_iq) != 6180);
 BUILD_BUG_ON(sizeof(ipe_stripes) != 64872);
 if (!c->output) c->output = vzalloc(IMAGE_BYTES);
 if (!c->output) return -ENOMEM;
 memcpy(shared + 0x4000, ipe_frame, sizeof(ipe_frame));
 *(u32 *)(shared + 0x4000 + 168) = index + 1;
 cmd[3] += index;
 memcpy(shared + 0x10000, ipe_iq, sizeof(ipe_iq));
 memcpy(shared + 0x20000, ipe_stripes, sizeof(ipe_stripes));
 memset(io + 0x2000, 0x5a, SZ_1M - 0x2000);
 if (c->user_input) {
  /* Pixel bytes only. Command addresses, sizes and IQ remain the pinned fixture. */
  memcpy(io + INPUT_OFFSET, c->input, IMAGE_BYTES);
 } else {
  /* Horizontal luma ramp with row markers and eight chroma bars. */
  for (y = 0; y < 480; y++) for (x = 0; x < 640; x++)
  io[INPUT_OFFSET + y * 640 + x] = 16 +
    ((index & 1 ? 639 - x : x) * 200 / 640) + (y / 60) * 2 + (index & 2 ? 16 : 0);
  for (y = 0; y < 240; y++) for (x = 0; x < 640; x += 2) {
  io[INPUT_OFFSET + 640 * 480 + y * 640 + x] = 64 + (x / 80) * 16;
  io[INPUT_OFFSET + 640 * 480 + y * 640 + x + 1] = 192 - (x / 80) * 16;
  }
 }
 memset(io + OUTPUT_OFFSET, 0xa5, IMAGE_BYTES);
 memset(io + SCRATCH_OFFSET, 0, c->scratch_size);
 c->input_crc = crc32_le(0, io + INPUT_OFFSET, IMAGE_BYTES);
 dma_wmb();
 ret = send(c, cmd, ARRAY_SIZE(cmd)); if (ret) return ret;
 c->frame_sent = true;
 ret = receive(c, 0x102000e, reply, capacity); if (ret < 0) return ret;
 if (ret < 9 || reply[2] != 6 || reply[3] != cmd[3] || reply[4]) return -EPROTO;
 c->frame_error = reply[7]; c->frame_rc = reply[8];
 dev_info(c->dev, "FRAME_PROCESS words=%d error=%u rc=%u\n", ret, reply[7], reply[8]);
 if (reply[7] || reply[8]) return -EREMOTEIO;
 dma_rmb();
 if (crc32_le(0, io + INPUT_OFFSET, IMAGE_BYTES) != c->input_crc ||
     memcmp(io + INPUT_OFFSET, io + OUTPUT_OFFSET, 640 * 480)) return -EILSEQ;
 for (x = 0x2000; x < SZ_1M; x++) {
  if ((x >= INPUT_OFFSET && x < INPUT_OFFSET + IMAGE_BYTES) ||
      (x >= OUTPUT_OFFSET && x < OUTPUT_OFFSET + IMAGE_BYTES) ||
      (x >= SCRATCH_OFFSET && x < SCRATCH_OFFSET + c->scratch_size)) continue;
  if (io[x] != 0x5a) return -EOVERFLOW;
 }
 dev_info(c->dev, "frame request=%u cookie=%08x output_crc=%08x luma_exact=1\n",
   index + 1, reply[3], crc32_le(0, io + OUTPUT_OFFSET, IMAGE_BYTES));
 c->completed_frames++;
 c->frame_done = true;
 return 0;
#else
 return -EOPNOTSUPP;
#endif
}
static void debug_dump(struct icp *c)
{
 struct qhdr *q;
 u32 *ring, r, w;
 unsigned int total = 0;
 if (!c->qt || !c->heap) return;
 q = &c->qt->q[2]; ring = c->heap + 4 * SZ_1M;
 r = READ_ONCE(q->field[Q_READ].value); w = READ_ONCE(q->field[Q_WRITE].value);
 dma_rmb();
 if (r >= 25600 || w >= 25600) return;
 while (r != w && total++ < 80) {
  char text[512];
  unsigned int i, n = ring[r] / 4, available = (w + 25600 - r) % 25600;
  if (n < 2 || n > available || ring[r] % 4) break;
  for (i = 0; i < min(n * 4, 511u); i++) {
   u8 v = ((u8 *)&ring[(r + i / 4) % 25600])[i % 4];
   text[i] = v >= 32 && v < 127 ? v : ' ';
  }
  text[i] = 0;
  dev_info(c->dev, "FW debug %s\n", text);
  r = (r + n) % 25600;
 }
 WRITE_ONCE(q->field[Q_READ].value, r);
}
static int create_ipe(struct icp *c, int level, u32 cfg_offset)
{
 u32 reply[128];
 u32 create[] = {28, 0x1010008, 2, 0, 0, 0, 0};
 int ret;
 c->stage = "create-ipe";
 ret = send(c, create, ARRAY_SIZE(create)); if (ret) return ret;
 ret = receive(c, 0x1020008, reply, ARRAY_SIZE(reply));
 if (ret < 0) return ret;
 if (ret < 8 || reply[2] || !reply[3]) return -EREMOTEIO;
 c->handle = reply[3];
 if (level >= 5) {
  c->stage = "config-ipe";
  ret = config_test(c, reply, ARRAY_SIZE(reply), cfg_offset); if (ret) return ret;
 }
 return 0;
}
static int session_start(struct icp *c, int level, u32 cfg_offset)
{
 u32 reply[128], init[] = {8, 0x10001};
 u32 ping[] = {16, 0x10005, 0x12345678, 0xabcdef01};
 u32 debug[] = {24, 0x10003, 1, 0x1, 0x1c, 1};
 int ret;
 c->stage = "power";
 ret = power_on(c); if (ret) return ret;
 dev_info(c->dev, "all hardware and debug XO clocks on\n");
 if (level == 1) return 0;
 c->stage = "memory";
 ret = memory_init(c); if (ret) return ret;
 dev_info(c->dev, "IOMMU memory mapped\n");
 hfi_memory_init(c);
 dev_info(c->dev, "HFI memory prepared; checking CSR\n");
 dev_info(c->dev, "HFI memory ready; pre-boot FW=%08x response=%08x\n", readl(c->csr + GP(1)), readl(c->csr + GP(3)));
 ret = boot(c); if (ret) return ret;
 c->stage = "hfi-init";
 dev_info(c->dev, "before HFI init\n");
 ret = hfi_init(c); if (ret || level == 2) return ret;
 c->stage = "sys-init";
 ret = send(c, init, ARRAY_SIZE(init)); if (ret) return ret;
 ret = receive(c, 0x20001, reply, ARRAY_SIZE(reply));
 if (ret < 0) return ret;
 if (ret < 4 || reply[2]) return -EREMOTEIO;
 if (level >= 6) { ret = send(c, debug, ARRAY_SIZE(debug)); if (ret) return ret; }
 c->stage = "ping";
 ret = send(c, ping, ARRAY_SIZE(ping)); if (ret) return ret;
 ret = receive(c, 0x20006, reply, ARRAY_SIZE(reply));
 if (ret < 0) return ret;
 if (ret < 4 || reply[2] != ping[2] || reply[3] != ping[3]) return -EPROTO;
 if (level == 3) return 0;
 return create_ipe(c, level, cfg_offset);
}
static int session_destroy(struct icp *c)
{
 u32 reply[128], destroy[] = {44, 0x101000a, 8, 0, 0, 0, 0, 1, c->handle, 0, 0};
 int ret;
 c->stage = "destroy-ipe";
 ret = send(c, destroy, ARRAY_SIZE(destroy)); if (ret) return ret;
 ret = receive(c, 0x102000a, reply, ARRAY_SIZE(reply));
 if (ret < 0) return ret;
 return ret >= 8 && reply[2] == 8 && !reply[7] ? 0 : -EREMOTEIO;
}
#ifdef ICP_HDR_STILL_FRAME
#include "hdr-snapshot-frame.h"
#endif
static int transport_test(struct icp *c, int level)
{
 u32 reply[128];
 unsigned int i;
 int ret = session_start(c, level, 0);
 if (ret || level < 4) return ret;
 if (level >= 6) {
  c->stage = "frame-ipe";
  for (i = 0; i < (level == 7 ? 8 : 1); i++) {
   c->frame_done = false;
   ret = frame_test(c, reply, ARRAY_SIZE(reply), i); if (ret) return ret;
   debug_dump(c);
  }
 }
#ifdef ICP_HDR_CONFIG_PROBE
 if (c->snapshot_probe) {
  u8 *shared = c->heap + 6 * SZ_1M;
  c->stage = "snapshot-dma";
#ifdef ICP_HDR_STILL_FRAME
#ifdef ICP_HDR_MAC_FRAME
  if (c->snapshot_action == 22) memcpy(shared + 0x4000, post_frame, sizeof(post_frame));
  else if (c->snapshot_action == 21 || c->snapshot_action == 23) memcpy(shared + 0x4000, mac_frame, sizeof(mac_frame));
  else
#endif
  if (c->snapshot_action >= 19) memcpy(shared + 0x4000, fusion_off_frame, sizeof(fusion_off_frame));
  else if (c->snapshot_action == 15) memcpy(shared + 0x4000, post_frame, sizeof(post_frame));
  else if (c->snapshot_action) memcpy(shared + 0x4000, pre_frame, sizeof(pre_frame));
#endif
  ret = snapshot_buffers(c->domain, c->snapshot_slots, (u32 *)shared,
                         (u32 *)(shared + 0x4000), c->scratch_size, NULL);
  if (ret) return ret;
  dev_info(c->dev, "snapshot stage %d mapped image planes and %u scratch bytes with IOMMU guards\n",
           c->snapshot_probe, c->scratch_size);
#ifdef ICP_HDR_STILL_FRAME
  if (c->snapshot_action) {
   c->stage = "snapshot-frame";
   ret = (c->snapshot_action == 15 || c->snapshot_action == 22) ? snapshot_post_frame(c, shared, false) : snapshot_to_internal(c, shared, NULL, NULL, NULL, NULL);
   if (ret) return ret;
  }
#endif
 }
#endif
 return session_destroy(c);
}

static ssize_t selftest_store(struct device *dev, struct device_attribute *attr,
 const char *buf, size_t count)
{
 struct icp *c = dev_get_drvdata(dev);
 int level, requested_level, ret, clean;
#ifdef ICP_HDR_CONFIG_PROBE
 int probe = 0;
#endif
 ret = kstrtoint(buf, 0, &level);
 if (ret) return ret;
 requested_level = level;
#ifdef ICP_HDR_CONFIG_PROBE
#ifdef ICP_HDR_STILL_FRAME
 if (level == 14 || (level >= 16 && level <= 18)) level = SNAP_PRE;
 else if (level == 15) level = SNAP_POST_THREE;
 else if (level == 19) level = SNAP_THREE;
#ifdef ICP_HDR_MAC_FRAME
 else if (level == 21 || level == 23) level = SNAP_THREE;
 else if (level == 22) { probe = SNAP_POST_P010; level = 5; }
#endif
 else if (level == 20) level = SNAP_TWO;
#endif
 if (level >= SNAP_PRE && level <= SNAP_POST_TWO) {
  probe = level;
  level = 5; /* The snapshot path sends its own frame after CONFIG_IO. */
 }
#endif
 if (level < 1 || level > 8) return -EINVAL;
#ifndef ICP_FRAME_FIXTURE
 if (level >= 6) return -EOPNOTSUPP;
#endif
 if (!mutex_trylock(&c->lock)) return -EBUSY;
 if (c->unsafe || c->streaming) { mutex_unlock(&c->lock); return c->unsafe ? -EIO : -EBUSY; }
#ifdef ICP_HDR_STILL_FRAME
 if (requested_level == 16 && c->input_ready) {
  ret = quiesce_idle_capture(c, true);
  if (ret) { mutex_unlock(&c->lock); return ret; }
 }
#endif
#ifdef ICP_HDR_NATIVE
 if (c->native && !(requested_level == (c->native == &front_native ? 23 : 21) || requested_level == 22 ||
                    (requested_level >= 16 && requested_level <= 18))) {
  mutex_unlock(&c->lock);
  return -EINVAL;
 }
#endif
#ifdef ICP_HDR_STILL_FRAME
 if (((requested_level == 14 || (requested_level >= 16 && requested_level <= 18)) && !c->input_ready) ||
     ((requested_level == 15 || requested_level == 22) && !c->pre_valid) ||
     (requested_level >= 17 && requested_level <= 18 && c->nlegs != requested_level - 16) ||
     ((requested_level == 19 || requested_level == 20 || requested_level == 21 || requested_level == 23) && c->nlegs != 3)) {
  mutex_unlock(&c->lock);
  return -ENODATA;
 }
 c->pre_valid = false;
#ifdef ICP_HDR_MAC_FRAME
 c->p010_valid = false;
#endif
 c->snapshot_action = requested_level >= 14 && requested_level <= 23 ? requested_level : 0;
 if (requested_level < 17) c->nlegs = 0;
#endif
 if (level == 8 && !c->input_ready) { mutex_unlock(&c->lock); return -ENODATA; }
 if (!try_module_get(THIS_MODULE)) { mutex_unlock(&c->lock); return -ENODEV; }
 c->interrupts = 0; c->irq_status = 0; c->messages = 0; c->handle = 0;
 c->frame_sent = false; c->frame_done = false; c->output_valid = false;
 c->changed = 0; c->output_crc = 0; c->frame_error = 0; c->frame_rc = 0;
 c->input_changed = 0; c->guard_changed = 0;
 c->completed_frames = 0;
 c->user_input = level == 8;
#ifdef ICP_HDR_STILL_FRAME
 c->user_input |= c->snapshot_action == 14 || (c->snapshot_action >= 16 && c->snapshot_action <= 18);
#endif
 c->temporal = false;
 c->width = 640; c->height = 480;
#ifdef ICP_HDR_CONFIG_PROBE
 if (c->native) {
  c->width = c->native->width;
  c->height = c->native->height;
 }
 c->snapshot_probe = probe;
#endif
 ret = transport_test(c, level);
#ifdef ICP_HDR_CONFIG_PROBE
 c->snapshot_probe = 0;
#endif
 debug_dump(c);
 clean = cleanup(c);
 if (!ret) ret = clean;
#ifdef ICP_HDR_STILL_FRAME
 if (!ret && c->frame_done && c->snapshot_action >= 16 && c->snapshot_action <= 18) {
  /* Move only firmware-produced intermediates into the ordered exposure set. */
  unsigned int i = c->snapshot_action - 16;
  swap(c->pre_output, c->legs[i]);
  c->nlegs++;
  c->input_ready = false;
  c->input_written = 0;
 }
 c->pre_valid = !ret && c->frame_done && (c->snapshot_action == 14 || (c->snapshot_action >= 19 && c->snapshot_action != 22));
#ifdef ICP_HDR_MAC_FRAME
 if (c->snapshot_action == 22) c->p010_valid = !ret && c->frame_done;
#endif
 if (c->snapshot_action == 15) c->output_valid = !ret && c->frame_done;
 if (ret || c->snapshot_action >= 19) c->nlegs = 0;
 c->snapshot_action = 0;
#endif
 c->result = ret;
 dev_info(dev, "selftest level=%d stage=%s result=%d cleanup=%d fw=%08x irq=%u\n",
   requested_level, c->stage, ret, clean, c->version, c->interrupts);
 if (!c->unsafe) module_put(THIS_MODULE);
 mutex_unlock(&c->lock);
 return ret ? ret : count;
}
static ssize_t status_show(struct device *dev, struct device_attribute *a, char *buf)
{
 struct icp *c = dev_get_drvdata(dev);
 ssize_t ret;
 mutex_lock(&c->lock);
 ret = sysfs_emit(buf, "stage=%s result=%d fw=%08x irq=%u irq_status=%08x messages=%u handle=%08x unsafe=%u frame_done=%u frame_error=%u frame_rc=%u changed=%u output_crc=%08x input_changed=%u guard_changed=%u completed_frames=%u user_input=%u input_ready=%u input_written=%zu input_crc=%08x\n",
   c->stage, c->result, c->version, c->interrupts, c->irq_status, c->messages, c->handle, c->unsafe,
   c->frame_done, c->frame_error, c->frame_rc, c->changed, c->output_crc, c->input_changed, c->guard_changed, c->completed_frames,
   c->user_input, c->input_ready, c->input_written, c->input_crc);
 mutex_unlock(&c->lock);
 return ret;
}
static DEVICE_ATTR_WO(selftest);
static DEVICE_ATTR_RO(status);
static ssize_t output_read(struct file *file, struct kobject *kobj,
 const struct bin_attribute *attr, char *buf, loff_t off, size_t count)
{
 struct icp *c = dev_get_drvdata(kobj_to_dev(kobj));
 ssize_t ret;
 if (off < 0) return -EINVAL;
 if (off >= IMAGE_BYTES) return 0;
 count = min_t(size_t, count, IMAGE_BYTES - off);
 mutex_lock(&c->lock);
 if (!c->output_valid) ret = -ENODATA;
 else { memcpy(buf, c->output + off, count); ret = count; }
 mutex_unlock(&c->lock);
 return ret;
}
static const struct bin_attribute output_attr = {
 .attr = { .name = "output_nv12", .mode = 0400 }, .size = IMAGE_BYTES, .read = output_read,
};
static ssize_t input_write(struct file *file, struct kobject *kobj,
 const struct bin_attribute *attr, char *buf, loff_t off, size_t count)
{
 struct icp *c = dev_get_drvdata(kobj_to_dev(kobj));
 ssize_t ret = count;
 size_t bytes = IMAGE_BYTES;
 if (!mutex_trylock(&c->lock)) return -EBUSY;
 if (c->unsafe) { ret = -EIO; goto unlock; }
#ifdef ICP_HDR_WIDE
 if (c->hdr_owner) { ret = -EBUSY; goto unlock; }
#endif
#ifdef ICP_HDR_NATIVE
 bytes = snapshot_image_bytes(c);
 if (!!attr->private != !!c->native) { ret = -EINVAL; goto unlock; }
#endif
 if (!count) goto unlock;
 c->input_ready = false;
 c->output_valid = false;
#ifdef ICP_HDR_STILL_FRAME
 c->pre_valid = false;
#ifdef ICP_HDR_MAC_FRAME
 c->p010_valid = false;
#endif
#endif
 if (off < 0 || off >= bytes || count > bytes - off ||
     (off && off != c->input_written)) {
  c->input_written = 0;
  ret = -EINVAL;
  goto unlock;
 }
 if (!off) c->input_written = 0;
 if (!c->input) c->input = vzalloc(bytes);
 if (!c->input) { ret = -ENOMEM; goto unlock; }
 memcpy(c->input + off, buf, count);
 c->input_written = off + count;
 c->input_ready = c->input_written == bytes;
unlock:
 mutex_unlock(&c->lock);
 return ret;
}
static const struct bin_attribute input_attr = {
 .attr = { .name = "input_nv12", .mode = 0200 }, .size = IMAGE_BYTES, .write = input_write,
};
#ifdef ICP_HDR_NATIVE
static const struct bin_attribute native_input_attr = {
 .attr = { .name = "snapshot_input", .mode = 0200 }, .size = 4080 * 3060 * 3 / 2,
 .write = input_write, .private = (void *)&native_input_attr,
};
#endif
static const struct bin_attribute * const bin_attrs[] = { &output_attr, &input_attr,
#ifdef ICP_HDR_STILL_FRAME
 &pre_output_attr,
#ifdef ICP_HDR_NATIVE
 &native_input_attr, &live_input_attr,
#endif
#ifdef ICP_HDR_MAC_FRAME
 &p010_output_attr,
#endif
#endif
 NULL };
static struct attribute *attrs[] = { &dev_attr_selftest.attr, &dev_attr_status.attr,
#ifdef ICP_HDR_NATIVE
 &dev_attr_snapshot_geometry.attr, &dev_attr_video_fuse.attr,
#endif
 NULL };
static const struct attribute_group group = { .attrs = attrs, .bin_attrs = bin_attrs };
#ifdef ICP_VIDEO_FIXTURE
#include "ipe-video.h"
#endif
static void pd_detach(void *p) { dev_pm_domain_detach(p, true); }
static int icp_probe(struct platform_device *pdev)
{
 struct device *dev = &pdev->dev;
 struct icp *c;
 int ret;
 c = devm_kzalloc(dev, sizeof(*c), GFP_KERNEL);
 if (!c) return -ENOMEM;
 c->dev = dev; c->stage = "idle";
 c->width = 640; c->height = 480;
 mutex_init(&c->lock);
 c->csr = devm_platform_ioremap_resource(pdev, 0);
 if (IS_ERR(c->csr)) return PTR_ERR(c->csr);
 c->cirq = devm_platform_ioremap_resource(pdev, 1);
 if (IS_ERR(c->cirq)) return PTR_ERR(c->cirq);
 c->wd = devm_platform_ioremap_resource(pdev, 2);
 if (IS_ERR(c->wd)) return PTR_ERR(c->wd);
 c->nclocks = devm_clk_bulk_get_all(dev, &c->clocks);
 if (c->nclocks < 0) return c->nclocks;
 if (c->nclocks != 17) return -EINVAL;
 c->ahb = devm_of_icc_get(dev, "ahb"); if (IS_ERR(c->ahb)) return PTR_ERR(c->ahb);
 c->sf = devm_of_icc_get(dev, "sf"); if (IS_ERR(c->sf)) return PTR_ERR(c->sf);
 init_waitqueue_head(&c->replies);
 c->irq = platform_get_irq(pdev, 0); if (c->irq < 0) return c->irq;
 ret = devm_request_irq(dev, c->irq, icp_irq, IRQF_NO_AUTOEN, "icp-test", c);
 if (ret) return ret;
 c->pd = dev_pm_domain_attach_by_name(dev, "top");
 if (IS_ERR_OR_NULL(c->pd)) return c->pd ? PTR_ERR(c->pd) : -ENODEV;
 ret = devm_add_action_or_reset(dev, pd_detach, c->pd); if (ret) return ret;
 c->bps = dev_pm_domain_attach_by_name(dev, "bps");
 if (IS_ERR_OR_NULL(c->bps)) return c->bps ? PTR_ERR(c->bps) : -ENODEV;
 ret = devm_add_action_or_reset(dev, pd_detach, c->bps); if (ret) return ret;
 c->ipe = dev_pm_domain_attach_by_name(dev, "ipe");
 if (IS_ERR_OR_NULL(c->ipe)) return c->ipe ? PTR_ERR(c->ipe) : -ENODEV;
 ret = devm_add_action_or_reset(dev, pd_detach, c->ipe); if (ret) return ret;
 platform_set_drvdata(pdev, c);
 ret = devm_pm_runtime_enable(dev); if (ret) return ret;
 ret = device_add_group(dev, &group); if (ret) return ret;
#ifdef ICP_VIDEO_FIXTURE
 ret = ipe_video_register(c); if (ret) { device_remove_group(dev, &group); return ret; }
#endif
#ifdef ICP_HDR_WIDE
 c->hdr_recorder = (struct miscdevice) {
  .minor = MISC_DYNAMIC_MINOR, .name = "liuqin-rear-hdr",
  .fops = &hdr_record_ops, .parent = dev, .mode = 0600,
 };
 ret = misc_register(&c->hdr_recorder);
 if (ret) {
#ifdef ICP_VIDEO_FIXTURE
  ipe_video_unregister(c);
#endif
  device_remove_group(dev, &group);
  return ret;
 }
#endif
 dev_info(dev, "bounded ICP transport test ready (manual start)\n");
 return 0;
}
static void icp_remove(struct platform_device *pdev)
{
 struct icp *c = platform_get_drvdata(pdev);
#ifdef ICP_HDR_WIDE
 misc_deregister(&c->hdr_recorder);
#endif
 device_remove_group(c->dev, &group);
#ifdef ICP_VIDEO_FIXTURE
 ipe_video_unregister(c);
#endif
 mutex_lock(&c->lock);
 cleanup(c);
 vfree(c->output);
 vfree(c->input);
#ifdef ICP_HDR_STILL_FRAME
 for (unsigned int i = 0; i < ARRAY_SIZE(c->legs); i++) vfree(c->legs[i]);
#ifdef ICP_HDR_NATIVE
 vfree(c->live_inputs);
#endif
 vfree(c->pre_output);
#ifdef ICP_HDR_MAC_FRAME
 vfree(c->p010);
#endif
#endif
 platform_set_drvdata(pdev, NULL);
 mutex_unlock(&c->lock);
}
static const struct of_device_id matches[] = { { .compatible = "qcom,sm8475-icp-test" }, {} };
MODULE_DEVICE_TABLE(of, matches);
static struct platform_driver icp_driver = {
 .probe = icp_probe, .remove = icp_remove,
 .driver = {
#ifdef ICP_VIDEO_FIXTURE
  .name = "qcom-ipe",
#else
  .name = "qcom-icp-test",
#endif
  .of_match_table = matches, .suppress_bind_attrs = true },
};
module_platform_driver(icp_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("SM8475 bounded ICP HFI transport bringup");
