#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Test the OEM normal-display packets and DRM post-backlight ordering."""
from pathlib import Path
import argparse,ast,json,re,subprocess,tempfile

def function(text,name):
 m=re.search(r'^(?:static )?(?:int|void|bool|unsigned int|ssize_t)\s+'+name+r'\([^;{]*\)\s*\{',text,re.M);assert m,name
 n=m.end();d=1
 while d:d+=(text[n]=='{')-(text[n]=='}');n+=1
 return text[m.start():n]

def main():
 pa=argparse.ArgumentParser(description=__doc__);pa.add_argument('--source',type=Path,required=True);pa.add_argument('--baseline',type=Path,required=True);pa.add_argument('--rom-tables',type=Path,required=True);pa.add_argument('--cc',default='cc');pa.add_argument('--report',type=Path,required=True);a=pa.parse_args()
 nt=(a.source/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text();old=(a.baseline/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text();core=(a.source/'drivers/gpu/drm/drm_panel.c').read_text();oldcore=(a.baseline/'drivers/gpu/drm/drm_panel.c').read_text()
 for name in ['nt36532_prepare','nt36532_enable','nt36532_disable','nt36532_unprepare','nt36532_probe','liuqin_reset','nt36532_enable_supplies','nt36532_disable_supplies','liuqin_send_oem_cmds']:
  assert function(nt,name)==function(old,name),name
 for table in ['liuqin_tm_on','liuqin_csot_on','liuqin_oem_off']:
  pattern=r'static const struct liuqin_oem_cmd '+table+r'\[\] = \{.*?\n\};'
  assert re.search(pattern,nt,re.S).group()==re.search(pattern,old,re.S).group()
 for name in ['drm_panel_prepare','drm_panel_unprepare','drm_panel_disable','drm_panel_get_first_frame_cookie']:
  assert function(core,name)==function(oldcore,name)
 for name in ['drm_panel_enable','drm_panel_complete_first_frame']:
  assert function(core,name).replace('ret = drm_panel_enable_backlight(panel);','ret = backlight_enable(panel->backlight);')==function(oldcore,name)
 # Reuse the existing 17 actual-core race/error cases with an absent new hook.
 tree=ast.parse((Path(__file__).parent/'display-first-frame.py').read_text());mainfn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='main');strings={}
 for n in mainfn.body:
  if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ['stubs','cases']:strings[n.targets[0].id]=ast.literal_eval(n.value)
 stubs=strings['stubs'].replace('int (*disable)(struct drm_panel *);};','int (*disable)(struct drm_panel *);int (*post_backlight_enable)(struct drm_panel *);};').replace('funcs={driver_enable,driver_disable};','funcs={driver_enable,driver_disable,NULL};')
 helpers=function(core,'drm_panel_enable_backlight')+'\n'+'\n'.join(function(core,n) for n in ['drm_panel_enable','drm_panel_disable','drm_panel_get_first_frame_cookie','drm_panel_complete_first_frame'])
 bridge=(a.baseline/'drivers/gpu/drm/bridge/panel.c').read_text();kms=(a.baseline/'drivers/gpu/drm/msm/disp/dpu1/dpu_kms.c').read_text()
 helpers+='\n'+'\n'.join(function(bridge,n) for n in ['drm_bridge_is_panel','drm_panel_bridge_get_first_frame_cookie','drm_panel_bridge_complete_first_frame'])+'\n'+function(kms,'dpu_kms_complete_panel_first_frame')
 hook=r'''
static int post_calls,post_result;
static bool hold_post;
static int driver_post(struct drm_panel *panel){
 assert(panel->enabled && panel->backlight && panel->backlight->on);
 if(hold_post){barrier(&inside_update);barrier(&finish_update);}
 post_calls++;return post_result;
}
static const struct drm_panel_funcs post_funcs={driver_enable,driver_disable,driver_post};
'''
 extra=r'''
    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;hold_post=false;
    drm_panel_enable(&p);assert(!post_calls && !bl.on);dpu_kms_complete_panel_first_frame(&encoder);
    assert(post_calls==1 && bl.on && !p.backlight_pending);dpu_kms_complete_panel_first_frame(&encoder);assert(post_calls==1);finish();

    setup(false);p.funcs=&post_funcs;post_calls=post_result=0;drm_panel_enable(&p);
    assert(post_calls==1 && bl.on);drm_panel_disable(&p);assert(!bl.on);finish();

    setup(true);p.funcs=&post_funcs;post_calls=0;post_result=-EIO;drm_panel_enable(&p);dpu_kms_complete_panel_first_frame(&encoder);
    assert(post_calls==1 && bl.on && p.backlight_pending);post_result=0;dpu_kms_complete_panel_first_frame(&encoder);
    assert(post_calls==2 && !p.backlight_pending);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;bl.error=-EIO;drm_panel_enable(&p);dpu_kms_complete_panel_first_frame(&encoder);
    assert(!post_calls && !bl.on && p.backlight_pending);bl.error=0;dpu_kms_complete_panel_first_frame(&encoder);assert(post_calls==1 && bl.on);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);drm_panel_disable(&p);drm_panel_complete_first_frame(&p,first);
    assert(!post_calls && !bl.on);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);drm_panel_disable(&p);drm_panel_enable(&p);
    drm_panel_complete_first_frame(&p,first);assert(!post_calls);dpu_kms_complete_panel_first_frame(&encoder);assert(post_calls==1);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;p.backlight=NULL;drm_panel_enable(&p);assert(!post_calls);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;enable_result=-EIO;drm_panel_enable(&p);assert(!post_calls && !bl.on);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;drm_panel_enable(&p);tx_result=-ETIMEDOUT;dpu_kms_complete_panel_first_frame(&encoder);assert(!post_calls && !bl.on);finish();

    setup(true);p.funcs=&post_funcs;post_calls=post_result=0;drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);hold_post=true;atomic_store(&disable_started,false);
    assert(!pthread_barrier_init(&inside_update,NULL,2));assert(!pthread_barrier_init(&finish_update,NULL,2));
    assert(!pthread_create(&a,NULL,complete_thread,&first));barrier(&inside_update);assert(!pthread_create(&b,NULL,disable_thread,NULL));
    while(!atomic_load(&disable_started))sched_yield();barrier(&finish_update);assert(!pthread_join(a,NULL));assert(!pthread_join(b,NULL));
    assert(post_calls==1 && !bl.on && !p.enabled && !p.backlight_pending);hold_post=false;
    assert(!pthread_barrier_destroy(&inside_update));assert(!pthread_barrier_destroy(&finish_update));finish();
'''
 cases=strings['cases'].replace('    printf("%d\\n",cases);return 0;',extra+'    printf("%d\\n",cases);return 0;')
 # Packet-level helper is compiled unchanged and checked against both ROM tables.
 table=re.search(r'static const struct liuqin_oem_cmd liuqin_oem_normal_display\[\] = \{.*?\n\};',nt,re.S).group()
 packetprefix=r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <sys/types.h>
typedef uint8_t u8;
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define MIPI_DSI_MSG_USE_LPM 2
#define dev_info(...) ((void)0)
struct drm_panel {void *dev;};
struct mipi_dsi_msg {u8 channel,type;unsigned short flags;size_t tx_len;const void *tx_buf;};
struct mipi_dsi_host;
struct mipi_dsi_host_ops {ssize_t (*transfer)(struct mipi_dsi_host*,const struct mipi_dsi_msg*);};
struct mipi_dsi_host {const struct mipi_dsi_host_ops *ops;};
struct mipi_dsi_device {struct mipi_dsi_host *host;u8 channel;};
struct nt36532 {struct drm_panel panel;const void *oem_panel;struct mipi_dsi_device *dsi;};
struct liuqin_oem_cmd {u8 type,flags,wait_ms,len,data[17];};
static bool oem_normal_display=true;
static int call_index,fail_index=-1,fail_ret;
static struct nt36532 *to_nt36532(struct drm_panel *p){return (struct nt36532*)p;}
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532 *c){return c->dsi;}
static ssize_t transfer(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){
 (void)h;printf("T %u %u %u ",m->type,m->flags,m->channel);
 for(size_t i=0;i<m->tx_len;i++)printf("%02x",((u8*)m->tx_buf)[i]);puts("");
 if(call_index++==fail_index)return fail_ret;
 return m->type==0x15?4:8;
}
'''
 packetmain=r'''
