#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute shipped panel mode enumeration; optionally check the host and OEM ROM."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
DRIVER = 'drivers/gpu/drm/panel/panel-novatek-nt36532.c'
SCHEDULE = 'drivers/gpu/drm/msm/dsi/dsi_cmd_schedule.h'
PANEL_ID = 'include/linux/platform_data/liuqin.h'
TOUCH = 'drivers/input/touchscreen/nt36523/nt36xxx.c'
ROM_SHA = '5e0377c7781e57f65c474f20edf3d7fb61f03f6030ca170e4edc29467a7993dd'


def dtb_tree(path):
    data=Path(path).read_bytes();header=struct.unpack_from('>10I',data)
    if header[0]!=0xd00dfeed or header[1]>len(data):raise ValueError('Invalid DTB')
    strings=data[header[3]:header[3]+header[8]];pos=header[2];stack=[];nodes={}
    while True:
        token=struct.unpack_from('>I',data,pos)[0];pos+=4
        if token==1:
            end=data.index(0,pos);stack.append(data[pos:end].decode());pos=(end+4)&~3
            nodes['/'+'/'.join(stack[1:])]={}
        elif token==2:stack.pop()
        elif token==3:
            length,offset=struct.unpack_from('>II',data,pos);pos+=8;end=strings.index(0,offset)
            nodes['/'+'/'.join(stack[1:])][strings[offset:end].decode()]=data[pos:pos+length];pos=(pos+length+3)&~3
        elif token==4:pass
        elif token==9:break
        else:raise ValueError('Invalid DTB token')
    return nodes


def function(source, name):
    match = re.search(r'^(?:static )?(?:inline )?(?:int|u32|bool|void|unsigned(?: int| long)?|enum liuqin_panel)\s+' +
                      name + r'\([^;{]*\)\s*\{', source, re.M)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


def added_file(patch, name):
    section = patch.split('diff --git a/' + name + ' b/' + name + '\n', 1)[1]
    section = section.split('\ndiff --git ', 1)[0]
    assert 'new file mode' in section
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++')) + '\n'


def assemble(root):
    recipe = json.loads((PROJECT / 'build/kernel/source.json').read_text())
    patches = [PROJECT / name for name in recipe['target_patches']]
    subprocess.run(['git', 'init', '-q', str(root)], check=True)

    def rebuild(name):
        matching = [patch for patch in patches if '+++ b/' + name + '\n' in patch.read_text()]
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(added_file(matching[0].read_text(), name))
        for patch in matching[1:]:
            subprocess.run(['git', 'apply', '--include=' + name, str(patch)], cwd=root, check=True)
        return path.read_text()

    return tuple(rebuild(name) for name in (DRIVER, SCHEDULE, PANEL_ID, TOUCH))


