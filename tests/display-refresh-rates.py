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
ROM_SHA = '5e0377c7781e57f65c474f20edf3d7fb61f03f6030ca170e4edc29467a7993dd'


def function(source, name):
    match = re.search(r'^(?:static )?(?:inline )?(?:int|bool|unsigned long)\s+' +
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
    driver = root / DRIVER
    driver.parent.mkdir(parents=True)
    driver.write_text(added_file(patches[0].read_text(), DRIVER))
    for patch in patches[1:]:
        if '+++ b/' + DRIVER in patch.read_text():
            subprocess.run(['git', 'apply', '--include=' + DRIVER, str(patch)],
                           cwd=root, check=True)
    scheduler = next(p for p in patches if '+++ b/' + SCHEDULE in p.read_text())
    return driver.read_text(), added_file(scheduler.read_text(), SCHEDULE)


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

CASES = r'''
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
    int tianma, csot;
    struct mipi_dsi_host_ops ops={.get_refresh_rate=fake_refresh}, missing={0};
    struct mipi_dsi_host hosts[2]={{.ops=&ops,.rate=120},{.ops=&ops,.rate=120}};
    struct mipi_dsi_device links[2]={{&hosts[0]},{&hosts[1]}};
    struct nt36532 ctx = {.desc = &liuqin_desc, .oem_panel = &tianma, .dsi = {&links[0], &links[1]}};
    assert(liuqin_multi_refresh && !liuqin_low_refresh); check_modes(&ctx, 3);
    for (int i=0;i<=3;i++) { allocation_limit=i; check_modes(&ctx,i); }
    allocation_limit=-1;
    const int blocked[]={60,50,48,30};
    for (unsigned i=0;i<ARRAY_SIZE(blocked);i++) {
        ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=blocked[i];
        assert(liuqin_apply_refresh(&ctx)==-EINVAL && !transfers);
    }
    ctx.refresh_status=0; hosts[0].rate=hosts[1].rate=120;
    liuqin_multi_refresh=false; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx) && !transfers);
    liuqin_multi_refresh=true; liuqin_low_refresh=true;
    check_modes(&ctx, 7); ctx.oem_panel = &csot; check_modes(&ctx, 7);
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
    ctx.oem_panel=NULL; check_modes(&ctx,1); assert(!liuqin_apply_refresh(&ctx)); ctx.oem_panel=&tianma;
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


def execute(panel, schedule, host, root, cc):
    mode = re.search(r'static const struct drm_display_mode liuqin_mode_120 = \{.*?\n\};', panel, re.S)[0]
    parameter = '\n'.join(re.search(r'static bool '+name+r'[^;]*;.*?MODULE_PARM_DESC\(.*?\);', panel, re.S)[0] for name in ('liuqin_multi_refresh','liuqin_low_refresh'))
    code = STUBS + mode + '\n' + parameter
    code += '\nstatic const struct panel_desc liuqin_desc = {.drm_mode = liuqin_mode_120};\n'
    table = re.search(r'struct liuqin_refresh_mode \{.*?liuqin_refresh_modes\[\] = \{.*?\n\};', panel, re.S)[0]
    code += '\n' + table + '\n'
    code += '\n'.join(function(panel, name) for name in ('liuqin_can_multi_refresh', 'liuqin_get_modes', 'nt36532_get_modes', 'liuqin_apply_refresh'))
    init = function(panel, 'liuqin_init_sequence')
    assert init.index('ctx->refresh_status = 0') < init.index('liuqin_send_oem_cmds')
    post = function(panel, 'nt36532_post_backlight_enable')
    assert post.index('liuqin_complete_late_wake') < post.index('liuqin_apply_refresh') < post.index('liuqin_esd_start')
    assert 'ret = liuqin_apply_refresh(ctx);\n\tif (ret)\n\t\treturn ret;' in post
    code += '\n' + schedule
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
    spec = importlib.util.spec_from_file_location('reclaim', PROJECT / 'build/lib/reclaim-memory.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    nodes = module.tree(dtbo)
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
        panel, schedule = assemble(root / 'recipe')
        execute(panel, schedule, None, root, args.cc)
        if args.source:
            source = args.source
            assert (source / DRIVER).read_text() == panel, 'Source does not match the current panel recipe'
            execute(panel, (source / SCHEDULE).read_text(),
                    (source / 'drivers/gpu/drm/msm/dsi/dsi_host.c').read_text(), root, args.cc)
        if args.rom_dtbo:
            check_rom(args.rom_dtbo, root)
        if args.oem_module:
            check_oem_module(args.oem_module)
    print(json.dumps({'shipped_panel_c_executed': True, 'default_rates': [120, 144, 90],
                      'experimental_rates': [120, 144, 90, 60, 50, 48, 30],
                      'scope_allocation_and_schedule_checks': True,
                      'actual_host_clocks_checked': bool(args.source),
                      'rom_timings_checked': bool(args.rom_dtbo),
                      'oem_module_profile_checked': bool(args.oem_module), 'hardware_validated': False}))


if __name__ == '__main__':
    main()
