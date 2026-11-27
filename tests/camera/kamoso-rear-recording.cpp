// SPDX-License-Identifier: MIT
// Compile with rearrecording.cpp from the patched Kamoso tree and
// pkg-config --cflags --libs Qt6Core gstreamer-check-1.0 gstreamer-base-1.0.
#include "rearrecording.h"
#include <algorithm>
#include <gst/base/gstbasesrc.h>
#include <gst/check/gstharness.h>
#include <gst/check/gsttestclock.h>

namespace {
using ClockTestDevice = GstDevice;
using ClockTestDeviceClass = GstDeviceClass;
G_DEFINE_TYPE(ClockTestDevice, clock_test_device, GST_TYPE_DEVICE)
void clock_test_device_class_init(ClockTestDeviceClass *klass)
{
    klass->create_element = [](GstDevice *, const gchar *name) {
        return gst_element_factory_make("pipewiresrc", name);
    };
    klass->reconfigure_element = [](GstDevice *, GstElement *source) -> gboolean {
        g_object_set(source, "target-object", "42", nullptr);
        return true;
    };
}
void clock_test_device_init(ClockTestDevice *) {}

void checkCaptureTime()
{
    g_autoptr(GstDevice) device = GST_DEVICE(g_object_new(clock_test_device_get_type(), nullptr));
    g_autoptr(GstElement) source = createRearCameraSource(device, "camera");
    g_assert_nonnull(source);
    g_autofree char *node = nullptr;
    g_object_get(source, "target-object", &node, nullptr);
    g_assert_cmpstr(node, ==, "42");
    g_autoptr(GstClock) clock = gst_system_clock_obtain();
    g_assert_true(gst_element_set_clock(source, clock));
    const auto base = gst_clock_get_time(clock) - 10 * GST_SECOND;
    gst_element_set_base_time(source, base);
    auto *src = GST_BASE_SRC(source);
    auto getTimes = GST_BASE_SRC_GET_CLASS(src)->get_times;
    g_autoptr(GstBuffer) buffer = gst_buffer_new();
    GstClockTime start = 0, end = 0;
    for (unsigned second : {2U, 3U}) {
        GST_BUFFER_PTS(buffer) = base + second * GST_SECOND;
        GST_BUFFER_DTS(buffer) = GST_BUFFER_PTS(buffer) - GST_MSECOND;
        getTimes(src, buffer, &start, &end);
        g_assert_cmpuint(GST_BUFFER_PTS(buffer), ==, second * GST_SECOND);
        g_assert_cmpuint(GST_BUFFER_DTS(buffer), ==, second * GST_SECOND - GST_MSECOND);
        g_assert_cmpuint(start, ==, GST_CLOCK_TIME_NONE);
        g_assert_cmpuint(end, ==, GST_CLOCK_TIME_NONE);
    }
    // A provider delivering relative PTS still gets its own scheduling.
    gst_base_src_set_live(src, true);
    GST_BUFFER_PTS(buffer) = GST_SECOND;
    getTimes(src, buffer, &start, &end);
    g_assert_cmpuint(GST_BUFFER_PTS(buffer), ==, GST_SECOND);
    g_assert_cmpuint(start, ==, GST_SECOND);
}

void expectSegment(GstHarness *branch, GstClockTime first, GstClockTime shutter)
{
    GstSegment original;
    gst_segment_init(&original, GST_FORMAT_TIME);
    original.start = original.position = first;
    GstEvent *event = gst_event_new_segment(&original);
    const auto seq = gst_event_get_seqnum(event);
    g_assert_true(gst_harness_push_event(branch, event));
    while ((event = gst_harness_try_pull_event(branch))) {
        if (GST_EVENT_TYPE(event) != GST_EVENT_SEGMENT) {
            gst_event_unref(event);
            continue;
        }
        const GstSegment *actual;
        gst_event_parse_segment(event, &actual);
        const auto start = std::max(first, shutter);
        g_assert_cmpuint(gst_event_get_seqnum(event), ==, seq);
        g_assert_cmpuint(actual->start, ==, start);
        g_assert_cmpuint(gst_segment_to_running_time(actual, GST_FORMAT_TIME, start),
                         ==, start - shutter);
        g_assert_cmpuint(gst_segment_to_running_time(actual, GST_FORMAT_TIME, start + GST_SECOND),
                         ==, start + GST_SECOND - shutter);
        if (first < shutter)
            g_assert_false(gst_segment_clip(actual, GST_FORMAT_TIME, first, shutter, nullptr, nullptr));
        gst_event_unref(event);
        return;
    }
    g_error("Missing recording segment");
}
}

