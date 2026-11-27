// SPDX-License-Identifier: MIT
/* Convert full-range, sRGB-encoded IFE NV12 and scene-linear HDR401 surfaces. Fusion is
 * performed by the IPE. Rows in the captured NV12 may include stride padding. */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(__aarch64__) && defined(HDR_VIDEO_LIBRARY)
#include <arm_neon.h>
#endif

static uint8_t component(double v)
{
 return (uint8_t)lrint(fmin(255., fmax(0., v)));
}

#if defined(__aarch64__) && defined(HDR_VIDEO_LIBRARY)
/* AArch64 TBL looks up 64 bytes at a time. Saturating narrowed indices
 * reproduce the black/white padding of the scalar inverse-sRGB table. */
struct gamma_tables { uint8x16x4_t bank[4]; };

static inline uint8x16_t gamma16(const struct gamma_tables *t, uint8x16_t i)
{
 uint8x16_t v = vqtbl4q_u8(t->bank[0], i);
 for (int k = 1; k < 4; ++k) {
  i = vsubq_u8(i, vdupq_n_u8(64));
  v = vqtbx4q_u8(v, t->bank[k], i);
 }
 return v;
}

static inline uint8x16_t linear_row(uint8x16_t y, int16x8_t offset, const struct gamma_tables *t)
{
 int16x8_t lo = vreinterpretq_s16_u16(vmovl_u8(vget_low_u8(y)));
 int16x8_t hi = vreinterpretq_s16_u16(vmovl_high_u8(y));
 lo = vaddq_s16(lo, vzip1q_s16(offset, offset));
 hi = vaddq_s16(hi, vzip2q_s16(offset, offset));
 return gamma16(t, vcombine_u8(vqmovun_s16(lo), vqmovun_s16(hi)));
}

/* Process eight chroma pairs (two rows). Green coefficients are exact
 * ten-thousandths. For half codes preserve the scalar double/ties-to-even
 * behavior, including the parity of the luma sample. */
static inline int linear_block(const uint8_t *row, const uint8_t *uv, uint8_t *out,
                               uint8_t *chroma, int stride, int width, const struct gamma_tables *gamma)
{
 uint8x8x2_t pair = vld2_u8(uv);
 int16x8_t u = vreinterpretq_s16_u16(vsubl_u8(pair.val[0], vdup_n_u8(128)));
 int16x8_t v = vreinterpretq_s16_u16(vsubl_u8(pair.val[1], vdup_n_u8(128)));
 int32x4_t gl = vmlal_n_s16(vmull_n_s16(vget_low_s16(u), -3457), vget_low_s16(v), -7168);
 int32x4_t gh = vmlal_high_n_s16(vmull_high_n_s16(u, -3457), v, -7168);
 int32x4_t dl = vcvtnq_s32_f32(vmulq_n_f32(vcvtq_f32_s32(gl), .0001f));
 int32x4_t dh = vcvtnq_s32_f32(vmulq_n_f32(vcvtq_f32_s32(gh), .0001f));
 if (vmaxvq_s32(vmaxq_s32(vabsq_s32(vmlsq_n_s32(gl, dl, 10000)),
                            vabsq_s32(vmlsq_n_s32(gh, dh, 10000)))) == 5000)
  return 0;
 int16x8_t dg = vcombine_s16(vmovn_s32(dl), vmovn_s32(dh));
 /* These Q14 coefficients round identically to the original doubles for
  * every signed 8-bit U and V. No red or blue offset is a half code. */
 int16x8_t db = vcombine_s16(vrshrn_n_s32(vmull_n_s16(vget_low_s16(u), 29152), 14),
                              vrshrn_n_s32(vmull_high_n_s16(u, 29152), 14));
 int16x8_t dr = vcombine_s16(vrshrn_n_s32(vmull_n_s16(vget_low_s16(v), 23056), 14),
                              vrshrn_n_s32(vmull_high_n_s16(v, 23056), 14));
 uint8x16_t y0 = vld1q_u8(row), y1 = vld1q_u8(row + stride);
 vst1q_u8(out, linear_row(y0, dg, gamma));
 vst1q_u8(out + width, linear_row(y1, dg, gamma));
 uint16x8_t blue = vaddq_u16(vpaddlq_u8(linear_row(y0, db, gamma)), vpaddlq_u8(linear_row(y1, db, gamma)));
 uint16x8_t red = vaddq_u16(vpaddlq_u8(linear_row(y0, dr, gamma)), vpaddlq_u8(linear_row(y1, dr, gamma)));
 uint8x8x2_t br = {{ vrshrn_n_u16(blue, 2), vrshrn_n_u16(red, 2) }};
 vst2_u8(chroma, br);
 return 1;
}
#endif

