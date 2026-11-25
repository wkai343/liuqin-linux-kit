// SPDX-License-Identifier: MIT
// Align short and long NV12 images to the middle exposure. Output the three
// tight NV12 planes followed by a per-pixel reference-only mask (0..255).
#include <opencv2/calib3d.hpp>
#include <opencv2/features2d.hpp>
#include <opencv2/imgproc.hpp>
#include <array>
#include <cmath>
#include <iostream>
#include <algorithm>
#ifdef HDR_VIDEO_LIBRARY
#include <opencv2/core/ocl.hpp>
#endif
#if defined(__aarch64__) && defined(HDR_VIDEO_LIBRARY)
#include "hdr-warp.h"
#endif

using namespace cv;

#ifdef HDR_VIDEO_LIBRARY
// Exact 12x12 area means for the 4K registration image. Accumulate across
// contiguous rows so the three full-resolution luma planes are read once.
static Mat reduceUhd(const Mat &input) {
    Mat small(180, 320, CV_8U);
    for (int y = 0; y < small.rows; ++y) {
        std::array<uint16_t, 3840> columns{};
        for (int dy = 0; dy < 12; ++dy) {
            const uchar *row = input.ptr(12*y + dy);
            for (size_t x = 0; x < columns.size(); ++x)
                columns[x] += row[x];
        }
        uchar *out = small.ptr(y);
        for (int x = 0; x < small.cols; ++x) {
            unsigned sum = 0;
            for (int dx = 0; dx < 12; ++dx)
                sum += columns[12*x + dx];
            out[x] = cvRound(sum * (1.f / 144));
        }
    }
    return small;
}
#endif

struct View {
    Mat y, uv, small, equalized, descriptors;
    std::vector<KeyPoint> points;
    View() = default;
    View(const Mat &raw, int width, int height, int stride, Size reduced) {
        y = Mat(height, width, CV_8U, raw.data, stride);
        uv = Mat(height / 2, width / 2, CV_8UC2, raw.data + size_t(height) * stride, stride);
#ifdef HDR_VIDEO_LIBRARY
        if (y.size() == Size(3840, 2160) && reduced == Size(320, 180))
            small = reduceUhd(y);
        else
#endif
            resize(y, small, reduced, 0, 0, INTER_AREA);
        createCLAHE(2.0, {8, 8})->apply(small, equalized);
        ORB::create(1600)->detectAndCompute(equalized, noArray(), points, descriptors);
    }
};

