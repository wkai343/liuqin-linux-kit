#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute kernel TDDI recovery with calibration, transfer and cancellation faults."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile

def function(text, name):
    match = re.search(r'^(?:static )?(?:int32_t|int|void|bool)\s+' + name +
                      r'\([^;{]*\)\s*\{', text, re.M)
    if not match:
        raise ValueError('Missing function: ' + name)
    end, depth = match.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]

parser=argparse.ArgumentParser(description="Execute actual shared-TDDI recovery C with calibration, transfer and cancellation faults")
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--report',type=Path)
args=parser.parse_args()
fn=function
s = r"""
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#define NVT_TOUCH_ESD_PROTECT 0
#define READ_ONCE(x) (x)
#define likely(x) (x)
#define unlikely(x) (x)
#define NVT_LOG(...) ((void)0)
#define NVT_ERR(...) ((void)0)
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
typedef uint8_t u8;
typedef enum {RESET_STATE_INIT=0xa0,RESET_STATE_REK,
 RESET_STATE_REK_FINISH,RESET_STATE_NORMAL_RUN,RESET_STATE_MAX=0xaf} RST_COMPLETE_STATE;
enum {NVT_IC_SUSPEND_OUT,NVT_IC_RESUME_IN,NVT_IC_RESUME_OUT};
#define EVENT_MAP_RESET_COMPLETE 0x60
struct mutex {pthread_mutex_t value;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->value));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->value));}
struct device {void *of_node;};
struct client {struct device dev;};
struct work_struct {int unused;};
struct drm_panel {struct device *dev;struct mutex backlight_lock;bool enabled,backlight_pending;unsigned int backlight_cookie;};
struct drm_panel_follower {struct drm_panel *panel;};
struct memory_map {unsigned int EVENT_BUF_ADDR;};
struct nvt_ts_data {
 struct client *client;struct memory_map *mmap;struct drm_panel_follower panel_follower;
 struct mutex lock;struct work_struct resume_work;bool is_panel_follower,touch_awake;
 bool dev_pm_suspend,irq_requested,panel_on,resources_ready,stopping;int lifecycle_lock;
 unsigned int resume_count;int ic_state,gesture_command_delayed,db_wakeup;
 void *event_wq;
};
static struct client client;
static struct memory_map memory_map={.EVENT_BUF_ADDR=0x28000};
static struct drm_panel panel;
static struct nvt_ts_data data,*ts=&data;
static bool liuqin,irq_on,cancel_on_poll,display_never_ready,blank_in_download;
static bool commit_fail,concurrent,async_cancel;
static int page_error,read_error,firmware_error,pen_error;
static unsigned int reads,ready_at,display_sleeps,firmware_calls,pen_calls;
static unsigned int irq_enables,irq_disables,cancel_calls,suspend_calls;
static atomic_bool firmware_entered,dimmer_attempted;
static atomic_bool cancellation_waiting;
static pthread_t resume_thread;
static unsigned int dimmer_calls;
static bool of_device_is_compatible(void *node,const char *s){
 assert(node==&memory_map);assert(!strcmp(s,"xiaomi,liuqin-nt36532"));return liuqin;
}
static bool nvt_panel_is_on(struct nvt_ts_data *t){return t->panel_on&&!t->stopping;}
static bool nvt_panel_resume_allowed(struct nvt_ts_data *t){return t->is_panel_follower&&t->resources_ready&&nvt_panel_is_on(t)&&!t->touch_awake;}
static void nvt_set_touch_awake_state(struct nvt_ts_data *t,bool b){t->touch_awake=b;}
static bool nvt_commit_follower_awake(struct nvt_ts_data *t){
 if(commit_fail||!nvt_panel_is_on(t))return false;t->touch_awake=true;return true;
}
static void pm_stay_awake(struct device *d){(void)d;}
static void pm_relax(struct device *d){(void)d;}
static void nvt_irq_enable(bool b){irq_on=b;if(b)irq_enables++;else irq_disables++;}
static int nvt_set_page(unsigned int p){(void)p;return page_error;}
static int CTP_SPI_READ(struct client *c,u8 *b,unsigned int n){
 (void)c;assert(n==6);reads++;if(read_error)return read_error;
 b[1]=reads>=ready_at?RESET_STATE_NORMAL_RUN:RESET_STATE_REK;return 0;
}
static void usleep_range(unsigned int a,unsigned int b){
 (void)a;(void)b;if(cancel_on_poll)ts->panel_on=false;
}
static void msleep(unsigned int ms){
 assert(ms==10);display_sleeps++;
 if(cancel_on_poll)ts->panel_on=false;
 if(!display_never_ready&&display_sleeps==3){panel.enabled=true;panel.backlight_pending=false;}
}
static int nvt_update_firmware(const char *s){(void)s;firmware_calls++;return firmware_error;}
static int nvt_update_firmware_no_eng_reset(const char *s){
 (void)s;firmware_calls++;
 if(EXPECT_FIXED&&liuqin){
  assert(panel.enabled&&!panel.backlight_pending);
  assert(pthread_mutex_trylock(&panel.backlight_lock.value)==EBUSY);
 }
 if(concurrent){
  atomic_store(&firmware_entered,true);
  while(!atomic_load(&dimmer_attempted))sched_yield();
 }
 if(async_cancel){
  atomic_store(&firmware_entered,true);
  while(!atomic_load(&cancellation_waiting))sched_yield();
 }
 if(blank_in_download)ts->panel_on=false;
 return firmware_error;
}
static int __nvt_set_pen_state(bool disable){(void)disable;pen_calls++;return pen_error;}
#define spin_lock_irqsave(lock,flags) do{(void)(lock);(flags)=0;}while(0)
#define spin_unlock_irqrestore(lock,flags) do{(void)(lock);(void)(flags);}while(0)
static void cancel_work_sync(struct work_struct *w){
 (void)w;assert(!ts->panel_on);cancel_calls++;
 if(async_cancel){atomic_store(&cancellation_waiting,true);assert(!pthread_join(resume_thread,NULL));}
}
static int nvt_ts_suspend(struct device *d){(void)d;suspend_calls++;ts->touch_awake=false;irq_on=false;return 0;}
"""
src=(args.source/'drivers/input/touchscreen/nt36523/nt36xxx.c').read_text()
core=(args.source/'drivers/gpu/drm/drm_panel.c').read_text()
s=s.replace('struct drm_panel {','struct drm_panel;\nstruct drm_panel_funcs {int (*recover_touch)(struct drm_panel *);void (*finish_touch_recovery)(struct drm_panel *,bool);};\nstruct drm_panel {const struct drm_panel_funcs *funcs;bool prepared;struct mutex follower_lock;')
s=s.replace('void *event_wq;','void *event_wq;const char *fw_name;unsigned int firmware_update_count;')
s=s.replace('static struct client client;','static bool liuqin_tddi_recovery;\n#define LIUQIN_TOUCH_FW_TM "tm.bin"\n#define EVENT_MAP_HOST_CMD 0x50\nstatic unsigned int recovery_calls,finish_calls,sleep_calls;static bool finish_ready,cancel_recovery,wait_recovery;static int read_mode,panel_error,second_fw_error;\nstatic atomic_bool recovery_entered,recovery_release;\nstatic struct client client;')
a=fn(s,'CTP_SPI_READ');b='''static int CTP_SPI_READ(struct client *c,u8 *b,unsigned int n){
 (void)c;assert(n==6);reads++;if(read_error)return read_error;
 if(read_mode==2)b[1]=reads%7==0?RESET_STATE_NORMAL_RUN:RESET_STATE_REK;
 else if(read_mode==1||read_mode==3)b[1]=firmware_calls>=2?RESET_STATE_NORMAL_RUN:(read_mode==3?RESET_STATE_INIT:RESET_STATE_REK);
 else b[1]=reads>=ready_at?RESET_STATE_NORMAL_RUN:RESET_STATE_REK;
 return 0;
}''';s=s.replace(a,b)
for name in ['nvt_update_firmware','nvt_update_firmware_no_eng_reset']:
 a=fn(s,name);s=s.replace(a,'''static int '''+name+'''(const char *s){(void)s;firmware_calls++;ts->firmware_update_count++;if(blank_in_download)ts->panel_on=false;return firmware_calls==2?second_fw_error:firmware_error;}''')
