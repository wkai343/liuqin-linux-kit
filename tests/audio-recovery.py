#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise audio sleep, ADSP recovery and offline cleanup from actual kernel C."""
import argparse
from pathlib import Path
import subprocess
import tempfile

from kernel_test import function, run_c, write_report


def check_services(args):
    def read(path):
        return (args.source / path).read_text()
    apm = read('sound/soc/qcom/qdsp6/q6apm.c')
    prm = read('sound/soc/qcom/qdsp6/q6prm.c')
    apr = read('drivers/soc/qcom/apr.c')
    machine = read('sound/soc/qcom/sc8280xp.c')
    clocks = read('sound/soc/qcom/qdsp6/q6prm-clocks.c')
    dai = read('sound/soc/qcom/qdsp6/q6apm-dai.c')
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <errno.h>
#define EPROBE_DEFER 517
#define GFP_KERNEL 0
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define __free(x)
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define dev_err(...) ((void)0)
struct mutex { int held; };
static void mutex_init(struct mutex *m) { m->held=0; }
static void mutex_lock(struct mutex *m) { assert(!m->held);m->held=1; }
static void mutex_unlock(struct mutex *m) { assert(m->held);m->held=0; }
static struct mutex *lock_scope(struct mutex *m) { mutex_lock(m);return m; }
static void unlock_scope(struct mutex **m) { mutex_unlock(*m); }
#define guard(t) struct mutex *scope __attribute__((cleanup(unlock_scope))) = lock_scope
static bool board=true;
static bool liuqin_board_quirks(void) { return board; }
struct work_struct { bool pending; };
struct device { void *data,*driver,*of_node; };
struct platform_device { struct device dev; };
struct device_node { int unused; };
struct apr_driver { void (*gpr_callback)(void); };
#define to_apr_driver(x) ((struct apr_driver *)(x))
struct packet_router { int rxwq; };
struct svc { struct packet_router *pr; void (*callback)(void); };
static bool transport_up=true;
static bool gpr_service_up(struct svc *s) { (void)s;return transport_up; }
struct apr_device { struct device dev; struct svc svc; };
typedef struct apr_device gpr_device_t;
static int flushes,action_error;
static void (*cleanup_action)(void *);
static void *cleanup_data;
static void flush_workqueue(int q) { assert(q==7);flushes++; }
static int devm_add_action_or_reset(struct device *d, void (*fn)(void *),void *v) {
 (void)d;
 if(action_error){fn(v);return action_error;}
 cleanup_action=fn;cleanup_data=v;return 0;
}
static void *dev_get_drvdata(struct device *d) { return d->data; }
static void dev_set_drvdata(struct device *d,void *v) { d->data=v; }
static void *devm_kzalloc(struct device *d,size_t n,int flags) {
 (void)d;(void)flags;return calloc(1,n);
}
static void init_waitqueue_head(int *w) { *w=1; }
struct q6prm { struct device *dev;gpr_device_t *gdev;int wait,result;struct mutex lock;struct device *clock_dev; };
static struct q6prm *liuqin_prm;
static struct mutex liuqin_prm_lock;
struct audioreach_graph { int start_count;int id; };
struct idr { struct audioreach_graph *graphs[4]; };
#define idr_for_each_entry(p,g,i) for((i)=0;(i)<4;(i)++) if(((g)=(p)->graphs[i]))
struct q6apm { struct device *dev;struct idr graph_idr;struct work_struct probe_work;
 bool prm_ready,audio_registered; };
static struct q6apm *g_apm;
static struct mutex apm_ready_lock;
static void liuqin_audio_set_ready(bool ready) { (void)ready; }
static int supplier_error;
static int liuqin_audio_resume_devices(void) { return supplier_error; }
static bool adsp_ready=true;
static bool q6apm_is_adsp_ready(void) { return adsp_ready; }
static int registered,unregistered,populated,depopulated,register_error,populate_error;
static const int q6apm_audio_component=1;
static int snd_soc_register_component(struct device *d,const void *c,void *p,int n) {
 (void)d;(void)c;(void)p;(void)n;
 if(register_error)return register_error;
 registered++;return 0;
}
static void snd_soc_unregister_component(struct device *d) { (void)d;unregistered++; }
static int of_platform_populate(void *node,void *a,void *b,struct device *d) {
 (void)node;(void)a;(void)b;(void)d;populated++;return populate_error;
}
static void of_platform_depopulate(struct device *d) { (void)d;depopulated++; }
static int system_unbound_wq;
static void queue_work(int q,struct work_struct *w) { (void)q;w->pending=true; }
static int cancellations;
static void cancel_work_sync(struct work_struct *w) { w->pending=false;cancellations++; }
static int stops,fail_graph=-1;
#define APM_CMD_GRAPH_STOP 3
static int audioreach_graph_mgmt_cmd(struct audioreach_graph *g,int cmd) {
 assert(cmd==APM_CMD_GRAPH_STOP);stops++;
 return g->id==fail_graph?-EIO:0;
}
'''
    for name in ['gpr_disable_callback', 'gpr_enable_callback']:
        code += function(apr, name) + '\n'
    for name in ['q6apm_register_audio', 'q6apm_probe_work', 'q6apm_set_prm_ready',
                 'apm_remove', 'q6apm_suspend_audio']:
        code += function(apm, name) + '\n'
    code += r'''
static int clock_probe_count;
static int recover_result=-EPROBE_DEFER,clock_puts;
static struct platform_device retained_clock;
static struct device_node clock_node;
static struct device_node *of_get_compatible_child(void *node,const char *name) {
 (void)node;(void)name;return &clock_node;
}
static struct platform_device *of_find_device_by_node(struct device_node *node) {
 assert(node==&clock_node);return &retained_clock;
}
static void put_device(struct device *dev) { assert(dev==&retained_clock.dev);clock_puts++; }
static int q6dsp_clock_recover(struct device *dev,bool online) {
 assert(dev==&retained_clock.dev);
 if(online){assert(liuqin_prm && liuqin_prm->gdev->svc.callback);
  liuqin_prm->gdev->svc.callback();clock_probe_count++;return recover_result;}
 return 0;
}
static int devm_of_platform_populate(struct device *d) {
 struct q6prm *p=d->data;
 /* An orphan's prepare() calls back synchronously from clock registration. */
 assert(p->wait && p->dev==d && p->gdev->svc.callback);
 p->gdev->svc.callback();clock_probe_count++;return populate_error;
}
static void devm_of_platform_depopulate(struct device *d) { (void)d;depopulated++; }
'''
    for name in ['q6prm_transport_ready', 'prm_probe', 'prm_remove']:
        code += function(prm, name) + '\n'
    code += r'''
static int clock_result;
static int q6dsp_clock_dev_probe(struct platform_device *p) { (void)p;return clock_result; }
'''
    code += function(clocks, 'q6prm_clock_probe') + '\n'
    code += r'''
struct sc8280xp_snd_data { int suspend_error; };
struct snd_soc_card { struct sc8280xp_snd_data *data; };
static void *snd_soc_card_get_drvdata(struct snd_soc_card *c) { return c->data; }
static int resume_count,suspend_count,suspend_error;
static int ports_error,ports_suspended;
static int liuqin_audio_suspend_ports(struct snd_soc_card *c) {
 (void)c;ports_suspended++;return ports_error;
}
'''
    code += function(machine, 'liuqin_audio_suspend_pre') + '\n'
    code += r'''
static int snd_soc_suspend(struct device *d) {
 int before=ports_suspended;
 suspend_count++;liuqin_audio_suspend_pre(d->data);
 /* DAPM removes the clock votes next; enabled ports would clash. */
 if (!((struct snd_soc_card *)d->data)->data->suspend_error)
  assert(ports_suspended==before+1);
 return suspend_error;
}
static int snd_soc_resume(struct device *d) { (void)d;resume_count++;return 0; }
'''
    for name in ['sc8280xp_system_suspend', 'sc8280xp_system_resume']:
        code += function(machine, name) + '\n'
    code += r'''
