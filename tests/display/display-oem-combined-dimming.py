#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise combined OEM dimming with fault inhibition and independent feature switches."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile

def function(s,n):
 m=re.search(r'^(?:static )?(?:(?:int|void|bool|unsigned int|ssize_t)\s+|struct mipi_dsi_device \*)'+n+r'\([^;{]*\)\s*\{',s,re.M);assert m,n
 i=m.end();d=1
 while d:d+=(s[i]=='{')-(s[i]=='}');i+=1
 return s[m.start():i]

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--rom-proof',type=Path,required=True);ap.add_argument('--cc',default='cc');ap.add_argument('--report',type=Path,required=True);a=ap.parse_args();a.report.unlink(missing_ok=True)
 name='drivers/gpu/drm/panel/panel-novatek-nt36532.c';s=(a.source/name).read_text();old=(a.baseline/name).read_text();core=(a.baseline/'drivers/gpu/drm/drm_panel.c').read_text()
 for n in ['liuqin_reset','nt36532_enable_supplies','nt36532_disable_supplies','liuqin_send_oem_cmds','nt36532_enable','liuqin_select_oem_panel']:
  assert function(s,n)==function(old,n),n
 for n in ['liuqin_tm_on','liuqin_csot_on','liuqin_oem_off']:
  pat=r'static const struct liuqin_oem_cmd '+n+r'\[\] = \{.*?\n\};';assert re.search(pat,s,re.S).group()==re.search(pat,old,re.S).group(),n
 for n in ['nt36532_prepare','nt36532_disable','nt36532_unprepare']:
  f=function(s,n);assert 'liuqin_cancel_dimming(ctx, false);' in f
  assert f.replace('\tliuqin_cancel_dimming(ctx, false);\n\n','').replace('\tliuqin_cancel_dimming(ctx, false);\n','').replace('\tliuqin_esd_stop(ctx, false);\n\n','').replace('\tliuqin_esd_stop(ctx, false);\n','')==function(old,n),n
 assert function(s,'nt36532_remove').index('liuqin_cancel_dimming(ctx, true)')<function(s,'nt36532_remove').index('drm_panel_remove')
 probe=function(s,'nt36532_probe');assert probe.index('INIT_DELAYED_WORK')<probe.index('drm_panel_add');assert 'liuqin_cancel_dimming(ctx, true);\n\t\t\tliuqin_esd_shutdown(ctx);\n\t\t\tdrm_panel_remove' in probe
 assert function(s,'nt36532_post_backlight_enable').replace('\tliuqin_schedule_dimming(ctx);\n','').replace('\tliuqin_esd_start(ctx);\n','').replace('\tif (!oem_normal_display)\n\t\tgoto ready;\n','').replace('ready:\n','').replace('if (!ctx->oem_panel)','if (!oem_normal_display || !ctx->oem_panel)')==function(old,'nt36532_post_backlight_enable')
 assert '.dimming_delay_ms = 0,' in s and '.dimming_delay_ms = 300,' in s
 table=re.search(r'static const struct liuqin_oem_cmd liuqin_oem_dimming_on\[\] = \{.*?\n\};',s,re.S).group()
 packets=[]
 for kind,flags,wait,length,payload in re.findall(r'\{ (0x[0-9a-f]+), (0x[0-9a-f]+|0), (\d+), (\d+), \{ ([^}]+) \} \}',table):
  values=bytes(int(x.strip(),0) for x in payload.split(','));assert len(values)==int(length);packets.append({'type':int(kind,0),'last':0,'channel':0,'flags':int(flags,0),'wait_ms':int(wait),'payload':values.hex()})
 proof=json.loads(a.rom_proof.read_text());assert len(packets)==4
 for item in proof['panels']:assert item['packets']==packets
 assert [x['effective_delay_ms'] for x in proof['panels']]==[0,300]
 settings=dict((name,int(delay)) for name,delay in re.findall(r'\.boot_name = "([^"]+)",.*?\.dimming_delay_ms = ([0-9]+),',s,re.S))
 for item in proof['panels']:assert settings['qcom,mdss_dsi_'+item['variant']+'_dualdsi_dsc_lcd_video']==item['effective_delay_ms']
 prefix=r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <sys/types.h>