int main(int argc,char **argv){
 if(argc!=2)return 2;int x=atoi(argv[1]);struct mipi_dsi_host_ops ops={transfer};struct mipi_dsi_host h={&ops};struct mipi_dsi_device d={&h,0};struct nt36532 c={.oem_panel=&ops,.dsi=&d};
 if(x==1)oem_normal_display=false;if(x==2)c.oem_panel=NULL;if(x==3)h.ops=NULL;if(x==4)ops.transfer=NULL;
 if(x>=5 && x<=8){fail_index=(x-5)%2;fail_ret=x>=7?1:-110;}
 printf("R %d\n",nt36532_post_backlight_enable(&c.panel));return 0;
}
'''
 with tempfile.TemporaryDirectory(prefix='oem-normal-display-') as tmp:
  flags=['-std=c11','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-fsanitize=undefined','-fno-sanitize-recover=all']
  c=Path(tmp)/'core.c';exe=Path(tmp)/'core';c.write_text(stubs+hook+helpers+cases)
  subprocess.run([a.cc,*flags,'-pthread',str(c),'-o',str(exe)],check=True)
  core_count=int(subprocess.check_output([str(exe)],text=True,timeout=20));assert core_count==27,core_count
  c=Path(tmp)/'packets.c';exe=Path(tmp)/'packets';c.write_text(packetprefix+table+function(nt,'nt36532_post_backlight_enable')+packetmain)
  subprocess.run([a.cc,*flags,str(c),'-o',str(exe)],check=True)
  calls=['T 21 2 0 ff10','T 57 2 0 1300']
  expected=[calls+['R 0'],['R 0'],['R 0'],['R -95'],['R -95'],calls[:1]+['R -110'],calls+['R -110'],calls[:1]+['R -5'],calls+['R -5']]
  for i,want in enumerate(expected):
   r=subprocess.run([str(exe),str(i)],capture_output=True,text=True,check=True);assert not r.stderr and r.stdout.splitlines()==want,(i,r.stdout,r.stderr)
  tables=json.loads(a.rom_tables.read_text());assert len(tables)==2
  for t in tables:assert t['raw_hex']=='15000000000002ff10390000000000021300'
 result={'cases':core_count+len(expected),'core_cases':core_count,'packet_cases':len(expected),'all_passed':True,'actual_candidate_c_executed':True,'post_commands_follow_backlight_enable':True,'deferred_hook_failure_retried_without_stale_generation':True,'concurrent_disable_serialized':True,'both_rom_packet_types_payloads_matched':True,'legacy_without_hook_17_cases_passed':True,'power_on_off_tables_reset_and_batch_transport_unchanged':True,'hardware_validation_required':True}
 a.report.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
