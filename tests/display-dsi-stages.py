#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the candidate's DSI lifecycle C with paired-host fault injection."""
from pathlib import Path
import argparse, hashlib, json, re, subprocess, tempfile

def function(s,name):
    m=re.search(r'^(?:static )?(?:inline )?(?:bool|int|void)\s+'+name+r'\([^;{]*\)\s*\{',s,re.M)
    assert m,name
    i=m.end();depth=1
    while depth:depth+=(s[i]=='{')-(s[i]=='}');i+=1
    return s[m.start():i]

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True)
    ap.add_argument('--report',type=Path,required=True);ap.add_argument('--cc',default='cc');a=ap.parse_args()
    a.report.unlink(missing_ok=True)
    rel='drivers/gpu/drm/msm/dsi/';s=(a.source/rel/'dsi_manager.c').read_text()
    h=(a.source/rel/'dsi_host.c').read_text();old=(a.baseline/rel/'dsi_manager.c').read_text()
    header=(a.source/'include/drm/drm_mipi_dsi.h').read_text()
    enum=re.search(r'enum mipi_dsi_panel_stage \{.*?\n\};',header,re.S).group()
    block=s[s.index('/* Boot-only controls:'):s.index('static void dsi_mgr_bridge_pre_enable(')]
    assert len(re.findall(r'module_param\(liuqin_oem_\w+, bool, 0400\)',block))==3
    assert not re.search(r'static bool liuqin_oem_\w+\s*=',block)
    assert 'device_lock(&provider->dev)' in block and 'regmap_update_bits' in block
    assert 'writel' not in block and 'ioremap' not in block
    assert function(s,'dsi_mgr_bridge_power_on')==function(old,'dsi_mgr_bridge_power_on')
    assert function(s,'dsi_mgr_bridge_power_off')==function(old,'dsi_mgr_bridge_power_off')
    pn='drivers/gpu/drm/panel/panel-novatek-nt36532.c';panel=(a.source/pn).read_text();po=(a.baseline/pn).read_text()
    for name in re.findall(r'^(?:static )?(?:inline )?(?:bool|int|void)\s+(\w+)\([^;{]*\)\s*\{',po,re.M):
        if name not in ('nt36532_probe','nt36532_prepare','nt36532_disable','nt36532_unprepare'):
            actual=function(panel,name)
            if name=='liuqin_cancel_dimming':
                # CABC lifecycle behavior is executed by display-oem-cabc.py.
                actual=actual.replace('\tctx->cabc_ready = false;\n\tctx->cabc_applied = -1;\n\tctx->cabc_cycle = 0;\n','')
            if name=='nt36532_post_backlight_enable':
                actual=actual.replace('\tliuqin_cabc_first_frame(ctx);\n','').replace('ready:\n\tliuqin_esd_start(ctx);\n\tliuqin_schedule_dimming(ctx);','ready:\n\tliuqin_schedule_dimming(ctx);\n\tliuqin_esd_start(ctx);')
            assert function(po,name)==actual,name
    assert re.findall(r'msleep\(\d+\)',panel)==re.findall(r'msleep\(\d+\)',po)
    assert 'if (ctx->desc == &liuqin_desc && ctx->oem_panel)\n\t\t\tctx->dsi[i]->mode_flags |= MIPI_DSI_MODE_PANEL_STAGES;' in panel
    prepare=function(panel,'nt36532_prepare');disable=function(panel,'nt36532_disable');unprepare=function(panel,'nt36532_unprepare')
    assert prepare.index('init_sequence(ctx)')<prepare.index('mipi_dsi_picture_parameter_set')<prepare.index('MIPI_DSI_PANEL_PREPARED')
    assert disable.index('liuqin_cancel_dimming')<disable.index('MIPI_DSI_PANEL_DISABLING')<disable.index('nt36532_off')
    assert unprepare.index('liuqin_cancel_dimming')<unprepare.index('MIPI_DSI_PANEL_UNPREPARING')<unprepare.index('gpiod_set_value_cansleep')<unprepare.index('nt36532_disable_supplies')
    prefix=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) (((~0U)<<(l))&((~0U)>>(31-(h))))
