// SPDX-License-Identifier: MIT
// Bilinear resampling on OpenCV's 1/32-pixel grid for small video motions.
// Inside the image, adjacent output pixels usually share the same pair of
// source rows with consecutive source columns. Read those spans directly.
#pragma once
#include <algorithm>
#include <arm_neon.h>
#include <cmath>
#include <opencv2/imgproc.hpp>
#include <vector>

static inline uint16x8_t warpFractions(const int *delta, int offset) {
    auto base = vdupq_n_s32(offset);
    auto lo = vshrn_n_s32(vaddq_s32(vld1q_s32(delta), base), 5);
    auto hi = vshrn_n_s32(vaddq_s32(vld1q_s32(delta + 4), base), 5);
    return vandq_u16(vreinterpretq_u16_s16(vcombine_s16(lo, hi)), vdupq_n_u16(31));
}
static inline uint8x8_t interpolate8(uint8x8_t a, uint8x8_t b, uint8x8_t c, uint8x8_t d, uint8x8_t fx,
                                     uint16x8_t fy) {
    auto wx = vsub_u8(vdup_n_u8(32), fx);
    auto wy = vsubq_u16(vdupq_n_u16(32), fy);
    auto top = vmlal_u8(vmull_u8(a, wx), b, fx);
    auto bot = vmlal_u8(vmull_u8(c, wx), d, fx);
    auto lo = vmlal_u16(vmull_u16(vget_low_u16(top), vget_low_u16(wy)), vget_low_u16(bot), vget_low_u16(fy));
    auto hi = vmlal_high_u16(vmull_high_u16(top, wy), bot, fy);
    return vmovn_u16(vcombine_u16(vrshrn_n_u32(lo, 10), vrshrn_n_u32(hi, 10)));
}

template <int channels>
static void warpVideoPlane(const cv::Mat &input, cv::Mat &output, const cv::Mat &inverse) {
    const double *m = inverse.ptr<double>();
    int w = input.cols, h = input.rows;
    output.create(input.size(), input.type());
    std::vector<int> dx(w), dy(w);
    for (int x = 0; x < w; ++x) {
        dx[x] = cvRound(1024 * (m[0] * x)) - 1024 * x;
        dy[x] = cvRound(1024 * (m[3] * x));
    }
    cv::parallel_for_(cv::Range(0, h), [&](const cv::Range &rows) {
        for (int y = rows.start; y < rows.end; ++y) {
            int ox = cvRound(1024 * (m[1] * y + m[2])) + 16;
            int oy = cvRound(1024 * (m[4] * y + m[5])) + 16;
            uchar *dst = output.ptr(y);
            for (int x = 0; x < w;) {
                int qx = (ox + dx[x]) >> 5, qy = (oy + dy[x]) >> 5;
                int sx = x + (qx >> 5), sy = qy >> 5;
                constexpr int span = 8;
                if (x + span <= w && sx >= 0 && sx + span < w && sy >= 0 && sy + 1 < h &&
                    (ox + dx[x + span - 1]) >> 10 == (qx >> 5) && (oy + dy[x + span - 1]) >> 10 == sy) {
                    const uchar *a = input.ptr(sy) + channels * sx;
                    const uchar *b = input.ptr(sy + 1) + channels * sx;
                    // Quantized offsets are monotone. Equal integer offsets
                    // at both ends cover the whole span, including both taps.
                    auto fx = vmovn_u16(warpFractions(dx.data() + x, ox));
                    auto fy = warpFractions(dy.data() + x, oy);
                    if constexpr (channels == 1) {
                        vst1_u8(dst + x,
                                interpolate8(vld1_u8(a), vld1_u8(a + 1), vld1_u8(b), vld1_u8(b + 1), fx, fy));
                    } else {
                        auto tl = vld2_u8(a), tr = vld2_u8(a + 2), bl = vld2_u8(b), br = vld2_u8(b + 2);
                        uint8x8x2_t uv;
                        for (int k = 0; k < 2; ++k)
                            uv.val[k] = interpolate8(tl.val[k], tr.val[k], bl.val[k], br.val[k], fx, fy);
                        vst2_u8(dst + 2 * x, uv);
                    }
                    x += span;
                } else {
                    int x0 = std::clamp(sx, 0, w - 1), x1 = std::clamp(sx + 1, 0, w - 1);
                    auto a = input.ptr(std::clamp(sy, 0, h - 1)), b = input.ptr(std::clamp(sy + 1, 0, h - 1));
                    unsigned fx = qx & 31, fy = qy & 31;
                    for (int c = 0; c < channels; ++c) {
                        unsigned top = (32 - fx) * a[channels * x0 + c] + fx * a[channels * x1 + c];
                        unsigned bot = (32 - fx) * b[channels * x0 + c] + fx * b[channels * x1 + c];
                        dst[x * channels + c] = ((32 - fy) * top + fy * bot + 512) >> 10;
                    }
                    ++x;
                }
            }
        }
    });
}
static bool warpVideo(const cv::Mat &input, cv::Mat &output, const cv::Mat &forward) {
    cv::Mat inverse;
    cv::invertAffineTransform(forward, inverse);
    const double *m = inverse.ptr<double>();
    if (std::abs(m[0] - 1) > .03 || std::abs(m[3]) > .03)
        return false;
    if (input.type() == CV_8UC1)
        warpVideoPlane<1>(input, output, inverse);
    else if (input.type() == CV_8UC2)
        warpVideoPlane<2>(input, output, inverse);
    else
        return false;
    return true;
}
