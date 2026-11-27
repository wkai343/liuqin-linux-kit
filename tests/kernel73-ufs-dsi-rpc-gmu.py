#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise M81 UFS clock ownership, DSI power errors and DSP/GPU resume."""
import argparse
from pathlib import Path
import re

from kernel_test import function, run_c, write_report


def ufs(source):
    text = (source/'drivers/ufs/host/ufs-qcom.c').read_text()
    platform = (source/'drivers/ufs/host/ufshcd-pltfrm.c').read_text()
    parser = function(platform, 'ufshcd_parse_clock_info')
    assert re.search(r'if \(hba->vops && hba->vops->owns_legacy_clk &&\s*'
                     r'hba->vops->owns_legacy_clk\(name\)\)\s*continue;', parser)
    assert parser.index('owns_legacy_clk(name)') < parser.index('list_add_tail')
    assert re.search(r'\.owns_legacy_clk\s*= ufs_qcom_owns_legacy_clk', text)
    names = re.search(r'static const char \* const ufs_qcom_lane_names\[\] = \{[^}]+};', text).group()
    funcs = ('ufs_qcom_owns_legacy_clk', 'ufs_qcom_get_liuqin_lane_clks', 'ufs_qcom_init_lane_clks',
             'ufs_qcom_enable_lane_clks', 'ufs_qcom_disable_lane_clks')
    run_c(r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#define ARRAY_SIZE(x) (sizeof(x)/sizeof(*(x)))
#define min(a,b) ((a)<(b)?(a):(b))
#define GFP_KERNEL 0
struct device { bool acpi; void *of_node; };
struct ufs_hba { struct device *dev; int lanes_per_direction; };
struct clk_bulk_data { const char *id; bool present; };
struct ufs_qcom_host { struct ufs_hba *hba; struct clk_bulk_data *clks; unsigned num_clks; bool is_lane_clks_enabled; };
static bool board, freq, optional, legacy;
static int mandatory_error, optional_error, enable_error, references;
#define liuqin_board_quirks() board
#define of_property_present(node,name) freq
#define has_acpi_companion(dev) ((dev)->acpi)
#define devm_kcalloc(dev,num,size,flags) calloc(num,size)
#define dev_err_probe(dev,err,...) (err)
static int match_string(const char *const *strings, size_t count, const char *id) {
    for (size_t i=0; i<count; i++) if (!strcmp(strings[i],id)) return i;
    return -1;
}
static int devm_clk_bulk_get(struct device *d, unsigned n, struct clk_bulk_data *c) {
    assert(n == 2 || n == 3);
    assert(!strcmp(c[0].id,"rx_lane0_sync_clk") && !strcmp(c[1].id,"tx_lane0_sync_clk"));
    if (n==3) assert(!strcmp(c[2].id,"rx_lane1_sync_clk"));
    for (unsigned i=0; i<n; i++) c[i].present=true;
    return mandatory_error;
}
static int devm_clk_bulk_get_optional(struct device *d, int n, struct clk_bulk_data *c) {
    assert(n==1 && !strcmp(c->id,"tx_lane1_sync_clk"));
    c->present=optional;
    return optional_error;
}
static int devm_clk_bulk_get_all(struct device *d,struct clk_bulk_data **c) { legacy=true; return 8; }
static int clk_bulk_prepare_enable(unsigned n,struct clk_bulk_data *c) {
    if (enable_error) return enable_error;
    for (unsigned i=0; i<n; i++) references+=c[i].present;
    return 0;
}
static void clk_bulk_disable_unprepare(unsigned n, struct clk_bulk_data *c) {
    for(unsigned i=0; i<n; i++) references-=c[i].present;
    assert(references>=0);
}
''' + names + '\n' + '\n'.join(function(text, n) for n in funcs) + r'''
int main(void) {
    struct device dev={0};
    struct ufs_hba hba={&dev,1};
    struct ufs_qcom_host host={.hba=&hba};
    board=freq=true;
    for (unsigned i=0;i<ARRAY_SIZE(ufs_qcom_lane_names);i++) assert(ufs_qcom_owns_legacy_clk(ufs_qcom_lane_names[i]));
    assert(!ufs_qcom_owns_legacy_clk("core_clk") && !ufs_qcom_owns_legacy_clk("ref_clk"));
    for (hba.lanes_per_direction=1;hba.lanes_per_direction<=2;hba.lanes_per_direction++) {
        for (int tx1=0;tx1<=1;tx1++) {
            optional=tx1;
            assert(!ufs_qcom_init_lane_clks(&host) && !legacy);
            enable_error=-EIO;
            assert(ufs_qcom_enable_lane_clks(&host)==-EIO && !host.is_lane_clks_enabled);
            enable_error=0;
            assert(!ufs_qcom_enable_lane_clks(&host));
            assert(!ufs_qcom_enable_lane_clks(&host));
            assert(references==(hba.lanes_per_direction==1 ? 2 : 3+tx1));
            ufs_qcom_disable_lane_clks(&host); ufs_qcom_disable_lane_clks(&host);
            assert(!references); free(host.clks);
        }
    }
    mandatory_error=-ENOENT; optional_error=-EAGAIN;
    assert(ufs_qcom_init_lane_clks(&host)==-ENOENT);
    mandatory_error=0; assert(ufs_qcom_init_lane_clks(&host)==-EAGAIN);
    dev.acpi=true; assert(!ufs_qcom_init_lane_clks(&host) && !legacy);
    dev.acpi=false; board=false; assert(!ufs_qcom_owns_legacy_clk("rx_lane0_sync_clk"));
    assert(!ufs_qcom_init_lane_clks(&host) && legacy);
}
''', flags=('-Wno-unused-parameter', '-Wno-sign-compare'))


def dsi(source):
    host = (source/'drivers/gpu/drm/msm/dsi/dsi_host.c').read_text()
    manager = (source/'drivers/gpu/drm/msm/dsi/dsi_manager.c').read_text()
    assert re.search(r'ret = msm_dsi_host_xfer_prepare\(host, msg\);\s*if \(ret\) \{[^}]*'
                     r'goto restore_host0;', manager)
    funcs = ('dsi_link_clk_set_rate_6g_v2_9', 'dsi_link_power_get', 'msm_dsi_host_xfer_prepare',
             'msm_dsi_host_xfer_restore', 'msm_dsi_host_power_on', 'msm_dsi_host_power_off')
    run_c(r'''
#include <assert.h>
#include <stdbool.h>
struct device { int unused; }; struct platform_device { struct device dev; };
struct msm_dsi_host; struct mipi_dsi_host { struct msm_dsi_host *host; };
struct mipi_dsi_msg { int flags; };
struct msm_dsi_host_cfg_ops { int (*link_clk_set_rate)(struct msm_dsi_host *);
    int (*link_clk_enable)(struct msm_dsi_host *); void (*link_clk_disable)(struct msm_dsi_host *); };
struct config { int num_regulators; };
struct msm_dsi_cfg_handler { struct config *cfg; const struct msm_dsi_host_cfg_ops *ops; };
struct msm_dsi_host { struct platform_device *pdev; struct msm_dsi_cfg_handler *cfg_hnd;
    int dev_mutex, byte_src_clk, pixel_src_clk, dsi_pll_byte_clk, dsi_pll_pixel_clk;
    int byte_intf_clk_div_2, dma_cmd_ctrl_restore, supplies; bool power_on; };
struct msm_dsi_phy_shared_timings { int byte_intf_clk_div_2; }; struct msm_dsi_phy;
enum { REGULATORS=1, PM, BYTE_PARENT, PIXEL_PARENT, RATE, ENABLE, PINCTRL };
static int fail, pm, clks, regs, lock, sfpb, mmio;
#define to_msm_dsi_host(h) ((h)->host)
#define MIPI_DSI_MSG_USE_LPM 1
#define REG_DSI_CTRL 0
#define DSI_CTRL_CMD_MODE_EN 1
#define DSI_CTRL_ENABLE 2
#define DSI_IRQ_MASK_CMD_DMA_DONE 4
#define pr_err(...) ((void)0)
#define dev_err(d,...) ((void)(d))
#define DBG(...) ((void)0)
#define mutex_lock(m) assert(!lock++)
#define mutex_unlock(m) assert(lock--==1)
#define msm_dsi_sfpb_config(h,v) (sfpb=(v))
#define regulator_bulk_enable(n,s) (fail==REGULATORS ? -fail : (++regs,0))
#define regulator_bulk_disable(n,s) assert(regs-->0)
#define pm_runtime_resume_and_get(d) ((void)(d),fail==PM ? -fail : (++pm,0))
#define pm_runtime_put(d) assert(pm-->0)
#define clk_set_parent(src,par) (assert(pm>0),(fail==(src) ? -fail : 0))
#define pinctrl_pm_select_default_state(d) (fail==PINCTRL ? -fail : 0)
#define pinctrl_pm_select_sleep_state(d) ((void)0)
static int dsi_link_clk_set_rate_6g(struct msm_dsi_host *h) { assert(pm>0); return fail==RATE ? -fail : 0; }
static int enable(struct msm_dsi_host *h) { assert(pm>0); return fail==ENABLE ? -fail : (++clks,0); }
static void disable(struct msm_dsi_host *h) { assert(clks-->0); }
static int access_dsi(void) { assert(clks>0 && pm>0); return ++mmio; }
#define dsi_read(...) access_dsi()
#define dsi_write(...) (void)access_dsi()
#define dsi_intr_ctrl(...) (void)access_dsi()
#define dsi_set_tx_power_mode(...) (void)access_dsi()
#define dsi_timing_setup(...) (void)access_dsi()
#define dsi_sw_reset(...) (void)access_dsi()
#define dsi_ctrl_enable(...) (void)access_dsi()
#define dsi_ctrl_disable(...) (void)access_dsi()
''' + '\n'.join(function(host, n) for n in funcs) + r'''
int main(void) {
    struct msm_dsi_host_cfg_ops ops={dsi_link_clk_set_rate_6g_v2_9,enable,disable};
    struct config cfg={1}; struct msm_dsi_cfg_handler handler={&cfg,&ops};
    struct platform_device pdev={0};
    struct msm_dsi_host h={.pdev=&pdev,.cfg_hnd=&handler,.byte_src_clk=BYTE_PARENT,.pixel_src_clk=PIXEL_PARENT};
    struct mipi_dsi_host bus={&h}; struct mipi_dsi_msg msg={0};
    struct msm_dsi_phy_shared_timings timings={0};
    for (fail=REGULATORS;fail<=PINCTRL;fail++) {
        assert(msm_dsi_host_power_on(&bus,&timings,true,0)==-fail);
        assert(!h.power_on && !pm && !clks && !regs && !lock && !sfpb && !mmio);
        if (fail>=PM && fail<=ENABLE) {
            assert(msm_dsi_host_xfer_prepare(&bus,&msg)==-fail);
            assert(!pm && !clks && !mmio);
        }
    }
    fail=0; assert(!msm_dsi_host_power_on(&bus,&timings,true,0));
    assert(!msm_dsi_host_xfer_prepare(&bus,&msg) && pm==2 && clks==2);
    msm_dsi_host_xfer_restore(&bus,&msg);
    assert(h.power_on && pm==1 && clks==1 && regs==1 && sfpb);
    assert(!msm_dsi_host_power_off(&bus)); assert(!pm && !clks && !regs && !sfpb && mmio);
}
''', flags=('-Wno-unused-parameter',))


def rpc_and_gmu(source):
    rpc = (source/'drivers/misc/fastrpc.c').read_text().replace('static inline int ', 'static int ')
    run_c(r'''
#include <assert.h>
#include <errno.h>
struct fastrpc_invoke_ctx { int work; };
typedef unsigned int u32;
enum { HZ=100, TASK_INTERRUPTIBLE=1, TASK_FREEZABLE=8 };
static int answer;
#define wait_for_completion_timeout(w,t) (assert((t)==10*HZ), answer)
#define wait_for_completion_state(w,s) (assert((s)==(TASK_INTERRUPTIBLE|TASK_FREEZABLE)),answer)
''' + function(rpc, 'fastrpc_wait_for_response') + r'''
int main(void) {
    struct fastrpc_invoke_ctx ctx={0};
    assert(!fastrpc_wait_for_response(&ctx,0));
    answer=-512; assert(fastrpc_wait_for_response(&ctx,0)==-512);
    answer=0; assert(fastrpc_wait_for_response(&ctx,1)==-ETIMEDOUT);
    answer=1; assert(!fastrpc_wait_for_response(&ctx,1));
}
''', flags=('-Wno-unused-parameter',))
    gmu = (source/'drivers/gpu/drm/msm/adreno/a6xx_gmu.c').read_text()
    resume = function(gmu, 'a6xx_gmu_resume')
    order = ['REG_A6XX_GMU_GMU2HOST_INTR_MASK, ~0', 'REG_A6XX_GMU_GMU2HOST_INTR_CLR, ~0',
             'a6xx_gmu_fw_start(', 'a6xx_hfi_start(', 'REG_A6XX_GMU_GMU2HOST_INTR_MASK, ~A6XX_HFI_IRQ_MASK',
             'enable_irq(gmu->hfi_irq)', 'a6xx_gmu_set_initial_freq(']
    positions = [resume.index(item) for item in order]
    assert positions == sorted(positions) and resume.count('REG_A6XX_GMU_GMU2HOST_INTR_CLR') == 1
    assert re.search(r'#define A6XX_HFI_IRQ_MASK\s*\\\s*\(A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT\)', gmu)
    irq = function(gmu.replace('irqreturn_t ', 'int '), 'a6xx_hfi_irq')
    run_c(r'''
#include <assert.h>
#include <stdint.h>
typedef uint32_t u32;
struct a6xx_gmu { unsigned pending, faults; };
#define A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT (1u<<23)
#define gmu_read(g,r) ((g)->pending)
#define gmu_write(g,r,v) ((g)->pending &= ~(v))
#define a6xx_hfi_trace(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define a6xx_gmu_fault(g) (++(g)->faults)
#define IRQ_HANDLED 1
''' + irq + r'''
int main(void) {
    struct a6xx_gmu gmu={.pending=0x101};
    a6xx_hfi_irq(0,&gmu); assert(gmu.pending==0x101 && !gmu.faults);
    gmu.pending|=A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT;
    a6xx_hfi_irq(0,&gmu); assert(gmu.pending==0x101 && gmu.faults==1);
}
''', flags=('-Wno-unused-parameter',))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    ufs(args.source)
    dsi(args.source)
    rpc_and_gmu(args.source)
    write_report(['UFS single/dual lane ownership, lookup errors and repeated gating',
                  'DSI PM, clock parent, rate, enable and power-on failures',
                  'FastRPC freezer flags, signal and bounded kernel wait',
                  'HFI boot masking, retained replies and CM3 fault handling'], args.report)
