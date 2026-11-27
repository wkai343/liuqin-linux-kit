#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the candidate's C scheduling/transfer paths with paired MMIO failures."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import tempfile


def function(text, name):
    match = re.search(r'^(?:static )?(?:inline )?(?:int|bool|void)\s+' + name + r'\([^;{]*\)\s*\{', text, re.M)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--cc', default='cc')
    args = parser.parse_args()
    base = args.source / 'drivers/gpu/drm/msm'
    host = (base / 'dsi/dsi_host.c').read_text()
    manager = (base / 'dsi/dsi_manager.c').read_text()
    dpu = (base / 'disp/dpu1/dpu_encoder.c').read_text()
    header = (base / 'dsi/dsi.h').read_text()
    state = re.search(r'struct msm_dsi_cmd_schedule \{.*?\n\};', header, re.S)[0]
    prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
typedef uint64_t ktime_t;
#define CONFIG_DRM_MSM_DPU 1
#define current current_is_a_kernel_macro()
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) (((~0U)>>(31-(h)))&((~0U)<<(l)))
#define DIV_ROUND_UP_ULL(n,d) (((n)+(d)-1)/(d))
#define DSI_MAX 2
#define DSI_0 0
#define DSI_1 1
#define MIPI_DSI_MODE_VIDEO 1
#define REG_DSI_STATUS0 4
#define REG_DSI_CTRL 0
#define DSI_STATUS0_CMD_MODE_DMA_BUSY 2
#define DSI_CTRL_CMD_MODE_EN 4
#define LIUQIN_DMA_SCHEDULE_CTRL 0xfc
#define LIUQIN_DMA_SCHEDULE_MASK (BIT(28)|GENMASK(15,0))
#define INTF_MODE_VIDEO 1
#define DPU_ENC_ENABLED 2
struct drm_display_mode { int clock,htotal,vdisplay,vsync_start,vsync_end,vtotal; };
struct platform_device { int dev; };
struct mipi_dsi_host { int id; };
struct msm_dsi_host { struct mipi_dsi_host base; bool power_on,enabled,cmd_dma_scheduled;
 unsigned long mode_flags; struct drm_display_mode *mode; struct platform_device *pdev;
 void *ctrl_base; };
struct msm_dsi { struct mipi_dsi_host *host; };
struct mipi_dsi_msg { const void *tx_buf; size_t tx_len; void *rx_buf; size_t rx_len; };
struct drm_encoder { int unused; };
struct hw_intf { struct { u32 (*get_line_count)(struct hw_intf *); } ops; u32 line; };
struct dpu_encoder_phys { int intf_mode,enable_state; struct hw_intf *hw_intf; };
struct dpu_encoder_virt { int num_phys_encs; struct dpu_encoder_phys *phys_encs[2]; };
static struct dpu_encoder_virt enc;
static struct dpu_encoder_virt *to_dpu_encoder_virt(struct drm_encoder *e){(void)e;return &enc;}
static struct msm_dsi_host hosts[2]; static struct msm_dsi dsis[2];
static u32 regs[2][128]; static struct drm_display_mode modes[2];
static struct hw_intf intfs[2]; static struct dpu_encoder_phys phys[2];
static struct drm_encoder encoder;
static struct { struct drm_encoder *encoder; } msm_dsim_glb;
static struct { bool powered; unsigned video; } liuqin_dsi;
static bool liuqin_cmd_dma_schedule,known,sync_mode;
static int clocks[2],restore_calls[2],writes[2],triggers[2],fail_prepare,fail_readback,tx_result,irq_depth,loops,advance;
static ktime_t now;
static int tests;
#define IS_SYNC_NEEDED() (sync_mode)
static bool liuqin_dsi_requested(void){return known;}
static struct msm_dsi *dsi_mgr_get_dsi(int i){return &dsis[i];}
static struct msm_dsi_host *to_msm_dsi_host(struct mipi_dsi_host *h){return &hosts[h->id];}
static u32 dsi_read(struct msm_dsi_host *h,u32 off){return regs[h->base.id][off/4];}
static void dsi_write(struct msm_dsi_host *h,u32 off,u32 value){
 int i=h->base.id;writes[i]++;if(off==0xfc&&fail_readback==i){fail_readback=-1;return;}regs[i][off/4]=value;}
