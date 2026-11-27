#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run actual panel/manager shutdown C with the DPU bridge phase boundary."""
from pathlib import Path
import argparse,ast,importlib.util,json,re,subprocess,tempfile

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,required=True);ap.add_argument('--report',type=Path,required=True);ap.add_argument('--cc',default='cc');a=ap.parse_args();a.report.unlink(missing_ok=True)
 test=Path(__file__).with_name('display-dsi-stages.py');spec=importlib.util.spec_from_file_location('stages',test);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);fn=module.function
 assignments={}
 for n in ast.walk(ast.parse(test.read_text())):
  if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ['prefix','suffix']:
   assignments[n.targets[0].id]=ast.literal_eval(n.value)
 prefix=assignments['prefix'].replace('struct mipi_dsi_host {int id;', 'struct mipi_dsi_host_ops;\nstruct mipi_dsi_host {const struct mipi_dsi_host_ops *ops;int id;')
 prefix=prefix.replace('static struct device_node nodes[2]', 'static bool dpu_running,require_post_disable;\nstatic int fail_stop;\nstatic struct device_node nodes[2]')
 prefix=prefix.replace('assert(h->on&&h->video);event("stop",h->id);h->video=false;return 0;', 'assert(h->on&&h->video&&!dpu_running);event("stop",h->id);if(fail_stop)return -EIO;h->video=false;return 0;')
 s=(a.source/'drivers/gpu/drm/msm/dsi/dsi_manager.c').read_text();panel=(a.source/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text();header=(a.source/'include/drm/drm_mipi_dsi.h').read_text()
 enum=re.search(r'enum mipi_dsi_panel_stage \{.*?\n\};',header,re.S).group()
 core=fn(s,'dsi_mgr_bridge_power_on')+'\n'+fn(s,'dsi_mgr_bridge_power_off')+'\n'+s[s.index('/* Boot-only controls:'):s.index('static void dsi_mgr_bridge_pre_enable(')]+'\n'+fn(s,'dsi_mgr_bridge_pre_enable')+'\n'+fn(s,'dsi_mgr_bridge_post_disable')
 # Verify the real DRM call boundary and panel bridge mapping, not just labels.
 drm=(a.source/'drivers/gpu/drm/drm_atomic_helper.c').read_text();bridge=(a.source/'drivers/gpu/drm/bridge/panel.c').read_text();panelcore=(a.source/'drivers/gpu/drm/drm_panel.c').read_text()
 boundary=drm[drm.index('bridge = drm_bridge_chain_get_first_bridge(encoder);'):drm.index('static void\ncrtc_disable')]
 assert boundary.index('drm_atomic_bridge_chain_disable')<boundary.index('funcs->atomic_disable')
 outputs=fn(drm,'disable_outputs')
 assert outputs.index('encoder_bridge_disable')<outputs.index('crtc_disable')<outputs.index('encoder_bridge_post_disable')
 assert 'drm_atomic_bridge_chain_post_disable' in fn(drm,'encoder_bridge_post_disable')
 assert 'drm_panel_unprepare(panel_bridge->panel)' in fn(bridge,'panel_bridge_atomic_post_disable')
 unprep=fn(panelcore,'drm_panel_unprepare');assert unprep.index('panel_unpreparing')<unprep.index('panel->funcs->unprepare')
 disable=fn(panel,'nt36532_disable');unprepare=fn(panel,'nt36532_unprepare')
 assert disable.index('liuqin_cancel_dimming')<disable.index('MIPI_DSI_PANEL_DEFER_OFF')<disable.index('MIPI_DSI_PANEL_DISABLING')
 assert unprepare.index('MIPI_DSI_PANEL_DISABLING')<unprepare.index('nt36532_off')<unprepare.index('MIPI_DSI_PANEL_UNPREPARING')<unprepare.index('nt36532_disable_supplies')
 assert unprepare.index('ctx->off_deferred = false')<unprepare.index('nt36532_off')
 declarations=r'''
#define MIPI_DSI_MODE_LPM BIT(11)
struct mipi_dsi_host_ops {int (*panel_stage)(struct mipi_dsi_host*,enum mipi_dsi_panel_stage);};
struct mipi_dsi_device {struct device dev;struct mipi_dsi_host *host;unsigned long mode_flags;};
struct nt36532;struct drm_panel {struct device *dev;struct nt36532 *ctx;};
struct nt36532 {bool off_deferred;struct drm_panel panel;struct mipi_dsi_device *dsi[2];int *reset_gpio;};
static struct nt36532 panel_ctx;static struct mipi_dsi_device dsi_devices[2];
static int reset_gpio,off_calls,fail_off,rail_calls;
static struct nt36532 *to_nt36532(struct drm_panel *p){return p->ctx;}
static int panel_stage_cb(struct mipi_dsi_host *h,enum mipi_dsi_panel_stage stage){return msm_dsi_manager_panel_stage(h->id,stage);}
static const struct mipi_dsi_host_ops ops={panel_stage_cb};
static void liuqin_esd_stop(struct nt36532 *c,bool permanent){assert(c==&panel_ctx&&!permanent);event("esd_stop",0);}
static void liuqin_cancel_dimming(struct nt36532 *c,bool permanent){assert(c==&panel_ctx&&!permanent);event("dimming_stop",0);}
static int nt36532_off(struct nt36532 *c){assert(c==&panel_ctx&&hosts[0].on&&hosts[1].on);if(require_post_disable)assert(!dpu_running&&!hosts[0].video&&!hosts[1].video);off_calls++;event("paneloff",0);return fail_off?-EIO:0;}
static void gpiod_set_value_cansleep(int *g,int value){assert(g==&reset_gpio&&value==1);event("reset",0);}
static void nt36532_disable_supplies(struct nt36532 *c){assert(c==&panel_ctx);if(require_post_disable)assert(!hosts[0].on&&!hosts[1].on&&!phy_live);rail_calls++;event("rails",0);}
'''
 setup=fn(assignments['suffix'],'setup')
 fixture=r'''
static struct drm_bridge bridge={0},slave={1};
static void setup_panel(int f){setup(f);dpu_running=false;require_post_disable=!!(f&2);fail_stop=fail_off=off_calls=rail_calls=0;memset(&panel_ctx,0,sizeof(panel_ctx));for(int i=0;i<2;i++){hosts[i].ops=&ops;dsi_devices[i].host=&hosts[i];dsi_devices[i].mode_flags=hosts[i].flags|MIPI_DSI_MODE_LPM;panel_ctx.dsi[i]=&dsi_devices[i];}panel_ctx.panel.ctx=&panel_ctx;panel_ctx.panel.dev=&dsi_devices[0].dev;panel_ctx.reset_gpio=&reset_gpio;}
static void ready(void){dsi_mgr_bridge_pre_enable(&bridge);assert(!mipi_dsi_panel_stage(panel_ctx.dsi[0],MIPI_DSI_PANEL_PREPARED));dpu_running=true;assert(hosts[0].video&&hosts[1].video);}
static void encoder_off(void){event("encoder_off",0);dpu_running=false;}
static void finish(void){assert(!nt36532_unprepare(&panel_ctx.panel));dsi_mgr_bridge_post_disable(&slave);dsi_mgr_bridge_post_disable(&bridge);assert(!phy_live&&phy_offs==1&&!panel_ctx.off_deferred&&off_calls==1&&rail_calls==1);assert(!device_refs&&!node_refs&&!pm_refs&&!provider_locked&&!dsi_cmd_lock);if(require_post_disable){before("encoder_off0","stop0");before("stop1","paneloff0");before("paneloff0","off1");before("off1","off0");before("phy0","rails0");}else{before("paneloff0","encoder_off0");before("rails0","off0");}}
int main(void){
 for(int f=0;f<8;f++)for(int round=0;round<3;round++){
  setup_panel(f);ready();assert(!nt36532_disable(&panel_ctx.panel));
  if(f&2){assert(panel_ctx.off_deferred&&off_calls==0&&find("stop0")==-1);assert(hosts[0].video&&hosts[1].video);int saved=count;assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_DEFER_OFF)==1);assert(saved==count);}else assert(!panel_ctx.off_deferred&&off_calls==1);
  encoder_off();finish();
 }
 // A failed OFF group is not replayed, but complete power cleanup still runs.
 setup_panel(7);ready();assert(!nt36532_disable(&panel_ctx.panel));encoder_off();fail_off=1;finish();
 // Video-stop failure holds the deferred OFF and rail state for a later retry.
 setup_panel(7);ready();assert(!nt36532_disable(&panel_ctx.panel));encoder_off();fail_stop=1;assert(nt36532_unprepare(&panel_ctx.panel)==-EIO);assert(panel_ctx.off_deferred&&!off_calls&&!rail_calls&&phy_live);fail_stop=0;finish();
 // A gate-clear failure occurs after OFF; retry must not repeat that group.
 setup_panel(7);ready();assert(!nt36532_disable(&panel_ctx.panel));encoder_off();fail_gate_call=3;assert(nt36532_unprepare(&panel_ctx.panel)==-EIO);assert(!panel_ctx.off_deferred&&off_calls==1&&!rail_calls&&phy_live);fail_gate_call=0;finish();
 // Merely querying the old path never turns off a host or changes registers.
 setup_panel(5);ready();int saved=count;assert(msm_dsi_manager_panel_stage(0,MIPI_DSI_PANEL_DEFER_OFF)==0&&saved==count);assert(!nt36532_disable(&panel_ctx.panel));encoder_off();finish();
 // Unknown panel/host topology retains the normal DRM panel OFF phase.
 setup_panel(7);hosts[0].flags&=~MIPI_DSI_MODE_PANEL_STAGES;require_post_disable=false;ready();assert(!nt36532_disable(&panel_ctx.panel)&&off_calls==1&&!panel_ctx.off_deferred);encoder_off();finish();
 // Querying before command-pair setup fails without asserting a deferred OFF.
 setup_panel(7);assert(nt36532_disable(&panel_ctx.panel)==-EIO&&!panel_ctx.off_deferred&&!off_calls&&!rail_calls);
 printf("%d\n",tests);return 0;
}
'''
 with tempfile.TemporaryDirectory(prefix='liuqin-off-phase-') as tmp:
  src=Path(tmp)/'test.c';exe=Path(tmp)/'test';src.write_text(prefix+enum+'\n'+core+declarations+fn(header,'mipi_dsi_panel_stage')+'\n'+disable+'\n'+unprepare+'\n'+setup+fixture)
  subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-variable','-Wno-unused-parameter','-fsanitize=undefined','-fno-sanitize-recover=all',str(src),'-o',str(exe)],check=True)
  count=int(subprocess.check_output([str(exe)],text=True,timeout=20))
 r=dict(all_passed=True,cases=count,actual_panel_and_manager_C_executed=True,drm_bridge_phase_mapping_checked=True,all_eight_switch_combinations=True,no_video_stop_while_dpu_running=True,off_precedes_link_and_rails_shutdown=True,partial_off_not_replayed_on_retry=True,failed_stop_and_gate_clear_preserve_rails=True,unknown_topology_and_disabled_switch_preserve_old_path=True,hardware_validation_required=True)
 a.report.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':main()