s+='''
static int CTP_SPI_WRITE(struct client *c,u8 *buf,unsigned int n){(void)c;assert(n==2&&buf[0]==0x50&&buf[1]==0x11);sleep_calls++;return 0;}
static int fake_recover(struct drm_panel *p){
 assert(pthread_mutex_trylock(&p->backlight_lock.value)==EBUSY);
 assert(!pthread_mutex_trylock(&data.lock.value));assert(!pthread_mutex_unlock(&data.lock.value));recovery_calls++;
 if(wait_recovery){atomic_store(&recovery_entered,true);while(!atomic_load(&recovery_release))sched_yield();}
 if(cancel_recovery)data.panel_on=false;return panel_error;
}
static void fake_finish(struct drm_panel *p,bool ready){
 assert(pthread_mutex_trylock(&p->backlight_lock.value)==EBUSY);
 assert(!pthread_mutex_trylock(&data.lock.value));assert(!pthread_mutex_unlock(&data.lock.value));finish_calls++;finish_ready=ready;
}
static const struct drm_panel_funcs ops={fake_recover,fake_finish};
'''
s+='\n'.join(fn(core,n) for n in ['drm_panel_recover_touch','drm_panel_finish_touch_recovery'])
s+='\n'.join(fn(src,n) for n in ['nvt_read_event_bytes','nvt_check_fw_reset_state','nvt_tddi_recovery_enabled','nvt_wait_tddi_normal','nvt_recover_tddi','nvt_quiesce_failed_tddi','nvt_ts_resume','nvt_resume_work'])
s += r"""static void base_setup(void){
 data=(struct nvt_ts_data){.client=&client,.mmap=&memory_map,.panel_follower={&panel},
  .is_panel_follower=true,.irq_requested=true,.panel_on=true,.resources_ready=true,.event_wq=&client,
  .gesture_command_delayed=-1};
 assert(!pthread_mutex_init(&data.lock.value,NULL));
 assert(!pthread_mutex_init(&panel.backlight_lock.value,NULL));
 panel.dev=&client.dev;client.dev.of_node=&memory_map;
 panel.enabled=true;panel.backlight_pending=false;panel.backlight_cookie=42;
 liuqin=true;irq_on=cancel_on_poll=display_never_ready=blank_in_download=false;
 commit_fail=concurrent=async_cancel=false;page_error=read_error=firmware_error=pen_error=0;
 reads=display_sleeps=firmware_calls=pen_calls=irq_enables=irq_disables=0;
 cancel_calls=suspend_calls=dimmer_calls=0;ready_at=3;
 atomic_store(&firmware_entered,false);atomic_store(&dimmer_attempted,false);
 atomic_store(&cancellation_waiting,false);
}static void base_finish(void){
 assert(!pthread_mutex_trylock(&data.lock.value));assert(!pthread_mutex_unlock(&data.lock.value));
 assert(!pthread_mutex_trylock(&panel.backlight_lock.value));assert(!pthread_mutex_unlock(&panel.backlight_lock.value));
 assert(!pthread_mutex_destroy(&data.lock.value));assert(!pthread_mutex_destroy(&panel.backlight_lock.value));
}"""
s += r"""
static void setup(void){base_setup();data.fw_name=LIUQIN_TOUCH_FW_TM;panel.funcs=&ops;panel.prepared=true;liuqin_tddi_recovery=true;recovery_calls=finish_calls=sleep_calls=0;finish_ready=cancel_recovery=wait_recovery=false;read_mode=panel_error=second_fw_error=0;assert(!pthread_mutex_init(&panel.follower_lock.value,NULL));atomic_store(&recovery_entered,false);atomic_store(&recovery_release,false);}
static void finish(void){base_finish();assert(!pthread_mutex_trylock(&panel.follower_lock.value));assert(!pthread_mutex_unlock(&panel.follower_lock.value));assert(!pthread_mutex_destroy(&panel.follower_lock.value));}
static int run(void){return nvt_ts_resume(&client.dev);}
static void *worker(void *unused){(void)unused;return (void *)(intptr_t)run();}
int main(void){unsigned int cases=0;
 setup();assert(!run()&&firmware_calls==1&&reads>=22&&irq_on&&data.touch_awake&&!recovery_calls&&!sleep_calls);finish();cases++;
 for(int mode=1;mode<=3;mode+=2){setup();read_mode=mode;assert(!run()&&firmware_calls==2&&recovery_calls==1&&finish_calls==1&&finish_ready&&irq_on&&data.touch_awake&&!sleep_calls&&reads>=170);finish();cases++;}
 setup();read_mode=2;assert(run()==-ETIMEDOUT&&firmware_calls==2&&recovery_calls==1&&finish_calls==1&&!finish_ready&&!irq_on&&!data.touch_awake&&sleep_calls==1&&reads==300);finish();cases++;
 setup();read_mode=1;panel_error=-EIO;assert(run()==-EIO&&firmware_calls==1&&recovery_calls==1&&finish_calls==1&&!finish_ready&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();read_mode=1;second_fw_error=-EBADMSG;assert(run()==-EBADMSG&&firmware_calls==2&&recovery_calls==1&&finish_calls==1&&!finish_ready&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();read_error=-EIO;assert(run()==-EIO&&firmware_calls==1&&!recovery_calls&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();page_error=-EIO;assert(run()==-EIO&&!reads&&!recovery_calls&&!sleep_calls&&!irq_on);finish();cases++;
 setup();firmware_error=-ENOENT;assert(run()==-ENOENT&&firmware_calls==1&&!recovery_calls&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();cancel_on_poll=true;assert(run()==-EHOSTDOWN&&firmware_calls==1&&!recovery_calls&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();blank_in_download=true;assert(run()==-EHOSTDOWN&&firmware_calls==1&&!recovery_calls&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();read_mode=1;cancel_recovery=true;assert(run()==-EHOSTDOWN&&firmware_calls==1&&recovery_calls==1&&finish_calls==1&&!finish_ready&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();read_mode=1;panel.backlight_pending=true;assert(run()==-EHOSTDOWN&&!recovery_calls&&finish_calls==1&&firmware_calls==1&&sleep_calls==1);finish();cases++;
 setup();read_mode=1;panel.enabled=false;assert(run()==-EHOSTDOWN&&!recovery_calls&&firmware_calls==1&&sleep_calls==1);finish();cases++;
 setup();read_mode=1;panel.prepared=false;assert(run()==-EHOSTDOWN&&!recovery_calls&&sleep_calls==1);finish();cases++;
 setup();read_mode=1;panel.funcs=NULL;assert(run()==-EOPNOTSUPP&&!recovery_calls&&!finish_calls&&firmware_calls==1&&sleep_calls==1);finish();cases++;
 setup();data.panel_on=false;assert(run()==-EHOSTDOWN&&!firmware_calls&&!recovery_calls&&!sleep_calls);finish();cases++;
 setup();data.stopping=true;assert(run()==-EHOSTDOWN&&!firmware_calls&&!sleep_calls);finish();cases++;
 setup();commit_fail=true;assert(run()==-EHOSTDOWN&&firmware_calls==1&&!recovery_calls&&sleep_calls==1&&!irq_on);finish();cases++;
 setup();pen_error=-EIO;assert(run()==-EIO&&firmware_calls==1&&!recovery_calls&&sleep_calls==1&&!irq_on);finish();cases++;
 for(int kind=0;kind<5;kind++){
  setup();ready_at=10000;if(kind==0)liuqin_tddi_recovery=false;if(kind==1)liuqin=false;if(kind==2)data.fw_name="csot.bin";if(kind==3)data.db_wakeup=1;if(kind==4)data.is_panel_follower=false;
  assert(!run()&&firmware_calls==1&&reads==1&&irq_on&&data.touch_awake&&!recovery_calls&&!sleep_calls);finish();cases++;
 }
 setup();data.touch_awake=true;assert(!run()&&!firmware_calls&&!recovery_calls);finish();cases++;
 setup();read_mode=1;wait_recovery=true;pthread_t w;assert(!pthread_create(&w,NULL,worker,NULL));while(!atomic_load(&recovery_entered))sched_yield();
 mutex_lock(&panel.follower_lock);data.panel_on=false;atomic_store(&recovery_release,true);void *result;assert(!pthread_join(w,&result));assert((intptr_t)result==-EHOSTDOWN);mutex_unlock(&panel.follower_lock);
 assert(recovery_calls==1&&finish_calls==1&&!finish_ready&&!irq_on&&!data.touch_awake&&sleep_calls==1&&firmware_calls==1);finish();cases++;
 assert(drm_panel_recover_touch(NULL)==-EOPNOTSUPP);drm_panel_finish_touch_recovery(NULL,false);cases++;
 printf("%u\n",cases);return 0;
}
"""
with tempfile.TemporaryDirectory() as d:
 q=Path(d);(q/'touch.c').write_text('#define EXPECT_FIXED 0\n'+s)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-function','-Wno-misleading-indentation','-fsanitize=undefined','-pthread',str(q/'touch.c'),'-o',str(q/'touch')],check=True)
 count=int(subprocess.check_output([str(q/'touch')],text=True,timeout=15))