struct snd_pcm_substream;
struct q6apm_dai_rtd { int state;unsigned int queue_ptr,last_pos_index;
 struct snd_pcm_substream *substream;void *graph; };
struct snd_pcm_runtime { void *private_data;int state; };
struct snd_pcm_substream { struct snd_pcm_runtime *runtime; };
struct snd_soc_component { int unused; };
enum { SNDRV_PCM_TRIGGER_START=1,SNDRV_PCM_TRIGGER_RESUME,SNDRV_PCM_TRIGGER_PAUSE_RELEASE,
 SNDRV_PCM_TRIGGER_STOP,SNDRV_PCM_TRIGGER_SUSPEND,SNDRV_PCM_TRIGGER_PAUSE_PUSH };
enum { Q6APM_STREAM_IDLE,Q6APM_STREAM_STOPPED,Q6APM_STREAM_RUNNING };
enum { APM_CLIENT_EVENT_WATERMARK_EVENT=20,APM_CLIENT_EVENT_CMD_EOS_DONE,
 APM_CLIENT_EVENT_DATA_WRITE_DONE,APM_CLIENT_EVENT_DATA_READ_DONE };
enum { SNDRV_PCM_STATE_PREPARED=2,SNDRV_PCM_STATE_SUSPENDED=7 };
static int read_requests,periods;
static bool suspend_on_period;
static void snd_pcm_period_elapsed(struct snd_pcm_substream *s) {
 periods++;if(suspend_on_period)s->runtime->state=SNDRV_PCM_STATE_SUSPENDED;
}
static void q6apm_read(void *graph) { (void)graph;read_requests++; }
'''
    code += function(dai, 'q6apm_dai_trigger') + '\n'
    code += function(dai, 'event_handler') + '\n'
    code += r'''
