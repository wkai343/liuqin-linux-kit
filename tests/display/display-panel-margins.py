#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute prepare margins with board/identity guards and failure cleanup."""
import argparse
import itertools
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(text, name):
    match = re.search(r'^static [^\n]+\b' + name + r'\(', text, re.M)
    assert match, name
    start = text.index('{', match.start())
    end, depth = start + 1, 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    relative = 'drivers/gpu/drm/panel/panel-novatek-nt36532.c'
    new = (args.source / relative).read_text()
    old = (args.baseline_source / relative).read_text()
    params = ['power_on', 'post_reset', 'post_pps']
    restored = new
    for param in params:
        restored = re.sub(r'static bool liuqin_skip_' + param +
                          r'_wait;\nmodule_param\([^\n]+\nMODULE_PARM_DESC\([^;]+;\n\n',
                          '', restored)
        restored = restored.replace('nt36532_prepare_margin(ctx, liuqin_skip_' +
                                    param + '_wait);', 'msleep(120);')
        assert f'module_param(liuqin_skip_{param}_wait, bool, 0444);' in new
    restored = restored.replace('/* Experimental removal of legacy margins, not OEM command-table waits. */\n', '')
    restored = restored.replace(function(restored, 'nt36532_prepare_margin') + '\n\n', '')
    assert restored == old, 'Unrelated panel code or OEM command table changed'
    assert old.count('{ 0x39, 0x00, 120, 2, { 0x11, 0x00 } }') == 2
    assert function(new, 'liuqin_reset') == function(old, 'liuqin_reset')
    stubs = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#define BIT(x) (1U << (x))
#define MIPI_DSI_MODE_LPM 1
#define LIUQIN_DIGITAL_VOTES 6
#define MIPI_DSI_PANEL_PREPARE_FAILED 1
#define MIPI_DSI_PANEL_PREPARED 2
struct device {int unused;};
struct drm_panel {struct device *dev;};
struct mipi_dsi_device {struct device dev;unsigned long mode_flags;};
struct drm_dsc_picture_parameter_set {int unused;};
struct nt36532;
struct panel_desc {void (*reset_sequence)(struct nt36532 *);int (*init_sequence)(struct nt36532 *);};
static struct panel_desc liuqin_desc, other_desc;
struct nt36532 {struct drm_panel panel;struct mipi_dsi_device *dsi[2];const struct panel_desc *desc;void *oem_panel,*reset_gpio;bool keep_iovdd;unsigned int supply_votes;int dsc;};
static bool liuqin_skip_power_on_wait,liuqin_skip_post_reset_wait,liuqin_skip_post_pps_wait;
static int fault;
static struct nt36532 *to_nt36532(struct drm_panel *p){return (struct nt36532 *)p;}
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532 *p){return p->dsi[1] ? p->dsi[1] : p->dsi[0];}
#define dev_err(dev,...) do {(void)(dev);puts("error");} while (0)
static void msleep(unsigned int ms){printf("sleep %u\n",ms);}
static void usleep_range(unsigned int a,unsigned int b){printf("reset-wait %u %u\n",a,b);}
static void gpiod_set_value_cansleep(void *p,int v){(void)p;printf("reset %d\n",v);}
static void liuqin_esd_stop(struct nt36532 *p,bool b){(void)p;(void)b;puts("esd-stop");}
static void liuqin_cancel_dimming(struct nt36532 *p,bool b){(void)p;(void)b;puts("cancel-dimming");}
static int nt36532_enable_supplies(struct nt36532 *p){(void)p;puts("enable-supplies");return fault==1?-5:0;}
static void nt36532_disable_supplies(struct nt36532 *p){(void)p;puts("disable-supplies");}
static void liuqin_log_supplies(struct nt36532 *p,const char *s){(void)p;(void)s;puts("log-supplies");}
static int init_sequence(struct nt36532 *p){(void)p;puts("OEM-sleep-out-wait-120");return fault==2||fault==6?-5:0;}
static void drm_dsc_pps_payload_pack(void *p,void *d){(void)p;(void)d;puts("pack-pps");}
static int mipi_dsi_picture_parameter_set(struct mipi_dsi_device *d,void *p){(void)d;(void)p;puts("pps");return fault==3?-5:0;}
static int mipi_dsi_compression_mode(struct mipi_dsi_device *d,bool b){(void)d;(void)b;puts("compression");return fault==4?-5:0;}
static int mipi_dsi_panel_stage(struct mipi_dsi_device *d,int stage){(void)d;printf("stage %d\n",stage);return (stage==2&&fault==5)||(stage==1&&fault==6)?-5:0;}
'''
    main_code = r'''
int main(int argc,char **argv){
 (void)argc;int board=atoi(argv[1]),id=atoi(argv[2]),flags=atoi(argv[3]);fault=atoi(argv[4]);
 struct mipi_dsi_device dsi[2]={0};
 liuqin_desc.reset_sequence=liuqin_reset;liuqin_desc.init_sequence=init_sequence;
 other_desc=liuqin_desc;
 struct nt36532 ctx={.desc=board?&liuqin_desc:&other_desc,.oem_panel=id?&dsi[0]:NULL,.dsi={&dsi[0],&dsi[1]},.keep_iovdd=true,.supply_votes=1};
 liuqin_skip_power_on_wait=flags&1;liuqin_skip_post_reset_wait=flags&2;liuqin_skip_post_pps_wait=flags&4;
 int ret=nt36532_prepare(&ctx.panel);printf("result %d modes %lu %lu\n",ret,dsi[0].mode_flags,dsi[1].mode_flags);return 0;
}
'''
    cases = 0
    with tempfile.TemporaryDirectory(prefix='liuqin-margins-') as directory:
        work = Path(directory)
        for label, text in [('old', old), ('new', new)]:
            code = stubs + function(text, 'liuqin_reset') + '\n'
            if label == 'new':
                code += function(text, 'nt36532_prepare_margin') + '\n'
            code += function(text, 'nt36532_prepare') + '\n' + main_code
            src = work / (label + '.c')
            src.write_text(code)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', str(src),
                            '-o', str(work / label)], check=True)
        for board, identity, flags, fault in itertools.product(range(2), range(2), range(8), range(7)):
            call = list(map(str, [board, identity, flags, fault]))
            before = subprocess.check_output([str(work / 'old'), *call], text=True).splitlines()
            after = subprocess.check_output([str(work / 'new'), *call], text=True).splitlines()
            expected, index = [], 0
            for event in before:
                if event == 'sleep 120':
                    omit = board and identity and flags & (1 << index)
                    index += 1
                    if omit:
                        continue
                expected.append(event)
            assert after == expected, (call, before, after, expected)
            cases += 1
    report = dict(cases=cases, actual_prepare_and_reset_executed=True,
                  all_eight_parameter_combinations=True, unknown_identity_and_other_boards_unchanged=True,
                  regulator_and_command_failure_cleanup_unchanged=True, defaults_keep_all_three_120ms_margins=True,
                  oem_sleep_out_120ms_and_reset_waveform_unchanged=True, hardware_validated=False)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
