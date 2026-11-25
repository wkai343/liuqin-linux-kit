// SPDX-License-Identifier: MIT
/* Test-only Video/Source. Preview comes from PipeWire. The official camerabin
 * invokes GstPhotography for stills; only then return a synthetic NV12 frame.
 * No tracing hooks, replacement Kamoso files, JPEG writer or LED access. */
#define GST_USE_UNSTABLE_API
#define PACKAGE "liuqin-camera-tests"
#include <gst/gst.h>
#include <gst/interfaces/photography.h>
#include <gst/video/video.h>
#include <string.h>

#define DEVICE_ID "liuqin-shutter-proof"
typedef struct { GstBin parent; gint seq, pending, preview_seen; } ProofSrc;
typedef GstBinClass ProofSrcClass;
typedef struct { GstDevice parent; } ProofDevice;
typedef GstDeviceClass ProofDeviceClass;
typedef struct { GstDeviceProvider parent; } ProofProvider;
typedef GstDeviceProviderClass ProofProviderClass;

static void photo_iface(GstPhotographyInterface *iface);
G_DEFINE_TYPE_WITH_CODE(ProofSrc, proof_src, GST_TYPE_BIN,
        G_IMPLEMENT_INTERFACE(GST_TYPE_PHOTOGRAPHY, photo_iface))
G_DEFINE_TYPE(ProofDevice, proof_device, GST_TYPE_DEVICE)
G_DEFINE_TYPE(ProofProvider, proof_provider, GST_TYPE_DEVICE_PROVIDER)

static gboolean prepare(GstPhotography *photo, GstPhotographyCapturePrepared done,
                        GstCaps *requested, gpointer user)
{
    ProofSrc *src = (ProofSrc *)photo;
    GstPad *pad = gst_element_get_static_pad(GST_ELEMENT(src), "src");
    GstCaps *caps = gst_pad_get_current_caps(pad);
    gst_object_unref(pad);
    if (!caps || !done || (requested && !gst_caps_can_intersect(caps, requested))) {
        gst_clear_caps(&caps);
        return FALSE;
    }
    gint seq = g_atomic_int_add(&src->seq, 1) + 1;
    if (!g_atomic_int_compare_and_exchange(&src->pending, 0, seq)) {
        gst_caps_unref(caps);
        return FALSE;
    }
    g_printerr("SHUTTERPROOF prepare %d %" G_GINT64_FORMAT "\n", seq, g_get_monotonic_time());
    done(user, caps);
    gst_caps_unref(caps);
    return TRUE;
}

static void photo_iface(GstPhotographyInterface *iface) { iface->prepare_for_capture = prepare; }

// Each rectangle is one bit. Center to edge: low four bits = shutter number,
// high four bits = 0xB. The marker survives flips, orientation and JPEG quantization.
static GstPadProbeReturn frame(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
    ProofSrc *s = data;
    gint seq = g_atomic_int_exchange(&s->pending, 0);
    if (!seq) {
        if (g_atomic_int_compare_and_exchange(&s->preview_seen, 0, 1))
            g_printerr("SHUTTERPROOF preview %" G_GINT64_FORMAT "\n", g_get_monotonic_time());
        return GST_PAD_PROBE_OK;
    }
    GstCaps *caps = gst_pad_get_current_caps(pad);
    GstVideoInfo vi;
    gboolean valid = caps && gst_video_info_from_caps(&vi, caps) &&
                     GST_VIDEO_INFO_FORMAT(&vi) == GST_VIDEO_FORMAT_NV12;
    gst_clear_caps(&caps);
    if (!valid) goto error;
    GstBuffer *replacement = gst_buffer_new_allocate(NULL, vi.size, NULL);
    GstMapInfo map;
    if (!replacement || !gst_buffer_map(replacement, &map, GST_MAP_WRITE)) {
        gst_clear_buffer(&replacement);
        goto error;
    }
    memset(map.data, 128, map.size);
    guint code = 0xb0 | (seq & 15);
    for (int bit = 7; bit >= 0; --bit) {
        int x = vi.width * (7 - bit) / 16, y = vi.height * (7 - bit) / 16;
        for (int row = y; row < vi.height - y; row++)
            memset(map.data + vi.offset[0] + row * vi.stride[0] + x,
                   (code & (1u << bit)) ? 204 : 48, vi.width - 2*x);
    }
    gst_buffer_unmap(replacement, &map);
    GstBuffer *original = GST_PAD_PROBE_INFO_BUFFER(info);
    gst_buffer_copy_into(replacement, original, GST_BUFFER_COPY_TIMESTAMPS, 0, -1);
    // Controlled test latency makes the last in-flight frame observable after UI stop.
    if (g_getenv("LIUQIN_PROOF_DELAY") && seq == 3)
        g_usleep(600000);
    GST_PAD_PROBE_INFO_DATA(info) = replacement;
    gst_buffer_unref(original);
    g_printerr("SHUTTERPROOF return %d %" G_GINT64_FORMAT " %dx%d\n",
               seq, g_get_monotonic_time(), vi.width, vi.height);
    return GST_PAD_PROBE_OK;
error:
    GST_ELEMENT_ERROR(s, STREAM, FAILED, ("Could not construct proof still"), (NULL));
    return GST_PAD_PROBE_DROP;
}