static int replies;
static void reply(void) { replies++; }
int main(void) {
 struct apr_driver driver={reply};struct packet_router router={7};
 struct apr_device svc={.dev={.driver=&driver},.svc={.pr=&router}};
 struct device audio_dev={0};struct q6apm apm={.dev=&audio_dev};audio_dev.data=&apm;
 g_apm=&apm;
 assert(!prm_probe(&svc));assert(replies==1 && clock_probe_count==1);
 assert(!apm.probe_work.pending); /* callback ready is not clock-provider ready */
 struct platform_device clock_dev={0};
 clock_result=-EIO;assert(q6prm_clock_probe(&clock_dev)==-EIO);
 assert(!apm.probe_work.pending);
 clock_result=0;assert(!q6prm_clock_probe(&clock_dev));assert(apm.probe_work.pending);
 q6apm_probe_work(&apm.probe_work);assert(registered==1 && apm.audio_registered);
 q6apm_set_prm_ready(true);q6apm_probe_work(&apm.probe_work);assert(registered==1);
 prm_remove(&svc);assert(!apm.probe_work.pending && !apm.prm_ready);
 assert(!liuqin_prm && clock_puts==1);
 q6apm_probe_work(&apm.probe_work);assert(registered==1);
 cleanup_action(cleanup_data);assert(!svc.svc.callback && flushes==1);
 free(svc.dev.data);svc.dev.data=NULL;
 adsp_ready=false;assert(prm_probe(&svc)==-EPROBE_DEFER);
 assert(!svc.svc.callback && clock_probe_count==1);free(svc.dev.data);adsp_ready=true;
 action_error=-ENOMEM;assert(prm_probe(&svc)==-ENOMEM);
 assert(!svc.svc.callback && clock_probe_count==1);free(svc.dev.data);action_error=0;
 recover_result=0;assert(!prm_probe(&svc));
 assert(liuqin_prm && liuqin_prm->clock_dev==&retained_clock.dev);
 assert(q6prm_transport_ready());transport_up=false;
 assert(!q6prm_transport_ready());transport_up=true;
 assert(apm.probe_work.pending && clock_probe_count==2); /* same provider on recovery */
 prm_remove(&svc);cleanup_action(cleanup_data);free(svc.dev.data);
 assert(clock_puts==2 && !liuqin_prm);
 board=false;assert(!prm_probe(&svc));assert(clock_probe_count==3);
 prm_remove(&svc);cleanup_action(cleanup_data);free(svc.dev.data);board=true;
 struct apr_device adev={.dev={.data=&apm}};
 q6apm_set_prm_ready(true);apm_remove(&adev);
 assert(!g_apm && !apm.probe_work.pending && unregistered==1);
 q6apm_set_prm_ready(true); /* late clock completion after APM removal */
 struct q6apm fail_apm={.dev=&audio_dev,.prm_ready=true};
 supplier_error=-EIO;int old_registered=registered;
 q6apm_probe_work(&fail_apm.probe_work);
 assert(!fail_apm.audio_registered && registered==old_registered);supplier_error=0;
 register_error=-ENOMEM;q6apm_probe_work(&fail_apm.probe_work);
 assert(!fail_apm.audio_registered);register_error=0;
 populate_error=-EIO;q6apm_probe_work(&fail_apm.probe_work);
 assert(!fail_apm.audio_registered && unregistered==2);populate_error=0;
 struct audioreach_graph a={2,0},b={0,1},c={1,2};
 apm.graph_idr=(struct idr){{&a,&b,&c,NULL}};g_apm=&apm;
 struct sc8280xp_snd_data data={0};struct snd_soc_card card={&data};
 struct device card_dev={.data=&card};
 assert(!sc8280xp_system_suspend(&card_dev));
 assert(stops==2 && !a.start_count && !c.start_count && !resume_count);
 assert(!sc8280xp_system_resume(&card_dev));assert(resume_count==1);
 assert(ports_suspended==1);ports_error=-EIO;
 assert(sc8280xp_system_suspend(&card_dev)==-EIO && resume_count==2);
 ports_error=0;resume_count=1;
 a.start_count=1;c.start_count=1;fail_graph=2;
 assert(sc8280xp_system_suspend(&card_dev)==-EIO);
 assert(!a.start_count && c.start_count==1 && resume_count==2);
 fail_graph=-1;board=false;int saved=stops;
 assert(!sc8280xp_system_suspend(&card_dev));assert(stops==saved);
 assert(!sc8280xp_system_resume(&card_dev));assert(resume_count==2);
 struct q6apm_dai_rtd prtd={.state=Q6APM_STREAM_RUNNING,.queue_ptr=123,.last_pos_index=7};
 struct snd_pcm_runtime runtime={.private_data=&prtd};struct snd_pcm_substream stream={&runtime};
 assert(!q6apm_dai_trigger(NULL,&stream,SNDRV_PCM_TRIGGER_SUSPEND));
 assert(prtd.state==Q6APM_STREAM_RUNNING);
 board=true;assert(!q6apm_dai_trigger(NULL,&stream,SNDRV_PCM_TRIGGER_SUSPEND));
 assert(prtd.state==Q6APM_STREAM_STOPPED && prtd.queue_ptr==123);
 assert(!q6apm_dai_trigger(NULL,&stream,SNDRV_PCM_TRIGGER_STOP));assert(!prtd.queue_ptr);
 assert(q6apm_dai_trigger(NULL,&stream,99)==-EINVAL);
 prtd.substream=&stream;prtd.state=Q6APM_STREAM_RUNNING;runtime.state=SNDRV_PCM_STATE_PREPARED;
 event_handler(APM_CLIENT_EVENT_DATA_READ_DONE,0,NULL,&prtd);
 assert(read_requests==1 && periods==1); /* capture prefill before ALSA START */
 suspend_on_period=true;
 event_handler(APM_CLIENT_EVENT_DATA_READ_DONE,0,NULL,&prtd);
 assert(read_requests==1 && periods==2); /* prepared stream has no SUSPEND trigger */
 board=false;event_handler(APM_CLIENT_EVENT_DATA_READ_DONE,0,NULL,&prtd);
 assert(read_requests==2);board=true;suspend_on_period=false;
 g_apm=NULL;assert(!q6apm_suspend_audio());
 return 0;
}
'''
    run_c(code, flags=('-Wno-unused-parameter',), timeout=15)
    assert 'runtime->hw.info &= ~SNDRV_PCM_INFO_RESUME' in dai
    assert '.probe_type = PROBE_PREFER_ASYNCHRONOUS' in prm
    report = {'status': 'PASS', 'scope': ['callback ready before synchronous clock probes',
              'clock provider before audio registration', 'failed probe cleanup',
              'duplicate and stale readiness notifications', 'remove drains queued work',
              'DSP stop counts and error rollback', 'board isolation and PCM suspend',
              'same provider after transport replacement', 'prepared capture stops requeue on suspend',
              'data ports disabled before DAPM removes clock votes']}
    return report


def check_clocks(args):
    clocks = (args.source / 'sound/soc/qcom/qdsp6/q6dsp-lpass-clocks.c').read_text()
    apr = (args.source / 'drivers/soc/qcom/apr.c').read_text()
    audio = (args.source / 'sound/soc/qcom/qdsp6/audioreach.c').read_text()
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
#define EPROBE_DEFER 517
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define READ_ONCE(x) (x)
#define Q6DSP_MAX_CLK_ID 4
#define Q6DSP_LPASS_CLK_ROOT_DEFAULT 0
struct mutex { bool held; };
static struct mutex *take(struct mutex *m) { assert(!m->held);m->held=true;return m; }
static void release(struct mutex **m) { assert((*m)->held);(*m)->held=false; }
#define guard(t) struct mutex *scope __attribute__((cleanup(release))) = take
struct device { void *data; };
static void *dev_get_drvdata(struct device *d) { return d->data; }
struct clk_hw { const char *name; };
static const char *clk_hw_get_name(struct clk_hw *hw) { return hw->name; }
struct q6dsp_clk { struct device *dev;int q6dsp_clk_id,attributes,rate;
 uint32_t handle;bool requested,vote;struct clk_hw hw; };
#define to_q6dsp_clk(p) ((struct q6dsp_clk *)((char *)(p)-offsetof(struct q6dsp_clk,hw)))
struct q6dsp_clk_desc {
 int (*lpass_set_clk)(struct device *,int,int,int,unsigned int);
 int (*lpass_vote_clk)(struct device *,uint32_t,const char *,uint32_t *);
 int (*lpass_unvote_clk)(struct device *,uint32_t,uint32_t);
};
struct q6dsp_cc { struct device *dev;struct q6dsp_clk *clks[4];
 const struct q6dsp_clk_desc *desc;struct mutex lock;bool offline,registered; };
struct request { int id;unsigned int rate;bool enable,vote; };
static struct request requests[64];static int count,fail_id=-1;
static int set_clk(struct device *d,int id,int attr,int root,unsigned int rate) {
 (void)d;(void)attr;assert(root==0);
 requests[count++]=(struct request){id,rate,!!rate,false};
 return id==fail_id && rate?-EIO:0;
}
static int vote_clk(struct device *d,uint32_t id,const char *name,uint32_t *handle) {
 (void)d;assert(name);*handle=id+100;
 requests[count++]=(struct request){id,0,true,true};return (int)id==fail_id?-EIO:0;
}
static int unvote_clk(struct device *d,uint32_t id,uint32_t handle) {
 (void)d;assert(handle==id+100);requests[count++]=(struct request){id,0,false,true};return 0;
}
'''
    for name in ['clk_q6dsp_prepare', 'clk_q6dsp_unprepare', 'clk_q6dsp_set_rate',
                 'clk_q6dsp_recalc_rate', 'clk_vote_q6dsp_block',
                 'clk_unvote_q6dsp_block', 'q6dsp_clock_recover']:
        code += function(clocks, name) + '\n'
    code += r'''
static bool board=true;
static bool liuqin_board_quirks(void) { return board; }
struct packet_router { void *audio_pd;bool audio_online; };
struct pkt_router_svc { struct packet_router *pr; };
typedef uint32_t u32;
typedef int wait_queue_head_t;
struct gpr_ibasic_rsp_result_t { uint32_t opcode,status; };
#define HZ 1000
static unsigned long jiffies;
#define msecs_to_jiffies(x) (x)
#define time_before(a,b) ((long)((a)-(b))<0)
static int waits;
static unsigned long down_at,reply_at;
static uint32_t reply_opcode;
static struct gpr_ibasic_rsp_result_t *pending;
static struct packet_router *router;
static void tick(unsigned long ticks) {
 waits++;jiffies+=ticks;
 if(down_at && jiffies>=down_at)router->audio_online=false;
 if(reply_at && jiffies>=reply_at)pending->opcode=reply_opcode;
}
#define wait_event_timeout(w,condition,ticks) ({ (void)(w); \
 if(!(condition))tick(ticks);(condition)?1:0; })
'''
    code += function(apr, 'gpr_service_up') + '\n'
    code += function(audio, 'audioreach_wait_cmd') + '\n'
    code += r'''
int main(void) {
 const struct q6dsp_clk_desc desc={set_clk,vote_clk,unvote_clk};
 struct device dev={0};struct q6dsp_cc cc={.dev=&dev,.desc=&desc,.registered=true};dev.data=&cc;
 struct q6dsp_clk mclk={.dev=&dev,.q6dsp_clk_id=1,.rate=19200000};
 struct q6dsp_clk vote={.dev=&dev,.q6dsp_clk_id=2,.vote=true,.hw={"core"}};
 struct q6dsp_clk off={.dev=&dev,.q6dsp_clk_id=3,.rate=19200000};
 cc.clks[0]=&mclk;cc.clks[1]=&vote;cc.clks[2]=&off;
 assert(!clk_q6dsp_prepare(&mclk.hw));assert(!clk_vote_q6dsp_block(&vote.hw));
 assert(mclk.requested && vote.requested && !off.requested);
 assert(!clk_q6dsp_set_rate(&mclk.hw,49152000,0));
 assert(!q6dsp_clock_recover(&dev,false));count=0;
 assert(clk_q6dsp_prepare(&off.hw)==-EBUSY);
 assert(clk_vote_q6dsp_block(&vote.hw)==-EBUSY && !count);
 assert(!q6dsp_clock_recover(&dev,true));assert(count==2 && !cc.offline);
 assert(requests[0].vote && requests[0].id==2 && requests[0].enable);
 assert(!requests[1].vote && requests[1].id==1 && requests[1].rate==49152000);
 assert(clk_q6dsp_recalc_rate(&mclk.hw,0)==49152000);
 assert(!q6dsp_clock_recover(&dev,false));count=0;
 clk_q6dsp_unprepare(&mclk.hw);assert(!mclk.requested && !count);
 assert(!q6dsp_clock_recover(&dev,true));assert(count==1 && requests[0].vote);
 assert(!clk_q6dsp_prepare(&mclk.hw));assert(!q6dsp_clock_recover(&dev,false));
 count=0;fail_id=1;assert(q6dsp_clock_recover(&dev,true)==-EIO);
 assert(cc.offline && mclk.requested && vote.requested);
 assert(count==3 && requests[2].vote && !requests[2].enable); /* undo partial replay */
 fail_id=-1;count=0;assert(!q6dsp_clock_recover(&dev,true));assert(count==2);
 clk_unvote_q6dsp_block(&vote.hw);assert(!vote.requested && !requests[count-1].enable);
 cc.registered=false;assert(q6dsp_clock_recover(&dev,true)==-EPROBE_DEFER);
 dev.data=NULL;assert(q6dsp_clock_recover(&dev,true)==-EPROBE_DEFER);

 struct packet_router pr={.audio_pd=&dev,.audio_online=true};router=&pr;
 struct pkt_router_svc svc={&pr};struct gpr_ibasic_rsp_result_t result={0};pending=&result;
 wait_queue_head_t wait=0;
 reply_at=200;reply_opcode=20;
 assert(audioreach_wait_cmd(&svc,&result,&wait,10,20)>0 && waits==2);
 jiffies=0;waits=0;result.opcode=0;reply_at=0;down_at=100;
 assert(audioreach_wait_cmd(&svc,&result,&wait,10,20)==-ENETRESET && waits==1);
 waits=0;assert(audioreach_wait_cmd(&svc,&result,&wait,10,20)==-ENETRESET && !waits);
 jiffies=0;down_at=0;pr.audio_online=true;waits=0;
 assert(!audioreach_wait_cmd(&svc,&result,&wait,10,20));assert(waits==50);
 board=false;pr.audio_online=false;waits=0;
 assert(!audioreach_wait_cmd(&svc,&result,&wait,10,20));assert(waits==1);
 return 0;
}
'''
    run_c(code, flags=('-Wno-sign-compare', '-Wno-unused-parameter', '-Wno-misleading-indentation'), timeout=15)
    report = {'status': 'PASS', 'scope': ['offline clock refusal', 'retained rates',
              'resource votes before clock replay', 'release during offline',
              'partial replay rollback and retry', 'unready provider',
              'bounded cancellation after transport loss', 'ordinary reply and timeout',
              'other boards retain the original wait']}
    return report


