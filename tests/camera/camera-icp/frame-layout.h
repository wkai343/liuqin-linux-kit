/* SPDX-License-Identifier: GPL-2.0-only */
/* Shipping Titan680 frame-buffer offsets, checked by camera-ipe-history.py.
 * Address layout only: declaring a reference port does not enable TF or HDR.
 */
#ifndef LIUQIN_IPE_FRAME_LAYOUT_H
#define LIUQIN_IPE_FRAME_LAYOUT_H

enum ipe_image_port {
 IPE_INPUT_FULL = 0,
 IPE_INPUT_DS4 = 1,
 IPE_INPUT_DS16 = 2,
 IPE_INPUT_DS64 = 3,
 IPE_INPUT_FULL_REF = 4,
 IPE_INPUT_DS4_REF = 5,
 IPE_INPUT_DS16_REF = 6,
 IPE_INPUT_DS64_REF = 7,
 IPE_OUTPUT_DISPLAY = 8,
 IPE_OUTPUT_VIDEO = 9,
 IPE_OUTPUT_FULL_REF = 10,
 IPE_OUTPUT_DS4_REF = 11,
 IPE_OUTPUT_DS16_REF = 12,
 IPE_OUTPUT_DS64_REF = 13,
 IPE_INPUT2_FULL = 14,
 IPE_INPUT2_DSX = 15,
 IPE_SCRATCH = 16,
};

static inline int ipe_buffer_offset(unsigned int batch, unsigned int port,
 unsigned int plane, unsigned int metadata)
{
 if (batch >= 16 || port >= 62 || plane >= 2 || metadata > 1)
  return -1;
 return 188 + batch * 1608 + port * 16 + plane * 4 + metadata * 8;
}

static inline int ipe_reference_input(unsigned int pass)
{
 return pass < 4 ? (int)pass + IPE_INPUT_FULL_REF : -1;
}

#endif
