/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed 640x480 lossless-UBWC4 snapshot descriptors. Shared by the CONFIG_IO probes and
 * the optional pinned still-image frame tests.
 */
#ifndef LIUQIN_HDR_CONFIG_H
#define LIUQIN_HDR_CONFIG_H
struct snapshot_native {
 u32 width, height;
 const unsigned char *mac_registers;
 const unsigned char *pre_cfg, *mac_cfg, *p010_cfg;
 const unsigned char *pre_iq, *mac_iq, *p010_iq;
 const unsigned char *pre_stripes, *mac_stripes, *p010_stripes;
 u32 pre_length, mac_length, p010_length;
};
enum snapshot_probe {
 SNAP_PRE = 9, SNAP_TWO, SNAP_THREE, SNAP_POST_THREE, SNAP_POST_TWO, SNAP_POST_P010 = 22,
};
static void snapshot_config(u32 *cfg, int stage)
{
 unsigned int port;
 bool post = stage == SNAP_POST_TWO || stage == SNAP_POST_THREE || stage == SNAP_POST_P010;
 if (stage == SNAP_TWO || stage == SNAP_THREE)
  cfg[0x1758 / 4] = 4; /* TOPOLOGY_TYPE_MFHDR from ConfigureIPE. */
 for (port = 0; port <= 15; port++) {
  u32 *d = cfg + port * 24;
  if ((stage == SNAP_PRE && port == 0) || (post && port == 8) ||
      (stage == SNAP_POST_TWO && port == 9)) {
   d[0] = stage == SNAP_POST_P010 && port == 8 ? 19 : 20;
   d[1] = 640; d[16] = d[18] = d[0] == 19 ? 1280 : 640;
   d[2] = d[17] = 480; d[19] = 240;
   continue;
  }
  if (!((stage != SNAP_PRE && port <= 1) ||
        (!post && (port == 10 || port == 11)) ||
        ((stage == SNAP_TWO || stage == SNAP_THREE) && (port == 4 || port == 5)) ||
        (stage == SNAP_THREE && (port == 14 || port == 15))))
   continue;
  if (port & 1) {
   /* DS4 PD10 packs two sensor rows in each buffer row. */
   d[0] = 10; d[1] = 160; d[2] = 120; d[16] = 768; d[17] = 64;
  } else {
   bool ref = port == 4 || port == 10;
   d[0] = 14; d[1] = 640; d[2] = 480; d[7] = 2; d[9] = 4;
   d[10] = ref ? 0x8340000 : 0x8440000;
   d[11] = ref ? 0x8080a : 0x60608;
   d[16] = d[18] = d[20] = d[22] = 1024;
   d[17] = 480; d[19] = 240; d[21] = 8; d[23] = 4;
  }
 }
}

/* Layout of the fixed descriptors above. UBWC plane data follows its metadata.
 * The linear NV12 planes share a packed allocation with no inter-plane padding.
 */
static unsigned int snapshot_layout(const u32 *d, u32 *sizes, u32 *meta)
{
 unsigned int count, i;
 if (d[0] == 10) count = 1;
 else if (d[0] == 14 || d[0] == 19 || d[0] == 20) count = 2;
 else return 0;
 for (i = 0; i < count; i++) {
  u32 pixels = d[16 + 2*i] * d[17 + 2*i];
  meta[i] = d[20 + 2*i] * d[21 + 2*i];
  if (d[0] == 14) pixels = (pixels + 4095) & ~4095u;
  sizes[i] = meta[i] + pixels;
 }
 return count;
}
#endif