#define readl_poll_timeout(addr,status,condition,delay,timeout) \
 ((void)(delay),(void)(timeout),(status=*(u32 *)(addr)),(condition)?0:-ETIMEDOUT)
#define wmb() do{}while(0)
#define dev_info_once(...) do{}while(0)
#define dev_err_ratelimited(...) do{}while(0)
#define pr_err(...) do{}while(0)
#define local_irq_save(flags) do{assert(!irq_depth);irq_depth++;(flags)=0;}while(0)
#define local_irq_restore(flags) do{(void)(flags);assert(irq_depth==1);irq_depth--;}while(0)
static ktime_t ktime_get(void){return now;}
static ktime_t ktime_add_ms(ktime_t t,int ms){return t+ms;}
static bool ktime_before(ktime_t a,ktime_t b){return a<b;}
static void usleep_range(int a,int b){assert(a==50&&b==100&&!irq_depth);now++;loops++;if(advance&&loops==advance)intfs[0].line=intfs[1].line=0;}
static u32 line_count(struct hw_intf *h){return h->line;}
static int msm_dsi_host_xfer_prepare(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){
 (void)m;if(h->id==fail_prepare)return -EIO;clocks[h->id]++;regs[h->id][0]|=4;return 0;}
static void msm_dsi_host_xfer_restore(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){
 (void)m;assert(clocks[h->id]==1);clocks[h->id]--;restore_calls[h->id]++;regs[h->id][0]&=~4U;}
int msm_dsi_manager_cmd_xfer_trigger(int id,u32 base,u32 len);
static int msm_dsi_host_cmd_tx(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){
 int r=msm_dsi_manager_cmd_xfer_trigger(h->id,0x1000,m->tx_len);return r<0?r:tx_result<0?tx_result:(int)m->tx_len;}
static int msm_dsi_host_cmd_tx_batch(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m,size_t n){
 int total=0;for(size_t i=0;i<n;i++)total+=m[i].tx_len;int r=msm_dsi_manager_cmd_xfer_trigger(h->id,0x1000,total);return r<0?r:tx_result<0?tx_result:total;}
static int msm_dsi_host_cmd_rx(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){(void)h;return m->rx_len;}
static void msm_dsi_host_cmd_xfer_commit(struct mipi_dsi_host *h,u32 base,u32 len){
 assert(base==0x1000&&len>0);triggers[h->id]++;
 if(hosts[h->id].cmd_dma_scheduled){assert(irq_depth==1);assert((regs[0][63]&BIT(28))&&(regs[1][63]&BIT(28)));}}
'''
    core = '\n'.join([state, (base / 'dsi/dsi_cmd_schedule.h').read_text(),
                       'static struct msm_dsi_cmd_schedule *liuqin_active_schedule;',
                       function(host, 'msm_dsi_host_cmd_schedule_prepare'),
                       function(host, 'msm_dsi_host_cmd_schedule_finish'),
                       function(dpu, 'dpu_encoder_get_cmd_linecounts'),
                       function(manager, 'msm_dsi_manager_cmd_xfer_trigger'),
                       function(manager, 'dsi_mgr_cmd_xfer')])
    suffix = r'''
