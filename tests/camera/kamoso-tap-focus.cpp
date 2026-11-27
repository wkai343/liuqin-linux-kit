// SPDX-License-Identifier: GPL-2.0-or-later
// Build with the patched Kamoso src/video/tapfocus.cpp, Qt6Core and gstreamer-video-1.0.
#include "tapfocus.h"
#include <gst/video/video.h>
#include <array>
#include <cmath>
#include <iostream>

namespace {
bool check(const char *filters, QSize item, QPointF tap, std::optional<QPointF> expected)
{
    std::string description = "videotestsrc name=camera ! video/x-raw,format=I420,width=320,height=240 ! ";
    description += filters;
    description += " ! fakesink name=view";
    GError *error = nullptr;
    GstElement *pipeline = gst_parse_launch(description.c_str(), &error);
    if (error || !pipeline) {
        std::cerr << (error ? error->message : "no pipeline") << '\n';
        return false;
    }
    gst_element_set_state(pipeline, GST_STATE_PAUSED);
    GstState state;
    gst_element_get_state(pipeline, &state, nullptr, 3 * GST_SECOND);
    GstElement *camera = gst_bin_get_by_name(GST_BIN(pipeline), "camera");
    GstElement *view = gst_bin_get_by_name(GST_BIN(pipeline), "view");
    GstPad *src = gst_element_get_static_pad(camera, "src");
    GstPad *sink = gst_element_get_static_pad(view, "sink");
    const auto actual = cameraFocusPoint(sink, src, item, tap);
    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(src);
    gst_object_unref(sink);
    gst_object_unref(camera);
    gst_object_unref(view);
    gst_object_unref(pipeline);
    const bool ok = state == GST_STATE_PAUSED && bool(actual) == bool(expected) &&
                    (!expected || (std::abs(actual->x() - expected->x()) < 0.002 &&
                                   std::abs(actual->y() - expected->y()) < 0.002));
    if (!ok) {
        std::cerr << filters << " @ " << tap.x() << ',' << tap.y() << " -> ";
        if (actual) std::cerr << actual->x() << ',' << actual->y();
        else std::cerr << "outside";
        std::cerr << '\n';
    }
    return ok;
}
}

int main(int argc, char **argv)
{
    gst_init(&argc, &argv);
    struct Orientation { const char *name; bool transpose; QPointF expected; };
    const std::array<Orientation, 8> cases{{
        {"identity", false, {0.25, 0.375}}, {"90r", true, {0.375, 0.75}},
        {"180", false, {0.75, 0.625}}, {"90l", true, {0.625, 0.25}},
        {"horiz", false, {0.75, 0.375}}, {"vert", false, {0.25, 0.625}},
        {"ul-lr", true, {0.375, 0.25}}, {"ur-ll", true, {0.625, 0.75}},
    }};
    bool ok = true;
    for (const auto &test : cases) {
        QSize size = test.transpose ? QSize(240, 320) : QSize(320, 240);
        std::string flip = std::string("videoflip video-direction=") + test.name;
        ok &= check(flip.c_str(), size, {0.25 * size.width(), 0.375 * size.height()}, test.expected);
    }
    ok &= check("videoflip video-direction=90r ! videoflip video-direction=horiz",
                {240, 320}, {60, 120}, {{0.375, 0.25}});
    const char *scale = "videoscale ! video/x-raw,width=160,height=120";
    ok &= check(scale, {800, 400}, {266.25, 150}, {{0.25, 0.375}});
    ok &= check(scale, {800, 400}, {100, 150}, {});
    ok &= check(scale, {320, 400}, {80, 79}, {});
    ok &= check(scale, {320, 400}, {80, 320}, {});
    ok &= check("videocrop left=80 top=24", {240, 216}, {60, 81}, {{140./320, 105./240}});
    ok &= check("identity", {320, 240}, {0, 0}, {{0, 0}});
    ok &= check("identity", {320, 240}, {319, 239}, {{319./320, 239./240}});
    if (ok) std::cout << "16 preview geometry cases passed\n";
    return ok ? 0 : 1;
}