static void pack(const uint8_t *src, const uint8_t *uv, uint8_t *dst, uint8_t *dst_uv,
                 int w, int h, int stride, int linear)
{
 /* Signed chroma offsets can extend 8-bit RGB into [-228, 481]. Pad the
  * LUT so four pixels can reuse each chroma shift without per-pixel pow/clamp. */
 uint8_t table[768], *curve = table + 256;
 memset(table, 0, 256);
 memset(curve + 256, 255, 256);
 for (unsigned i = 0; i < 256; i++) {
  double v = i / 255.;
  curve[i] = linear ? component(255. * (v <= 0.04045 ? v / 12.92 : pow((v + .055) / 1.055, 2.4))) : i;
 }

#if defined(__aarch64__) && defined(HDR_VIDEO_LIBRARY)
 struct gamma_tables gamma;
 for (int i = 0; i < 4; ++i) gamma.bank[i] = vld1q_u8_x4(curve + 64*i);
#endif
 for (int y = 0; y < h; y += 2) {
  for (int x = 0; x < w; x += 2) {
#if defined(__aarch64__) && defined(HDR_VIDEO_LIBRARY)
   if (linear && x+16 <= w && linear_block(src + y*stride+x, uv + y/2*stride+x, dst + y*w+x,
                                           dst_uv + y/2*w+x, stride, w, &gamma)) {
    x += 14;
    continue;
   }
#endif
   int src_uv = (y / 2) * stride + x;
   int u = uv[src_uv] - 128, v = uv[src_uv + 1] - 128, sum = 0;
   unsigned blue = 0, red = 0;
   /* Neither coefficient times an 8-bit chroma offset can be a half integer. */
   int db = linear ? (int)lrint(1.7793 * u) : 0, dr = linear ? (int)lrint(1.40723 * v) : 0;
   double offset = -0.3457 * u - 0.7168 * v;
   /* Rounding commutes with an integer luma offset except at half codes.
    * Keep the original ties-to-even operation for those chroma pairs. */
   int dg = (int)lrint(offset);
   int tie = fabs(fabs(offset - dg) - .5) < 1e-10;
   for (int dy = 0; dy < 2; dy++)
    for (int dx = 0; dx < 2; dx++) {
     int luma = src[(y + dy) * stride + x + dx];
     if (linear) {
      /* Average light, not gamma codes, in the subsampled B/R plane. */
      blue += curve[luma + db];
      red += curve[luma + dr];
     } else {
      sum += luma;
     }
     dst[(y + dy) * w + x + dx] = tie ? curve[component(luma + offset)] : curve[luma + dg];
    }
   int out = (y / 2) * w + x;
   dst_uv[out] = linear ? (blue + 2) / 4 : component(sum * 0.25 + 1.7793 * u);
   dst_uv[out + 1] = linear ? (red + 2) / 4 : component(sum * 0.25 + 1.40723 * v);
  }
 }
}