// The proof advertises no camera controls. Use the photography interface
// defaults for its required properties; the only implemented operation is prepare.
static void get_property(GObject *o, guint id, GValue *v, GParamSpec *p)
{ (void)o; (void)id; g_param_value_set_default(p, v); }
static void set_property(GObject *o, guint id, const GValue *v, GParamSpec *p)
{ (void)o; (void)id; (void)v; (void)p; }
static void proof_src_class_init(ProofSrcClass *k)
{
    GObjectClass *oc = G_OBJECT_CLASS(k);
    oc->get_property = get_property;
    oc->set_property = set_property;
    gpointer iface = g_type_default_interface_ref(GST_TYPE_PHOTOGRAPHY);
    guint n;
    GParamSpec **props = g_object_interface_list_properties(iface, &n);
    for (guint i = 0; i < n; i++)
        g_object_class_override_property(oc, i + 1, props[i]->name);
    g_free(props);
    g_type_default_interface_unref(iface);
}
static void proof_src_init(ProofSrc *s) { (void)s; }

static GstElement *create(GstDevice *dev, const gchar *name)
{
    (void)dev;
    GstElement *src = g_object_new(proof_src_get_type(), "name", name, NULL);
    GstElement *pw = gst_element_factory_make("pipewiresrc", NULL);
    GstElement *filter = gst_element_factory_make("capsfilter", NULL);
    if (!pw || !filter) {
        gst_clear_object(&pw); gst_clear_object(&filter); goto fail;
    }
    g_object_set(pw, "target-object", g_getenv("LIUQIN_PROOF_NODE"), NULL);
    GstCaps *caps = gst_caps_from_string("video/x-raw,format=NV12");
    g_object_set(filter, "caps", caps, NULL);
    gst_caps_unref(caps);
    gst_bin_add_many(GST_BIN(src), pw, filter, NULL);
    if (!gst_element_link(pw, filter)) goto fail;
    GstPad *target = gst_element_get_static_pad(filter, "src");
    GstPad *out = gst_ghost_pad_new("src", target);
    gst_object_unref(target);
    gst_pad_add_probe(out, GST_PAD_PROBE_TYPE_BUFFER, frame, src, NULL);
    if (!gst_element_add_pad(src, out)) { gst_object_unref(out); goto fail; }
    return src;
fail:
    gst_object_unref(src);
    return NULL;
}
static void proof_device_class_init(ProofDeviceClass *k) { k->create_element = create; }
static void proof_device_init(ProofDevice *d) { (void)d; }

static GList *probe(GstDeviceProvider *provider)
{
    (void)provider;
    if (!g_getenv("LIUQIN_PROOF_NODE")) return NULL;
    GstCaps *caps = gst_caps_from_string("video/x-raw,format=NV12");
    GstStructure *props = gst_structure_new("properties", "object.id", G_TYPE_STRING, DEVICE_ID, NULL);
    GstDevice *dev = g_object_new(proof_device_get_type(), "display-name", "Liuqin shutter proof",
                "device-class", "Video/Source", "caps", caps, "properties", props, NULL);
    gst_structure_free(props);
    gst_caps_unref(caps);
    return g_list_append(NULL, dev);
}
static void proof_provider_class_init(ProofProviderClass *k)
{
    k->probe = probe;
    gst_device_provider_class_set_static_metadata(k, "Liuqin shutter proof", "Video/Source",
        "Opt-in Kamoso still capture test", "Liuqin Linux Kit");
}
static void proof_provider_init(ProofProvider *p) { (void)p; }
static gboolean plugin_init(GstPlugin *p)
{
    return gst_device_provider_register(p, "liuqinproofprovider", GST_RANK_PRIMARY + 10,
                                        proof_provider_get_type());
}
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, liuqinshutterproof, "Standalone shutter proof",
                  plugin_init, "1.0", "MIT/X11", PACKAGE, "https://gstreamer.freedesktop.org/")
