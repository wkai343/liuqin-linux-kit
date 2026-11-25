// SPDX-License-Identifier: MIT
// Run on AArch64. Compare the recorder's bilinear sampler with OpenCV,
// including padded input, partial vector spans and replicated borders.
#include "camera-icp/hdr-warp.h"
#include <iostream>

int main()
{
    cv::setNumThreads(2);
    cv::RNG rng(596);
    for (cv::Size size : {cv::Size(71, 37), cv::Size(3840, 2160), cv::Size(1920, 1080)}) {
        for (int type : {CV_8UC1, CV_8UC2}) {
            cv::Mat padded(size.height, size.width + 7, type);
            cv::Mat input = padded(cv::Rect({}, size));
            rng.fill(input, cv::RNG::UNIFORM, 0, 256);
            for (int i = 0; i < 16; ++i) {
                double angle = i < 4 ? 0 : rng.uniform(-1.2, 1.2);
                double zoom = i < 4 ? 1 : rng.uniform(.989, 1.011);
                cv::Mat m = cv::getRotationMatrix2D({size.width / 2.f, size.height / 2.f}, angle, zoom);
                double x = i < 4 ? i / 32. : rng.uniform(-25., 25.);
                m.at<double>(0, 2) += x;
                m.at<double>(1, 2) -= x + .5;
                cv::Mat reference, result;
                cv::warpAffine(input, reference, m, size, cv::INTER_LINEAR, cv::BORDER_REPLICATE);
                if (!warpVideo(input, result, m) || cv::norm(reference, result, cv::NORM_INF) != 0) {
                    std::cerr << "warp mismatch " << size << " type " << type << " " << m << "\n";
                    return 1;
                }
            }
            cv::Mat out;
            if (warpVideo(input, out, cv::getRotationMatrix2D({}, 12., 1.)))
                return 1;
        }
    }
    std::cout << "PASS all 96 luma/chroma comparisons and large-motion fallback\n";
}