#ifndef HDR_VIDEO_LIBRARY
static int render(const uint8_t *src, uint8_t *dst, int w, int h, double gain)
{
 /* MAC unity is 255 in the middle exposure. Apply the shared highlight
  * shoulder in linear light, then encode each channel for an SDR JPEG. */
 uint8_t srgb[4096];
 for (unsigned i = 0; i < 4096; i++) {
  double v = i / 4095.;
  srgb[i] = component(255. * (v <= .0031308 ? 12.92 * v : 1.055 * pow(v, 1./2.4) - .055));
 }
 /* Fade the AE lift from low midtones (linear 20, sRGB ~79); anchor
  * brighter fabric and the highlight shoulder at linear 100 (sRGB ~168).
  * A single scale preserves linear RGB ratios. The slope stays positive:
  * x/(1+(x/20)^2) has slope >= -1/8; tapering it costs at most 4/25
  * more, even at the maximum gain (1 - 3*(1/8+4/25) > 0). */
 double scales[1024];
 for (int peak = 0; peak < 1024; peak++) {
  double scale = 1.;
  if (peak < 100) {
   double mid = peak / 20., high = peak / 100.;
   double taper = 1. - high*high;
   scale += (gain - 1.) * taper*taper / (1. + mid*mid);
  } else if (peak > 176) {
   double d = peak - 176;
   scale = (176. + 79. * d / (79. + d)) / peak;
  }
  scales[peak] = scale * (4095. / 255.);
 }
 const uint16_t *samples = (const uint16_t *)src;
 for (int y = 0; y < h; y++) {
  for (int x = 0; x < w; x++) {
   int pos = y * w + x, uv = h * w + (y / 2) * w + (x & ~1);
   unsigned r = samples[uv + 1], g = samples[pos], b = samples[uv];
   if ((r | g | b) & 63) return -1;
   r >>= 6; g >>= 6; b >>= 6;
   unsigned peak = r > b ? r : b;
   if (g > peak) peak = g;
   double scale = scales[peak];
   dst[3*pos] = srgb[lrint(r * scale)];
   dst[3*pos + 1] = srgb[lrint(g * scale)];
   dst[3*pos + 2] = srgb[lrint(b * scale)];
  }
 }
 return 0;
}

#endif

/* For occlusions, use the actual middle sample at the MAC's unity
 * exposure. Share the most conservative luma mask in each chroma block. */
static inline uint16_t blend_sample(uint16_t fused, uint8_t reference, unsigned mask)
{
 return (((fused >> 6) * (255 - mask) + reference * mask + 127) / 255) << 6;
}

static void deghost(uint16_t *p, const uint8_t *middle, const uint8_t *mask, int w, int h)
{
 int pixels = w * h;
 for (int y = 0; y < h; y += 2) {
  const uint8_t *row = mask + y * w;
  int uv = pixels + (y / 2) * w;
  for (int start = 0; start < w; start += 32) {
   int count = w - start < 32 ? w - start : 32;
   int top = y*w + start, bottom = top + w;
   static const uint8_t zero[32] = {};
   if (!memcmp(row + start, zero, count) && !memcmp(row + w + start, zero, count)) {
    /* A zero mask keeps all ten bits. Clear any unused low bits just as
     * blend_sample does, including on diagnostic input with dirty padding. */
    for (int k = 0; k < count; k++) {
     p[top+k] &= 0xffc0;
     p[bottom+k] &= 0xffc0;
     p[uv+start+k] &= 0xffc0;
    }
    continue;
   }
   for (int k = 0; k < count; k++) {
    p[top+k] = blend_sample(p[top+k], middle[top+k], mask[top+k]);
    p[bottom+k] = blend_sample(p[bottom+k], middle[bottom+k], mask[bottom+k]);
   }
   for (int x = start; x < start + count; x += 2) {
    unsigned m = row[x];
    if (row[x + 1] > m) m = row[x + 1];
    if (row[w + x] > m) m = row[w + x];
    if (row[w + x + 1] > m) m = row[w + x + 1];
    p[uv + x] = blend_sample(p[uv + x], middle[uv + x], m);
    p[uv + x + 1] = blend_sample(p[uv + x + 1], middle[uv + x + 1], m);
   }
  }
 }
}

/* Cache complete 10-bit RGB triplets. Subsampled chroma and smooth exposure
 * regions often repeat the same triplet across several neighbouring blocks. */
struct hlg_sample {
 uint32_t key;
 uint16_t y;
 double cb, cr;
};

static inline void hlg_sample(struct hlg_sample *entry, const double *curve,
                             unsigned r, unsigned g, unsigned b, uint32_t key)
{
 double rr = curve[lrint(.627404*r + .329283*g + .043313*b)];
 double gg = curve[lrint(.069097*r + .919540*g + .011363*b)];
 double bb = curve[lrint(.016391*r + .088013*g + .895596*b)];
 double l = .2627*rr + .6780*gg + .0593*bb;
 entry->key = key;
 entry->y = lrint(64 + 876*l);
 entry->cb = (bb-l) / 1.8814;
 entry->cr = (rr-l) / 1.4746;
}