typedef uint8_t u8;
#define READ_ONCE(v) (v)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define GFP_KERNEL 0
#define MIPI_DSI_DCS_LONG_WRITE 0x39
#define MIPI_DSI_MSG_USE_LPM 2
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
struct mutex {pthread_mutex_t m;};
static _Thread_local struct mutex *held;
static _Thread_local bool disable_actor;
static pthread_barrier_t disable_attempt;
static void mutex_lock(struct mutex *m){assert(!held);if(disable_actor){assert(pthread_mutex_trylock(&m->m)==EBUSY);pthread_barrier_wait(&disable_attempt);disable_actor=false;}assert(!pthread_mutex_lock(&m->m));held=m;}
static void mutex_unlock(struct mutex *m){assert(held==m);held=NULL;assert(!pthread_mutex_unlock(&m->m));}
#define lockdep_assert_held(m) assert(held==(m))
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;atomic_bool queued;bool started;pthread_t thread;void (*fn)(struct work_struct*);};
#define to_delayed_work(w) container_of(w,struct delayed_work,work)
static bool immediate_thread;
static unsigned long queued_delay;static int queue_calls,cancel_calls;
static int system_wq;
static void *work_thread(void *p){struct delayed_work *d=p;atomic_store(&d->queued,false);d->fn(&d->work);return NULL;}
static void join_work(struct delayed_work *d){if(d->started){assert(!pthread_join(d->thread,NULL));d->started=false;}}
static unsigned long msecs_to_jiffies(unsigned int n){return n;}
static bool mod_delayed_work(int w,struct delayed_work *d,unsigned long delay){(void)w;assert(held && !d->started);queued_delay=delay;queue_calls++;atomic_store(&d->queued,true);if(immediate_thread){assert(!delay);assert(!pthread_create(&d->thread,NULL,work_thread,d));d->started=true;}return true;}
static bool cancel_delayed_work_sync(struct delayed_work *d){assert(!held);cancel_calls++;atomic_store(&d->queued,false);join_work(d);return true;}
struct device {int unused;};
struct backlight_device {int brightness;bool on;};
struct drm_panel;
struct drm_panel_funcs {int (*enable)(struct drm_panel*);int (*disable)(struct drm_panel*);int (*post_backlight_enable)(struct drm_panel*);};
struct drm_panel {struct device *dev;struct mutex backlight_lock;bool enabled,backlight_pending,backlight_after_first_frame;unsigned int backlight_cookie;struct backlight_device *backlight;const struct drm_panel_funcs *funcs;};
struct mipi_dsi_msg {u8 channel,type;unsigned short flags;const void *tx_buf;size_t tx_len;};
struct mipi_dsi_host;
struct mipi_dsi_host_ops {ssize_t (*transfer)(struct mipi_dsi_host*,const struct mipi_dsi_msg*);ssize_t (*transfer_batch)(struct mipi_dsi_host*,const struct mipi_dsi_msg*,size_t);};
struct mipi_dsi_host {const struct mipi_dsi_host_ops *ops;};
struct mipi_dsi_device {struct mipi_dsi_host *host;u8 channel;};
struct liuqin_oem_cmd {u8 type,flags,wait_ms,len;u8 data[17];};
struct liuqin_oem_panel {unsigned int dimming_delay_ms;};
struct nt36532 {struct drm_panel panel;struct mipi_dsi_device *dsi[2];const struct liuqin_oem_panel *oem_panel;struct delayed_work dimming_work;unsigned int dimming_cookie;bool dimming_pending,dimming_stopping,esd_pending;};
static void liuqin_esd_start(struct nt36532 *ctx){assert(held==&ctx->panel.backlight_lock);}
static struct nt36532 *to_nt36532(struct drm_panel *p){return container_of(p,struct nt36532,panel);}
static bool oem_batch_commands=true,oem_normal_display=true,oem_delayed_dimming=true;
static int allocation_fail,transfers,fail_at=-1,short_at=-1,success_logs,error_logs,cases;
static bool hold_transfer;static pthread_barrier_t entered_transfer,release_transfer;
static struct {int n;u8 type[2],data[2][17],channel[2];} trace[24];
static int backlight_get_brightness(struct backlight_device *bl){return bl->on?bl->brightness:0;}
static int backlight_enable(struct backlight_device *bl){if(bl)bl->on=true;return 0;}
static int backlight_disable(struct backlight_device *bl){if(bl)bl->on=false;return 0;}
static void log_info(void *d,const char *fmt,...) __attribute__((format(printf,2,3)));
static void log_info(void *d,const char *fmt,...){(void)d;if(strstr(fmt,"delayed dimming enabled"))success_logs++;}
static void log_error(void *d,const char *fmt,...) __attribute__((format(printf,2,3)));
static void log_error(void *d,const char *fmt,...){(void)d;(void)fmt;error_logs++;}
#define dev_info log_info
#define dev_warn log_error
#define dev_err log_error
#define dev_err_ratelimited log_error
#define DRM_DEV_INFO log_error
#define EXPORT_SYMBOL(...)
static void *kcalloc(size_t n,size_t z,int flags){(void)flags;return allocation_fail?NULL:calloc(n,z);}
#define kfree free
static void usleep_range(unsigned long a,unsigned long b){(void)a;(void)b;abort();}
static ssize_t tx(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m,size_t n){
 (void)h;assert(held && n && n<=2 && transfers<24);int i=transfers++;trace[i].n=n;size_t total=0;
 for(size_t j=0;j<n;j++){assert(m[j].tx_len==2 && m[j].flags==2);trace[i].type[j]=m[j].type;trace[i].channel[j]=m[j].channel;memcpy(trace[i].data[j],m[j].tx_buf,2);total+=m[j].tx_len;}
 if(hold_transfer && i==0){pthread_barrier_wait(&entered_transfer);pthread_barrier_wait(&release_transfer);}
 return i==fail_at?-ETIMEDOUT:(ssize_t)total-(i==short_at);
}
static ssize_t single(struct mipi_dsi_host *h,const struct mipi_dsi_msg *m){return tx(h,m,1);}
static const struct liuqin_oem_cmd liuqin_oem_normal_display[]={{0x15,0,0,2,{0xff,0x10}},{0x39,0,0,2,{0x13,0}}};
'''
 bodies='\n'.join(function(s,n) for n in ['nt36532_cmd_dsi','liuqin_send_oem_cmds','liuqin_send_dimming_on','liuqin_dimming_work','liuqin_schedule_dimming','liuqin_cancel_dimming','nt36532_post_backlight_enable'])
 corebody='\n'.join(function(core,n) for n in ['drm_panel_enable_backlight','drm_panel_enable','drm_panel_complete_first_frame','drm_panel_disable'])
 suffix=r'''