r={'touch_and_drm_core_cases':count,'actual_candidate_c_executed':True,'single_retry_and_stable20_polls':True,'a1_transients_do_not_enable_irq':True,'input_lock_not_held_under_panel_lock':True,'follower_lock_cancel_join_no_deadlock':True,'baseline_and_otherboard_paths_unchanged':True}
panel_source=(args.source/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text()
ps = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>
#include <errno.h>
typedef uint8_t u8;
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define READ_ONCE(x) (x)
#define LIUQIN_ALL_VOTES 7
#define MIPI_DSI_MSG_USE_LPM 2
#define MIPI_DSI_DCS_LONG_WRITE 0x39
#define MIPI_DSI_DCS_SHORT_WRITE_PARAM 0x15
#define dev_info(...) ((void)0)
struct mutex {int held;};
struct drm_panel {struct mutex backlight_lock;bool enabled,backlight_pending;unsigned int backlight_cookie;void *dev;};
struct mipi_dsi_msg {unsigned int channel,type,flags;size_t tx_len;const void *tx_buf;};
struct mipi_dsi_host;
struct mipi_dsi_host_ops {int (*transfer)(struct mipi_dsi_host *,const struct mipi_dsi_msg *);};
struct mipi_dsi_host {const struct mipi_dsi_host_ops *ops;};
struct mipi_dsi_device {unsigned int channel;struct mipi_dsi_host *host;};
struct panel_desc {int id;};struct liuqin_oem_panel {int id;};
static const struct panel_desc liuqin_desc={1},other_desc={2};
static const struct liuqin_oem_panel liuqin_oem_panels[]={{1},{2}};
struct nt36532 {struct drm_panel panel;const struct panel_desc *desc;const struct liuqin_oem_panel *oem_panel;struct mipi_dsi_device *dsi[2];unsigned int supply_votes;bool dimming_stopping,off_deferred,esd_pending,esd_enabled,tddi_recovering,dimming_pending,cabc_ready;unsigned int tddi_recovery_cookie,esd_armed;struct mutex esd_lock;int esd_arm_work,esd_gpio[2];int requested;};
static struct nt36532 ctx;static struct mipi_dsi_device dsi0,dsi1;static struct mipi_dsi_host host0,host1;
static int transfers,waits_seen,mask_calls,cancel_calls,start_calls,dim_calls,cabc_calls,fault_calls,fail_at;static bool short_write,pending_race;static unsigned int waited[4];static int applied;
static void mutex_lock(struct mutex *m){assert(!m->held);if(m==&ctx.esd_lock&&pending_race){ctx.esd_pending=true;ctx.esd_enabled=true;pending_race=false;}m->held=1;}
static void mutex_unlock(struct mutex *m){assert(m->held==1);m->held=0;}
#define lockdep_assert_held(m) assert((m)->held==1)
static struct nt36532 *to_nt36532(struct drm_panel *p){assert(p==&ctx.panel);return &ctx;}
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532 *c){return c->dsi[1]?c->dsi[1]:c->dsi[0];}
static void liuqin_esd_mask_locked(struct nt36532 *c){assert(c->esd_lock.held);c->esd_armed=0;mask_calls++;}
static void cancel_delayed_work(int *p){assert(p==&ctx.esd_arm_work&&ctx.panel.backlight_lock.held);cancel_calls++;}
static void liuqin_esd_start(struct nt36532 *c){assert(c->panel.backlight_lock.held);c->esd_enabled=true;c->esd_pending=false;start_calls++;}
static bool liuqin_esd_fault(struct nt36532 *c,unsigned int line,bool injected){assert(c->esd_enabled&&injected&&line==2);assert(!c->esd_pending);c->esd_pending=true;fault_calls++;return true;}
static void liuqin_schedule_dimming(struct nt36532 *c){assert(c->cabc_ready&&!c->esd_pending&&c->esd_enabled);dim_calls++;}
static void liuqin_cabc_first_frame(struct nt36532 *c){assert(c->esd_enabled&&!c->esd_pending);c->cabc_ready=true;applied=c->requested;cabc_calls++;}
static void msleep(unsigned int t){assert(waits_seen<4);waited[waits_seen++]=t;}
static int transfer(struct mipi_dsi_host *host,const struct mipi_dsi_msg *m){
 const u8 commands[]={0xff,0x28,0x10,0x11,0x29};const u8 *v=m->tx_buf;int i=transfers++;
 assert(host==&host1&&m->channel==3&&m->flags==2&&m->tx_len==2);
 assert(i<5&&v[0]==commands[i]&&v[1]==(i?0:0x10));assert(m->type==(i?0x39:0x15));
 assert(!ctx.esd_enabled&&!ctx.esd_armed&&!ctx.dimming_pending&&!ctx.cabc_ready&&ctx.tddi_recovering);
 if(i==fail_at)return short_write?1:-ETIMEDOUT;return 2;
}
static const struct mipi_dsi_host_ops ops={transfer},no_ops={NULL};
static void setup(void){ctx=(struct nt36532){.panel={.backlight_lock={1},.enabled=true,.backlight_cookie=42},.desc=&liuqin_desc,.oem_panel=&liuqin_oem_panels[0],.supply_votes=7,.dimming_pending=true,.cabc_ready=true,.esd_enabled=true,.esd_armed=3,.requested=1};host0.ops=host1.ops=&ops;dsi0=(struct mipi_dsi_device){7,&host0};dsi1=(struct mipi_dsi_device){3,&host1};ctx.dsi[0]=&dsi0;ctx.dsi[1]=&dsi1;transfers=waits_seen=mask_calls=cancel_calls=start_calls=dim_calls=cabc_calls=fault_calls=0;applied=-1;fail_at=-1;short_write=pending_race=false;memset(waited,0,sizeof(waited));}
static void clean(void){assert(ctx.panel.backlight_lock.held==1&&!ctx.esd_lock.held);}
"""
ps+='\n'.join(fn(panel_source,n) for n in ['nt36532_recover_touch','nt36532_finish_touch_recovery'])
ps += r"""
int main(void){unsigned int count=0;
 setup();assert(!nt36532_recover_touch(&ctx.panel));assert(transfers==5&&waits_seen==4&&!memcmp(waited,(unsigned int[]){20,120,120,20},sizeof(waited)));assert(mask_calls==1&&cancel_calls==1&&ctx.tddi_recovering&&ctx.tddi_recovery_cookie==42&&!start_calls&&!fault_calls);ctx.requested=3;nt36532_finish_touch_recovery(&ctx.panel,true);assert(!ctx.tddi_recovering&&start_calls==1&&cabc_calls==1&&dim_calls==1&&applied==3&&!fault_calls);clean();count++;
 for(int type=0;type<7;type++){setup();if(type==0)ctx.desc=&other_desc;if(type==1)ctx.oem_panel=&liuqin_oem_panels[1];if(type==2)ctx.dsi[1]=NULL;if(type==3)ctx.dimming_stopping=true;if(type==4)ctx.off_deferred=true;if(type==5)ctx.supply_votes=3;if(type==6)host1.ops=&no_ops;assert(nt36532_recover_touch(&ctx.panel)<0&&!transfers&&!mask_calls&&ctx.esd_enabled&&!ctx.tddi_recovering);clean();count++;}
 setup();ctx.esd_pending=true;assert(nt36532_recover_touch(&ctx.panel)==-EBUSY&&ctx.esd_pending&&ctx.esd_enabled&&!mask_calls&&!transfers);nt36532_finish_touch_recovery(&ctx.panel,false);assert(ctx.esd_pending&&ctx.esd_enabled&&!start_calls&&!fault_calls);clean();count++;
 setup();pending_race=true;assert(nt36532_recover_touch(&ctx.panel)==-EBUSY&&ctx.esd_pending&&ctx.esd_enabled&&!mask_calls&&!transfers);clean();count++;
 setup();ctx.tddi_recovering=true;assert(nt36532_recover_touch(&ctx.panel)==-EBUSY&&!transfers&&ctx.esd_enabled);clean();count++;
 for(int i=0;i<5;i++)for(int partial=0;partial<2;partial++){setup();fail_at=i;short_write=partial;int ret=nt36532_recover_touch(&ctx.panel);assert(ret==(partial?-EIO:-ETIMEDOUT)&&transfers==i+1&&ctx.tddi_recovering&&!ctx.esd_enabled);nt36532_finish_touch_recovery(&ctx.panel,false);assert(!ctx.tddi_recovering&&ctx.esd_pending&&ctx.esd_enabled&&start_calls==1&&fault_calls==1&&!dim_calls&&!cabc_calls);clean();count++;}
 setup();assert(!nt36532_recover_touch(&ctx.panel));ctx.panel.enabled=false;nt36532_finish_touch_recovery(&ctx.panel,true);assert(!ctx.tddi_recovering&&!start_calls&&!fault_calls&&!dim_calls);clean();count++;
 setup();assert(!nt36532_recover_touch(&ctx.panel));ctx.panel.backlight_pending=true;nt36532_finish_touch_recovery(&ctx.panel,true);assert(!ctx.tddi_recovering&&!start_calls&&!dim_calls);clean();count++;
 setup();assert(!nt36532_recover_touch(&ctx.panel));ctx.panel.backlight_cookie=43;ctx.esd_enabled=true;nt36532_finish_touch_recovery(&ctx.panel,false);assert(!ctx.tddi_recovering&&ctx.esd_enabled&&!start_calls&&!fault_calls&&!dim_calls);clean();count++;
 setup();assert(!nt36532_recover_touch(&ctx.panel));ctx.dimming_stopping=true;nt36532_finish_touch_recovery(&ctx.panel,false);assert(!ctx.tddi_recovering&&!start_calls&&!fault_calls);clean();count++;
 setup();nt36532_finish_touch_recovery(&ctx.panel,true);assert(!start_calls&&!fault_calls&&!dim_calls);clean();count++;
 printf("%u\n",count);return 0;
}
"""
with tempfile.TemporaryDirectory() as d:
 q=Path(d);(q/'panel.c').write_text(ps)
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-function','-Wno-misleading-indentation','-fsanitize=undefined',str(q/'panel.c'),'-o',str(q/'panel')],check=True)
 panel_count=int(subprocess.check_output([str(q/'panel')],text=True,timeout=15))
r.update(panel_cases=panel_count,exact_ddic_packets_and_delays_verified=True,preexisting_and_racing_esd_fault_preserved=True,partial_transfer_no_replay=True,old_generation_finish_cannot_touch_new_panel=True,cabc_replay_after_firmware_ready=True)

if args.report:args.report.write_text(json.dumps(r,indent=2)+"\n")
print(json.dumps(r,indent=2))