#ifdef HDR_VIDEO_LIBRARY
/* Low scene-linear codes occur frequently in indoor footage. Build each
 * chroma row on first use and combine four entries for each 2x2 block. */
#define HLG_LOW 64
static _Thread_local uint64_t low_rgb[HLG_LOW][HLG_LOW][HLG_LOW];
static _Thread_local uint8_t low_ready[HLG_LOW][HLG_LOW];

static inline const uint64_t *hlg_low_row(const double *curve, unsigned r, unsigned b)
{
 uint64_t *row = low_rgb[r][b];
 if (!low_ready[r][b]) {
  for (unsigned g = 0; g < HLG_LOW; g++) {
   struct hlg_sample v;
   hlg_sample(&v, curve, r, g, b, 1);
   /* The four Y codes sum to at most 3760. Each biased chroma sum is
    * in [64,960] with 16 fractional bits, so 12+26+26 bits suffice. */
   uint64_t cb = 8388608 + lrint((224. * 65536.) * v.cb);
   uint64_t cr = 8388608 + lrint((224. * 65536.) * v.cr);
   row[g] = v.y | (cb << 12) | (cr << 38);
  }
  low_ready[r][b] = 1;
 }
 return row;
}
#endif

/* One row of 2x2 chroma blocks. A NULL cache takes the direct path for
 * high-entropy pixels; both paths use the same rounding and accumulation. */
static inline unsigned hlg_blocks(const uint16_t *src, uint16_t *dst, int w, int pixels,
                                  int y, int first, int end, const double *curve,
                                  struct hlg_sample *cache, int count_hits, int try_low)
{
 unsigned hits = 0;
 for (int x = first; x < end; x += 2) {
  int cp = (y/2)*w + x;
  unsigned r = src[pixels+cp+1] >> 6, b = src[pixels+cp] >> 6;
#ifdef HDR_VIDEO_LIBRARY
  if (try_low && (r | b) < HLG_LOW) {
   int top = y*w+x, bot = top+w;
   unsigned g0 = src[top] >> 6, g1 = src[top+1] >> 6;
   unsigned g2 = src[bot] >> 6, g3 = src[bot+1] >> 6;
   if ((g0 | g1 | g2 | g3) < HLG_LOW) {
    const uint64_t *v = hlg_low_row(curve, r, b);
    uint64_t tl = v[g0], tr = v[g1], bl = v[g2], br = v[g3];
    uint64_t sum = tl + tr + bl + br;
    unsigned cb = (sum >> 12) & 0x3ffffff, cr = sum >> 38;
    /* The sum of four rounded contributions is within 2/65536 of the
     * original code. At a half-code boundary use the full precision path
     * below, including its original summation order and ties-to-even. */
    unsigned cb_frac = cb & 65535, cr_frac = cr & 65535;
    if ((cb_frac < 32766 || cb_frac > 32770) && (cr_frac < 32766 || cr_frac > 32770)) {
     dst[top] = tl & 1023; dst[top+1] = tr & 1023;
     dst[bot] = bl & 1023; dst[bot+1] = br & 1023;
     dst[pixels+cp/2] = (cb + 32768) >> 16;
     dst[pixels+pixels/4+cp/2] = (cr + 32768) >> 16;
     if (count_hits) hits += 4;
     continue;
    }
   }
  }
#else
  (void)try_low;
#endif
  unsigned chroma = (r << 20) | (b << 10), hash = r*521 + b*131;
  double cb = 0, cr = 0;
  for (int dy = 0; dy < 2; dy++) for (int dx = 0; dx < 2; dx++) {
   int i = (y+dy)*w + x+dx;
   unsigned g = src[i] >> 6;
   uint32_t key = (chroma | g) + 1;
   struct hlg_sample direct;
   struct hlg_sample *entry = cache ? &cache[(hash+g) & 4095] : &direct;
   if (!cache || entry->key != key) hlg_sample(entry, curve, r, g, b, key);
   else if (count_hits) ++hits;
   dst[i] = entry->y;
   cb += entry->cb;
   cr += entry->cr;
  }
  dst[pixels+cp/2] = lrint(512 + 224*cb);
  dst[pixels+pixels/4+cp/2] = lrint(512 + 224*cr);
 }
 return hits;
}