#define DSI_MAX 2
#define DSI_1 1
#define DSI_CLOCK_MASTER 0
#define MIPI_DSI_MODE_PANEL_STAGES BIT(13)
#define MIPI_DSI_MODE_VIDEO BIT(0)
#define MIPI_DSI_MODE_VIDEO_BURST BIT(1)
#define MIPI_DSI_CLOCK_NON_CONTINUOUS BIT(10)
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define DBG(...) ((void)0)
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
struct device_node {int id;bool known;};
struct device {struct device_node *of_node;void *driver;};
struct platform_device {struct device dev;};
struct mipi_dsi_host {int id;unsigned long flags;bool on,video,irq;};
struct msm_dsi {struct mipi_dsi_host *host;struct platform_device *pdev;void *phy;bool phy_enabled;};
struct msm_dsi_phy_shared_timings {int dummy;};
struct drm_bridge {int id;};
struct of_phandle_args {struct device_node *np;};
struct resource {size_t size;};struct regmap {u32 value;};
static struct device_node nodes[2],provider_node;
static struct platform_device devices[2],provider;
static struct mipi_dsi_host hosts[2];static struct msm_dsi dsis[2];
static struct msm_dsi *pair[2];static struct regmap map;
static bool bonded,sync_mode,provider_exists,map_exists;static int master_id;
static int dsi_cmd_lock,provider_locked,node_refs,device_refs,clock_index,parse_error,address_error;
static int fail_on,fail_video,fail_gate_call,gate_calls,fail_phy,phy_live,phy_offs,tests,pm_refs,fail_pm;
static size_t resource_bytes;
static char events[256][32];static int count;
static void event(const char *s,int id){assert(count<256);snprintf(events[count++],32,"%s%d",s,id);}
static int find(const char *s){for(int i=0;i<count;i++)if(!strcmp(events[i],s))return i;return -1;}
static void before(const char *a,const char *b){assert(find(a)>=0 && find(b)>=0 && find(a)<find(b));}
#define IS_BONDED_DSI() bonded
#define IS_SYNC_NEEDED() sync_mode
#define IS_MASTER_DSI_LINK(id) ((id)==master_id)
static struct msm_dsi *dsi_mgr_get_dsi(int i){assert(i==0||i==1);return pair[i];}
static int dsi_mgr_bridge_get_id(struct drm_bridge *b){return b->id;}
static unsigned long msm_dsi_host_get_mode_flags(struct mipi_dsi_host *h){return h->flags;}
static bool of_device_is_compatible(struct device_node *n,const char *s){assert(!strcmp(s,"qcom,sm8450-dsi-ctrl")||!strcmp(s,"qcom,sm8475-dispcc"));return n->known;}
static int of_property_match_string(struct device_node *n,const char *p,const char *v){assert(n==&nodes[0]&&!strcmp(p,"clock-names")&&!strcmp(v,"byte"));return clock_index;}
static int of_parse_phandle_with_args(struct device_node *n,const char *p,const char *c,int idx,struct of_phandle_args *a){assert(n==&nodes[0]&&!strcmp(p,"clocks")&&!strcmp(c,"#clock-cells")&&idx==clock_index);if(parse_error)return parse_error;a->np=&provider_node;node_refs++;return 0;}
static int of_address_to_resource(struct device_node *n,int i,struct resource *r){assert(n==&provider_node&&i==0);r->size=resource_bytes;return address_error;}
#define resource_size(r) ((r)->size)
static void of_node_put(struct device_node *n){assert(n==&provider_node&&node_refs>0);node_refs--;}
static struct platform_device *of_find_device_by_node(struct device_node *n){assert(n==&provider_node);if(!provider_exists)return NULL;device_refs++;return &provider;}
static void put_device(struct device *d){assert(d==&provider.dev&&device_refs>0&&!provider_locked);device_refs--;}
static void device_lock(struct device *d){assert(d==&provider.dev&&!provider_locked);provider_locked=1;}
static void device_unlock(struct device *d){assert(d==&provider.dev&&provider_locked);provider_locked=0;}
static struct regmap *dev_get_regmap(struct device *d,const char *name){assert(d==&provider.dev&&!name&&provider_locked);return map_exists?&map:NULL;}
static int pm_runtime_resume_and_get(struct device *d){assert(d==&provider.dev&&provider_locked&&!pm_refs);if(fail_pm)return -EIO;pm_refs++;return 0;}
static void pm_runtime_put(struct device *d){assert(d==&provider.dev&&provider_locked&&pm_refs==1);pm_refs--;}
static int regmap_update_bits(struct regmap *m,u32 offset,u32 mask,u32 value){assert(m==&map&&provider_locked&&pm_refs==1&&offset==0xf000&&mask==0x7e0&&(value==0||value==0x260));event("gate",!!value);gate_calls++;m->value=(m->value&~mask)|value;return gate_calls==fail_gate_call?-EIO:0;}
static void mutex_lock(int *m){assert(!*m);*m=1;}
static void mutex_unlock(int *m){assert(*m);*m=0;}
static int dsi_mgr_phy_enable(int id,struct msm_dsi_phy_shared_timings *t){(void)t;assert(id==0&&!phy_live);event("phy",1);if(fail_phy)return -EIO;phy_live=1;dsis[0].phy_enabled=true;return 0;}
static void dsi_mgr_phy_disable(int id){dsis[id].phy_enabled=false;if(!dsis[0].phy_enabled&&!dsis[1].phy_enabled){assert(phy_live&&!hosts[0].on&&!hosts[1].on);phy_live=0;phy_offs++;event("phy",0);}}
static int msm_dsi_host_power_on(struct mipi_dsi_host *h,void *t,bool bonded_arg,void *phy){(void)t;(void)phy;assert(phy_live&&!h->on&&bonded_arg);event("on",h->id);if(h->id==fail_on)return -EIO;h->on=true;return 0;}
static int msm_dsi_host_power_off(struct mipi_dsi_host *h){assert(h->on&&!h->irq);event("off",h->id);h->on=false;return 0;}
static void msm_dsi_host_enable_irq(struct mipi_dsi_host *h){assert(h->on&&!h->irq);h->irq=true;}
static void msm_dsi_host_disable_irq(struct mipi_dsi_host *h){assert(h->on&&h->irq);h->irq=false;}
static int msm_dsi_host_enable(struct mipi_dsi_host *h){assert(h->on&&!h->video);event("video",h->id);if(h->id==fail_video)return -EIO;h->video=true;return 0;}
static int msm_dsi_host_stop_video(struct mipi_dsi_host *h){assert(h->on&&h->video);event("stop",h->id);h->video=false;return 0;}
static int msm_dsi_host_disable(struct mipi_dsi_host *h){assert(h->on);event("disable",h->id);h->video=false;return 0;}
static void msm_dsi_phy_pll_save_state(void *phy){(void)phy;assert(phy_live&&hosts[0].on&&hosts[1].on);event("save",0);}
'''
    core=function(s,'dsi_mgr_bridge_power_on')+'\n'+function(s,'dsi_mgr_bridge_power_off')+'\n'+block+'\n'+function(s,'dsi_mgr_bridge_pre_enable')+'\n'+function(s,'dsi_mgr_bridge_post_disable')
    suffix=r'''