STUBS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint16_t u16;
typedef uint8_t u8;
typedef uint32_t u32;
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MIPI_DSI_MODE_PANEL_STAGES (1U << 14)
#define MIPI_DSI_MODE_VIDEO_BURST (1U << 1)
#define DRM_MODE_TYPE_DRIVER 1
#define DRM_MODE_TYPE_PREFERRED 2
#define DIV_ROUND_UP_ULL(n,d) (((n)+(d)-1)/(d))
#define DIV_ROUND_CLOSEST_ULL(n,d) (((n)+(d)/2)/(d))
#define READ_ONCE(v) (v)
#define WRITE_ONCE(v,n) ((v)=(n))
#define DIV_ROUND_UP(n,d) DIV_ROUND_UP_ULL(n,d)
#define module_param(n,t,p)
#define MODULE_PARM_DESC(n,d)
struct drm_display_mode {
    int clock, hdisplay, hsync_start, hsync_end, htotal;
    int vdisplay, vsync_start, vsync_end, vtotal, width_mm, height_mm, type;
    int flags, hskew, vscan;
    char name[32];
};
struct drm_connector {
    void *dev;
    struct { int width_mm, height_mm; } display_info;
    struct drm_display_mode *modes[8];
    int count;
};
struct panel_desc { struct drm_display_mode drm_mode; };
struct drm_panel { void *dev; };
struct mipi_dsi_host;
struct mipi_dsi_host_ops { int (*get_refresh_rate)(struct mipi_dsi_host *); };
struct mipi_dsi_host { const struct mipi_dsi_host_ops *ops; int rate; };
struct mipi_dsi_device { struct mipi_dsi_host *host; };
struct liuqin_oem_cmd { u8 type, flags, wait_ms, len, data[17]; };
#define dev_info(...) ((void)0)
struct nt36532 {
    struct drm_panel panel;
    const struct panel_desc *desc;
    void *oem_panel; struct mipi_dsi_device *dsi[2]; int refresh_status;
};
static int liuqin_oem_panels[2];
static int transfers, send_error;
static u8 sent_b3, sent_b2;
static int liuqin_send_oem_cmds(struct nt36532 *ctx, const struct liuqin_oem_cmd *cmds, size_t count) {
    (void)ctx; assert(count==3);
    for (int i=0;i<3;i++) { assert(cmds[i].type==0x39 && cmds[i].flags==(i==2?0:0x40)); assert(cmds[i].len==2 && !cmds[i].wait_ms); }
    assert(cmds[0].data[0]==0xff && cmds[0].data[1]==0x10);
    assert(cmds[1].data[0]==0xb3 && cmds[2].data[0]==0xb2);
    sent_b3=cmds[1].data[1]; sent_b2=cmds[2].data[1]; transfers++; return send_error;
}
static int fake_refresh(struct mipi_dsi_host *h) { return h->rate; }
static struct nt36532 *to_nt36532(struct drm_panel *p) { return (struct nt36532 *)p; }
static int allocation_limit = -1, allocations;
static struct drm_display_mode *drm_mode_duplicate(void *dev, const struct drm_display_mode *m) {
    (void)dev;
    if (allocation_limit >= 0 && allocations == allocation_limit) return NULL;
    struct drm_display_mode *copy = malloc(sizeof(*copy));
    assert(copy); *copy = *m; allocations++; return copy;
}
static void drm_mode_set_name(struct drm_display_mode *m) { strcpy(m->name, "1800x2880"); }
static void drm_mode_probed_add(struct drm_connector *c, struct drm_display_mode *m) {
    assert(c->count < 8); c->modes[c->count++] = m;
}
static int drm_connector_helper_get_modes_fixed(struct drm_connector *c, const struct drm_display_mode *m) {
    struct drm_display_mode *copy = drm_mode_duplicate(c->dev, m);
    if (!copy) return 0;
    copy->type |= DRM_MODE_TYPE_PREFERRED; drm_mode_set_name(copy);
    drm_mode_probed_add(c, copy);
    c->display_info.width_mm = m->width_mm; c->display_info.height_mm = m->height_mm;
    return 1;
}
'''

HOST_STUBS = r'''
struct drm_dsc_config { int bits_per_component, bits_per_pixel; };
static int drm_dsc_get_bpp_int(const struct drm_dsc_config *d) { return d->bits_per_pixel >> 4; }
#define mult_frac(x,n,d) ((uint64_t)(x)*(n)/(d))
struct msm_dsi_host {
    struct mipi_dsi_host base;
    u8 lanes; int format; bool cphy_mode; struct drm_dsc_config *dsc;
    unsigned int refresh_rate; unsigned long mode_flags; struct drm_display_mode *mode; void *dev;
};
static struct msm_dsi_host *to_msm_dsi_host(struct mipi_dsi_host *p) { return (struct msm_dsi_host *)p; }
static int mipi_dsi_pixel_format_to_bpp(int f) { (void)f; return 24; }
#define pr_err(...) ((void)0)
static void drm_mode_destroy(void *dev, struct drm_display_mode *m) { (void)dev; free(m); }
static int drm_mode_vrefresh(const struct drm_display_mode *m) { return ((uint64_t)m->clock*1000+(uint64_t)m->htotal*m->vtotal/2)/((uint64_t)m->htotal*m->vtotal); }
'''

LIVE_STUBS = r'''
#define BIT(n) (1U << (n))
#define INTF_0 1
#define INTF_1 2
#define INTF_2 3
#define INTF_DSI 1
#define DPU_ENC_ENABLED 2
#define drm_warn(...) ((void)0)
#define drm_info(...) ((void)0)
#define spin_lock_irqsave(lock, flags) do { (void)(lock); (flags) = 0; } while (0)
#define spin_unlock_irqrestore(lock, flags) do { (void)(lock); (void)(flags); } while (0)
#define read_poll_timeout(op, value, condition, delay, timeout, sleep, ...) \
    ((value) = op(__VA_ARGS__), (condition) ? 0 : -ETIMEDOUT)