/* Linear BT.709 camera RGB -> BT.2020 primaries -> BT.2100 HLG OETF,
 * then limited-range BT.2020 non-constant-luminance I420_10LE. */
static void hlg(const uint16_t *src, uint16_t *dst, int w, int h)
{
 double curve[1024];
 for (int i = 0; i < 1024; i++) {
  double v = i / 1023.;
  curve[i] = v <= 1./12 ? sqrt(3*v) : .17883277*log(12*v-.28466892)+.55991073;
 }
 int pixels = w*h;
#ifdef HDR_VIDEO_LIBRARY
 struct hlg_sample cache[4096] = {0};
 int probe = w < 128 ? w : 128;
 for (int y = 0; y < h; y += 2) {
  unsigned hits = hlg_blocks(src, dst, w, pixels, y, 0, probe, curve, cache, 1, 1);
  if (hits > (unsigned)probe / 2)
   hlg_blocks(src, dst, w, pixels, y, probe, w, curve, cache, 0, 1);
  else
   hlg_blocks(src, dst, w, pixels, y, probe, w, curve, NULL, 0, 0);
 }
#else
 for (int y = 0; y < h; y += 2)
  hlg_blocks(src, dst, w, pixels, y, 0, w, curve, NULL, 0, 0);
#endif
}

#ifdef HDR_VIDEO_LIBRARY
void hdr_linear_band(const uint8_t *src, const uint8_t *uv, uint8_t *dst, uint8_t *dst_uv, int w, int rows)
{
 pack(src, uv, dst, dst_uv, w, rows, w, 1);
}
void hdr_linear_planes(const uint8_t *src, const uint8_t *uv, uint8_t *dst, int w, int h)
{
 hdr_linear_band(src, uv, dst, dst + (size_t)w*h, w, h);
}
void hdr_linear(const uint8_t *src, uint8_t *dst, int w, int h)
{
 hdr_linear_planes(src, src + (size_t)w * h, dst, w, h);
}
void hdr_video_output_sized(uint16_t *src, const uint8_t *middle, const uint8_t *mask, uint16_t *out, int w, int h)
{
 deghost(src, middle, mask, w, h);
 hlg(src, out, w, h);
}
void hdr_video_output(uint16_t *src, const uint8_t *middle, const uint8_t *mask, uint16_t *out)
{
 hdr_video_output_sized(src, middle, mask, out, 640, 480);
}
#else
int main(int argc, char **argv)
{
 if (argc != 5 && argc != 6) return 2;
 double gain = 1.;
 if (argc == 6) {
  char *end;
  gain = strtod(argv[5], &end);
  if (strcmp(argv[1], "render") || *end || !isfinite(gain) || gain < 1. || gain > 4.) return 2;
 }
 int w = atoi(argv[2]), h = atoi(argv[3]), stride = atoi(argv[4]);
 int linear = !strcmp(argv[1], "linear");
 int encode = linear || !strcmp(argv[1], "pack");
 int blend = !strcmp(argv[1], "deghost");
 int video = !strcmp(argv[1], "hlg");
 if ((!encode && !blend && !video && strcmp(argv[1], "render")) || w < 2 || h < 2 || (w | h | stride) & 1 ||
     w > 4080 || h > 3060 || stride < w || stride > 8192 || (!encode && stride != w)) return 2;
 size_t in = encode ? (size_t)stride*h*3/2 : (size_t)w*h*3;
 size_t out = encode ? (size_t)w*h*3/2 : (size_t)w*h*3;
 size_t pixels = (size_t)w*h;
 if (blend) in += pixels*5/2;
 uint8_t *src = malloc(in), *dst = malloc(out);
 int rc = 1;
 if (!src || !dst || fread(src, 1, in, stdin) != in || getchar() != EOF) goto done;
 if (encode) pack(src, src + (size_t)stride * h, dst, dst + pixels, w, h, stride, linear);
 else if (blend) {
  memcpy(dst,src,out);
  deghost((uint16_t *)dst,src+out,src+out+pixels*3/2,w,h);
 }
 else if (video) hlg((const uint16_t *)src,(uint16_t *)dst,w,h);
 else if (render(src, dst, w, h, gain)) goto done;
 if (fwrite(dst, 1, out, stdout) == out && fflush(stdout) == 0) rc = 0;
done:
 free(src);
 free(dst);
 return rc;
}
#endif