static struct nt36532 c;static struct backlight_device bl;static struct liuqin_oem_panel variant;
static struct mipi_dsi_host_ops ops;static struct mipi_dsi_host host;static struct mipi_dsi_device d;
static int enabled(struct drm_panel *p){(void)p;return 0;}
static int disabled(struct drm_panel *p){liuqin_cancel_dimming(to_nt36532(p),false);return 0;}
static const struct drm_panel_funcs funcs={enabled,disabled,nt36532_post_backlight_enable};
static void reset(void){memset(&c,0,sizeof(c));memset(trace,0,sizeof(trace));assert(!pthread_mutex_init(&c.panel.backlight_lock.m,NULL));
 bl=(struct backlight_device){1500,true};variant=(struct liuqin_oem_panel){0};ops=(struct mipi_dsi_host_ops){single,tx};host=(struct mipi_dsi_host){&ops};d=(struct mipi_dsi_device){&host,0};
 c.panel.backlight=&bl;c.panel.enabled=true;c.panel.backlight_after_first_frame=true;c.panel.backlight_cookie=1;c.panel.funcs=&funcs;c.dsi[0]=c.dsi[1]=&d;c.oem_panel=&variant;c.dimming_work.fn=liuqin_dimming_work;atomic_init(&c.dimming_work.queued,false);
 transfers=success_logs=error_logs=allocation_fail=queue_calls=cancel_calls=0;fail_at=short_at=-1;immediate_thread=hold_transfer=false;oem_batch_commands=oem_normal_display=oem_delayed_dimming=true;cases++;}