struct dpu_hw_intf {
    int idx, line;
    u32 period;
    struct { u32 (*get_line_count)(struct dpu_hw_intf *); } ops;
};
struct dpu_hw_ctl { int dummy; };
struct dpu_kms { struct { void *dsi[2]; } base; };
struct dpu_encoder_phys {
    struct dpu_hw_intf *hw_intf;
    struct dpu_hw_ctl *hw_ctl;
    struct dpu_kms *dpu_kms;
    int enable_state;
    struct drm_display_mode cached_mode;
};
struct drm_encoder { int dev; };
struct dpu_encoder_virt {
    struct drm_encoder base;
    bool enabled, intfs_swapped;
    int num_phys_encs, enc_spinlock;
    struct { int intf_type; bool is_cmd_mode; } disp_info;
    struct dpu_encoder_phys *phys_encs[2];
    void *dsc;
};
static struct dpu_encoder_virt *to_dpu_encoder_virt(struct drm_encoder *encoder) {
    return (struct dpu_encoder_virt *)encoder;
}
static bool drm_mode_equal(const struct drm_display_mode *a, const struct drm_display_mode *b) {
    return a->clock == b->clock && a->vtotal == b->vtotal;
}
static void drm_mode_copy(struct drm_display_mode *dst, const struct drm_display_mode *src) {
    *dst = *src;
}
static bool lock_held, flush_timeout, dsi_mismatch;
static unsigned int writes;
static u32 line_count(struct dpu_hw_intf *intf) { return intf->line; }
static u32 dpu_hw_intf_read_vperiod(struct dpu_hw_intf *intf) { return intf->period; }
static void dpu_hw_intf_write_vperiod(struct dpu_hw_intf *intf, u32 period) {
    assert(lock_held && writes++ == (unsigned int)intf->idx - 1);
    intf->period = period;
}
static bool dpu_hw_ctl_can_flush_intfs(struct dpu_hw_ctl *ctl, u32 mask) {
    assert(ctl && mask == 6);
    return !(flush_timeout && writes == 4);
}
static void dpu_hw_ctl_flush_intfs(struct dpu_hw_ctl *ctl, u32 mask) {
    assert(ctl && mask == 6 && lock_held && writes++ == 3);
}
static bool msm_dsi_live_vfp_begin(void *master, const struct drm_display_mode *old) {
    assert(!lock_held && old->vtotal);
    return lock_held = master != NULL;
}
static void msm_dsi_live_vfp_write(unsigned int vtotal) {
    assert(lock_held && writes++ == 0 && (vtotal == 3122 || vtotal == 6244));
}
static bool msm_dsi_live_vfp_end(bool programmed, const struct drm_display_mode *mode) {
    assert(lock_held && programmed == (writes == 4));
    lock_held = false;
    return mode && !dsi_mismatch;
}
'''

LIVE_CASES = r'''
static void check_live_vfp(void) {
    struct drm_display_mode fast = {.clock = 785245, .vtotal = 3122};
    struct drm_display_mode slow = {.clock = 785245, .vtotal = 6244};
    struct dpu_hw_intf left = {INTF_1, 70, 3122 * 448, {line_count}};
    struct dpu_hw_intf right = {INTF_2, 72, 3122 * 448, {line_count}};
    struct dpu_hw_ctl ctl;
    struct dpu_kms kms = {.base.dsi = {&ctl, &ctl}};
    struct dpu_encoder_phys p0 = {&left, &ctl, &kms, DPU_ENC_ENABLED, fast};
    struct dpu_encoder_phys p1 = {&right, &ctl, &kms, DPU_ENC_ENABLED, fast};
    struct dpu_encoder_virt enc = {.enabled = true, .num_phys_encs = 2,
        .disp_info.intf_type = INTF_DSI, .phys_encs = {&p0, &p1}, .dsc = &ctl};

    right.line = 2800;
    assert(!dpu_encoder_live_vfp(&enc.base, &fast, &slow) && !writes && !lock_held);
    right.line = 72;
    assert(dpu_encoder_live_vfp(&enc.base, &fast, &slow) && writes == 4 && !lock_held);
    assert(left.period == 6244 * 448 && right.period == 6244 * 448);
    assert(p0.cached_mode.vtotal == 6244 && p1.cached_mode.vtotal == 6244);
    writes = 0;
    assert(dpu_encoder_live_vfp(&enc.base, &slow, &fast) && writes == 4 && !lock_held);
    assert(left.period == 3122 * 448 && right.period == 3122 * 448);

    writes = 0;
    flush_timeout = true;
    assert(!dpu_encoder_live_vfp(&enc.base, &fast, &slow) && writes == 4 && !lock_held);
    assert(p0.cached_mode.vtotal == 3122 && p1.cached_mode.vtotal == 3122);
    flush_timeout = false;
    writes = 0;
    left.period = right.period = 3122 * 448;
    dsi_mismatch = true;
    assert(!dpu_encoder_live_vfp(&enc.base, &fast, &slow) && writes == 4 && !lock_held);
    assert(p0.cached_mode.vtotal == 3122 && p1.cached_mode.vtotal == 3122);
}
'''

CRTC_MASK_STUBS = r'''
struct drm_crtc { unsigned mask; };
struct drm_crtc_state { bool enable; };
struct drm_atomic_commit {
    struct drm_crtc crtcs[3];
    struct drm_crtc_state old[3], new[3];
};
#define drm_crtc_mask(crtc) ((crtc)->mask)
#define for_each_oldnew_crtc_in_state(state, crtc, old_state, new_state, i) \
    for ((i) = 0; (i) < ARRAY_SIZE((state)->crtcs) && \
         ((crtc) = &(state)->crtcs[i], (old_state) = &(state)->old[i], \
          (new_state) = &(state)->new[i], true); (i)++)
