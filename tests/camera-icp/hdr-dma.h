/* SPDX-License-Identifier: GPL-2.0-only */
/* Private still-image buffers. Leave the rest of each IOVA
 * interval unmapped. The extra page before the first plane is a leading guard.
 */
#include "frame-layout.h"
#define SNAP_IOVA_BASE 0x24000000u
#ifdef ICP_HDR_NATIVE
#define SNAP_SLOT_SIZE SZ_64M
#else
#define SNAP_SLOT_SIZE SZ_2M
#endif
struct snapshot_surface {
 void *data;
 unsigned int mapped;
 u32 iova;
 bool borrowed;
};

static u32 snapshot_base(unsigned int port)
{
 return SNAP_IOVA_BASE + SNAP_SLOT_SIZE * port + PAGE_SIZE;
}

static void snapshot_unmap(struct iommu_domain *domain, struct snapshot_surface *s)
{
 while (s->mapped) {
  s->mapped -= PAGE_SIZE;
  iommu_unmap(domain, s->iova + s->mapped, PAGE_SIZE);
 }
 if (!s->borrowed) vfree(s->data);
 s->data = NULL;
 s->borrowed = false;
}
static void snapshot_free(struct iommu_domain *domain, struct snapshot_surface *slots)
{
 unsigned int port;
 for (port = 0; port <= IPE_SCRATCH; port++) snapshot_unmap(domain, &slots[port]);
}

static int snapshot_map(struct iommu_domain *domain, struct snapshot_surface *s,
 u32 base, u32 length, unsigned int prot, void *backing)
{
 int ret;
 s->iova = base;
 if (!length || length > SNAP_SLOT_SIZE - 2 * PAGE_SIZE || s->data) return -EINVAL;
 s->borrowed = !!backing;
 s->data = backing ? backing : vzalloc(PAGE_ALIGN(length));
 if (!s->data) return -ENOMEM;
 while (s->mapped < length) {
  phys_addr_t phys = page_to_phys(vmalloc_to_page(s->data + s->mapped));
  ret = iommu_map(domain, base + s->mapped, phys, PAGE_SIZE, prot, GFP_KERNEL);
  if (ret) return ret;
  s->mapped += PAGE_SIZE;
  if (iommu_iova_to_phys(domain, base + s->mapped - PAGE_SIZE) != phys) return -EIO;
 }
 if (iommu_iova_to_phys(domain, base - PAGE_SIZE) || iommu_iova_to_phys(domain, base + s->mapped))
  return -EINVAL;
 return 0;
}

static void snapshot_address(u32 *frame, unsigned int port, u32 base, unsigned int planes,
                             const u32 *sizes, const u32 *meta)
{
 for (unsigned int i = 0; i < planes; i++) {
  if (meta[i]) frame[ipe_buffer_offset(0, port, i, 1) / 4] = base;
  frame[ipe_buffer_offset(0, port, i, 0) / 4] = base + meta[i];
  base += sizes[i];
 }
}

/* Plane sizes include page rounding from ImageFormatUtils::GetPlaneSize.
 * For UBWC each plane has its metadata before its pixels. PD10 is one plane.
 * Call after CONFIG_IO so the scratch length belongs to the current context.
 */
static int snapshot_buffers(struct iommu_domain *domain, struct snapshot_surface *slots,
 const u32 *cfg, u32 *frame, u32 scratch_size, const u32 *offsets)
{
 unsigned int port;
 for (port = 0; port <= IPE_SCRATCH; port++) {
  const u32 *d = cfg + 24 * port;
  unsigned int nplanes = 1;
  u32 sizes[2] = {}, meta[2] = {};
  const u32 offset_bytes = offsets ? offsets[port] : 0;
  const u32 base = snapshot_base(port) + offset_bytes;
  unsigned int prot = IOMMU_READ | IOMMU_CACHE;
  int ret;
  if (port == IPE_SCRATCH) {
   sizes[0] = scratch_size;
  } else {
   if (!d[0]) continue;
   nplanes = snapshot_layout(d, sizes, meta);
   if (!nplanes) return -EINVAL;
  }
  if ((port >= IPE_OUTPUT_DISPLAY && port <= IPE_OUTPUT_DS4_REF) || port == IPE_SCRATCH)
   prot |= IOMMU_WRITE;
  if (offset_bytes > SNAP_SLOT_SIZE - 2 * PAGE_SIZE ||
      PAGE_ALIGN(sizes[0] + sizes[1]) > SNAP_SLOT_SIZE - 2 * PAGE_SIZE - offset_bytes)
   return -E2BIG;
  if (slots[port].data) {
   /* A recording reuses this pass's mappings after its previous HFI ACK. */
   if (slots[port].iova != base || slots[port].mapped != PAGE_ALIGN(sizes[0] + sizes[1]))
    return -EINVAL;
  } else {
   ret = snapshot_map(domain, &slots[port], base, sizes[0] + sizes[1], prot, NULL);
   if (ret) return ret;
  }
  snapshot_address(frame, port, base, nplanes, sizes, meta);
 }
 return 0;
}