static Mat alignTo(const View &src, const View &ref, Mat &y, Mat &uv, bool verbose) {
    std::vector<Point2f> a, b;
    if (!src.descriptors.empty() && ref.descriptors.rows >= 2) {
        std::vector<std::vector<DMatch>> pairs;
        BFMatcher(NORM_HAMMING).knnMatch(src.descriptors, ref.descriptors, pairs, 2);
        for (auto &p : pairs) {
            if (p.size() == 2 && p[0].distance < 0.78 * p[1].distance) {
                a.push_back(src.points[p[0].queryIdx].pt);
                b.push_back(ref.points[p[0].trainIdx].pt);
            }
        }
    }
    Mat inliers, transform;
    if (a.size() >= 12)
        transform = estimateAffinePartial2D(a, b, inliers, RANSAC, 2.0);
    bool trusted = false;
    if (!transform.empty()) {
        std::vector<Point2f> locations;
        for (size_t i = 0; i < b.size(); ++i)
            if (inliers.at<uchar>(i))
                locations.push_back(b[i]);
        Rect extent = boundingRect(locations);
        double zoom = std::hypot(transform.at<double>(0, 0), transform.at<double>(0, 1));
        trusted = locations.size() >= 12 && locations.size() * 2 >= a.size() && zoom >= 0.88 &&
                  zoom <= 1.14 && extent.width >= ref.small.cols / 4 &&
                  extent.height >= ref.small.rows / 4 &&
                  std::abs(transform.at<double>(0, 2)) < ref.small.cols * .24 &&
                  std::abs(transform.at<double>(1, 2)) < ref.small.rows * .24;
    }
    // If geometry is ambiguous, compare the identity candidate locally, then
    // select the reference in inconsistent areas. Never warp by a guessed offset.
    if (!trusted)
        transform = Mat::eye(2, 3, CV_64F);
    Mat aligned, valid(src.small.size(), CV_8U, Scalar(255));
    warpAffine(src.small, aligned, transform, ref.small.size(), INTER_LINEAR, BORDER_REPLICATE);
    Mat coverage;
    warpAffine(valid, coverage, transform, ref.small.size(), INTER_LINEAR, BORDER_CONSTANT);
    // Learn the monotonic inter-exposure response from corresponding pixels.
    // Saturated source values have no unique inverse. Compare both observations
    // at the lower exposure, where saturation in the brighter leg is a lower bound.
    Scalar srcMean = mean(aligned), refMean = mean(ref.small);
    bool brighter = srcMean[0] > refMean[0];
    const Mat &low = brighter ? ref.small : aligned, &high = brighter ? aligned : ref.small;
    int hist[32][256] = {};
    for (int j = 0; j < low.rows; j += 2)
        for (int i = 0; i < low.cols; i += 2)
            if (coverage.at<uchar>(j, i) == 255)
                ++hist[low.at<uchar>(j, i) / 8][high.at<uchar>(j, i)];
    std::vector<float> xs, ys;
    for (int k = 0; k < 32; k++) {
        int total = 0, acc = 0, v = 0;
        for (int n : hist[k])
            total += n;
        if (total < 12)
            continue;
        while (v < 255 && (acc += hist[k][v]) < (total + 1) / 2)
            ++v;
        xs.push_back(8 * k + 3.5f);
        ys.push_back(ys.empty() ? v : std::max<float>(ys.back(), v));
    }
    Mat mask(ref.small.size(), CV_8U, Scalar(255));
    if (xs.size() >= 2) {
        Mat lut(1, 256, CV_8U);
        for (int k = 0; k < 256; k++) {
            auto it = std::lower_bound(xs.begin(), xs.end(), k);
            size_t n = it - xs.begin();
            float v = n == 0 ? ys.front()
                      : n == xs.size()
                          ? ys.back()
                          : ys[n - 1] + (ys[n] - ys[n - 1]) * (k - xs[n - 1]) / (xs[n] - xs[n - 1]);
            lut.at<uchar>(k) = saturate_cast<uchar>(v);
        }
        Mat predicted, errors;
        LUT(low, lut, predicted);
        absdiff(predicted, high, errors);
        blur(errors, errors, {3, 3});
        threshold(errors, mask, 20, 255, THRESH_BINARY);
        mask.setTo(255, coverage < 254);
        dilate(mask, mask, getStructuringElement(MORPH_ELLIPSE, {9, 9}));
    }
    // Conjugate the reduced pixel-centre transform into each sample grid. UV
    // has half the luma dimensions and its own chroma sample centres.
    auto warp = [&](const Mat &input, Mat &output) {
        double sx = double(input.cols) / src.small.cols, sy = double(input.rows) / src.small.rows;
        Mat m = transform.clone();
        m.at<double>(0, 1) *= sx / sy;
        m.at<double>(1, 0) *= sy / sx;
        double ox = (sx - 1) / 2, oy = (sy - 1) / 2;
        m.at<double>(0, 2) =
            sx * transform.at<double>(0, 2) + ox - m.at<double>(0, 0) * ox - m.at<double>(0, 1) * oy;
        m.at<double>(1, 2) =
            sy * transform.at<double>(1, 2) + oy - m.at<double>(1, 0) * ox - m.at<double>(1, 1) * oy;
#if defined(__aarch64__) && defined(HDR_VIDEO_LIBRARY)
        if (src.y.cols >= 3840 && warpVideo(input, output, m))
            return;
#endif
        warpAffine(input, output, m, input.size(), INTER_LINEAR, BORDER_REPLICATE);
    };
    if (trusted) {
        warp(src.y, y);
        warp(src.uv, uv);
    } else {
        y = src.y;
        uv = src.uv;
    }
    if (verbose)
        std::cerr << "motion matches=" << a.size()
                  << " inliers=" << (inliers.empty() ? 0 : countNonZero(inliers))
                  << " fitted=" << trusted
                  << " dx=" << transform.at<double>(0, 2) * ref.y.cols / ref.small.cols
                  << " dy=" << transform.at<double>(1, 2) * ref.y.rows / ref.small.rows << "\n";
    return mask;
}