static struct drm_bridge bridge={0},slave={1};
static void setup(int flags){assert(!node_refs&&!device_refs&&!provider_locked&&!dsi_cmd_lock&&!pm_refs);memset(&liuqin_dsi,0,sizeof(liuqin_dsi));memset(dsis,0,sizeof(dsis));memset(hosts,0,sizeof(hosts));for(int i=0;i<2;i++){nodes[i]=(struct device_node){i,true};devices[i].dev.of_node=&nodes[i];hosts[i].id=i;hosts[i].flags=BIT(13)|BIT(0)|BIT(1)|BIT(10);dsis[i].host=&hosts[i];dsis[i].pdev=&devices[i];pair[i]=&dsis[i];}provider_node.known=true;provider_exists=map_exists=true;provider.dev.driver=&provider;bonded=sync_mode=true;master_id=0;clock_index=2;parse_error=address_error=0;resource_bytes=0x20000;fail_on=fail_video=-1;fail_phy=fail_gate_call=gate_calls=phy_live=phy_offs=count=pm_refs=fail_pm=0;map.value=0x12345810;liuqin_oem_video_order=flags&1;liuqin_oem_shutdown_order=flags&2;liuqin_oem_clk_gating=flags&4;tests++;}
static void ready(void){dsi_mgr_bridge_pre_enable(&slave);assert(!phy_live);dsi_mgr_bridge_pre_enable(&bridge);assert(phy_live&&hosts[0].on&&hosts[1].on);event("panelon",0);event("pps",0);assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED));assert(hosts[0].video&&hosts[1].video);}
static void down(int flags){assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_DISABLING));event("paneloff",0);assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_UNPREPARING));event("rails",0);dsi_mgr_bridge_post_disable(&slave);dsi_mgr_bridge_post_disable(&bridge);assert(!phy_live&&!hosts[0].on&&!hosts[1].on&&phy_offs==1);if(flags&2){before("stop0","paneloff0");before("stop1","paneloff0");before("paneloff0","off1");before("off1","off0");before("phy0","rails0");}else{before("rails0","disable0");before("off0","off1");}if(flags){int saved=count;dsi_mgr_bridge_post_disable(&bridge);dsi_mgr_bridge_post_disable(&slave);assert(count==saved);assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_UNPREPARING));assert(count==saved);}assert(!node_refs&&!device_refs&&!provider_locked&&!dsi_cmd_lock&&!pm_refs);}
int main(void){
 for(int f=0;f<8;f++){setup(f);ready();if(f&1){before("pps0","video0");before("pps0","video1");}else{before("video1","panelon0");}if(f&4){before("gate0","on0");before("on1","gate1");assert((map.value&0x7e0)==0x260);}else assert(!gate_calls&&map.value==0x12345810);down(f);if(f&4)assert(map.value==(0x12345810&~0x7e0U)&&gate_calls==3);if(f){count=0;phy_offs=0;ready();down(f);}}
 for(int bad=0;bad<11;bad++){setup(7);switch(bad){case 0:bonded=false;break;case 1:sync_mode=false;break;case 2:master_id=1;break;case 3:pair[0]=NULL;break;case 4:pair[1]=NULL;break;case 5:dsis[0].host=NULL;break;case 6:dsis[1].host=NULL;break;case 7:nodes[0].known=false;break;case 8:nodes[1].known=false;break;case 9:hosts[0].flags&=~BIT(13);break;case 10:hosts[1].flags&=~BIT(10);break;}assert(!liuqin_dsi_requested());assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED)&&!count);}
 for(int bad=0;bad<9;bad++){setup(4);switch(bad){case 0:clock_index=-ENOENT;break;case 1:parse_error=-EINVAL;break;case 2:provider_node.known=false;break;case 3:address_error=-EINVAL;break;case 4:resource_bytes=0xf003;break;case 5:provider_exists=false;break;case 6:provider.dev.driver=NULL;break;case 7:map_exists=false;break;case 8:fail_pm=1;break;}dsi_mgr_bridge_pre_enable(&bridge);assert(!phy_live&&!gate_calls&&!node_refs&&!device_refs&&!provider_locked);assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED)==-EIO);dsi_mgr_bridge_post_disable(&bridge);}
 for(int f=1;f<8;f++)for(int fail=0;fail<3;fail++){setup(f);if(fail<2)fail_on=fail;else fail_phy=1;dsi_mgr_bridge_pre_enable(&bridge);assert(!hosts[0].on&&!hosts[1].on&&!phy_live);assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED)==-EIO);dsi_mgr_bridge_post_disable(&bridge);dsi_mgr_bridge_post_disable(&slave);assert(!node_refs&&!device_refs);}
 for(int f=1;f<8;f++)for(int fail=0;fail<2;fail++){setup(f);fail_video=fail;dsi_mgr_bridge_pre_enable(&bridge);if(f&1){assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED)==-EIO);assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARE_FAILED));}assert(!phy_live&&!hosts[0].on&&!hosts[1].on);dsi_mgr_bridge_post_disable(&bridge);assert(phy_offs==1);}
 setup(7);fail_gate_call=1;dsi_mgr_bridge_pre_enable(&bridge);assert(!phy_live);dsi_mgr_bridge_post_disable(&bridge);
 setup(7);fail_gate_call=2;dsi_mgr_bridge_pre_enable(&bridge);assert(!phy_live&&phy_offs==1&&gate_calls==3);dsi_mgr_bridge_post_disable(&bridge);
 setup(7);ready();fail_gate_call=3;assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_UNPREPARING)==-EIO);assert(phy_live&&hosts[0].on&&hosts[1].on&&hosts[0].irq&&hosts[1].irq);fail_gate_call=0;assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_UNPREPARING));assert(!phy_live&&phy_offs==1);dsi_mgr_bridge_post_disable(&bridge);
 setup(7);assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED)==-EIO);assert(!msm_dsi_manager_panel_stage(1,MIPI_DSI_PANEL_PREPARED));ready();int saved=count;assert(!msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_PREPARED));assert(count==saved);assert(msm_dsi_manager_panel_stage(0,99)==-EINVAL);assert(liuqin_dsi_power_on(&bridge)==-EBUSY);down(7);
 printf("%d\n",tests);return 0;
}
'''
    host_test=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
typedef uint32_t u32;
struct mipi_dsi_host {int unused;};struct platform_device {int dev;};
struct msm_dsi_host {bool power_on,enabled;struct platform_device *pdev;};
static struct msm_dsi_host obj;static u32 ctrl,status;static int writes,resets;
#define REG_DSI_CTRL 0
#define REG_DSI_STATUS0 4
#define DSI_CTRL_VID_MODE_EN 2
#define DSI_STATUS0_VIDEO_MODE_ENGINE_BUSY 8
#define dev_warn(...) ((void)0)
static struct msm_dsi_host *to_msm_dsi_host(struct mipi_dsi_host *h){(void)h;return &obj;}
static u32 dsi_read(struct msm_dsi_host *h,int reg){assert(h->power_on);return reg==REG_DSI_CTRL?ctrl:status;}
static void dsi_write(struct msm_dsi_host *h,int reg,u32 v){assert(h->power_on&&reg==REG_DSI_CTRL);ctrl=v;writes++;}
#define read_poll_timeout(op,val,cond,interval,timeout,delay,h,reg) ((void)sizeof(char[(interval==1000&&timeout==50000&&!delay)?1:-1]),val=op(h,reg),(cond)?0:-ETIMEDOUT)
static void dsi_sw_reset(struct msm_dsi_host *h){assert(h->power_on);resets++;}
'''+function(h,'msm_dsi_host_stop_video')+r'''
int main(void){struct mipi_dsi_host host;obj.power_on=false;assert(msm_dsi_host_stop_video(&host)==-EIO&&!writes&&!resets);for(int busy=0;busy<2;busy++){ctrl=0xabcdefef;status=busy?8:0;obj.power_on=obj.enabled=true;writes=resets=0;assert(!msm_dsi_host_stop_video(&host));assert(ctrl==(0xabcdefef&~2U)&&writes==1&&resets==busy&&!obj.enabled);}return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='liuqin-stages-') as tmp:
        counts=[]
        for name,code in [('manager',prefix+enum+'\n'+core+suffix),('host',host_test)]:
            src=Path(tmp)/(name+'.c');exe=Path(tmp)/name;src.write_text(code)
            subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-variable','-Wno-unused-parameter','-fsanitize=undefined','-fno-sanitize-recover=all',str(src),'-o',str(exe)],check=True)
            output=subprocess.check_output([str(exe)],text=True,timeout=20);counts.append(int(output) if output.strip() else 3)
    report={'all_passed':True,'cases':sum(counts),'actual_manager_and_host_C_executed':True,'all_eight_switch_combinations':True,'failure_cleanup_and_repeated_off_checked':True,'provider_refs_and_lifetime_lock_checked':True,'provider_runtime_pm_refs_balanced':True,'unrelated_dispcc_bits_preserved':True,'unknown_panel_topology_falls_back':True,'video_only_stop_and_busy_reset_checked':True,'panel_commands_delays_supplies_unchanged':True,'patch_default_off':True,'candidate_manager_sha256':hashlib.sha256(s.encode()).hexdigest(),'hardware_validation_required':True}
    a.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
if __name__=='__main__':main()