def check_ports(args):
    machine = (args.source / 'sound/soc/qcom/sc8280xp.c').read_text()
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <errno.h>
#define AFE_PORT_MAX 8
#define dev_err(...) ((void)0)
#define IS_ERR_OR_NULL(x) (!(x))
struct sdw_stream_runtime { bool enabled; int disable_error,enable_error; };
struct snd_soc_dai { unsigned int id; };
struct snd_soc_card;
struct snd_soc_pcm_runtime;
struct snd_pcm_substream { void *runtime; struct snd_soc_pcm_runtime *rtd;
 struct sdw_stream_runtime *sdw; };
struct snd_soc_dai_link { bool no_pcm; };
struct snd_soc_pcm_runtime { struct snd_soc_dai cpu;struct snd_soc_dai_link *dai_link;
 struct snd_pcm_substream *streams[2];struct snd_soc_card *card; };
struct priv { int (*snd_prepare)(struct snd_pcm_substream *); };
struct sc8280xp_snd_data { bool stream_prepared[AFE_PORT_MAX];
 struct sdw_stream_runtime *suspended_stream[AFE_PORT_MAX];struct priv *priv; };
struct snd_soc_card { struct sc8280xp_snd_data *data;struct snd_soc_pcm_runtime *rtd[4]; };
static void *snd_soc_card_get_drvdata(struct snd_soc_card *c) { return c->data; }
static struct snd_soc_pcm_runtime *snd_soc_substream_to_rtd(struct snd_pcm_substream *s) { return s->rtd; }
static struct snd_soc_dai *snd_soc_rtd_to_cpu(struct snd_soc_pcm_runtime *r,int n) { (void)n;return &r->cpu; }
static struct snd_pcm_substream *snd_soc_dpcm_get_substream(struct snd_soc_pcm_runtime *r,int s) { return r->streams[s]; }
static struct sdw_stream_runtime *qcom_snd_sdw_get_stream(struct snd_pcm_substream *s) { return s->sdw; }
#define for_each_card_rtds(c,r) for(int n=0;n<4;n++) if(((r)=(c)->rtd[n]))
#define for_each_pcm_streams(s) for((s)=0;(s)<2;(s)++)
static int enables,disables;
static int sdw_disable_stream(struct sdw_stream_runtime *s) {
 disables++;s->enabled=false;return s->disable_error;
}
static int sdw_enable_stream(struct sdw_stream_runtime *s) {
 enables++;if(s->enable_error)return s->enable_error;s->enabled=true;return 0;
}
static int qcom_snd_sdw_prepare(struct snd_pcm_substream *s,bool *prepared) {
 if(!*prepared){s->sdw->enabled=true;*prepared=true;}return 0;
}
static int qcom_snd_sdw_hw_free(struct snd_pcm_substream *s,bool *prepared) {
 s->sdw->enabled=false;*prepared=false;return 0;
}
'''
    for name in ['liuqin_audio_suspend_ports', 'liuqin_audio_resume_pre',
                 'sc8280xp_snd_prepare', 'sc8280xp_snd_hw_free']:
        code += function(machine, name) + '\n'
    code += r'''