static void finish(void){liuqin_cancel_dimming(&c,true);assert(!pthread_mutex_destroy(&c.panel.backlight_lock.m));}
static int send(void){mutex_lock(&c.panel.backlight_lock);int r=liuqin_send_dimming_on(&c);mutex_unlock(&c.panel.backlight_lock);return r;}
static void schedule(void){mutex_lock(&c.panel.backlight_lock);liuqin_schedule_dimming(&c);mutex_unlock(&c.panel.backlight_lock);}
static void run_work(void){atomic_store(&c.dimming_work.queued,false);liuqin_dimming_work(&c.dimming_work.work);}
static void check_packets(bool batch){int n=0;const u8 payload[4][2]={{255,39},{209,40},{255,16},{83,44}};for(int i=0;i<transfers;i++)for(int j=0;j<trace[i].n;j++){assert(trace[i].type[j]==(n<2?57:21));assert(!memcmp(trace[i].data[j],payload[n++],2));}assert(n==4 && transfers==(batch?3:4));}
static atomic_bool disable_done;
static void *disable_thread(void *arg){disable_actor=true;drm_panel_disable(arg);atomic_store(&disable_done,true);return NULL;}
int main(void){
 for(int choice=0;choice<3;choice++){reset();if(choice==1)ops.transfer_batch=NULL;if(choice==2)oem_batch_commands=false;assert(!send());check_packets(!choice);finish();}
 for(int fallback=0;fallback<2;fallback++)for(int short_ret=0;short_ret<2;short_ret++)for(int i=0;i<(fallback?4:3);i++){reset();if(fallback)ops.transfer_batch=NULL;if(short_ret)short_at=i;else fail_at=i;assert(send()==(short_ret?-EIO:-ETIMEDOUT));assert(transfers==i+1);finish();}
 reset();allocation_fail=1;assert(send()==-ENOMEM && !transfers);finish();
 reset();host.ops=NULL;assert(send()==-EOPNOTSUPP && !transfers);finish();
 reset();ops.transfer=NULL;assert(send()==-EOPNOTSUPP && !transfers);finish();
 reset();c.dsi[1]=NULL;d.channel=1;assert(!send());for(int i=0;i<transfers;i++)for(int j=0;j<trace[i].n;j++)assert(trace[i].channel[j]==1);finish();
 for(int delay=0;delay<2;delay++){reset();variant.dimming_delay_ms=delay?300:0;schedule();assert(queued_delay==variant.dimming_delay_ms && queue_calls==1 && !transfers);run_work();assert(success_logs==1 && !error_logs && !c.dimming_pending);check_packets(true);run_work();assert(transfers==3);finish();}
 for(int bad=0;bad<5;bad++){reset();if(bad==0)oem_delayed_dimming=false;if(bad==1)c.oem_panel=NULL;if(bad==2)c.dimming_stopping=true;if(bad==3)c.panel.enabled=false;if(bad==4)c.panel.backlight_after_first_frame=false;schedule();assert(!queue_calls && !transfers);finish();}
 for(int bad=0;bad<9;bad++){reset();schedule();if(bad==0)c.dimming_pending=false;if(bad==1)c.dimming_stopping=true;if(bad==2)oem_delayed_dimming=false;if(bad==3)c.oem_panel=NULL;if(bad==4)c.panel.enabled=false;if(bad==5)c.panel.backlight_pending=true;if(bad==6)c.panel.backlight_cookie++;if(bad==7)c.panel.backlight=NULL;if(bad==8)c.esd_pending=true;run_work();assert(!transfers && !c.dimming_pending);finish();}
 reset();schedule();bl.on=false;run_work();assert(!transfers);finish();
 reset();schedule();bl.brightness=0;run_work();assert(!transfers);finish();
 reset();schedule();fail_at=1;run_work();assert(error_logs==1 && !success_logs && transfers==2);fail_at=-1;run_work();assert(transfers==2);finish();
 reset();schedule();liuqin_cancel_dimming(&c,false);run_work();assert(!transfers && !atomic_load(&c.dimming_work.queued));c.panel.backlight_cookie=2;schedule();run_work();assert(success_logs==1 && c.dimming_cookie==2);finish();
 reset();schedule();liuqin_cancel_dimming(&c,true);schedule();assert(queue_calls==1);run_work();assert(!transfers);finish();
 for(int failure=0;failure<2;failure++){reset();c.panel.enabled=false;fail_at=failure;drm_panel_enable(&c.panel);assert(c.panel.backlight_pending);drm_panel_complete_first_frame(&c.panel,c.panel.backlight_cookie);assert(transfers==failure+1 && !queue_calls && c.panel.backlight_pending);finish();}
 reset();c.panel.enabled=false;immediate_thread=true;drm_panel_enable(&c.panel);assert(c.panel.backlight_pending && !queue_calls);unsigned int cookie=c.panel.backlight_cookie;drm_panel_complete_first_frame(&c.panel,cookie);join_work(&c.dimming_work);assert(success_logs==1 && transfers==5 && !c.panel.backlight_pending);drm_panel_disable(&c.panel);assert(!c.panel.enabled && !bl.on);finish();
 reset();c.panel.enabled=false;immediate_thread=true;oem_normal_display=false;drm_panel_enable(&c.panel);drm_panel_complete_first_frame(&c.panel,c.panel.backlight_cookie);join_work(&c.dimming_work);assert(queue_calls==1 && transfers==3 && success_logs==1);finish();
 reset();schedule();hold_transfer=true;assert(!pthread_barrier_init(&entered_transfer,NULL,2));assert(!pthread_barrier_init(&release_transfer,NULL,2));assert(!pthread_create(&c.dimming_work.thread,NULL,work_thread,&c.dimming_work));c.dimming_work.started=true;pthread_barrier_wait(&entered_transfer);atomic_init(&disable_done,false);assert(!pthread_barrier_init(&disable_attempt,NULL,2));pthread_t t;assert(!pthread_create(&t,NULL,disable_thread,&c.panel));pthread_barrier_wait(&disable_attempt);assert(!atomic_load(&disable_done));pthread_barrier_wait(&release_transfer);assert(!pthread_join(t,NULL));assert(atomic_load(&disable_done) && !c.panel.enabled && !bl.on && !c.dimming_work.started && transfers==3);pthread_barrier_destroy(&disable_attempt);pthread_barrier_destroy(&entered_transfer);pthread_barrier_destroy(&release_transfer);finish();
 printf("%d\n",cases);return 0;
}
'''
 with tempfile.TemporaryDirectory(prefix='liuqin-dimming-tests-') as tmp:
  src=Path(tmp)/'test.c';exe=Path(tmp)/'test';src.write_text(prefix+table+'\n'+bodies+'\n'+corebody+'\n'+suffix)
  subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-pthread','-fsanitize=undefined','-fno-sanitize-recover=all',str(src),'-o',str(exe)],check=True)
  count=int(subprocess.check_output([str(exe)],text=True,timeout=20))
 report={'cases':count,'all_passed':True,'actual_candidate_c_executed':True,'actual_drm_core_enable_complete_disable_executed':True,'rom_packet_types_groups_and_delays_verified':True,'zero_delay_worker_waits_for_first_frame_hook_unlock':True,'work_disable_race_checked':True,'normal_hook_failures_leave_work_unqueued':True,'cancellation_before_poweroff_and_remove_checked':True,'partial_transfers_stop_without_replay':True,'stale_generations_dark_backlight_and_unknown_panels_skipped':True,'supply_reset_on_off_and_host_transport_unchanged':True,'pending_panel_fault_inhibits_dimming':True,'normal_display_switch_does_not_disable_dimming':True,'hardware_validation_required':True};a.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
if __name__=='__main__':main()
