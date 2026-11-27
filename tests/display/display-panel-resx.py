#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check M81 physical RESX levels at probe, prepare, power-down and failure."""
import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_test import function, run_c, write_report


def oem_tables(paths):
    # OS2.0.6.0.VMYCNXM dtbo entry 15, Tianma and CSOT.
    expected = [0, 10, 1, 3, 0, 3, 1, 15]
    nodes = r'qcom,mdss_dsi_m81_(36_02_0a|42_02_0b)_dualdsi_dsc_lcd_video\s*\{([^}]+)'
    reset = r'qcom,mdss-dsi-reset-sequence\s*=\s*<([^>]+)>;'
    count = 0
    for path in paths:
        text = path.read_text()
        # A decompiled overlay also contains homonymous phandle-fixup nodes.
        panels = [(name, block) for name, block in re.findall(nodes, text)
                  if 'qcom,mdss-dsi-panel-name' in block or re.search(reset, block)]
        blocks = [block for _, block in panels] if panels else [text]
        if '__local_fixups__' in text or len(blocks) > 1:
            assert {name for name, _ in panels} == {'36_02_0a', '42_02_0b'}
        for block in blocks:
            table, = re.findall(reset, block)
            assert [int(cell, 0) for cell in table.split()] == expected, path
            count += 1
    return expected, count


def check(source, baseline, waveform):
    relative = 'drivers/gpu/drm/panel/panel-novatek-nt36532.c'
    panel = (source / relative).read_text()
    dt = (source / 'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts').read_text()
    node = re.search(r'panel: panel@0\s*\{(.*?)\n\t};', dt, re.S)[1]
    assert 'reset-gpios = <&tlmm 0 GPIO_ACTIVE_LOW>;' in node
    for name in ('liuqin', 'pipa', 'yudi'):
        desc = re.search(r'static const struct panel_desc ' + name + r'_desc = \{(.*?)\n};', panel, re.S)[1]
        assert '.reset_sequence = ' + name + '_reset' in desc
    if baseline:
        old = (baseline / relative).read_text()
        for name in ('pipa_reset', 'yudi_reset', 'pipa_init_sequence', 'yudi_init_sequence'):
            assert function(old, name) == function(panel, name), name
    probe = function(panel, 'nt36532_probe')
    assert probe.index('ctx->desc = of_device_get_match_data(dev)') < probe.index('nt36532_get_reset_gpio(') < probe.index('drm_panel_add(')
    drm = (source / 'drivers/gpu/drm/drm_atomic_helper.c').read_text()
    outputs = function(drm, 'disable_outputs')
    assert outputs.index('commit_encoder_bridge_disable(') < outputs.index('commit_encoder_bridge_post_disable(')
    disable = function(drm, 'drm_atomic_helper_commit_encoder_bridge_disable')
    assert disable.index('drm_atomic_bridge_chain_disable(') < disable.index('funcs->atomic_disable(')
    bridge = (source / 'drivers/gpu/drm/bridge/panel.c').read_text()
    assert 'drm_panel_unprepare(' in function(bridge, 'panel_bridge_atomic_post_disable')
    stubs = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define LIUQIN_ALL_VOTES 31
#define MIPI_DSI_MODE_LPM 1
enum { MIPI_DSI_PANEL_PREPARED, MIPI_DSI_PANEL_PREPARE_FAILED, MIPI_DSI_PANEL_DISABLING, MIPI_DSI_PANEL_UNPREPARING };
enum { GPIOD_ASIS, GPIOD_OUT_HIGH };
struct gpio_desc { bool active_low; int physical; };
struct device { struct gpio_desc gpio; };
struct drm_panel { struct device *dev; };
struct drm_dsc_picture_parameter_set { int unused; };
struct mipi_dsi_device { struct device dev; unsigned mode_flags; };
struct nt36532;
struct panel_desc { void (*reset_sequence)(struct nt36532 *); int (*init_sequence)(struct nt36532 *); };
struct nt36532 {
    struct drm_panel panel; struct mipi_dsi_device *dsi[2]; struct panel_desc *desc;
    struct gpio_desc *reset_gpio; unsigned supply_votes;
    bool oem_panel, tddi_recovering, off_deferred; int dsc;
};
static struct panel_desc liuqin_desc, other_desc;
static int request_error, gpio_error, direction_error, rail_error, init_error, pps_error, stage_error = -1;
static int flags, released, pulse[8], pulse_count;
static bool record_pulse;
static struct gpio_desc *devm_gpiod_get(struct device *d, const char *name, int requested) {
    assert(!strcmp(name, "reset")); flags = requested;
    if (request_error) return (struct gpio_desc *)(intptr_t)request_error;
    if (flags == GPIOD_OUT_HIGH) d->gpio.physical = !d->gpio.active_low;
    return &d->gpio;
}
#define IS_ERR(g) ((intptr_t)(g) < 0)
#define PTR_ERR(g) ((int)(intptr_t)(g))
#define gpiod_is_active_low(g) ((g)->active_low)
#define gpiod_toggle_active_low(g) ((g)->active_low = !(g)->active_low)
static int gpiod_get_value_cansleep(struct gpio_desc *g) {
    return gpio_error ? gpio_error : (g->physical ^ g->active_low);
}
static int gpiod_direction_output(struct gpio_desc *g, int logical) {
    assert((logical ^ g->active_low) == g->physical);
    return direction_error;
}
static void gpiod_set_value_cansleep(struct gpio_desc *g, int logical) {
    g->physical = logical ^ g->active_low;
    if (record_pulse) { assert(pulse_count < 8); pulse[pulse_count++] = g->physical; }
}
static void usleep_range(int min, int max) {
    if (record_pulse) { assert(pulse_count < 8 && max == min + 100); pulse[pulse_count++] = min; }
}
#define to_nt36532(p) ((struct nt36532 *)(p))
#define nt36532_cmd_dsi(c) ((c)->dsi[0])
#define dev_err(d, ...) ((void)(d))
#define liuqin_esd_stop(...) ((void)0)
#define liuqin_cancel_dimming(...) ((void)0)
#define liuqin_log_supplies(...) ((void)0)
#define nt36532_prepare_margin(...) ((void)0)
#define drm_dsc_pps_payload_pack(...) ((void)0)
#define mipi_dsi_picture_parameter_set(d, p) ((void)(p), pps_error)
#define mipi_dsi_compression_mode(...) 0
#define nt36532_off(c) 0
static int nt36532_enable_supplies(struct nt36532 *ctx) {
    if (ctx->supply_votes != LIUQIN_ALL_VOTES) assert(!ctx->reset_gpio->physical);
    if (rail_error) return rail_error;
    ctx->supply_votes = LIUQIN_ALL_VOTES;
    return 0;
}
static int nt36532_disable_supplies(struct nt36532 *ctx) {
    assert(!ctx->reset_gpio->physical); released++;
    ctx->supply_votes = 0;
    return 0;
}
static int init_sequence(struct nt36532 *ctx) { assert(ctx->reset_gpio->physical); return init_error; }
static int mipi_dsi_panel_stage(struct mipi_dsi_device *d, int stage) { return stage == stage_error ? -EBUSY : 0; }
'''
    actual = '\n'.join(function(panel, name) for name in (
        'nt36532_get_reset_gpio', 'liuqin_reset', 'nt36532_prepare', 'nt36532_unprepare'))
    levels_us = [v if i % 2 == 0 else v * 1000 for i, v in enumerate(waveform)]
    expected = 'const int expected[] = {' + ','.join(map(str, levels_us)) + '};\n'
    run_c(stubs + actual + '\n' + expected + r'''