int main(void) {
 struct priv priv={0};struct sc8280xp_snd_data data={.priv=&priv};
 struct snd_soc_card card={.data=&data};
 struct snd_soc_dai_link be={true},fe={false};
 struct snd_soc_pcm_runtime mic={.cpu={1},.dai_link=&be,.card=&card};
 struct snd_soc_pcm_runtime headphone={.cpu={2},.dai_link=&be,.card=&card};
 struct snd_soc_pcm_runtime front={.cpu={3},.dai_link=&fe,.card=&card};
 struct sdw_stream_runtime a={true,0,0},b={true,0,0};
 struct snd_pcm_substream cap={&data,&mic,&a},play={&data,&headphone,&b};
 mic.streams[1]=&cap;headphone.streams[0]=&play;
 card.rtd[0]=&front;card.rtd[1]=&mic;card.rtd[2]=&headphone;
 /* No enabled BE, including an unconfigured stream: no hardware calls. */
 assert(!liuqin_audio_suspend_ports(&card) && disables==0);
 data.stream_prepared[1]=true;data.stream_prepared[2]=true;
 assert(!liuqin_audio_suspend_ports(&card) && disables==2);
 assert(!a.enabled && !b.enabled && data.stream_prepared[1]);
 assert(!liuqin_audio_resume_pre(&card));assert(a.enabled && b.enabled);
 assert(!data.suspended_stream[1] && !data.suspended_stream[2]);
 assert(!liuqin_audio_resume_pre(&card) && enables==2);
 /* Partial disable failure must retain both streams for rollback. */
 b.disable_error=-EIO;assert(liuqin_audio_suspend_ports(&card)==-EIO);
 assert(data.suspended_stream[1]==&a && data.suspended_stream[2]==&b);
 assert(!liuqin_audio_resume_pre(&card) && a.enabled && b.enabled);
 b.disable_error=0;assert(!liuqin_audio_suspend_ports(&card));
 a.enable_error=-EBUSY;assert(liuqin_audio_resume_pre(&card)==-EBUSY);
 assert(data.suspended_stream[1]==&a && !data.suspended_stream[2]);
 assert(sc8280xp_snd_prepare(&cap)==-EBUSY && !a.enabled);
 a.enable_error=0;assert(!sc8280xp_snd_prepare(&cap) && a.enabled);
 assert(!data.suspended_stream[1]);
 /* A closed suspended stream must never be revisited at resume. */
 assert(!liuqin_audio_suspend_ports(&card));
 assert(!sc8280xp_snd_hw_free(&cap));assert(!data.suspended_stream[1]);
 assert(!liuqin_audio_resume_pre(&card) && !a.enabled && b.enabled);
 return 0;
}
'''
    run_c(code)
    report = {'status': 'PASS', 'scope': ['idle sleep', 'prepared/running SoundWire ports',
              'disable failure rollback', 'resume failure retry', 'close during suspension']}
    return report


def check_playback_queue(args):
    source = (args.source / 'sound/soc/qcom/qdsp6/q6apm-dai.c').read_text()
    prepare = function(source, 'q6apm_dai_prepare')
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#define dev_err(d,...) ((void)(d))
#define WRITE_ONCE(x,v) ((x)=(v))
#define SNDRV_PCM_STREAM_PLAYBACK 0
#define SNDRV_PCM_STREAM_CAPTURE 1
#define SND_AUDIOCODEC_PCM 1
#define NO_TIMESTAMP 0
enum { Q6APM_STREAM_IDLE,Q6APM_STREAM_STOPPED,Q6APM_STREAM_RUNNING };
enum { SNDRV_PCM_TRIGGER_START=1,SNDRV_PCM_TRIGGER_RESUME,SNDRV_PCM_TRIGGER_PAUSE_RELEASE,
 SNDRV_PCM_TRIGGER_STOP,SNDRV_PCM_TRIGGER_SUSPEND,SNDRV_PCM_TRIGGER_PAUSE_PUSH };
struct device { int unused; };
struct q6apm_dai_data { int unused; };
struct snd_soc_component { struct device *dev; };
struct snd_pcm_control { unsigned long appl_ptr; };
struct q6apm_dai_rtd { void *graph;int state,bits_per_sample;unsigned long queue_ptr;
 unsigned int last_pos_index,pcm_count,pcm_size,push_pull_size,periods,phys,pos_phys; };
struct snd_pcm_runtime { void *private_data;struct snd_pcm_control *control;
 unsigned int rate,channels,periods;unsigned long period_size; };
struct snd_pcm_substream { struct snd_pcm_runtime *runtime;int stream; };
struct audioreach_module_config { int direction,sample_rate,num_channels,bit_width,fmt; };
static bool board=true,push_pull;
static int writes,reads,allocations,starts,prepare_error;
static bool liuqin_board_quirks(void) { return board; }
static struct q6apm_dai_data pdata;
static void *snd_soc_component_get_drvdata(struct snd_soc_component *c) { return &pdata; }
static void q6apm_dai_set_channel_mapping(struct audioreach_module_config *c) { }
static int q6apm_graph_stop(void *g) { return 0; }
static void q6apm_free_fragments(void *g,int d) { }
static unsigned int snd_pcm_lib_period_bytes(struct snd_pcm_substream *s) {
 return s->runtime->period_size*s->runtime->channels*2;
}
static bool q6apm_is_graph_in_push_pull_mode(void *g) { return push_pull; }
static int q6apm_push_pull_config(void *g,unsigned int p,unsigned int pos,unsigned int n) { return 0; }
static int q6apm_register_watermark_event(void *g,unsigned int size,unsigned int periods) { return 0; }
static int q6apm_alloc_fragments(void *g,int d,unsigned int p,unsigned int size,unsigned int periods) {
 allocations++;return 0;
}
static int q6apm_graph_media_format_pcm(void *g,struct audioreach_module_config *c) { return 0; }
static int q6apm_graph_media_format_shmem(void *g,struct audioreach_module_config *c) { return 0; }
static int q6apm_graph_prepare(void *g) { return prepare_error; }
static int q6apm_graph_start(void *g) { starts++;return 0; }
static int q6apm_read(void *g) { reads++;return 0; }
static int q6apm_write_async(void *g,unsigned int n,int a,int b,int flags) {
 assert(++writes<10000);return 0;
}
'''
    code += prepare + '\n'
    # Keep the pre-fix variant executable: it must reproduce the missing prefill.
    old = prepare.replace('q6apm_dai_prepare(', 'old_prepare(', 1)
    old = old.replace('\tif (liuqin_board_quirks())\n\t\tprtd->queue_ptr = 0;\n', '')
    assert old != prepare.replace('q6apm_dai_prepare(', 'old_prepare(', 1)
    code += old + '\n'
    for name in ('q6apm_dai_ack', 'q6apm_dai_trigger'):
        code += function(source, name) + '\n'
    code += r'''
int main(void) {
 struct device dev={0};struct snd_soc_component c={&dev};
 struct q6apm_dai_rtd p={.graph=&dev,.state=Q6APM_STREAM_RUNNING,
  .bits_per_sample=16,.queue_ptr=64*1024,.pcm_size=16384,.periods=4};
 struct snd_pcm_control control={0};
 struct snd_pcm_runtime runtime={&p,&control,48000,2,4,1024};
 struct snd_pcm_substream s={&runtime,SNDRV_PCM_STREAM_PLAYBACK};
 assert(!q6apm_dai_trigger(&c,&s,SNDRV_PCM_TRIGGER_SUSPEND));
 assert(!old_prepare(&c,&s));control.appl_ptr=2048;
 assert(!q6apm_dai_ack(&c,&s) && writes==0); /* RUNNING, no hardware progress */
 for(int cycle=0;cycle<100;cycle++) {
  p.queue_ptr=(cycle+8)*1024;p.last_pos_index=7;control.appl_ptr=0;
  assert(!q6apm_dai_trigger(&c,&s,SNDRV_PCM_TRIGGER_SUSPEND));
  assert(!q6apm_dai_prepare(&c,&s) && !p.queue_ptr && !p.last_pos_index);
  control.appl_ptr=2048;int previous=writes;
  assert(!q6apm_dai_ack(&c,&s) && writes==previous+2 && p.queue_ptr==2048);
  assert(!q6apm_dai_ack(&c,&s) && writes==previous+2); /* no duplicate queue */
  assert(!q6apm_dai_trigger(&c,&s,SNDRV_PCM_TRIGGER_START));
  control.appl_ptr+=1024;
  assert(!q6apm_dai_ack(&c,&s) && writes==previous+3);
 }
 board=false;p.queue_ptr=8192;assert(!q6apm_dai_prepare(&c,&s));assert(p.queue_ptr==8192);
 board=true;s.stream=SNDRV_PCM_STREAM_CAPTURE;int previous=reads;
 assert(!q6apm_dai_prepare(&c,&s) && reads==previous+4);
 prepare_error=-EIO;assert(!q6apm_dai_trigger(&c,&s,SNDRV_PCM_TRIGGER_SUSPEND));
 previous=starts;assert(q6apm_dai_prepare(&c,&s)==-EIO && starts==previous);
 assert(p.state==Q6APM_STREAM_STOPPED);
 return 0;
}
'''
    run_c(code, flags=('-Wno-unused-parameter', '-Wno-sign-compare'), timeout=15)
    report = {'status': 'PASS', 'cycles': 100,
              'scope': ['old suspended queue blocks prefill', 'prepare resets playback queue',
                        'duplicate ack does not queue twice', 'capture and failed prepare', 'board isolation']}
    return report


def check_card(args):
    machine = (args.source / 'sound/soc/qcom/sc8280xp.c').read_text()
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
enum { DISPLAY_PORT_RX_0=10, DISPLAY_PORT_RX_1=20, DISPLAY_PORT_RX_7=26,
       TX_CODEC_DMA_TX_0=30, TX_CODEC_DMA_TX_3=33 };
struct sc8280xp_snd_data { bool jack_setup;bool stream_prepared[40];void *suspended_stream[40]; };
struct device { int pin_state; };
struct snd_soc_card { struct sc8280xp_snd_data *data;struct device *dev; };
static unsigned int generation;
static int pin_fault;
static unsigned int liuqin_audio_generation(void) { return generation; }
static int pinctrl_pm_select_sleep_state(struct device *d) {
 if(pin_fault==1)return -EIO;d->pin_state=1;return 0;
}
static int pinctrl_pm_select_default_state(struct device *d) {
 assert(d->pin_state==1);if(pin_fault==2)return -EIO;d->pin_state=2;return 0;
}
struct component { void *jack; };
struct snd_soc_dai { unsigned int id;struct component *component; };
struct snd_soc_pcm_runtime { struct snd_soc_dai cpu,codecs[2]; };
static void *snd_soc_card_get_drvdata(struct snd_soc_card *c) { return c->data; }
static struct snd_soc_dai *snd_soc_rtd_to_cpu(struct snd_soc_pcm_runtime *r,int n) { (void)n;return &r->cpu; }
#define for_each_rtd_codec_dais(r,i,d) for((i)=0;(i)<2 && ((d)=&(r)->codecs[i]);(i)++)
static int detached;
static int snd_soc_component_set_jack(struct component *c,void *j,void *data) {
 assert(!j && !data);c->jack=NULL;detached++;return 0;
}
'''
    for name in ['liuqin_snd_exit', 'liuqin_snd_card_probe']:
        code += function(machine, name) + '\n'
    code += r'''