static const u32 saved[2]={0xa8001234,0x48005678};
static const struct mipi_dsi_msg msg={.tx_buf="x",.tx_len=2};
static void setup(void){
 memset(regs,0,sizeof(regs));memset(clocks,0,sizeof(clocks));memset(writes,0,sizeof(writes));
 memset(triggers,0,sizeof(triggers));memset(restore_calls,0,sizeof(restore_calls));
 liuqin_cmd_dma_schedule=known=sync_mode=true;liuqin_dsi.powered=true;liuqin_dsi.video=3;
 liuqin_active_schedule=NULL;fail_prepare=fail_readback=-1;tx_result=irq_depth=loops=advance=now=0;
 msm_dsim_glb.encoder=&encoder;enc.num_phys_encs=2;
 for(int i=0;i<2;i++){
  modes[i]=(struct drm_display_mode){785245,2096,2880,2906,2908,3122};
  hosts[i]=(struct msm_dsi_host){.base={i},.power_on=true,.enabled=true,.mode_flags=1,.mode=&modes[i],.ctrl_base=regs[i]};
  dsis[i].host=&hosts[i].base;regs[i][0]=0x1f3;regs[i][63]=saved[i];
  intfs[i]=(struct hw_intf){.ops={line_count},.line=50};
  phys[i]=(struct dpu_encoder_phys){.intf_mode=1,.enable_state=2,.hw_intf=&intfs[i]};enc.phys_encs[i]=&phys[i];
 }tests++;
}
static void clean(void){for(int i=0;i<2;i++){assert(!clocks[i]&&!hosts[i].cmd_dma_scheduled);assert(regs[i][63]==saved[i]);assert((regs[i][0]&~4U)==0x1f3);}assert(!irq_depth&&!liuqin_active_schedule);}
int main(void){
 u32 line,guard;
 setup();assert(!dsi_cmd_schedule_calculate(&modes[0],&line,&guard));
 assert(line==3097&&guard==38);
 assert(dsi_cmd_schedule_line_safe(line-guard-1,line,guard,3122));
 assert(!dsi_cmd_schedule_line_safe(line-guard,line,guard,3122));
 assert(!dsi_cmd_schedule_line_safe(line+1,line,guard,3122));
 assert(dsi_cmd_schedule_line_safe(line+2,line,guard,3122));
 assert(!dsi_cmd_schedule_line_safe(3123,line,guard,3122));
 assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==2);clean();assert(triggers[0]==1&&triggers[1]==1);
 for(int disable=0;disable<5;disable++){setup();switch(disable){case 0:liuqin_cmd_dma_schedule=false;break;case 1:known=false;break;case 2:liuqin_dsi.powered=false;break;case 3:liuqin_dsi.video=0;break;case 4:hosts[0].enabled=hosts[1].enabled=false;break;}assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==2);clean();assert(!writes[0]&&!writes[1]);}
 setup();struct mipi_dsi_msg read=msg;char rx;read.rx_buf=&rx;read.rx_len=1;assert(dsi_mgr_cmd_xfer(1,&read,NULL,0)==1);clean();assert(!writes[0]&&!writes[1]&&!triggers[0]);
 for(int i=0;i<2;i++){setup();fail_readback=i;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-EIO);clean();assert(!triggers[0]&&!triggers[1]);}
 setup();modes[1].vsync_start=modes[1].vdisplay;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-EINVAL);clean();assert(!triggers[0]);
 setup();modes[1].vtotal++;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-EINVAL);clean();assert(!triggers[0]);
 setup();hosts[1].enabled=false;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-EINVAL);clean();assert(!triggers[0]);
 setup();tx_result=-ETIMEDOUT;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-ETIMEDOUT);clean();assert(triggers[0]==1);
 for(int i=0;i<2;i++){setup();regs[i][1]=2;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-ETIMEDOUT);clean();assert(!(regs[i][0]&4));}
 setup();intfs[1].line=3097;advance=3;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==2);assert(loops==3);clean();
 setup();intfs[0].line=3097;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-ETIMEDOUT);assert(loops==50&&!triggers[0]&&!triggers[1]);clean();
 setup();phys[1].enable_state=0;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-ETIMEDOUT);assert(loops==50&&!triggers[0]);clean();
 setup();enc.num_phys_encs=1;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-EOPNOTSUPP);clean();assert(!triggers[0]);
 setup();struct mipi_dsi_msg batch[2]={msg,msg};assert(dsi_mgr_cmd_xfer(1,&msg,batch,2)==4);clean();
 for(int i=0;i<2;i++){setup();fail_prepare=i;assert(dsi_mgr_cmd_xfer(1,&msg,NULL,0)==-EIO);clean();assert(!writes[0]&&!writes[1]&&!triggers[0]);}
 setup();assert(dsi_mgr_cmd_xfer(0,&msg,NULL,0)==2);clean();assert(!triggers[0]&&!writes[0]&&!writes[1]);
 printf("{\"executed_transfer_scenarios\":%d,\"scanline_boundaries_checked\":true,\"paired_cleanup_checked\":true}\n",tests);
}
'''
    args.report.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='liuqin-cmd-schedule-') as directory:
        path = Path(directory)
        (path / 'test.c').write_text(prefix + '\n' + core + '\n' + suffix)
        subprocess.run([args.cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O2',
                        str(path / 'test.c'), '-o', str(path / 'test')], check=True)
        result = subprocess.check_output([str(path / 'test')], text=True)
        report = json.loads(result)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(result, end='')


if __name__ == '__main__':
    main()