int main(void) {
    struct mipi_dsi_device dsi = {0};
    struct device *dev = &dsi.dev;
    struct nt36532 ctx = {.panel.dev = dev, .dsi = {&dsi}, .desc = &liuqin_desc, .oem_panel = true};
    liuqin_desc.reset_sequence = liuqin_reset; liuqin_desc.init_sequence = init_sequence;
    for (int polarity = 0; polarity <= 1; polarity++) for (int physical = 0; physical <= 1; physical++) {
        dev->gpio = (struct gpio_desc){polarity, physical};
        assert(!nt36532_get_reset_gpio(&ctx, dev) && flags == GPIOD_ASIS);
        assert(dev->gpio.physical == physical && dev->gpio.active_low);
    }
    request_error = -EBUSY; assert(nt36532_get_reset_gpio(&ctx, dev) == -EBUSY && dev->gpio.physical);
    request_error = 0; gpio_error = -EIO; assert(nt36532_get_reset_gpio(&ctx, dev) == -EIO);
    gpio_error = 0; direction_error = -EBUSY; assert(nt36532_get_reset_gpio(&ctx, dev) == -EBUSY);
    direction_error = 0;
    record_pulse = true; liuqin_reset(&ctx); record_pulse = false;
    assert(pulse_count == 8 && !memcmp(pulse, expected, sizeof(expected)));
    for (unsigned votes = 0; votes <= LIUQIN_ALL_VOTES; votes++) {
        ctx.supply_votes = votes; dev->gpio.physical = 1; released = 0;
        assert(!nt36532_prepare(&ctx.panel) && dev->gpio.physical);
        ctx.off_deferred = true;
        assert(!nt36532_unprepare(&ctx.panel) && released == 1 && !dev->gpio.physical);
    }
    rail_error = -EIO; released = 0;
    assert(nt36532_prepare(&ctx.panel) == -EIO && !released && !dev->gpio.physical);
    rail_error = 0;
    for (int failure = 0; failure < 3; failure++) {
        init_error = failure == 0 ? -EIO : 0; pps_error = failure == 1 ? -EIO : 0;
        stage_error = failure == 2 ? MIPI_DSI_PANEL_PREPARED : -1;
        released = 0;
        assert(nt36532_prepare(&ctx.panel) < 0 && released == 1 && !dev->gpio.physical);
    }
    init_error = -EIO; pps_error = 0; stage_error = MIPI_DSI_PANEL_PREPARE_FAILED;
    released = 0; assert(nt36532_prepare(&ctx.panel) == -EIO && !released && dev->gpio.physical);
    stage_error = MIPI_DSI_PANEL_UNPREPARING;
    assert(nt36532_unprepare(&ctx.panel) == -EBUSY && !released && dev->gpio.physical);
    init_error = 0; stage_error = -1;
    assert(!nt36532_unprepare(&ctx.panel) && released == 1 && !dev->gpio.physical);
    ctx.desc = &other_desc; dev->gpio.active_low = false;
    assert(!nt36532_get_reset_gpio(&ctx, dev) && flags == GPIOD_OUT_HIGH && dev->gpio.physical);
}
''', flags=('-Wno-unused-parameter',))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline-source', type=Path)
    parser.add_argument('--oem-panel', type=Path, action='append', default=[],
                        help='Decompiled M81 panel node or complete stock entry 15')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    waveform, count = oem_tables(args.oem_panel)
    check(args.source, args.baseline_source, waveform)
    checks = ['boot level and legacy DT polarity', 'physical waveform and reset before rail release',
              'all five rail votes, partial votes, prepare/unprepare failures and retry']
    if count:
        checks.append(f'{count} supplied OEM reset tables')
    if args.baseline_source:
        checks.append('Pipa/Yudi reset and initialization match the supplied baseline')
    write_report(checks, args.report)
