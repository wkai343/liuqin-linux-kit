// SPDX-License-Identifier: MIT
// Exercise the patched Kamoso pad probe with raw buffers; no LED or camera is opened.
#include "flashframewindow.h"
#include <cassert>
#include <cstdio>
#include <fstream>

using namespace FlashCapture;
constexpr GstClockTime base = 5 * GST_SECOND;
constexpr GstClockTime before = base + GST_SECOND;
constexpr size_t bytes = 4080 * 3060 * 3 / 2;

struct Stream {
    GstPad *src = gst_pad_new("src", GST_PAD_SRC), *sink = gst_pad_new("sink", GST_PAD_SINK);
    GstBuffer *image = gst_buffer_new_allocate(nullptr, bytes, nullptr);
    Stream() {
        gst_buffer_memset(image, 0, 128, bytes);
        gst_pad_set_query_function(sink, +[](GstPad *p, GstObject *o, GstQuery *q) -> gboolean {
            if (GST_QUERY_TYPE(q) == GST_QUERY_ACCEPT_CAPS) {
                gst_query_set_accept_caps_result(q, TRUE);
                return TRUE;
            }
            return gst_pad_query_default(p, o, q);
        });
        gst_pad_set_chain_function(sink, +[](GstPad *, GstObject *, GstBuffer *b) { gst_buffer_unref(b); return GST_FLOW_OK; });
        gst_pad_set_active(sink, TRUE);
        gst_pad_set_active(src, TRUE);
        assert(gst_pad_link(src, sink) == GST_PAD_LINK_OK);
        assert(gst_pad_push_event(src, gst_event_new_stream_start("offline")));
        caps(4080, 3060);
        GstSegment seg;
        gst_segment_init(&seg, GST_FORMAT_TIME);
        seg.start = GST_SECOND * 3;  // PTS is not running time.
        assert(gst_pad_push_event(src, gst_event_new_segment(&seg)));
    }
    void caps(int w, int h) {
        GstCaps *c = gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, "NV12", "width", G_TYPE_INT, w,
                                         "height", G_TYPE_INT, h, "framerate", GST_TYPE_FRACTION, 30, 1, nullptr);
        assert(gst_pad_push_event(src, gst_event_new_caps(c)));
        gst_caps_unref(c);
    }
    void push(GstClockTime since, unsigned flags = 0, size_t size = bytes) {
        GstBuffer *b = gst_buffer_copy_region(image, GST_BUFFER_COPY_MEMORY, 0, size);
        GST_BUFFER_PTS(b) = since == GST_CLOCK_TIME_NONE ? since : 3 * GST_SECOND + before - base + since;
        GST_BUFFER_FLAGS(b) |= flags;
        assert(gst_pad_push(src, b) == GST_FLOW_OK);
    }
    ~Stream() {
        gst_buffer_unref(image);
        gst_pad_unlink(src, sink);
        gst_pad_set_active(sink, FALSE);
        gst_pad_set_active(src, FALSE);
        gst_object_unref(src);
        gst_object_unref(sink);
    }
};

int main(int argc, char **argv)
{
    gst_init(&argc, &argv);
    Stream s;
    // A slow STATUS read and only one fully illuminated frame (with DISCONT).
    // Drive the streaming callback before status returns: selection must already be armed.
    auto shot = std::make_shared<Window>(base);
    gulong probe = 0;
    int reads = 0, fires = 0;
    auto now = [&] { return before + (reads++ ? 2 * GST_MSECOND : 0); };
    assert(arm(shot, s.src, probe, now, [&] { ++fires; return true; }, [&] {
        s.push(GST_CLOCK_TIME_NONE);
        s.push(131 * GST_MSECOND); // too early by 1 ms given the strobe ioctl uncertainty
        s.caps(640, 480);
        s.push(145 * GST_MSECOND);
        s.caps(4080, 3060);
        for (auto flag : { GST_BUFFER_FLAG_CORRUPTED, GST_BUFFER_FLAG_GAP, GST_BUFFER_FLAG_DECODE_ONLY, GST_BUFFER_FLAG_HEADER })
            s.push(150 * GST_MSECOND, flag);
        s.push(151 * GST_MSECOND, GST_BUFFER_FLAG_DISCONT, bytes - 1);
        s.push(155 * GST_MSECOND, GST_BUFFER_FLAG_DISCONT);
        s.push(188 * GST_MSECOND); // may not overwrite the already selected frame
        return true;
    }));
    assert(fires == 1 && reads == 2 && shot->selectedStamp == before + 155 * GST_MSECOND);
    assert(shot->early == 1 && shot->invalid == 1 && shot->incomplete == 6);
    gst_pad_remove_probe(s.src, probe);
    GstCaps *jpegCaps = gst_caps_new_empty_simple("image/jpeg");
    Sample jpeg(gst_video_convert_sample(shot->frame.get(), jpegCaps, 3 * GST_SECOND, nullptr), gst_sample_unref);
    gst_caps_unref(jpegCaps);
    assert(jpeg);
    if (argc == 2) {
        GstMapInfo map;
        GstBuffer *b = gst_sample_get_buffer(jpeg.get());
        assert(gst_buffer_map(b, &map, GST_MAP_READ));
        std::ofstream out(argv[1], std::ios::binary);
        out.write(reinterpret_cast<char *>(map.data), map.size);
        assert(out.good());
        gst_buffer_unmap(b, &map);
    }
    // Both ends of the verified window are inclusive even when DISCONT is the only buffer.
    for (unsigned edge : {132u, 190u}) {
        auto w = std::make_shared<Window>(base);
        reads = 0;
        assert(arm(w, s.src, probe, now, [] { return true; }, [] { return true; }));
        s.push(edge * GST_MSECOND, GST_BUFFER_FLAG_DISCONT);
        assert(w->selectedStamp == before + edge * GST_MSECOND);
        gst_pad_remove_probe(s.src, probe);
    }
    // Unsafe timing never falls back to an adjacent frame, even when the ioctl itself stalls.
    for (unsigned delay : { 2u, 70u }) {
        auto w = std::make_shared<Window>(base);
        reads = 0;
        assert(arm(w, s.src, probe, [&] { return before + (reads++ ? delay * GST_MSECOND : 0); },
                   [] { return true; }, [] { return true; }));
        s.push(129 * GST_MSECOND);
        if (delay == 70) s.push(170 * GST_MSECOND);
        s.push(191 * GST_MSECOND);
        assert(!w->frame);
        gst_pad_remove_probe(s.src, probe);
    }
    // A failed strobe or unconfirmed LED must never authorize JPEG encoding.
    for (bool didFire : {false, true}) {
        auto w = std::make_shared<Window>(base);
        bool polled = false;
        assert(!arm(w, s.src, probe, [] { return before; }, [&] { return didFire; }, [&] {
            polled = true; s.push(152 * GST_MSECOND); return false;
        }));
        assert(polled == didFire);
        gst_pad_remove_probe(s.src, probe);
    }
    // Optional stdin: replay each known good frame offset (ns) from an existing trace.
    unsigned long long ns;
    unsigned count = 0;
    while (std::scanf("%llu", &ns) == 1) {
        auto w = std::make_shared<Window>(base);
        assert(arm(w, s.src, probe, [] { return before; }, [] { return true; }, [] { return true; }));
        s.push(ns);
        assert(w->frame && w->selectedStamp == before + ns);
        gst_pad_remove_probe(s.src, probe);
        ++count;
    }
    std::printf("PASS: flash selection, JPEG encoding, exclusion boundaries and %u recorded offsets\n", count);
}