static void registerViews(std::vector<View> &views, std::array<Mat, 3> &y, std::array<Mat, 3> &uv,
                          Mat &full, bool verbose) {
    Mat left = alignTo(views[0], views[1], y[0], uv[0], verbose),
        right = alignTo(views[2], views[1], y[2], uv[2], verbose);
    Mat mask;
    cv::max(left, right, mask);
    GaussianBlur(mask, mask, {5, 5}, 1.2);
    resize(mask, full, views[1].y.size(), 0, 0, INTER_LINEAR);
    y[1] = views[1].y;
    uv[1] = views[1].uv;
}

#ifdef HDR_VIDEO_LIBRARY
extern "C" void hdr_linear_band(const unsigned char *, const unsigned char *, unsigned char *, unsigned char *, int, int);
extern "C" int hdr_prepare_video_sized(const unsigned char *sources, unsigned char *packed,
                                 unsigned char *mask, int w, int h) try {
    if (!((w == 640 && h == 480) || (w == 3840 && h == 2160) || (w == 4080 && h == 2296)))
        return -1;
    const int bytes = w * h * 3 / 2;
    static const bool initialized = []() {
        setNumThreads(2);
        return true;
    }();
    (void)initialized;
    // Registration uses CPU Mat buffers. Probing OpenCL on this worker can
    // hold the first bracket until all three capture slots are occupied.
    cv::ocl::setUseOpenCL(false);
    std::vector<View> views(3);
    parallel_for_(Range(0, 3), [&](const Range &legs) {
        // OpenCV keeps the OpenCL setting per thread, including pool workers.
        cv::ocl::setUseOpenCL(false);
        for (int i = legs.start; i < legs.end; ++i) {
            Mat frame(1, bytes, CV_8U, const_cast<unsigned char *>(sources + i * bytes));
            views[i] = View(frame, w, h, w, Size(320, cvRound(320. * h / w)));
        }
    });
    std::array<Mat, 3> y, uv;
    Mat reject(h, w, CV_8U, mask);
    registerViews(views, y, uv, reject, false);
    // Split all three exposures evenly across the two workers. Keep each
    // pair of luma rows together with its subsampled chroma row.
    const int split = (h / 2) & ~1;
    parallel_for_(Range(0, 6), [&](const Range &range) {
        for (int band = range.start; band < range.end; ++band) {
            int leg = band / 2, row = band % 2 ? split : 0;
            unsigned char *dst = packed + leg * bytes;
            hdr_linear_band(y[leg].ptr(row), uv[leg].ptr(row / 2), dst + row*w,
                            dst + w*h + (row/2)*w, w, row ? h - split : split);
        }
    });
    return 0;
} catch (const std::exception &e) {
    std::cerr << e.what() << "\n";
    return -1;
}
extern "C" int hdr_prepare_video(const unsigned char *sources, unsigned char *packed, unsigned char *mask) {
    return hdr_prepare_video_sized(sources, packed, mask, 640, 480);
}
#else
static void writeMat(const Mat &m) {
    for (int y = 0; y < m.rows; y++)
        std::cout.write(reinterpret_cast<const char *>(m.ptr(y)), m.cols * m.elemSize());
}

int main(int argc, char **argv) try {
    if (argc != 4)
        return 2;
    int w = std::stoi(argv[1]), h = std::stoi(argv[2]), s = std::stoi(argv[3]);
    if (w < 32 || h < 32 || w > 4080 || h > 3060 || s < w || s > 8192 || ((w | h | s) & 1))
        return 2;
    setNumThreads(2);
    Size reduced(std::min(w, 640), cvRound(double(h) * std::min(w, 640) / w));
    std::array<Mat, 3> data, y, uv;
    std::vector<View> views;
    for (auto &raw : data) {
        raw.create(1, s * h * 3 / 2, CV_8U);
        if (!std::cin.read(reinterpret_cast<char *>(raw.data), raw.total()))
            return 1;
        views.emplace_back(raw, w, h, s, reduced);
    }
    if (std::cin.peek() != EOF)
        return 1;
    Mat full;
    registerViews(views, y, uv, full, true);
    std::cerr << "reference fraction=" << mean(full)[0] / 255.0 << "\n";
    for (size_t i = 0; i < 3; i++) {
        writeMat(y[i]);
        writeMat(uv[i]);
    }
    writeMat(full);
    return std::cout.flush() ? 0 : 1;
} catch (const std::exception &e) {
    std::cerr << e.what() << "\n";
    return 1;
}
#endif