'''

CRTC_MASK_CASES = r'''
static void check_live_crtc_mask(void) {
    /* The same three CRTC entries KWin sent on the tablet. */
    struct drm_atomic_commit state = {.crtcs = {{1}, {2}, {4}},
        .old = {{true}, {false}, {false}}, .new = {{true}, {false}, {false}}};
    assert(get_crtc_mask(&state, false) == 7);
    assert(get_crtc_mask(&state, true) == 1);

    state.new[1].enable = true;
    assert(get_crtc_mask(&state, true) == 3);
    state.old[1].enable = true;
    state.new[1].enable = false;
    assert(get_crtc_mask(&state, true) == 3);
    state.old[1].enable = false;
    state.old[0].enable = state.new[0].enable = false;
    assert(get_crtc_mask(&state, true) == 0);
    assert(get_crtc_mask(&state, false) == 7);
}
'''

LIVE_DSI_STUBS = r'''
#define DSI_MAX 2
#define DSI_CLOCK_MASTER 0
#define MIPI_DSI_MODE_LIVE_VFP (1U << 15)
struct msm_dsi { struct mipi_dsi_host *host; };
static struct mipi_dsi_host bonded_hosts[2];
static struct msm_dsi bonded_dsi[2] = {{&bonded_hosts[0]}, {&bonded_hosts[1]}};
static unsigned long host_flags[2];
static bool bonded_configured;
static struct msm_dsi *dsi_mgr_get_dsi(int id) { return &bonded_dsi[id]; }
static bool liuqin_dsi_requested(void) { return bonded_configured; }
static unsigned long msm_dsi_host_get_mode_flags(struct mipi_dsi_host *host) {
    return host_flags[host - bonded_hosts];
}
'''

LIVE_DSI_CASES = r'''
static void check_live_dsi_capability(void) {
    struct msm_dsi *master = &bonded_dsi[0];
    assert(!msm_dsi_live_vfp_supported(master));
    bonded_configured = true;
    host_flags[0] = MIPI_DSI_MODE_LIVE_VFP;
    assert(!msm_dsi_live_vfp_supported(master));
    host_flags[1] = MIPI_DSI_MODE_LIVE_VFP;
    assert(!msm_dsi_live_vfp_supported(NULL));
    assert(!msm_dsi_live_vfp_supported(&bonded_dsi[1]));
    assert(msm_dsi_live_vfp_supported(master));
    host_flags[0] = 0;
    assert(!msm_dsi_live_vfp_supported(master));
}
'''

M81_STUBS = r'''
#include <ctype.h>
#define ABS_X 0
#define ABS_Y 1
#define PANEL_DEFAULT_WIDTH_MM 148
#define PANEL_DEFAULT_HEIGHT_MM 237
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
struct pen_input { int max[2], res[2]; };
struct nvt_ts_data {
    struct pen_input *pen_input_dev;
    unsigned int abs_x_max, abs_y_max, pen_coord_scale;
    bool pen_subpixel;
};
static struct nvt_ts_data *ts;
static void input_set_abs_params(struct pen_input *pen, int axis, int min,
                                 int max, int fuzz, int flat)
{
    assert(!min && !fuzz && !flat);
    pen->max[axis] = max;
}
static void input_abs_set_res(struct pen_input *pen, int axis, int res)
{
    pen->res[axis] = res;
}
'''

CASES = r'''
static void check_m81_input(void)
{
#define PRIMARY "msm_drm.dsi_display0="
#define TM "qcom,mdss_dsi_m81_36_02_0a_dualdsi_dsc_lcd_video"
#define CSOT "qcom,mdss_dsi_m81_42_02_0b_dualdsi_dsc_lcd_video"
    assert(liuqin_panel_from_bootargs(NULL) == LIUQIN_PANEL_UNKNOWN);
    assert(liuqin_panel_from_bootargs("msm_drm.dsi_display1=" TM) == LIUQIN_PANEL_UNKNOWN);
    assert(liuqin_panel_from_bootargs("quiet\t" PRIMARY TM ":config\n") == LIUQIN_PANEL_TIANMA);
    assert(liuqin_panel_from_bootargs("msm_drm.dsi_display1=" TM " " PRIMARY CSOT) == LIUQIN_PANEL_CSOT);
    assert(liuqin_panel_from_bootargs(PRIMARY TM " " PRIMARY CSOT) == LIUQIN_PANEL_UNKNOWN);
    assert(liuqin_panel_from_bootargs(PRIMARY TM " " PRIMARY "unknown") == LIUQIN_PANEL_UNKNOWN);
    assert(liuqin_panel_from_bootargs(PRIMARY TM "_extra") == LIUQIN_PANEL_UNKNOWN);
    assert(liuqin_panel_from_bootargs(PRIMARY TM " " PRIMARY TM ":config") == LIUQIN_PANEL_TIANMA);
#undef CSOT
#undef TM
#undef PRIMARY

    struct pen_input pen = {0};
    struct nvt_ts_data data = {&pen, 1800, 2880, 10, true};
    ts = &data;
    nvt_ts_set_pen_abs_params();
    assert(pen.max[ABS_X] == 17999 && pen.max[ABS_Y] == 28799);
    assert(pen.res[ABS_X] == 17999 / 148 && pen.res[ABS_Y] == 28799 / 237);
    assert(nvt_pen_position(101, data.abs_x_max) == 101);
    assert(nvt_pen_position(0xffff, data.abs_y_max) == 28799);
    data.abs_x_max = 1799;
    nvt_ts_set_pen_abs_params();
    assert(pen.max[ABS_X] == 17989 && nvt_pen_position(18000, data.abs_x_max) == 17989);
    data.pen_subpixel = false;
    data.pen_coord_scale = 2;
    nvt_ts_set_pen_abs_params();
    assert(pen.max[ABS_X] == 1798 && nvt_pen_position(101, data.abs_x_max) == 50);
    ts = NULL;
}

