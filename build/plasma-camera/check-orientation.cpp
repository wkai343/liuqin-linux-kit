// SPDX-License-Identifier: GPL-2.0-or-later
#include "liuqinorientation.h"
#include <array>
#include <cstdio>

int main()
{
    QImage chart(5, 3, QImage::Format_RGB32);
    for (int y = 0; y < chart.height(); ++y) {
        for (int x = 0; x < chart.width(); ++x) {
            chart.setPixel(x, y, qRgb(x * 40, y * 80, x + y));
        }
    }

    // RightUp, TopUp, LeftUp, TopDown. Rotation is clockwise in image coordinates.
    constexpr std::array poses{270, 0, 90, 180};
    constexpr std::array rear{0, 90, 180, 270};
    constexpr std::array front{0, 270, 180, 90};
    for (size_t i = 0; i < poses.size(); ++i) {
        if (LiuqinOrientation::degrees(poses[i], false) != rear[i] ||
            LiuqinOrientation::degrees(poses[i], true) != front[i]) {
            std::fprintf(stderr, "Unexpected camera rotation for pose %d\n", poses[i]);
            return 1;
        }
    }

    for (int angle : {0, 90, 180, 270, 450, -90}) {
        const auto image = LiuqinOrientation::rotateStill(chart, angle);
        const auto mirrored = LiuqinOrientation::rotateStill(chart, angle, true);
        const int quarter = ((angle / 90) % 4 + 4) % 4;
        if (mirrored.size() != image.size() || image.size() != (quarter % 2 ? chart.size().transposed() : chart.size())) {
            return 1;
        }
        for (int y = 0; y < chart.height(); ++y) {
            for (int x = 0; x < chart.width(); ++x) {
                const int right = chart.width() - 1 - x;
                const int bottom = chart.height() - 1 - y;
                const std::array dest{QPoint(x, y), QPoint(bottom, x), QPoint(right, bottom), QPoint(y, right)};
                const QPoint target = dest[quarter];
                const QPoint reflected(image.width() - 1 - target.x(), target.y());
                if (image.pixel(target) != chart.pixel(x, y) || mirrored.pixel(reflected) != chart.pixel(x, y)) {
                    return 1;
                }
            }
        }
    }
}