int main(int argc, char **argv)
{
    gst_init(&argc, &argv);
    checkCaptureTime();
    RearRecording recording;
    g_autoptr(GstElement) bin = gst_element_factory_make("camerabin", nullptr);
    g_assert_nonnull(bin);
    auto *pipeline = GST_PIPELINE(bin);
    g_autoptr(GstElement) camera = GST_ELEMENT(gst_object_ref_sink(gst_element_factory_make("fakesrc", nullptr)));
    g_autoptr(GstElement) wrapped = recording.cameraSource(camera, true);
    recording.configure(pipeline, true);
    g_autoptr(GstClock) clock = gst_test_clock_new_with_start_time(20 * GST_SECOND);
    g_assert_true(gst_element_set_clock(bin, clock));
    gst_element_set_base_time(bin, 10 * GST_SECOND);
    g_assert_false(recording.begin(pipeline));

    g_autoptr(GstPad) src = gst_element_get_static_pad(camera, "src");
    g_autoptr(GstPad) input = gst_pad_get_peer(src);
    g_assert_true(gst_pad_set_active(input, true));
    g_autoptr(GstCaps) native = gst_caps_from_string(
        "video/x-raw(memory:DMABuf),format=NV12,width=4080,height=3060,framerate=0/1,colorimetry=1:4:7:1");
    g_autoptr(GstEvent) capsEvent = gst_event_new_caps(native);
    g_assert_cmpint(gst_pad_store_sticky_event(input, capsEvent), ==, GST_FLOW_OK);
    g_assert_true(recording.begin(pipeline));
    g_autoptr(GstElement) filter = gst_pad_get_parent_element(input);
    g_autoptr(GstCaps) pinned = nullptr;
    g_object_get(filter, "caps", &pinned, nullptr);
    g_assert_true(gst_caps_is_equal(pinned, native));

    g_autoptr(GstElement) audio = nullptr;
    g_autoptr(GstElement) video = nullptr;
    g_object_get(pipeline, "audio-filter", &audio, "video-filter", &video, nullptr);
    auto *mic = gst_harness_new_with_element(audio, "sink", "src");
    auto *cam = gst_harness_new_with_element(video, "sink", "src");
    expectSegment(mic, 10 * GST_SECOND + 50 * GST_MSECOND, 10 * GST_SECOND);
    expectSegment(cam, 10 * GST_SECOND + 550 * GST_MSECOND, 10 * GST_SECOND);

    gst_test_clock_set_time(GST_TEST_CLOCK(clock), 40 * GST_SECOND);
    g_assert_true(recording.begin(pipeline));
    expectSegment(cam, 30 * GST_SECOND - 20 * GST_MSECOND, 30 * GST_SECOND);
    expectSegment(mic, 30 * GST_SECOND + 180 * GST_MSECOND, 30 * GST_SECOND);
    gst_harness_teardown(mic);
    gst_harness_teardown(cam);

    recording.releaseMode();
    g_autoptr(GstCaps) released = nullptr;
    g_object_get(filter, "caps", &released, nullptr);
    g_assert_true(gst_caps_is_any(released));

    recording.configure(pipeline, false);
    g_autoptr(GstElement) front = recording.cameraSource(camera, false);
    g_assert_true(front == camera);
    g_assert_true(recording.begin(pipeline));
    GstElement *frontAudio = nullptr, *frontVideo = nullptr;
    g_object_get(bin, "audio-filter", &frontAudio, "video-filter", &frontVideo, nullptr);
    g_assert_null(frontAudio);
    g_assert_null(frontVideo);
}