int main(void) {
 struct sc8280xp_snd_data data={.jack_setup=true};struct device dev={0};
 struct snd_soc_card card={&data,&dev};
 data.stream_prepared[30]=true;data.suspended_stream[30]=&data;
 assert(!liuqin_snd_card_probe(&card));
 assert(!data.jack_setup && !data.stream_prepared[30] && !data.suspended_stream[30]);
 assert(!dev.pin_state);generation=1;
 for(pin_fault=1;pin_fault<=2;pin_fault++)assert(liuqin_snd_card_probe(&card)==-EIO);
 pin_fault=0;assert(!liuqin_snd_card_probe(&card) && dev.pin_state==2);
 struct component a={&card},b={&card};
 struct snd_soc_pcm_runtime r={.codecs={{.component=&a},{.component=&b}}};
 for(unsigned int id=0;id<40;id++) {
  r.cpu.id=id;a.jack=&card;b.jack=&card;detached=0;liuqin_snd_exit(&r);
  bool jack_dai=id==10 || (id>=20 && id<=26) || (id>=30 && id<=33);
  assert(detached==(jack_dai?2:0));
  assert(jack_dai?(!a.jack && !b.jack):(a.jack==&card && b.jack==&card));
 }
 return 0;
}
'''
    run_c(code, flags=('-Wno-misleading-indentation',), timeout=15)
    result = {'status': 'PASS', 'scope': ['detach display and headset jack callbacks',
               'leave other DAIs intact', 'clear stale per-card stream and jack state']}
    return result


def check_device_order(args):
    source=(args.source/'drivers/remoteproc/qcom_q6v5_pas.c').read_text()
    header=(args.source/'include/linux/platform_data/liuqin-audio.h').read_text()
    code=r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <errno.h>
struct list_head { struct list_head *next,*prev; };
#define LIST_HEAD(n) struct list_head n={&(n),&(n)}
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define list_entry(p,t,m) container_of(p,t,m)
#define list_for_each_entry(p,h,m) for(p=list_entry((h)->next,__typeof__(*p),m);&(p)->m!=(h);p=list_entry((p)->m.next,__typeof__(*p),m))
#define list_for_each_entry_reverse(p,h,m) for(p=list_entry((h)->prev,__typeof__(*p),m);&(p)->m!=(h);p=list_entry((p)->m.prev,__typeof__(*p),m))
static void list_add_tail(struct list_head *n,struct list_head *h) { n->next=h;n->prev=h->prev;h->prev->next=n;h->prev=n; }
static void list_del(struct list_head *n) { n->prev->next=n->next;n->next->prev=n->prev; }
struct mutex { bool held; };
#define DEFINE_MUTEX(n) struct mutex n
static void mutex_lock(struct mutex *m) { assert(!m->held);m->held=true; }
static void mutex_unlock(struct mutex *m) { assert(m->held);m->held=false; }
static struct mutex *take(struct mutex *m) { mutex_lock(m);return m; }
static void release(struct mutex **m) { mutex_unlock(*m); }
#define guard(t) struct mutex *scope __attribute__((cleanup(release))) = take
struct regmap { bool cache_only,dirty; };
struct device { int order,disabled,suspend_fault,resume_fault;bool suspended;void *entry;void (*release)(void *);struct regmap map; };
static struct regmap *dev_get_regmap(struct device *d,void *name) { (void)name;return d->order==1 || d->order==2 || d->order==5?&d->map:NULL; }
static void regcache_cache_only(struct regmap *m,bool only) { m->cache_only=only; }
static void regcache_mark_dirty(struct regmap *m) { m->dirty=true; }
static bool board=true,alloc_fail,action_fail;
static int log_order[100],log_count;
#define GFP_KERNEL 0
static void *devm_kzalloc(struct device *d,size_t n,int flag) { (void)flag;if(alloc_fail)return NULL;return d->entry=calloc(1,n); }
static int devm_add_action_or_reset(struct device *d,void (*f)(void *),void *p) {
 if(action_fail){f(p);return -ENOMEM;}d->release=f;return 0;
}
static bool liuqin_board_quirks(void) { return board; }
static bool pm_runtime_enabled(struct device *d) { return !d->disabled; }
static int pm_runtime_force_suspend(struct device *d) {
 assert(!d->disabled);log_order[log_count++]=d->order;
 if(d->suspend_fault)return -EIO;d->disabled++;d->suspended=true;return 0;
}
static int pm_runtime_force_resume(struct device *d) {
 assert(d->disabled==1);log_order[log_count++]=d->order;d->disabled--;
 if(d->resume_fault)return -EIO;d->suspended=false;return 0;
}
#define dev_err(d,fmt,e) do { (void)(d);assert(e==-EIO); } while(0)
'''
    start=header.index('enum liuqin_audio_pm_order')
    code+=header[start:header.index('};',start)+2]+'\n'
    start=source.index('struct liuqin_audio_pm_device {')
    code+=source[start:source.index('static void liuqin_audio_cache_devices',start)]
    code+=function(source,'liuqin_audio_cache_devices')+'\n'
    for name in ['liuqin_audio_pm_unregister','devm_liuqin_audio_pm_register',
                 'liuqin_audio_suspend_devices','liuqin_audio_resume_devices']:
        code+=function(source,name)+'\n'
    code+=r'''
int main(void) {
 struct device d[7]={0};int insertion[]={5,3,2,1,6,0,4};
 for(int n=0;n<7;n++){int i=insertion[n];d[i].order=i;assert(!devm_liuqin_audio_pm_register(&d[i],i));}
 liuqin_audio_cache_devices();
 for(int i=0;i<7;i++)assert(d[i].map.cache_only==(i==1 || i==2 || i==5) && d[i].map.dirty==d[i].map.cache_only);
 for(int cycle=0;cycle<100;cycle++) {
  log_count=0;liuqin_audio_suspend_devices();assert(log_count==7);
  for(int i=0;i<7;i++)assert(log_order[i]==6-i && d[i].disabled==1);
  liuqin_audio_suspend_devices();assert(log_count==7);
  log_count=0;assert(!liuqin_audio_resume_devices());assert(log_count==7);
  for(int i=0;i<7;i++)assert(log_order[i]==i && !d[i].disabled && !d[i].suspended);
  assert(!liuqin_audio_resume_devices() && log_count==7);
 }
 log_count=0;d[2].suspend_fault=1;d[4].disabled=1;
 liuqin_audio_suspend_devices();assert(log_count==6 && !d[2].disabled);
 d[1].resume_fault=1;log_count=0;assert(liuqin_audio_resume_devices()==-EIO);
 assert(log_count==5 && d[4].disabled==1); /* unrelated system-PM disable untouched */
 for(int i=0;i<7;i++)if(i!=4)assert(!d[i].disabled);
 d[2].suspend_fault=0;d[1].resume_fault=0;d[4].disabled=0;
 log_count=0;board=false;liuqin_audio_suspend_devices();assert(!liuqin_audio_resume_devices() && !log_count);
 struct device other={0};assert(!devm_liuqin_audio_pm_register(&other,0) && !other.entry);board=true;
 alloc_fail=true;assert(devm_liuqin_audio_pm_register(&other,0)==-ENOMEM);alloc_fail=false;
 action_fail=true;assert(devm_liuqin_audio_pm_register(&other,0)==-ENOMEM);free(other.entry);action_fail=false;
 log_count=0;liuqin_audio_suspend_devices();assert(log_count==7);
 for(int i=0;i<7;i++){d[i].release(d[i].entry);assert(!d[i].disabled);free(d[i].entry);}
 assert(liuqin_audio_pm_devices.next==&liuqin_audio_pm_devices);
 return 0;
}
'''
    run_c(code, flags=('-Wno-misleading-indentation',), timeout=15)
    result={'status':'PASS','actual_kernel_functions_executed':True,'cycles':100,
            'consumer_first_suspend':True,'provider_first_resume':True,
            'duplicate_notifications_balanced':True,'failures_balance_pm_disable':True,
            'preexisting_disable_untouched':True,'unregister_balanced':True,'other_boards_unchanged':True}
    return result


