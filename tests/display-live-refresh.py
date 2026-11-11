#!/usr/bin/env python3
"""Execute shipped live-refresh C against fault-injected DPU/DSI registers."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('refresh', Path(__file__).with_name('display-refresh-rates.py'))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

STUBS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,y) ((x)=(y))
#define wmb() ((void)0)
static char diagnostics[8192];
static __attribute__((format(printf,1,2))) void log_message(const char *format,...) {
 va_list args;va_start(args,format);
 size_t used=strlen(diagnostics);
 vsnprintf(diagnostics+used,sizeof(diagnostics)-used,format,args);
 va_end(args);
}
#define dev_info(dev,...) log_message(__VA_ARGS__)
#define drm_info(dev,...) log_message(__VA_ARGS__)
#define drm_warn(dev,...) log_message(__VA_ARGS__)
#define pr_info(...) log_message(__VA_ARGS__)
#define EXPORT_SYMBOL_GPL(x)
#define MIPI_DSI_MODE_LIVE_VFP BIT(15)
#define INTF_0 1
#define INTF_1 2
#define INTF_2 3
#define INTF_DSI 1
#define DPU_ENC_ENABLED 2
#define DSI_CLOCK_MASTER 0
#define DSI_MAX 2
#define DSI_0 0
#define DSI_1 1
#define LIUQIN_PANEL_TIANMA 0
#define LIUQIN_PANEL_CSOT 1
#define LIUQIN_ALL_VOTES 7
#define DSI_VIDEO_TIMING_DB_MODE 0x1e4
#define DSI_VIDEO_TIMING_FLUSH 0x1e0
#define LIUQIN_DSI_LINE_CLOCKS 448
#define REG_DSI_CTRL 0x00
#define REG_DSI_ACTIVE_H 0x20
#define REG_DSI_ACTIVE_V 0x24
#define REG_DSI_TOTAL 0x28
#define REG_DSI_ACTIVE_HSYNC 0x2c
#define REG_DSI_ACTIVE_VSYNC_HPOS 0x30
#define REG_DSI_ACTIVE_VSYNC_VPOS 0x34
#define DSI_CTRL_ENABLE 1
#define DSI_CTRL_VID_MODE_EN 2
#define DSI_ACTIVE_H_START(x) (x)
#define DSI_ACTIVE_H_END(x) ((x)<<16)
#define DSI_ACTIVE_V_START(x) (x)
#define DSI_ACTIVE_V_END(x) ((x)<<16)
#define DSI_TOTAL_H_TOTAL(x) (x)
#define DSI_TOTAL_V_TOTAL(x) ((x)<<16)
#define DSI_ACTIVE_HSYNC_END(x) ((x)<<16)
#define DSI_ACTIVE_VSYNC_VPOS_END(x) ((x)<<16)
struct drm_display_mode {
 int clock,hdisplay,hsync_start,hsync_end,htotal,vdisplay,vsync_start,vsync_end,vtotal;
 int flags,hskew,vscan;
};
static bool drm_mode_equal(const struct drm_display_mode *a,const struct drm_display_mode *b) {return !memcmp(a,b,sizeof(*a));}
static void drm_mode_copy(struct drm_display_mode *a,const struct drm_display_mode *b) {*a=*b;}
static int drm_mode_vrefresh(const struct drm_display_mode *m) {return ((uint64_t)m->clock*1000+(uint64_t)m->htotal*m->vtotal/2)/((uint64_t)m->htotal*m->vtotal);}
struct mutex {bool held;};
static void mutex_lock(struct mutex *m) {assert(!m->held);m->held=true;}
static void mutex_unlock(struct mutex *m) {assert(m->held);m->held=false;}
#define lockdep_assert_held(m) assert((m)->held)
#define spin_lock_irqsave(m,f) do {mutex_lock(m);(f)=0;} while(0)
#define spin_unlock_irqrestore(m,f) do {(void)(f);mutex_unlock(m);} while(0)
#define read_poll_timeout(op,val,condition,delay,timeout,sleep,...) ((val)=op(__VA_ARGS__), (condition)?0:-ETIMEDOUT)
struct drm_panel;
struct drm_panel_funcs {
 int (*prepare_live_refresh)(struct drm_panel *,const struct drm_display_mode *,const struct drm_display_mode *);
 int (*apply_live_refresh)(struct drm_panel *,const struct drm_display_mode *,const struct drm_display_mode *);
};
struct drm_panel {struct mutex backlight_lock;const struct drm_panel_funcs *funcs;bool prepared,enabled,backlight_pending;};
struct drm_bridge {bool panel;};
struct panel_bridge {struct drm_bridge bridge;struct drm_panel *panel;};
static bool drm_bridge_is_panel(const struct drm_bridge *b) {return b->panel;}
static struct panel_bridge *drm_bridge_to_panel_bridge(struct drm_bridge *b) {return (void*)b;}
struct drm_encoder {struct drm_bridge *bridge;void *dev;};
#define drm_for_each_bridge_in_chain(enc,b) for(struct drm_bridge *b=(enc)->bridge;b;b=NULL)
struct drm_dsc_config {int bits_per_component,bits_per_pixel;};
struct mipi_dsi_host;
struct mipi_dsi_host_ops {int (*get_refresh_rate)(struct mipi_dsi_host *);};
struct mipi_dsi_host {const struct mipi_dsi_host_ops *ops;};
struct msm_dsi_host {
 struct mipi_dsi_host base;u32 regs[128];bool power_on,enabled;
 struct drm_display_mode *mode;unsigned int refresh_rate;
 unsigned long mode_flags,pixel_clk_rate,byte_clk_rate;int lanes;
 struct drm_dsc_config *dsc;void *byte_clk,*pixel_clk;int id;
};
static long rounded_byte_rate;
static unsigned long actual_byte_rate,actual_pixel_rate;
static __attribute__((unused)) long clk_round_rate(void *clk,unsigned long rate) {
 assert(clk==&rounded_byte_rate&&rate==125878969UL);return rounded_byte_rate;
}
static unsigned long clk_get_rate(void *clk) {return clk==&rounded_byte_rate?actual_byte_rate:actual_pixel_rate;}
static struct msm_dsi_host *to_msm_dsi_host(struct mipi_dsi_host *h) {return (void*)h;}
static int get_rate(struct mipi_dsi_host *h) {return to_msm_dsi_host(h)->refresh_rate;}
static unsigned long msm_dsi_host_get_mode_flags(struct mipi_dsi_host *h) {return to_msm_dsi_host(h)->mode_flags;}
struct msm_dsi {struct mipi_dsi_host *host;};
static struct msm_dsi dsi[2];
static struct msm_dsi *dsi_mgr_get_dsi(int id) {return &dsi[id];}
static bool liuqin_dsi_requested(void) {return dsi[0].host && dsi[1].host;}
static struct {bool active,powered,gated;unsigned int video;} liuqin_dsi;
static struct mutex dsi_cmd_lock;
static unsigned int writes,commands,flushes,legacy_calls;
static bool fail_flush,fail_frames,fail_dsi_readback,fail_dpu_readback,latched;
static int command_error;
static u8 sent_b3,sent_b2;
static int initial_rate;
static unsigned int settle_delays,simulated_us,last_db_disable_us;
static __attribute__((unused)) void usleep_range(unsigned int min,unsigned int max) {
 assert(dsi_cmd_lock.held&&min==2000&&max==2010);
 simulated_us+=min;settle_delays++;
}
static u32 dsi_read(struct msm_dsi_host *h,unsigned int reg) {
 u32 value=h->regs[reg/4];
 if(fail_dsi_readback && latched && reg==REG_DSI_TOTAL) value++;
 return value;
}
static void dsi_write(struct msm_dsi_host *h,unsigned int reg,u32 value) {
 assert(dsi_cmd_lock.held);
 // A DPU latch alone does not establish the downstream panel-vsync margin.
 if(reg==DSI_VIDEO_TIMING_DB_MODE&&!value&&commands) {
  assert(simulated_us-last_db_disable_us>=2000);
  last_db_disable_us=simulated_us;
 }
 h->regs[reg/4]=value;writes++;
}
struct dpu_hw_blk_reg_map {u32 regs[192];};
static u32 reg_read(struct dpu_hw_blk_reg_map *hw,unsigned int reg) {
 u32 value=hw->regs[reg/4];
 if(fail_dpu_readback && latched && reg==0x008) value++;
 return value;
}
#define DPU_REG_READ(hw,r) reg_read(hw,r)
#define DPU_REG_WRITE(hw,r,v) do {(hw)->regs[(r)/4]=(v);writes++;} while(0)
struct dpu_hw_intf {
 struct dpu_hw_blk_reg_map hw;int idx;
 struct {u32 (*get_line_count)(struct dpu_hw_intf *);} ops;
};
static struct dpu_hw_intf intfs[2];
static u32 line_count(struct dpu_hw_intf *intf) {return intf->hw.regs[0xb0/4];}
struct dpu_hw_ctl {bool pending;};
static bool dpu_hw_ctl_can_flush_intfs(struct dpu_hw_ctl *ctl,u32 mask) {
 assert(mask==6);
 if(ctl->pending) {
  if(fail_flush)return false;
  ctl->pending=false;latched=true;
  if(!fail_frames)for(int i=0;i<2;i++)intfs[i].hw.regs[0xac/4]++;
 }
 return true;
}
static void dpu_hw_ctl_flush_intfs(struct dpu_hw_ctl *ctl,u32 mask) {assert(mask==6&&dsi_cmd_lock.held);ctl->pending=true;flushes++;}
struct dpu_kms {struct {struct msm_dsi *dsi[2];} base;};
struct dpu_encoder_phys {struct dpu_hw_intf *hw_intf;struct dpu_hw_ctl *hw_ctl;struct dpu_kms *dpu_kms;int enable_state;struct drm_display_mode cached_mode;};
struct dpu_encoder_virt {
 struct drm_encoder base;bool enabled,intfs_swapped;int num_phys_encs;struct mutex enc_spinlock;
 struct {int intf_type;bool is_cmd_mode;} disp_info;struct drm_dsc_config *dsc;
 struct dpu_encoder_phys *phys_encs[2];
};
static struct dpu_encoder_virt *to_dpu_encoder_virt(struct drm_encoder *e) {return (void*)e;}
struct mipi_dsi_device {struct mipi_dsi_host *host;};
struct nt36532 {
 struct drm_panel panel;const void *desc,*oem_panel;struct mipi_dsi_device *dsi[2];int refresh_status;
 bool tddi_recovering,dimming_stopping,off_deferred,esd_pending;int supply_votes;struct mutex esd_lock;
};
static struct nt36532 *to_nt36532(struct drm_panel *panel) {return (void*)panel;}
static int liuqin_desc,liuqin_oem_panels[2];
static bool liuqin_multi_refresh=true,liuqin_live_60=true,liuqin_live_60_csot=true,liuqin_low_refresh,liuqin_live_all;
struct liuqin_oem_cmd {u8 type,flags,wait_ms,len,data[17];};
static int liuqin_send_oem_cmds(struct nt36532 *ctx,const struct liuqin_oem_cmd *cmds,size_t count) {
 assert(ctx->panel.backlight_lock.held && !dsi_cmd_lock.held && count==3);
 assert(!latched);
 assert(get_rate(dsi[0].host)==initial_rate&&get_rate(dsi[1].host)==initial_rate);
 assert(cmds[0].data[0]==0xff&&cmds[0].data[1]==0x10);
 assert(cmds[1].data[0]==0xb3 && cmds[2].data[0]==0xb2);
 for(int i=0;i<3;i++)assert(cmds[i].type==0x39&&cmds[i].len==2&&cmds[i].flags==(i==2?0:0x40));
 commands++;sent_b3=cmds[1].data[1];sent_b2=cmds[2].data[1];return command_error;
}
'''

CASES = r'''
static struct drm_display_mode modes[]={
 {785245,1800,2000,2004,2096,2880,2906,2908,3122,0,0,0},
 {672478,1800,2300,2304,2396,2880,2906,2908,3122,0,0,0},
 {874197,1800,1852,1856,1948,2880,2906,2908,3122,0,0,0},
 {785245,1800,2000,2004,2096,2880,6028,6030,6244,0,0,0},
};
static struct nt36532 ctx;
static struct panel_bridge bridge;
static struct msm_dsi_host hosts[2];
static struct drm_display_mode cached[2];
static struct mipi_dsi_device links[2];
static struct drm_dsc_config dsc={8,8<<4};
static struct dpu_hw_ctl ctl;
static struct dpu_kms kms;
static struct dpu_encoder_phys phys[2];
static struct dpu_encoder_virt enc;
static const struct mipi_dsi_host_ops ops={get_rate};
static const struct drm_panel_funcs panel_ops={nt36532_prepare_live_refresh,nt36532_apply_live_refresh};
static void setup(int index) {
 memset(&ctx,0,sizeof(ctx));memset(&ctl,0,sizeof(ctl));memset(&enc,0,sizeof(enc));
 memset(hosts,0,sizeof(hosts));memset(intfs,0,sizeof(intfs));
 memset(&dsi_cmd_lock,0,sizeof(dsi_cmd_lock));
 ctx.panel=(struct drm_panel){.funcs=&panel_ops,.prepared=true,.enabled=true};
 ctx.desc=&liuqin_desc;ctx.oem_panel=&liuqin_oem_panels[0];ctx.refresh_status=1;ctx.supply_votes=7;
 bridge=(struct panel_bridge){{true},&ctx.panel};
 initial_rate=drm_mode_vrefresh(&modes[index]);
 rounded_byte_rate=125878949;
 actual_byte_rate=125878949;actual_pixel_rate=167838599;
 for(int i=0;i<2;i++) {
  cached[i]=modes[index];
  hosts[i]=(struct msm_dsi_host){.base.ops=&ops,.mode=&cached[i],.power_on=true,.enabled=true,
   .refresh_rate=drm_mode_vrefresh(&modes[index]),.mode_flags=MIPI_DSI_MODE_LIVE_VFP,
   .pixel_clk_rate=167838626UL,.byte_clk_rate=rounded_byte_rate,.lanes=4,.dsc=&dsc,
   .byte_clk=&rounded_byte_rate,.id=i};
  unsigned int line=liuqin_line_clocks(&modes[index]);
  hosts[i].regs[REG_DSI_CTRL/4]=3;
  hosts[i].regs[REG_DSI_TOTAL/4]=(line-1)|((modes[index].vtotal-1)<<16);
  hosts[i].regs[REG_DSI_ACTIVE_H/4]=48|(348<<16);
  hosts[i].regs[REG_DSI_ACTIVE_V/4]=216|(3096<<16);
  hosts[i].regs[REG_DSI_ACTIVE_HSYNC/4]=2<<16;
  hosts[i].regs[REG_DSI_ACTIVE_VSYNC_VPOS/4]=2<<16;
  dsi[i].host=&hosts[i].base;links[i].host=dsi[i].host;ctx.dsi[i]=&links[i];
  intfs[i].idx=INTF_1+i;intfs[i].ops.get_line_count=line_count;
  intfs[i].hw.regs[INTF_TIMING_ENGINE_EN/4]=1;
  intfs[i].hw.regs[INTF_DISPLAY_HCTL/4]=48|(347<<16);
  intfs[i].hw.regs[INTF_LINE_COUNT/4]=70+i;
  intfs[i].hw.regs[INTF_FRAME_COUNT/4]=100;
  dpu_hw_intf_live_timing_write(&intfs[i],&modes[index]);
  kms.base.dsi[i]=&dsi[i];
  phys[i]=(struct dpu_encoder_phys){&intfs[i],&ctl,&kms,DPU_ENC_ENABLED,modes[index]};
  enc.phys_encs[i]=&phys[i];
 }
 enc.base.bridge=&bridge.bridge;enc.enabled=true;enc.num_phys_encs=2;enc.disp_info.intf_type=INTF_DSI;enc.dsc=&dsc;
 liuqin_dsi.active=liuqin_dsi.powered=true;liuqin_dsi.gated=false;liuqin_dsi.video=3;
 liuqin_live_all=true;liuqin_live_60=liuqin_live_60_csot=true;
 writes=commands=flushes=legacy_calls=0;command_error=0;
 settle_delays=simulated_us=last_db_disable_us=0;
 fail_flush=fail_frames=fail_dsi_readback=fail_dpu_readback=latched=false;
 diagnostics[0]=0;
}
static void unlocked(void) {assert(!ctx.panel.backlight_lock.held&&!ctx.esd_lock.held&&!dsi_cmd_lock.held&&!enc.enc_spinlock.held);}
static bool change(int a,int b) {bool ok=dpu_encoder_live_refresh(&enc.base,&modes[a],&modes[b]);unlocked();return ok;}
static void unchanged(void) {assert(!writes&&!commands&&!flushes&&!settle_delays);unlocked();}
int main(void) {
 unsigned int transitions=0,faults=0;
 for(int panel=0;panel<2;panel++)for(int a=0;a<4;a++)for(int b=0;b<4;b++) {
  if(a==b)continue;
  setup(a);ctx.oem_panel=&liuqin_oem_panels[panel];assert(change(a,b));
  for(int i=0;i<2;i++) {
   assert(drm_mode_equal(hosts[i].mode,&modes[b]));
   assert(drm_mode_equal(&phys[i].cached_mode,&modes[b]));
   assert(dpu_hw_intf_live_timing_matches(&intfs[i],&modes[b]));
   assert(msm_dsi_host_live_hfp_matches(&hosts[i].base,&modes[b]));
   assert(hosts[i].regs[DSI_VIDEO_TIMING_DB_MODE/4]==0);
  }
  bool hfp=modes[a].htotal!=modes[b].htotal;
  assert(commands==(unsigned)hfp&&flushes==1);
  assert(settle_delays==(hfp?2U:0U));
  if(hfp)assert(sent_b3==(b==1?0x80:b==2?0:0x40)&&sent_b2==(b==0||b==3?0x91:0));
  transitions++;
 }
 // The opt-in applies to horizontal changes only; the proven pair remains.
 setup(0);liuqin_live_all=false;assert(!change(0,1));unchanged();faults++;
 setup(0);liuqin_live_all=false;assert(change(0,3)&&!commands);faults++;
 #define VETO(statement) do {setup(0);statement;assert(!change(0,1));unchanged();assert(strstr(diagnostics,"Liuqin live reject: stage="));faults++;} while(0)
 VETO(ctx.panel.prepared=false);VETO(ctx.panel.enabled=false);VETO(ctx.panel.backlight_pending=true);
 VETO(ctx.refresh_status=-EIO);VETO(ctx.esd_pending=true);VETO(ctx.tddi_recovering=true);
 VETO(ctx.dimming_stopping=true);VETO(ctx.off_deferred=true);VETO(ctx.supply_votes=0);
 VETO(ctx.oem_panel=NULL);VETO(liuqin_live_60=false);VETO(ctx.panel.funcs=NULL);
 VETO(hosts[1].mode_flags=0);VETO(hosts[1].byte_clk_rate++);VETO(hosts[0].refresh_rate=60);
 VETO(actual_byte_rate=0);VETO(actual_pixel_rate=0);VETO(hosts[1].byte_clk_rate=125878969UL);
 VETO(hosts[1].enabled=false);VETO(hosts[1].regs[REG_DSI_TOTAL/4]++);
 VETO(enc.num_phys_encs=1);VETO(enc.intfs_swapped=true);VETO(enc.disp_info.is_cmd_mode=true);
 VETO(intfs[0].hw.regs[INTF_CONFIG/4]=BIT(31));VETO(intfs[1].hw.regs[INTF_HSYNC_CTL/4]++);
 VETO(intfs[1].hw.regs[INTF_LINE_COUNT/4]=2800);VETO(liuqin_dsi.gated=true);
 // Keep actionable values in the earliest rejection, not just the caller's summary.
 setup(0);hosts[1].byte_clk_rate++;assert(!change(0,1));unchanged();
 assert(strstr(diagnostics,"stage=dsi-clock host=1 pixel=167838626 byte=125878950 actual_pixel=167838599 actual_byte=125878949"));faults++;
 setup(0);intfs[1].hw.regs[INTF_HSYNC_CTL/4]++;assert(!change(0,1));unchanged();
 assert(strstr(diagnostics,"stage=intf intf=2 reg=0x8 actual=")&&strstr(diagnostics,"expected="));faults++;
 // Real r3 observation: a repeated round query differs from the running rate.
 setup(0);rounded_byte_rate=125878967;assert(change(0,1));faults++;
 VETO(actual_byte_rate++);VETO(actual_pixel_rate=167838727);VETO(actual_pixel_rate=167838525);
 VETO(hosts[0].byte_clk_rate=hosts[1].byte_clk_rate=actual_byte_rate=125879070);
 VETO(hosts[0].byte_clk_rate=hosts[1].byte_clk_rate=actual_byte_rate=125878868);
 for(int edge=0;edge<2;edge++) {
  setup(0);hosts[0].byte_clk_rate=hosts[1].byte_clk_rate=actual_byte_rate=125878969+(edge?100:-100);
  actual_pixel_rate=167838626+(edge?100:-100);assert(change(0,1));faults++;
 }
 setup(0);assert(!change(0,0));unchanged();faults++;
 for(int field=0;field<12;field++) {
  struct drm_display_mode bad=modes[1];((int*)&bad)[field]++;
  setup(0);assert(!dpu_encoder_live_refresh(&enc.base,&modes[0],&bad));unchanged();faults++;
 }
 for(int fault=0;fault<4;fault++) {
  setup(0);fail_flush=fault==0;fail_frames=fault==1;fail_dsi_readback=fault==2;fail_dpu_readback=fault==3;
  assert(!change(0,1)&&flushes==1&&commands==1);
  for(int i=0;i<2;i++)assert(drm_mode_equal(hosts[i].mode,&modes[0])&&hosts[i].regs[DSI_VIDEO_TIMING_DB_MODE/4]==0);
  faults++;
 }
 setup(0);command_error=-EIO;assert(!change(0,1)&&commands==1&&ctx.refresh_status==-EIO);
 // A partial scan command cannot be retried through this path.
 unsigned int before=writes;assert(!change(1,2)&&commands==1&&writes==before);faults++;
 printf("{\"directed_transitions_two_panels\":%u,\"fault_and_scope_cases\":%u,\"hardware_validated\":false}\n",transitions,faults);
 return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--cc', default=shutil.which('cc') or 'gcc')
    args = parser.parse_args()
    lock = json.loads((PROJECT / 'build/kernel/source.json').read_text())
    patches = [PROJECT / p for p in lock['target_patches']]
    rebuilt = {}
    def reconstruct(name):
        if name in rebuilt:
            return rebuilt[name]
        # Retain known context by line number across every patch. This reconstructs
        # newly added functions even when later patches change only a few lines.
        lines = []
        for patch in patches:
            marker = 'diff --git a/' + name + ' b/' + name + '\n'
            if marker not in patch.read_text():
                continue
            section = patch.read_text().split(marker, 1)[1].split('\ndiff --git ', 1)[0]
            hunks = re.split(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@[^\n]*\n', section, flags=re.M)
            delta = 0
            for offset in range(1, len(hunks), 3):
                start = int(hunks[offset])
                count = int(hunks[offset+1]) if hunks[offset+1] else 1
                index = start - bool(count) + delta
                lines.extend([None] * max(0, index + count - len(lines)))
                old, replacement = [], []
                for row in hunks[offset+2].splitlines():
                    if not row or row[0] not in ' +-':
                        continue
                    if row[0] != '+':
                        old.append(row[1:])
                    if row[0] != '-':
                        replacement.append(row[1:])
                assert len(old) == count, (name, start)
                if any(actual is not None and actual != expected
                       for actual, expected in zip(lines[index:index+count], old)):
                    # A net patch generated on a newer stable base may use
                    # shifted line numbers. Relocate only when the complete
                    # old context is already known and matches exactly once;
                    # never overwrite a conflict or guess from unknown lines.
                    matches = [i for i in range(len(lines) - count + 1)
                               if lines[i:i+count] == old]
                    assert len(matches) == 1, (name, start, matches)
                    delta += matches[0] - index
                    index = matches[0]
                for actual, expected in zip(lines[index:index+count], old):
                    assert actual is None or actual == expected, (name, start, actual, expected)
                lines[index:index+count] = replacement
                delta += len(replacement) - count
        rebuilt[name] = '\n'.join(row if row is not None else '/* unreconstructed base line */' for row in lines)
        return rebuilt[name]
    def funcs(name, names):
        text = (args.source / name).read_text() if args.source else reconstruct(name)
        bodies = [refresh.function(text, fn) for fn in names]
        assert all('unreconstructed base line' not in body for body in bodies)
        if args.source:
            assert bodies == [refresh.function(reconstruct(name), fn) for fn in names]
        return '\n'.join(bodies)+'\n'
    with tempfile.TemporaryDirectory(prefix='liuqin-live-refresh-') as temporary:
        directory = Path(temporary)
        panel, _, _, _ = refresh.assemble(directory/'recipe')
        code = STUBS
        intf = 'drivers/gpu/drm/msm/disp/dpu1/dpu_hw_intf.c'
        # Register offsets come from the first patch exposing each register.
        if args.source:
            defs = (args.source / intf).read_text()
        else:
            # Existing interface offsets are not new patch lines.
            defs = '''#define INTF_TIMING_ENGINE_EN 0x000
#define INTF_CONFIG 0x004
#define INTF_HSYNC_CTL 0x008
#define INTF_VSYNC_PERIOD_F0 0x00c
#define INTF_VSYNC_PULSE_WIDTH_F0 0x014
#define INTF_DISPLAY_V_START_F0 0x01c
#define INTF_DISPLAY_V_END_F0 0x024
#define INTF_DISPLAY_HCTL 0x03c
#define INTF_HSYNC_SKEW 0x04c
#define INTF_FRAME_COUNT 0x0ac
#define INTF_LINE_COUNT 0x0b0
#define INTF_CFG_ACTIVE_H_EN BIT(29)
#define INTF_CFG_ACTIVE_V_EN BIT(30)
'''
        code += '\n'.join(line for line in defs.splitlines() if line.startswith('#define INTF_'))+'\n'
        code += funcs('include/drm/drm_liuqin.h', ['liuqin_live_refresh_mode','liuqin_line_clocks'])
        code += funcs(intf,['dpu_hw_intf_live_timing_matches','dpu_hw_intf_live_timing_write','dpu_hw_intf_frame_count'])
        # The VFP-only functions are unchanged, and extracted from their original patch in default mode.
        code += funcs(intf,['dpu_hw_intf_read_vperiod','dpu_hw_intf_write_vperiod'])
        host='drivers/gpu/drm/msm/dsi/dsi_host.c'
        code += funcs(host,['msm_dsi_host_live_hfp_matches','msm_dsi_host_live_hfp_ready','msm_dsi_host_live_hfp_write',
                            'msm_dsi_host_live_vfp_matches','msm_dsi_host_live_vfp_ready','msm_dsi_host_live_vfp_write','msm_dsi_host_live_vfp_finish'])
        manager='drivers/gpu/drm/msm/dsi/dsi_manager.c'
        code += funcs(manager,['msm_dsi_live_vfp_supported','msm_dsi_live_hfp_begin','msm_dsi_live_hfp_write','msm_dsi_live_hfp_release','msm_dsi_live_hfp_end',
                               'msm_dsi_live_vfp_begin','msm_dsi_live_vfp_write','msm_dsi_live_vfp_end'])
        code += re.search(r'struct liuqin_refresh_mode \{.*?liuqin_refresh_modes\[\] = \{.*?\n\};',panel,re.S)[0]+'\n'
        code += '\n'.join(refresh.function(panel,fn) for fn in ['liuqin_can_multi_refresh','liuqin_live_60_allowed','liuqin_refresh_allowed',
                            'liuqin_set_refresh','nt36532_prepare_live_refresh','nt36532_apply_live_refresh'])+'\n'
        code += funcs('drivers/gpu/drm/bridge/panel.c',['drm_panel_bridge_live_refresh_begin','drm_panel_bridge_live_refresh_apply','drm_panel_bridge_live_refresh_end'])
        code += funcs('drivers/gpu/drm/msm/disp/dpu1/dpu_encoder.c',['dpu_live_vfp_in_window','dpu_encoder_live_vfp',
                            'dpu_live_refresh_latched','dpu_encoder_live_hfp','dpu_encoder_live_refresh'])
        source=directory/'live.c';source.write_text(code+CASES)
        binary=directory/'live'
        subprocess.run([args.cc,'-std=gnu11','-Wall','-Wextra','-Werror','-O2','-fsanitize=undefined',
                        '-fno-sanitize-recover=all',str(source),'-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True,timeout=15)


if __name__ == '__main__':
    main()