static void check_modes(struct nt36532 *ctx, int expected) {
    struct drm_connector c = {0};
    const unsigned int rates[] = {120, 144, 90, 60, 50, 48, 30};
    const unsigned int totals[] = {3122, 3122, 3122, 6244, 5620, 5854, 9366};
    const unsigned int hfp[] = {200, 52, 500, 200, 498, 498, 498};
    const unsigned int clocks[] = {785245, 874197, 672478, 785245, 673042, 673042, 673042};
    allocations = 0;
    assert(nt36532_get_modes(&ctx->panel, &c) == expected);
    assert(c.count == expected && allocations == expected);
    assert(c.display_info.width_mm == (expected ? 148 : 0));
    assert(c.display_info.height_mm == (expected ? 237 : 0));
    for (int i = 0; i < expected; i++) {
        struct drm_display_mode *m = c.modes[i];
        assert(m->clock == (int)clocks[i] && m->hdisplay == 1800 && m->htotal == (int)(1896+hfp[i]));
        assert(m->hsync_start == (int)(1800+hfp[i]) && m->hsync_end == (int)(1804+hfp[i]));
        assert(m->vdisplay == 2880 && m->vtotal == (int)totals[i]);
        assert(m->vsync_end - m->vsync_start == 2 && m->vtotal - m->vsync_end == 214);
        assert(m->vsync_start > m->vdisplay && m->vtotal < 65536);
        assert(m->type == (DRM_MODE_TYPE_DRIVER | (i ? 0 : DRM_MODE_TYPE_PREFERRED)));
        assert(m->width_mm == 148 && m->height_mm == 237 && m->name[0]);
        uint64_t area = (uint64_t)m->htotal * m->vtotal;
        assert(((uint64_t)m->clock * 1000 + area / 2) / area == rates[i]);
        /* OEM porch rounding differs from nominal Hz by less than 0.2%. */
        int64_t error = (int64_t)m->clock * 1000 - area * rates[i];
        assert(llabs(error) * 500 < (int64_t)area * rates[i]);
        u32 line, guard;
        assert(!dsi_cmd_schedule_calculate(m, &line, &guard));
        assert(line == 3097 && guard >= 28 && guard <= 45 && line + 1 < (u32)m->vtotal);
        assert(dsi_cmd_schedule_line_safe(0, line, guard, m->vtotal));
        assert(!dsi_cmd_schedule_line_safe(line, line, guard, m->vtotal));
        assert(dsi_cmd_schedule_line_safe(m->vtotal, line, guard, m->vtotal));
#ifdef CHECK_LIVE
        bool eligible = rates[i] == 120 || rates[i] == 60 || rates[i] == 90 || rates[i] == 144;
        assert(liuqin_live_refresh_mode(m) == eligible);
        if (eligible) {
            struct drm_display_mode altered = *m;
            altered.clock++;
            assert(!liuqin_live_refresh_mode(&altered));
            altered = *m;
            altered.vsync_start++;
            assert(!liuqin_live_refresh_mode(&altered));
            altered = *m;
            altered.flags = 1;
            assert(!liuqin_live_refresh_mode(&altered));
        }
#endif
#ifdef CHECK_HOST
        struct drm_dsc_config dsc = {.bits_per_component = 8, .bits_per_pixel = 8 << 4};
        struct msm_dsi_host host = {.lanes = 4, .dsc = &dsc, .mode_flags=MIPI_DSI_MODE_PANEL_STAGES|MIPI_DSI_MODE_VIDEO_BURST};
        unsigned long byte_rate = dsi_byte_clk_get_rate(&host.base, true, m);
        assert(byte_rate==125878969UL && dsi_host_pclk_rate(&host,true,m)==167838626UL);
        host.mode_flags=0; assert(dsi_host_pclk_rate(&host,true,m)==dsi_get_pclk_rate(m,&dsc,true));
        host.mode_flags=MIPI_DSI_MODE_PANEL_STAGES|MIPI_DSI_MODE_VIDEO_BURST;
        assert(dsi_host_pclk_rate(&host,false,m)==dsi_get_pclk_rate(m,&dsc,false));
        int limit=allocation_limit; allocation_limit=-1;
        assert(!msm_dsi_host_set_display_mode(&host.base,m));
        assert(dsi_host_get_refresh_rate(&host.base)==(int)rates[i]);
        free(host.mode); allocation_limit=limit;
        assert(byte_rate * 8 >= 1007031320UL && byte_rate * 8 <= 1007033320UL);
#endif
        free(m);
    }
}
int main(void) {
    check_m81_input();
#ifdef CHECK_LIVE
    check_live_dsi_capability();
    check_live_crtc_mask();
    check_live_vfp();
#endif
    int *tianma = &liuqin_oem_panels[0], *csot = &liuqin_oem_panels[1];
    struct mipi_dsi_host_ops ops={.get_refresh_rate=fake_refresh}, missing={0};
    struct mipi_dsi_host hosts[2]={{.ops=&ops,.rate=120},{.ops=&ops,.rate=120}};
    struct mipi_dsi_device links[2]={{&hosts[0]},{&hosts[1]}};
    struct nt36532 ctx = {.desc = &liuqin_desc, .oem_panel = tianma, .dsi = {&links[0], &links[1]}};
    assert(liuqin_multi_refresh && !liuqin_low_refresh && !liuqin_live_60 && !liuqin_live_60_csot);
    check_modes(&ctx, 3);
    for (int i=0;i<=3;i++) { allocation_limit=i; check_modes(&ctx,i); }
    allocation_limit=-1;
    const int blocked[]={60,50,48,30};
    for (unsigned i=0;i<ARRAY_SIZE(blocked);i++) {
        ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=blocked[i];
        assert(liuqin_apply_refresh(&ctx)==-EINVAL && !transfers);
    }
    ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=120;
    liuqin_multi_refresh=false; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx) && !transfers);
    liuqin_multi_refresh=true; liuqin_live_60=true;
    check_modes(&ctx, 4);
    ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=60;
    assert(!liuqin_apply_refresh(&ctx) && sent_b3==0x40 && sent_b2==0x91);
    ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=48;
    assert(liuqin_apply_refresh(&ctx)==-EINVAL);
    ctx.oem_panel=csot; check_modes(&ctx, 3);
    ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=60;
    assert(liuqin_apply_refresh(&ctx)==-EINVAL);
    liuqin_live_60=false; liuqin_live_60_csot=true;
    check_modes(&ctx, 4);
    ctx.refresh_status=0;
    assert(!liuqin_apply_refresh(&ctx) && sent_b3==0x40 && sent_b2==0x91);
    ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=48;
    assert(liuqin_apply_refresh(&ctx)==-EINVAL);
    ctx.oem_panel=NULL; check_modes(&ctx, 1); assert(!liuqin_live_60_allowed(&ctx));
    ctx.oem_panel=tianma; check_modes(&ctx, 3);
    /* The desktop boot arguments enable both panel-specific flags. */
    liuqin_live_60=true;
    check_modes(&ctx, 4);
    ctx.oem_panel=csot; check_modes(&ctx, 4);
    liuqin_live_60=false; liuqin_live_60_csot=false;
    liuqin_multi_refresh=true; liuqin_low_refresh=true;
    check_modes(&ctx, 7); ctx.oem_panel = csot; check_modes(&ctx, 7);
    for (int i=0;i<=7;i++) { allocation_limit=i; check_modes(&ctx,i); }
    allocation_limit=-1; check_modes(&ctx,7);
    const int rates[]={120,144,90,60,50,48,30};
    const u8 b3[]={0x40,0,0x80,0x40,0x80,0x80,0x80};
    const u8 b2[]={0x91,0,0,0x91,0,0,0};
    for (unsigned i=0;i<ARRAY_SIZE(rates);i++) {
        ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=rates[i];
        int before=transfers; assert(!liuqin_apply_refresh(&ctx));
        assert(transfers==before+1 && ctx.refresh_status==1 && sent_b3==b3[i] && sent_b2==b2[i]);
        assert(!liuqin_apply_refresh(&ctx) && transfers==before+1);
        for (int err=-EIO;err>=-ETIMEDOUT;err-=ETIMEDOUT-EIO) {
            ctx.refresh_status=0; send_error=err; before=transfers;
            assert(liuqin_apply_refresh(&ctx)==err && transfers==before+1);
            assert(liuqin_apply_refresh(&ctx)==err && transfers==before+1);
        }
        send_error=0;
    }
    int invalid[]={0,-EIO,24,59,165};
    for (unsigned i=0;i<ARRAY_SIZE(invalid);i++) {
        ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=invalid[i]; int before=transfers;
        assert(liuqin_apply_refresh(&ctx)==-EINVAL && before==transfers);
    }
    ctx.refresh_status=0; hosts[0].rate=120; hosts[1].rate=60;
    int before=transfers; assert(liuqin_apply_refresh(&ctx)==-EINVAL && before==transfers);
    liuqin_multi_refresh=false; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx)); liuqin_multi_refresh=true;
    ctx.oem_panel=NULL; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx)); ctx.oem_panel=tianma;
    ctx.dsi[1]=NULL; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx)); ctx.dsi[1]=&links[1];
    hosts[0].ops=NULL; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx)); hosts[0].ops=&ops;
    hosts[1].ops=&missing; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx)); hosts[1].ops=&ops;
    struct panel_desc other={.drm_mode=liuqin_mode_120};ctx.desc=&other;
    check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx));
    allocation_limit=0;check_modes(&ctx,0);
    assert(liuqin_mode_120.vtotal==3122 && liuqin_mode_120.vsync_start==2906);
    assert(liuqin_mode_120.type==DRM_MODE_TYPE_DRIVER);
    return 0;
}
'''


def execute(panel, schedule, panel_id, touch, host, root, cc, dpu=None, encoder=None,
            atomic=None, dsi_manager=None, live_header=None):
    mode = re.search(r'static const struct drm_display_mode liuqin_mode_120 = \{.*?\n\};', panel, re.S)[0]
    parameter = '\n'.join(re.search(r'static bool '+name+r'\b[^;]*;.*?MODULE_PARM_DESC\(.*?\);', panel, re.S)[0]
                          for name in ('liuqin_multi_refresh', 'liuqin_low_refresh',
                                       'liuqin_live_60', 'liuqin_live_60_csot'))
    code = STUBS + M81_STUBS
    code += re.search(r'enum liuqin_panel \{.*?\n\};', panel_id, re.S)[0] + '\n'
    code += function(panel_id, 'liuqin_panel_from_bootargs') + '\n'
    code += '\n'.join(function(touch, name) for name in (
        'nvt_pen_axis_max', 'nvt_pen_position', 'nvt_ts_set_pen_abs_params')) + '\n'
    assert 'liuqin_boot_panel()' in function(panel, 'liuqin_select_oem_panel')
    assert 'liuqin_boot_panel()' in touch and 'strstr(saved_command_line' not in touch
    code += mode + '\n' + parameter
    code += '\nstatic const struct panel_desc liuqin_desc = {.drm_mode = liuqin_mode_120};\n'
    table = re.search(r'struct liuqin_refresh_mode \{.*?liuqin_refresh_modes\[\] = \{.*?\n\};', panel, re.S)[0]
    code += '\n' + table + '\n'
    code += '\n'.join(function(panel, name) for name in (
        'liuqin_can_multi_refresh', 'liuqin_live_60_allowed', 'liuqin_refresh_allowed',
        'liuqin_get_modes', 'nt36532_get_modes', 'liuqin_set_refresh', 'liuqin_apply_refresh'))
    init = function(panel, 'liuqin_init_sequence')
    assert init.index('ctx->refresh_status = 0') < init.index('liuqin_send_oem_cmds')
    post = function(panel, 'nt36532_post_backlight_enable')
    assert post.index('liuqin_complete_late_wake') < post.index('liuqin_apply_refresh') < post.index('liuqin_esd_start')
    assert 'ret = liuqin_apply_refresh(ctx);\n\tif (ret)\n\t\treturn ret;' in post
    code += '\n' + schedule
    if dpu:
        code += '\n#define CHECK_LIVE\n' + function(live_header, 'liuqin_live_refresh_mode')
        code += LIVE_STUBS + '\n' + '\n'.join(function(encoder, name) for name in (
            'dpu_live_vfp_in_window', 'dpu_encoder_live_vfp')) + LIVE_CASES
        code += CRTC_MASK_STUBS + '\n' + function(atomic, 'get_crtc_mask') + CRTC_MASK_CASES
        code += LIVE_DSI_STUBS + '\n' + function(dsi_manager, 'msm_dsi_live_vfp_supported') + LIVE_DSI_CASES
    if host:
        code += '\n#define CHECK_HOST\n' + HOST_STUBS
        code += '\n'.join(function(host, n) for n in (
            'dsi_adjust_pclk_for_compression', 'dsi_get_pclk_rate', 'dsi_host_pclk_rate', 'dsi_byte_clk_get_rate',
            'dsi_host_get_refresh_rate', 'msm_dsi_host_set_display_mode'))
    path = root / 'modes.c'; path.write_text(code + '\n' + CASES)
    binary = root / 'modes'
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O2',
                    '-fsanitize=undefined', '-fno-sanitize-recover=all', str(path), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=15)


def check_rom(path, root):
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == ROM_SHA, 'Use the pinned OS2.0.6.0 ROM DTBO'
    magic, total, header, width, count, offset, page, version = struct.unpack_from('>8I', data)
    assert magic == 0xd7b7ab1e and count > 15
    size, start = struct.unpack_from('>2I', data, offset + 15 * width)
    dtbo = root / 'entry15.dtbo'; dtbo.write_bytes(data[start:start + size])
    nodes = dtb_tree(dtbo)
    for variant in ('36_02_0a', '42_02_0b'):
        name = 'qcom,mdss_dsi_m81_' + variant + '_dualdsi_dsc_lcd_video'
        node = next(p for p in nodes if p.startswith('/fragment@') and p.endswith('/' + name))
        props = nodes[node]; timing = nodes[node + '/qcom,mdss-dsi-display-timings/timing@0']
        assert struct.unpack('>7I', props['qcom,dsi-supported-dfps-list']) == (120, 60, 144, 90, 50, 48, 30)
        assert props['qcom,mdss-dsi-pan-fps-update'] == b'dfps_immediate_porch_mode_vfp\0'
        values = {'panel-width': 900, 'panel-height': 2880, 'panel-framerate': 120,
                  'h-front-porch': 100, 'h-back-porch': 46, 'h-pulse-width': 2,
                  'v-front-porch': 26, 'v-back-porch': 214, 'v-pulse-width': 2,
                  'panel-clockrate': 1007032320}
        for key, value in values.items():
            assert struct.unpack('>I', timing['qcom,mdss-dsi-' + key])[0] == value, key
        assert timing['qcom,mdss-dsi-panel-phy-timings'] == bytes.fromhex('00 22 08 09 19 23 09 09 09 02 04 00 1c 0e')
        for rate,b3,b2 in ((120,0x40,0x91),(144,0,0),(90,0x80,0),(60,0x40,0x91),(48,0x80,0),(30,0x80,0)):
            prefix='qcom,mdss-dsi-dispparam-pen-'+str(rate)+'hz-command'
            assert timing[prefix] == bytes([0x39,0,0,0x40,0,0,2,0xff,0x10,0x39,0,0,0x40,0,0,2,0xb3,b3,0x39,0,0,0,0,0,2,0xb2,b2])



def check_oem_module(path):
    """Audit the M81-only switch and its literal porches in the pinned ROM ELF."""
    from elftools.elf.elffile import ELFFile
    digest = '3079f8d315440d34d58a57de82d31e42b8bc4ff01b5cd64bf141a4c5dfb1d7ba'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        rodata = elf.get_section_by_name('.rodata').data()
        code = elf.get_section_by_name('.text').data()
    expected = {120: (200, 26), 144: (52, 26), 90: (500, 26),
                60: (200, 3148), 50: (498, 2524), 48: (498, 2758), 30: (498, 6270)}
    result = {120: (200, 26)}  # The M81 switch leaves the original mode at 120 Hz.
    for rate in (144, 90, 60, 50, 48, 30):
        offset = {144: 0x13b034, 90: 0x13b0ec}.get(rate)
        if offset is None:
            offset = 0x13afb8 + 4 * rodata[0x6bdd2 + rate - 30]
        values = {}
        for pos in range(offset, offset + 20, 4):
            insn = struct.unpack_from('<I', code, pos)[0]
            if insn & 0xff800000 == 0x52800000:  # MOVZ Wd, literal, LSL shift
                values[insn & 31] = ((insn >> 5) & 0xffff) << (((insn >> 21) & 3) * 16)
        result[rate] = (values[9], values[8])  # stores to h_front_porch / v_front_porch
    assert result == expected, result
    # 50 Hz uses the 48 Hz scan command for both M81 panel IDs.
    pen_targets = {r: 0x1763ac + 4 * rodata[0x6e322 + r - 30] for r in (30,48,50,60)}
    assert pen_targets == {30: 0x1763ac, 48: 0x1763f4, 50: 0x1763d8, 60: 0x176400}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Also execute the assembled panel and DSI clock functions')
    parser.add_argument('--rom-dtbo', type=Path, help='Verify timings against the pinned original dtbo.img')
    parser.add_argument('--oem-module', type=Path, help='Audit M81 timing literals in the pinned ROM msm_drm.ko (pyelftools)')
    parser.add_argument('--cc', default=shutil.which('cc') or 'gcc')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='liuqin-refresh-') as directory:
        root = Path(directory)
        panel, schedule, panel_id, touch = assemble(root / 'recipe')
        execute(panel, schedule, panel_id, touch, None, root, args.cc)
        if args.source:
            source = args.source
            assert (source / DRIVER).read_text() == panel, 'Source does not match the current panel recipe'
            assert (source / PANEL_ID).read_text() == panel_id
            assert (source / TOUCH).read_text() == touch
            execute(panel, (source / SCHEDULE).read_text(), panel_id, touch,
                    (source / 'drivers/gpu/drm/msm/dsi/dsi_host.c').read_text(), root, args.cc,
                    (source / 'drivers/gpu/drm/msm/disp/dpu1/dpu_kms.c').read_text(),
                    (source / 'drivers/gpu/drm/msm/disp/dpu1/dpu_encoder.c').read_text(),
                    (source / 'drivers/gpu/drm/msm/msm_atomic.c').read_text(),
                    (source / 'drivers/gpu/drm/msm/dsi/dsi_manager.c').read_text(),
                    (source / 'include/drm/drm_liuqin.h').read_text())
        if args.rom_dtbo:
            check_rom(args.rom_dtbo, root)
        if args.oem_module:
            check_oem_module(args.oem_module)
    print(json.dumps({'shipped_panel_c_executed': True, 'm81_panel_id_and_pen_axes_checked': True,
                      'default_rates': [120, 144, 90],
                      'opt_in_tianma_rates': [120, 144, 90, 60],
                      'opt_in_csot_rates': [120, 144, 90, 60],
                      'live_switch_fallback_checked': bool(args.source),
                      'live_switch_ignores_unused_crtcs_checked': bool(args.source),
                      'live_switch_bonded_host_capability_checked': bool(args.source),
                      'experimental_rates': [120, 144, 90, 60, 50, 48, 30],
                      'scope_allocation_and_schedule_checks': True,
                      'actual_host_clocks_checked': bool(args.source),
                      'rom_timings_checked': bool(args.rom_dtbo),
                      'oem_module_profile_checked': bool(args.oem_module), 'hardware_validated': False}))


if __name__ == '__main__':
    main()