def check_transport_state(args):
    source = args.source.resolve()
    pas = (source / 'drivers/remoteproc/qcom_q6v5_pas.c').read_text()
    declarations = pas[pas.index('static DEFINE_SPINLOCK(liuqin_audio_state_lock)'):
                       pas.index('bool liuqin_audio_transport_ready(void)')]
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdatomic.h>
#define DEFINE_SPINLOCK(n) int n
#define spin_lock_irqsave(l,f) do { assert(!*(l));*(l)=1;(f)=0; } while (0)
#define spin_unlock_irqrestore(l,f) do { assert(*(l));*(l)=0;(void)(f); } while (0)
#define READ_ONCE(x) __atomic_load_n(&(x), __ATOMIC_RELAXED)
#define WRITE_ONCE(x,v) __atomic_store_n(&(x), (v), __ATOMIC_RELAXED)
#define smp_load_acquire(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define smp_store_release(p,v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
bool board = true;
bool liuqin_board_quirks(void) { return board; }
static unsigned int invalidations;
static void liuqin_audio_cache_devices(void) { invalidations++; }
'''
    code += declarations
    for name in ['liuqin_audio_transport_ready', 'liuqin_audio_generation',
                 'liuqin_audio_set_ready']:
        code += function(pas, name) + '\n'
    code += r'''
void builtin_check(bool ready, unsigned int generation);
void module_check(bool ready, unsigned int generation);
static void check(bool ready, unsigned int generation) {
 builtin_check(ready, generation); module_check(ready, generation);
}
int main(void) {
 check(true, 0);
 for (unsigned int generation=0; generation<1000; generation++) {
  liuqin_audio_set_ready(false);check(false, generation);
  liuqin_audio_set_ready(false);check(false, generation);
  liuqin_audio_set_ready(true);check(true, generation+1);
  liuqin_audio_set_ready(true);check(true, generation+1);
 }
 board=false;liuqin_audio_set_ready(false);check(true, 0);
 assert(invalidations==2000);
 board=true;check(true, 1000);
 return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='liuqin-audio-state-') as tmp:
        tmp = Path(tmp)
        headers = {
            'generated/autoconf.h': '#define CONFIG_QCOM_Q6V5_PAS 1\n#define CONFIG_SND_SOC_QDSP6_PRM_MODULE 1\n',
            'linux/pm_runtime.h': '''#include <stdbool.h>
#include <errno.h>
struct device;
int pm_runtime_force_suspend(struct device *);
int pm_runtime_force_resume(struct device *);
''',
            'linux/pm_clock.h': 'int pm_clk_resume(struct device *);\n',
            'linux/regmap.h': 'struct regmap;\nint regmap_update_bits(struct regmap *,unsigned int,unsigned int,unsigned int);\n',
            'linux/platform_data/liuqin.h': 'bool liuqin_board_quirks(void);\n',
        }
        for name, content in headers.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (tmp / 'owner.c').write_text(code)
        flags = ['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined',
                 '-I'+str(tmp), '-I'+str(source/'include'), '-include', 'linux/kconfig.h']
        objects = []
        for kind, extra in [('builtin', []), ('module', ['-DMODULE=1'])]:
            path = tmp / (kind+'.c')
            path.write_text('''#include <assert.h>
#include <linux/platform_data/liuqin-audio.h>
void '''+kind+'''_check(bool ready, unsigned int generation) {
 assert(liuqin_audio_transport_ready()==ready);
 assert(liuqin_audio_generation()==generation);
}
''')
            obj = path.with_suffix('.o')
            subprocess.run(flags+extra+['-c', str(path), '-o', str(obj)], check=True)
            objects.append(str(obj))
        subprocess.run(flags+[str(tmp/'owner.c'), *objects, '-o', str(tmp/'test')], check=True)
        subprocess.run([str(tmp/'test')], check=True, timeout=15)
    result = {'status': 'PASS', 'actual_kernel_functions_executed': True,
              'restart_cycles': 1000, 'built_in_and_module_share_state': True,
              'duplicate_notifications_do_not_advance_generation': True,
              'other_boards_unchanged': True}
    return result


def check_macro_reset(args):
    header=(args.source/'include/linux/platform_data/liuqin-audio.h').read_text()
    code=r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
struct device { int refs,pm_error; };
struct regmap { unsigned int value;int writes,fail_at;int reset_asserts,reset_clears; };
struct clk_hw { void *owner; };
struct rx_macro { struct device *dev;struct regmap *regmap;unsigned int audio_generation;int mclk_refs,mclk_error; };
struct tx_macro { struct device *dev;struct regmap *regmap;unsigned int audio_generation;int mclk_refs,mclk_error; };
#define to_rx_macro(h) ((struct rx_macro *)(h)->owner)
#define to_tx_macro(h) ((struct tx_macro *)(h)->owner)
#define CDC_RX_CLK_RST_CTRL_SWR_CONTROL 7
#define CDC_TX_CLK_RST_CTRL_SWR_CONTROL 7
#define CDC_RX_SWR_CLK_EN_MASK 1
#define CDC_TX_SWR_CLK_EN_MASK 1
#define CDC_RX_SWR_RESET_MASK 2
#define CDC_TX_SWR_RESET_MASK 2
static bool board=true;
static unsigned int generation=2;
static bool liuqin_board_quirks(void) { return board; }
static unsigned int liuqin_audio_generation(void) { return generation; }
static int pm_runtime_resume_and_get(struct device *d) { if(d->pm_error)return d->pm_error;d->refs++;return 0; }
static void pm_runtime_put_autosuspend(struct device *d) { assert(d->refs>0);d->refs--; }
static int regmap_update_bits(struct regmap *m,unsigned int reg,unsigned int mask,unsigned int value) {
 assert(reg==7);m->writes++;
 if(m->writes==m->fail_at)return -EIO;
 m->value=(m->value&~mask)|(value&mask);
 if(mask==2){if(value)m->reset_asserts++;else m->reset_clears++;}
 return 0;
}
static int rx_macro_mclk_enable(struct rx_macro *m,bool enable) {
 if(enable && m->mclk_error)return m->mclk_error;
 m->mclk_refs+=enable?1:-1;assert(m->mclk_refs>=0);return 0;
}
static int tx_macro_mclk_enable(struct tx_macro *m,bool enable) {
 if(enable && m->mclk_error)return m->mclk_error;
 m->mclk_refs+=enable?1:-1;assert(m->mclk_refs>=0);return 0;
}
'''
    code+=function(header.replace('static inline','static'),'liuqin_audio_swr_clock_enable')+'\n'
    for kind in ['rx','tx']:
        source=(args.source/f'sound/soc/codecs/lpass-{kind}-macro.c').read_text()
        for action in ['enable','disable']:
            code+=function(source,'swclk_gate_'+action).replace('swclk_gate_'+action,kind+'_'+action)+'\n'
    code+=r'''
#define TEST(kind) do { \
 for(int fault=0;fault<=5;fault++){ \
  struct device d={0};struct regmap map={0}; \
  struct kind##_macro m={.dev=&d,.regmap=&map,.audio_generation=1};struct clk_hw h={&m}; \
  if(fault<=3)map.fail_at=fault;else if(fault==4)d.pm_error=-EIO;else m.mclk_error=-EIO; \
  int ret=kind##_enable(&h); \
  if(fault){assert(ret==-EIO && !d.refs && !m.mclk_refs && m.audio_generation==1 && !(map.value&1));} \
  else {assert(!ret && d.refs==1 && m.mclk_refs==1 && m.audio_generation==2); \
   assert(map.reset_asserts==1 && map.reset_clears==1 && map.value==1);kind##_disable(&h); \
   assert(!d.refs && !m.mclk_refs && map.value==0); \
   for(int i=0;i<30;i++){assert(!kind##_enable(&h));kind##_disable(&h);} \
   assert(map.reset_asserts==1 && map.reset_clears==1); \
  } \
 } \
 struct device d={0};struct regmap map={0}; \
 struct kind##_macro m={.dev=&d,.regmap=&map};struct clk_hw h={&m};board=false; \
 assert(!kind##_enable(&h));assert(!map.reset_asserts && !map.reset_clears);kind##_disable(&h);board=true; \
} while(0)
int main(void) { TEST(rx);TEST(tx);return 0; }
'''
    run_c(code)
    result={'status':'PASS','actual_kernel_functions_executed':True,'rx_tx_reset_on_restart':True,
            'ordinary_clock_votes_preserve_domain':True,'clock_failures_unwind_runtime_votes':True,
            'failed_reset_does_not_commit_generation':True,'other_boards_do_not_reset':True}
    return result


def check_offline_graphs(args):
    source = (args.source / 'sound/soc/qcom/qdsp6/q6apm.c').read_text()
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <errno.h>
#define APM_CMD_GRAPH_START 1
#define APM_CMD_GRAPH_PREPARE 2
#define APM_CMD_GRAPH_FLUSH 3
#define APM_CMD_GRAPH_STOP 4
#define APM_CMD_GRAPH_CLOSE 5
#define APM_MODULE_INSTANCE_ID 7
#define APM_PARAM_ID_SUB_GRAPH_LIST 8
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
struct service {bool online;};
struct gdev {struct service svc;};
struct kref {int count;};
struct audioreach_sub_graph {uint32_t sub_graph_id;};
struct audioreach_graph_info {int num_sub_graphs;struct audioreach_sub_graph sg_list[2];};
struct q6apm {struct gdev *gdev;int lock;void *graph_idr;};
struct audioreach_graph {struct audioreach_graph_info *info;struct q6apm *apm;
 int start_count,id;struct kref refcount;void *graph;};
struct q6apm_graph {struct audioreach_graph *ar_graph;void *port;};
struct apm_module_param_data {uint32_t module_instance_id,param_id,param_size;};
struct apm_graph_mgmt_cmd {struct apm_module_param_data param_data;uint32_t num_sub_graphs,sub_graph_id_list[];};
struct gpr_pkt {uint32_t opcode;};
#define GPR_HDR_SIZE sizeof(struct gpr_pkt)
#define APM_CMD_HDR_SIZE 0
#define APM_MODULE_PARAM_DATA_SIZE sizeof(struct apm_module_param_data)
#define APM_GRAPH_MGMT_PSIZE(p,n) (sizeof(struct apm_graph_mgmt_cmd)+4*(n))
#define list_for_each_entry(s,list,node) for(int j=0;j<2 && ((s)=&(*(list))[j]);j++)
static bool board=true,lose_service,allocation_error;
static int sent,allocated,freed,ports_freed,send_error;
static bool liuqin_board_quirks(void){return board;}
static bool gpr_service_up(struct service *s){return s->online;}
static void *alloc(size_t size){void *p=calloc(1,size);assert(p);allocated++;return p;}
static void kfree(void *p){if(p && !IS_ERR(p)){freed++;free(p);}}
static void cleanup_kfree(struct gpr_pkt **p){kfree(*p);}
#define __free(x) __attribute__((cleanup(cleanup_kfree)))
static struct gpr_pkt *audioreach_alloc_apm_cmd_pkt(int size,uint32_t opcode,int token){
 if(allocation_error)return (void *)(intptr_t)-ENOMEM;
 struct gpr_pkt *p=alloc(GPR_HDR_SIZE+size);p->opcode=opcode;return p;
}
static int q6apm_send_cmd_sync(struct q6apm *apm,struct gpr_pkt *pkt,int reply){
 struct apm_graph_mgmt_cmd *cmd=(void *)(pkt+1);
 assert(cmd->num_sub_graphs==2 && cmd->sub_graph_id_list[0]==21 && cmd->sub_graph_id_list[1]==22);
 assert(cmd->param_data.module_instance_id==7 && cmd->param_data.param_id==8);
 sent++;if(lose_service){apm->gdev->svc.online=false;return -ENETRESET;}
 if(!apm->gdev->svc.online)return -ENETRESET;
 return send_error;
}
static void mutex_lock(int *m){assert(!*m);*m=1;}
static void mutex_unlock(int *m){assert(*m);*m=0;}
static void *idr_remove(void **p,int id){assert(id==4 && *p);void *old=*p;*p=NULL;return old;}
static void kref_put(struct kref *r,void (*release)(struct kref *)){assert(r->count>0);if(!--r->count)release(r);}
static void gpr_free_port(void *p){ports_freed++;kfree(p);}
'''
    for name in ['audioreach_graph_teardown_offline', 'audioreach_graph_mgmt_cmd',
                 'q6apm_put_audioreach_graph', 'q6apm_graph_stop', 'q6apm_graph_close']:
        code += function(source, name) + '\n'
    code += r'''
int main(void){
 struct gdev device={.svc={.online=true}};struct q6apm apm={.gdev=&device};
 struct audioreach_graph_info info={.num_sub_graphs=2,.sg_list={{21},{22}}};
 struct audioreach_graph graph={.apm=&apm,.info=&info};
 for(int opcode=1;opcode<=5;opcode++){
  device.svc.online=false;int before=sent,a=allocated;
  int ret=audioreach_graph_mgmt_cmd(&graph,opcode);
  if(opcode>=4){assert(!ret && sent==before && allocated==a);}
  else assert(ret==-ENETRESET && sent==before+1);
  device.svc.online=true;lose_service=true;before=sent;
  ret=audioreach_graph_mgmt_cmd(&graph,opcode);assert(sent==before+1);
  assert(ret==(opcode>=4?0:-ENETRESET));lose_service=false;
  device.svc.online=true;
  int errors[]={0,-EIO,-ETIMEDOUT,-ENETRESET};
  for(unsigned int i=0;i<sizeof(errors)/sizeof(errors[0]);i++){
   send_error=errors[i];assert(audioreach_graph_mgmt_cmd(&graph,opcode)==errors[i]);
  }
  send_error=0;allocation_error=true;
  assert(audioreach_graph_mgmt_cmd(&graph,opcode)==-ENOMEM);
  device.svc.online=false;
  assert(audioreach_graph_mgmt_cmd(&graph,opcode)==(opcode>=4?0:-ENOMEM));
  allocation_error=false;board=false;before=sent;
  assert(audioreach_graph_mgmt_cmd(&graph,opcode)==-ENETRESET && sent==before+1);board=true;
 }
 assert(allocated==freed);
 /* Shared graphs still retire each host start and reference exactly once. */
 for(int online=0;online<2;online++){
  device.svc.online=online;int before=sent,ports=ports_freed;
  struct audioreach_graph *shared=alloc(sizeof(*shared));*shared=graph;
  shared->id=4;shared->graph=alloc(16);shared->refcount.count=2;shared->start_count=2;apm.graph_idr=shared;
  struct q6apm_graph *a=alloc(sizeof(*a)),*b=alloc(sizeof(*b));
  a->ar_graph=b->ar_graph=shared;a->port=alloc(8);b->port=alloc(8);
  assert(!q6apm_graph_stop(a) && shared->start_count==1 && sent==before);
  assert(!q6apm_graph_stop(b) && !shared->start_count && sent==before+online);
  assert(!q6apm_graph_stop(b) && sent==before+online);
  assert(!q6apm_graph_close(a) && apm.graph_idr && ports_freed==ports+1);
  assert(!q6apm_graph_close(b) && !apm.graph_idr && ports_freed==ports+2);
  assert(sent==before+2*online && !apm.lock && allocated==freed);
 }
 return 0;
}
'''
    run_c(code, flags=('-Wno-unused-parameter',), timeout=15)
    result = {'status': 'PASS', 'actual_kernel_functions_executed': True,
              'offline_stop_close_send_no_packets': True, 'reset_during_wait_completes_cleanup': True,
              'online_and_non_teardown_errors_preserved': True, 'shared_counts_and_memory_released': True,
              'other_boards_unchanged': True}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.report:
        args.report.unlink(missing_ok=True)
    checks = {
        'services': check_services(args),
        'clocks': check_clocks(args),
        'ports': check_ports(args),
        'playback_queue': check_playback_queue(args),
        'card': check_card(args),
        'device_order': check_device_order(args),
        'transport_state': check_transport_state(args),
        'macro_reset': check_macro_reset(args),
        'offline_graphs': check_offline_graphs(args)
    }
    write_report(checks, args.report)


if __name__ == '__main__':
    main()
