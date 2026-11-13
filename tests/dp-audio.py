#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise DP audio link, frame and ALSA lifetimes from actual kernel C."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

from kernel_test import function, run_c, write_report


def check_lifecycle(args):
    directory = args.source / 'drivers/gpu/drm/msm/dp'
    audio = (directory / 'dp_audio.c').read_text()
    display = (directory / 'dp_display.c').read_text()
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdatomic.h>
#include <pthread.h>
typedef uint32_t u32;
struct mutex { pthread_mutex_t mutex; };
struct msm_dp_audio { u32 lane_count, bw_code; };
struct msm_dp_audio_private {
 struct mutex lock;
 bool prepared, link_on, enabled;
 u32 channels;
 struct msm_dp_audio msm_dp_audio;
};
struct msm_dp { struct msm_dp_audio *msm_dp_audio; };
struct drm_bridge { struct msm_dp *msm_dp_display; };
struct drm_connector { int unused; };
struct hdmi_codec_daifmt { int unused; };
struct hdmi_codec_params { unsigned int channels; };
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_dp_bridge(b) (b)
#define IS_ERR(p) ((intptr_t)(p)<0)
#define PTR_ERR(p) ((int)(intptr_t)(p))
static _Thread_local int held;
static struct mutex *take(struct mutex *m) {
 assert(!held); assert(!pthread_mutex_lock(&m->mutex)); held++; return m;
}
static void release(struct mutex **m) {
 assert(held==1); held--; assert(!pthread_mutex_unlock(&(*m)->mutex));
}
#define guard(type) __attribute__((cleanup(release))) struct mutex *held_lock=take
static _Atomic bool clocked;
static int writes, starts, stops, step;
static u32 last_bw, last_lanes, last_channels;
static struct msm_dp_audio_private *msm_dp_audio_get_data(struct msm_dp *d) {
 return container_of(d->msm_dp_audio, struct msm_dp_audio_private, msm_dp_audio);
}
static void check_mmio(void) { assert(held==1 && clocked); writes++; }
static void msm_dp_audio_setup_sdp(struct msm_dp_audio_private *a) {
 check_mmio(); assert(a->link_on); assert(a->prepared); step=1; last_channels=a->channels;
}
static void msm_dp_audio_setup_acr(struct msm_dp_audio_private *a) {
 check_mmio(); assert(step==1); step=2; last_bw=a->msm_dp_audio.bw_code;
}
static void msm_dp_audio_safe_to_exit_level(struct msm_dp_audio_private *a) {
 check_mmio(); assert(step==2); step=3; last_lanes=a->msm_dp_audio.lane_count;
}
static void msm_dp_audio_enable(struct msm_dp_audio_private *a, bool enable) {
 check_mmio(); assert(a->link_on);
 if(enable) { assert(step==3); starts++; step=0; } else { stops++; }
}
'''
    for name in ('msm_dp_audio_start', 'msm_dp_audio_link_enable', 'msm_dp_audio_link_disable',
                 'msm_dp_audio_prepare', 'msm_dp_audio_shutdown'):
        code += function(audio, name) + '\n'
    code += r'''
static struct msm_dp_audio_private audio = { .lock.mutex=PTHREAD_MUTEX_INITIALIZER };
static struct msm_dp dp = { .msm_dp_audio=&audio.msm_dp_audio };
static struct drm_bridge bridge = { .msm_dp_display=&dp };
static struct hdmi_codec_params stereo = { .channels=2 };
static void on(u32 bw, u32 lanes) {
 clocked=true; msm_dp_audio_link_enable(&audio.msm_dp_audio,bw,lanes);
}
static void off(void) {
 msm_dp_audio_link_disable(&audio.msm_dp_audio); clocked=false;
 assert(!audio.link_on && !audio.enabled);
}
static void *alsa_race(void *unused) {
 for(int i=0;i<20000;i++) {
  assert(!msm_dp_audio_prepare(&bridge,NULL,NULL,&stereo));
  msm_dp_audio_shutdown(&bridge,NULL);
 }
 return NULL;
}
static void *link_race(void *unused) {
 for(int i=0;i<20000;i++) { on(0x14,(i&1)?2:4); off(); }
 return NULL;
}
int main(void) {
 int cases=0;
 /* A prepare during unplug/blanking is deferred, with no unclocked access. */
 assert(!msm_dp_audio_prepare(&bridge,NULL,NULL,&stereo));
 assert(audio.prepared && audio.channels==2 && !writes); cases++;
 on(0x14,4);
 assert(audio.enabled && starts==1 && last_bw==0x14 && last_lanes==4 && last_channels==2); cases++;
 /* Same open PCM, different lane/rate assignments after link reset. */
 for(int i=0;i<6;i++) {
  int s=starts; off(); assert(audio.prepared);
  int n=writes; msm_dp_audio_link_disable(&audio.msm_dp_audio); assert(writes==n);
  on((i&1)?0x0a:0x14,(i&1)?2:4); assert(starts==s+1 && audio.enabled);
  assert(last_lanes==((i&1)?2U:4U) && last_bw==((i&1)?0x0aU:0x14U)); cases++;
 }
 /* Close while blanked cancels restoration; repeated close touches no MMIO. */
 off(); int n=writes; msm_dp_audio_shutdown(&bridge,NULL); msm_dp_audio_shutdown(&bridge,NULL);
 assert(!audio.prepared && writes==n); on(0x14,4); assert(!audio.enabled && writes==n); cases++;
 /* Reopen on a running link and shut down exactly once. */
 assert(!msm_dp_audio_prepare(&bridge,NULL,NULL,&stereo)); assert(audio.enabled);
 int count=stops; msm_dp_audio_shutdown(&bridge,NULL);
 assert(!audio.enabled && !audio.prepared && stops==count+1);
 n=writes; msm_dp_audio_shutdown(&bridge,NULL); assert(writes==n); off(); cases++;
 /* Closing a prepare that never acquired a link cannot start stale audio. */
 msm_dp_audio_prepare(&bridge,NULL,NULL,&stereo); msm_dp_audio_shutdown(&bridge,NULL);
 count=starts; on(0x14,4); assert(starts==count && !audio.enabled); off(); cases++;
 /* Exercise the mutex at the actual ALSA/DRM boundary, not a model copy. */
 pthread_t alsa,link;
 assert(!pthread_create(&alsa,NULL,alsa_race,NULL));
 assert(!pthread_create(&link,NULL,link_race,NULL));
 assert(!pthread_join(alsa,NULL)); assert(!pthread_join(link,NULL));
 assert(!audio.prepared && !audio.enabled && !audio.link_on && !clocked); cases++;
 printf("%d\n",cases);
}
'''
    # Execute display callbacks too, so ordering and the failed-enable path
    # remain covered when the surrounding DRM code is changed.
    integration = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
struct msm_dp { bool power_on; void *connector; };
struct msm_dp_display_private { struct msm_dp msm_dp_display; void *ctrl,*audio,*panel; bool audio_supported,audio_link_pending; int audio_link_lock; };
struct drm_atomic_commit { int unused; };
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define drm_dbg_dp(...) ((void)0)
#define DRM_ERROR(...) ((void)0)
#define drm_err(...) ((void)0)
#define guard(type) __attribute__((unused)) int *link_lock=
#define WRITE_ONCE(x,y) ((x)=(y))
static int step,quiesced,pushed,prepare_error,enable_error,post_called,post_error,disabled;
static int drm_connector_hdmi_audio_link_notify(void *connector,bool active) { assert(!active && step==0); step=1; return 0; }
static void msm_dp_audio_link_disable(void *audio) { assert(step==1); step=2; quiesced++; }
static void msm_dp_ctrl_push_idle(void *ctrl) { assert(step==2); step=3; pushed++; }
static int msm_dp_display_prepare_link(struct msm_dp_display_private *d) { return prepare_error; }
static int msm_dp_display_enable(struct msm_dp_display_private *d,void *panel) {
 if(!enable_error)
  d->msm_dp_display.power_on=true;
 return enable_error;
}
static int msm_dp_display_post_enable(struct msm_dp *d) { assert(d->power_on); post_called++; return post_error; }
static void msm_dp_display_disable(struct msm_dp_display_private *d,void *panel) { disabled++; }
'''
    for name in ('msm_dp_display_audio_link_disable', 'msm_dp_display_atomic_disable', 'msm_dp_display_atomic_enable'):
        integration += function(display, name) + '\n'
    integration += r'''
int main(void) {
 struct msm_dp_display_private d={.audio_supported=true};
 msm_dp_display_atomic_disable(&d.msm_dp_display); assert(!quiesced&&!pushed);
 d.msm_dp_display.power_on=true; msm_dp_display_atomic_disable(&d.msm_dp_display);
 assert(quiesced==1&&pushed==1&&step==3);
 d.msm_dp_display.power_on=false; prepare_error=-1;
 msm_dp_display_atomic_enable(&d.msm_dp_display,NULL); assert(!post_called);
 prepare_error=0; enable_error=-1;
 msm_dp_display_atomic_enable(&d.msm_dp_display,NULL); assert(!post_called);
 enable_error=0; msm_dp_display_atomic_enable(&d.msm_dp_display,NULL); assert(post_called==1);
 post_error=-1; msm_dp_display_atomic_enable(&d.msm_dp_display,NULL); assert(disabled==1);
 puts("6");
}
'''
    unplug = function(display, 'msm_dp_hpd_unplug_handle')
    assert unplug.index('msm_dp_display_audio_link_disable') < unplug.index('msm_dp_display_host_phy_exit')
    disable = function(display, 'msm_dp_display_disable')
    assert disable.index('msm_dp_display_audio_link_disable') < disable.index('msm_dp_ctrl_off_pixel_clk')
    post = function(display, 'msm_dp_display_post_enable')
    assert 'msm_dp_audio_link_enable' not in post and 'audio_link_pending, true' in post
    frame = function(display, 'msm_dp_audio_complete_frame')
    assert frame.index('msm_dp_audio_link_enable') < frame.index('drm_connector_hdmi_audio_link_notify')
    assert not re.search(r'\baudio_comp\b', display)
    assert 'msm_dp_display_handle_plugged_change' not in function(display, 'msm_dp_display_atomic_post_disable')
    assert 'mutex_init(&audio->lock)' in audio
    counts = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name, body in (('audio', code), ('integration', integration)):
            source = work / (name + '.c'); binary = work / name
            source.write_text(body)
            subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-parameter', '-pthread', '-fsanitize=undefined',
                            '-fno-sanitize-recover=all', str(source), '-o', str(binary)], check=True)
            counts.append(int(subprocess.check_output([binary], text=True, timeout=15)))
    result = dict(all_passed=True, cases=sum(counts), concurrent_transitions=80000,
                  actual_kernel_functions_executed=True, no_unclocked_mmio=True,
                  shutdown_cancels_pending_prepare=True, failed_enable_does_not_restore_audio=True,
                  hardware_validation_required=True)
    return result


def check_link(args):
    pcm = (args.source / 'sound/soc/soc-pcm.c').read_text()
    hdmi = (args.source / 'drivers/gpu/drm/display/drm_hdmi_audio_helper.c').read_text()
    dpcm = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
struct snd_soc_pcm_runtime;
struct snd_soc_card { int depth,n; bool instantiated,component_chaining; struct snd_soc_pcm_runtime *rtds[3]; };
struct snd_soc_component { struct snd_soc_card *card; bool dpcm_link_blocked,dpcm_link_managed; };
struct snd_soc_dai { struct snd_soc_component *component; };
struct snd_soc_dpcm { struct snd_soc_pcm_runtime *be; int state; };
struct snd_pcm_substream { void *runtime; };
struct snd_soc_pcm_runtime {
 struct snd_soc_card *card;
 struct snd_soc_dai *codecs[2]; int num_codecs;
 struct snd_soc_dpcm *links[2]; int num_links;
 struct {int state;} dpcm[2];
 struct snd_pcm_substream substream;
 bool fe_compr;
};
struct snd_soc_dapm_widget { int id; struct snd_soc_pcm_runtime *be; };
struct snd_soc_dapm_widget_list { int num; struct snd_soc_dapm_widget *widgets[2]; };
enum {SNDRV_PCM_STREAM_PLAYBACK, SNDRV_PCM_STREAM_CAPTURE};
enum {SND_SOC_DPCM_STATE_NEW, SND_SOC_DPCM_STATE_OPEN, SND_SOC_DPCM_STATE_HW_PARAMS, SND_SOC_DPCM_STATE_CLOSE};
enum {SND_SOC_DPCM_LINK_STATE_FREE=7,SND_SOC_DPCM_UPDATE_BE=8};
enum {snd_soc_dapm_dai_in,snd_soc_dapm_dai_out};
#define dev_dbg(...) ((void)0)
#define dev_err(...) ((void)0)
#define snd_soc_ret(dev,ret,...) (ret)
#define for_each_rtd_codec_dais(r,i,d) for((i)=0;(i)<(r)->num_codecs && ((d)=(r)->codecs[i]);(i)++)
#define for_each_dpcm_be(r,s,d) for(int k=0;k<(r)->num_links && ((d)=(r)->links[k]);k++)
#define for_each_dapm_widgets(l,i,w) for((i)=0;(i)<(l)->num && ((w)=(l)->widgets[i]);(i)++)
#define for_each_card_rtds(c,r) for(int k=0;k<(c)->n && ((r)=(c)->rtds[k]);k++)
static int connected,updates,connect_result=1,walk_count,walk_error,walk[8];
static bool dpcm_be_is_active(struct snd_soc_dpcm *d,int stream,struct snd_soc_dapm_widget_list *l) {
 for(int i=0;i<l->num;i++) if(l->widgets[i]->be==d->be)return true;
 return false;
}
static void dpcm_set_be_update_state(struct snd_soc_pcm_runtime *b,int s,int state) { assert(state==SND_SOC_DPCM_UPDATE_BE); updates++; }
static struct snd_pcm_substream *snd_soc_dpcm_get_substream(struct snd_soc_pcm_runtime *r,int s) { return &r->substream; }
static struct snd_soc_pcm_runtime *dpcm_get_be(struct snd_soc_card *c,struct snd_soc_dapm_widget *w,int s) { return w->be; }
static int dpcm_be_connect(struct snd_soc_pcm_runtime *f,struct snd_soc_pcm_runtime *b,int s) { connected++; return connect_result; }
static void snd_soc_dpcm_mutex_lock(struct snd_soc_card *c) { assert(!c->depth); c->depth++; }
static void snd_soc_dpcm_mutex_unlock(struct snd_soc_card *c) { assert(c->depth==1); c->depth--; }
static void snd_soc_dpcm_mutex_assert_held(struct snd_soc_card *c) { assert(c->depth==1); }
static bool snd_soc_card_is_instantiated(struct snd_soc_card *c) { return c->instantiated; }
static int soc_dpcm_fe_runtime_update(struct snd_soc_pcm_runtime *r,int phase) {
 assert(r->card->depth==1); assert(walk_count<8); walk[walk_count++]=phase; return walk_error;
}
'''
    for name in ('dpcm_be_link_available', 'dpcm_prune_paths', 'dpcm_add_paths',
                 'soc_dpcm_runtime_update', 'snd_soc_dpcm_runtime_update', 'snd_soc_dpcm_set_link_state'):
        dpcm += function(pcm, name) + '\n'
    dpcm += r'''
int main(void) {
 int cases=0;
 struct snd_soc_card card={0};
 struct snd_soc_component a={.card=&card},b={.card=&card};
 struct snd_soc_dai da={&a},db={&b};
 struct snd_soc_pcm_runtime fe={.card=&card,.substream.runtime=&card};
 struct snd_soc_pcm_runtime be={.card=&card,.num_codecs=2,.codecs={&da,&db}};
 struct snd_soc_dpcm link={.be=&be};
 struct snd_soc_dapm_widget widget={.id=snd_soc_dapm_dai_in,.be=&be};
 struct snd_soc_dapm_widget_list widgets={.num=1,.widgets={&widget}},*list=&widgets;
 fe.num_links=1;fe.links[0]=&link;card.n=2;card.rtds[0]=&fe;card.rtds[1]=&be;
 assert(dpcm_be_link_available(&be)); cases++;
 assert(!dpcm_prune_paths(&fe,0,&list)); assert(dpcm_add_paths(&fe,0,&list)==1); cases++;
 /* Unavailable codec blocks an initial connection and prunes an old one. */
 a.dpcm_link_blocked=true;assert(!dpcm_be_link_available(&be));
 connected=0;assert(!dpcm_add_paths(&fe,0,&list)&&!connected);
 assert(dpcm_prune_paths(&fe,0,&list)==1&&link.state==SND_SOC_DPCM_LINK_STATE_FREE);cases++;
 /* Multiple codecs cannot make the BE available until all links return. */
 b.dpcm_link_blocked=true;a.dpcm_link_blocked=false;
 assert(!dpcm_be_link_available(&be));assert(!dpcm_add_paths(&fe,0,&list));cases++;
 b.dpcm_link_blocked=false;assert(dpcm_add_paths(&fe,0,&list)==1);cases++;
 /* Availability only gates playback; capture remains routed. */
 a.dpcm_link_blocked=true;widget.id=snd_soc_dapm_dai_out;
 assert(!dpcm_prune_paths(&fe,1,&list));assert(dpcm_add_paths(&fe,1,&list)==1);cases++;
 a.dpcm_link_blocked=false;widget.id=snd_soc_dapm_dai_in;
 fe.substream.runtime=NULL;assert(!dpcm_add_paths(&fe,0,&list));cases++;
 fe.substream.runtime=&card;connect_result=0;assert(!dpcm_add_paths(&fe,0,&list));cases++;
 /* A late hook records state without walking a card still being probed. */
 assert(!snd_soc_dpcm_set_link_state(&a,true)&&!a.dpcm_link_managed&&!walk_count);cases++;
 assert(!snd_soc_dpcm_set_link_state(&a,false)&&a.dpcm_link_blocked&&a.dpcm_link_managed&&!walk_count&&!card.depth);cases++;
 card.instantiated=true;
 assert(!snd_soc_dpcm_set_link_state(&a,true)&&!a.dpcm_link_blocked&&!card.depth);
 assert(walk_count==4&&walk[0]==0&&walk[1]==0&&walk[2]==1&&walk[3]==1);cases++;
 /* Stop/start errors propagate; failed requests remain retryable. */
 walk_count=0;walk_error=-5;
 assert(snd_soc_dpcm_set_link_state(&a,false)==-5&&a.dpcm_link_blocked&&!card.depth&&walk_count==1);cases++;
 walk_count=0;walk_error=0;assert(!snd_soc_dpcm_set_link_state(&a,false)&&walk_count==4&&!card.depth);cases++;
 walk_count=0;assert(!snd_soc_dpcm_runtime_update(&card)&&walk_count==4&&!card.depth);cases++;
 printf("%d\n",cases);
}
'''
    notify = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stddef.h>
struct mutex { int held; };
struct device { int unused; };
typedef int (*hdmi_codec_link_cb)(struct device *,bool);
struct drm_connector { struct { struct mutex lock; bool last_link_state; hdmi_codec_link_cb link_cb; struct device *link_cb_dev; } hdmi_audio; };
static struct mutex *take(struct mutex *m) { assert(!m->held);m->held++;return m; }
static void release(struct mutex **m) { assert((*m)->held==1);(*m)->held--; }
#define guard(type) __attribute__((cleanup(release))) struct mutex *held=take
static int calls,error;static bool requested;
static struct drm_connector connector={.hdmi_audio.last_link_state=true};
static struct device codec;
static int callback(struct device *dev,bool active) { assert(dev==&codec&&connector.hdmi_audio.lock.held);calls++;requested=active;return error; }
'''
    for name in ('drm_connector_hdmi_audio_hook_link_cb', 'drm_connector_hdmi_audio_link_notify'):
        notify += function(hdmi, name) + '\n'
    notify += r'''
int main(void) {
 int cases=0;
 assert(!drm_connector_hdmi_audio_hook_link_cb(NULL,&connector,callback,&codec));
 assert(calls==1&&requested&&!connector.hdmi_audio.lock.held);cases++;
 assert(!drm_connector_hdmi_audio_link_notify(&connector,false));
 assert(calls==2&&!requested&&!connector.hdmi_audio.last_link_state);cases++;
 assert(!drm_connector_hdmi_audio_link_notify(&connector,false)&&calls==2);cases++;
 error=-5;assert(drm_connector_hdmi_audio_link_notify(&connector,true)==-5);
 assert(!connector.hdmi_audio.last_link_state&&!connector.hdmi_audio.lock.held);cases++;
 error=0;assert(!drm_connector_hdmi_audio_link_notify(&connector,true)&&calls==4);cases++;
 assert(!drm_connector_hdmi_audio_hook_link_cb(NULL,&connector,NULL,NULL));
 assert(!connector.hdmi_audio.link_cb&&!connector.hdmi_audio.link_cb_dev);cases++;
 assert(!drm_connector_hdmi_audio_link_notify(&connector,false)&&calls==4);cases++;
 assert(!drm_connector_hdmi_audio_hook_link_cb(NULL,&connector,callback,&codec));assert(!requested&&calls==5);cases++;
 assert(!drm_connector_hdmi_audio_hook_link_cb(NULL,&connector,NULL,NULL));
 error=-5;assert(drm_connector_hdmi_audio_hook_link_cb(NULL,&connector,callback,&codec)==-5);
 assert(!connector.hdmi_audio.link_cb&&!connector.hdmi_audio.lock.held);cases++;
 printf("%d\n",cases);
}
'''
    # The error must be returned only after route and pending-state cleanup.
    update = function(pcm, 'soc_dpcm_fe_runtime_update')
    assert 'ret = dpcm_run_update_startup' in update and 'ret = dpcm_run_update_shutdown' in update
    assert update.index('dpcm_clear_pending_state') < update.index('if (ret < 0)')
    counts = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in (('dpcm', dpcm), ('notify', notify)):
            src = Path(tmp) / (name+'.c'); binary = Path(tmp) / name
            src.write_text(body)
            subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                            '-fsanitize=undefined','-fno-sanitize-recover=all',str(src),'-o',str(binary)],check=True)
            counts.append(int(subprocess.check_output([binary],text=True,timeout=10)))
    result = dict(all_passed=True,cases=sum(counts),actual_kernel_functions_executed=True,
                  closed_frontend_not_restarted=True,capture_unchanged=True,
                  multiple_codec_availability=True,errors_and_callback_removal_verified=True,
                  hardware_validation_required=True)
    return result


def check_frame(args):
    display = (args.source / 'drivers/gpu/drm/msm/dp/dp_display.c').read_text()
    kms = (args.source / 'drivers/gpu/drm/msm/disp/dpu1/dpu_kms.c').read_text()
    frame = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
struct mutex { int held; };
struct msm_dp { bool power_on,psr_supported; void *connector; };
struct link { struct {unsigned int rate,num_lanes;} link_params; };
struct msm_dp_display_private {
 struct msm_dp msm_dp_display; struct mutex audio_link_lock;
 bool audio_link_pending,audio_supported,plugged;
 struct link *link; void *audio,*ctrl,*panel;
};
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,y) ((x)=(y))
#define drm_err(...) ((void)0)
static struct mutex *take(struct mutex *m) {assert(!m->held);m->held=1;return m;}
static void drop(struct mutex **m) {assert((*m)->held);(*m)->held=0;}
#define guard(t) __attribute__((cleanup(drop))) struct mutex *lock=take
static int enabled,disabled,started,stopped,jacks,error;
static bool engine;
static unsigned int drm_dp_link_rate_to_bw_code(unsigned int rate) {return rate/27000;}
static void msm_dp_audio_link_enable(void *a,unsigned int rate,unsigned int lanes) {
 assert(rate==0x14 && lanes==4);engine=true;enabled++;
}
static void msm_dp_audio_link_disable(void *a) {engine=false;disabled++;}
static int drm_connector_hdmi_audio_link_notify(void *c,bool active) {
 if(active) {assert(engine);started++;} else {stopped++;}
 return error;
}
static void msm_dp_display_handle_plugged_change(struct msm_dp *d,bool plugged) {assert(plugged);jacks++;}
static void msm_dp_ctrl_config_psr(void *c,void *p) {}
'''
    for name in ('msm_dp_display_audio_link_disable', 'msm_dp_display_post_enable', 'msm_dp_audio_complete_frame'):
        frame += function(display, name) + '\n'
    frame += r'''
int main(void) {
 int cases=0;
 struct link link={.link_params={540000,4}};
 struct msm_dp_display_private d={.audio_supported=true,.plugged=true,.link=&link,
   .msm_dp_display.power_on=true};
 msm_dp_audio_complete_frame(NULL);assert(!started);cases++;
 msm_dp_display_post_enable(&d.msm_dp_display);
 assert(d.audio_link_pending&&!started&&!enabled&&jacks==1);cases++;
 msm_dp_audio_complete_frame(&d.msm_dp_display);
 assert(!d.audio_link_pending&&enabled==1&&started==1&&!d.audio_link_lock.held);cases++;
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==1);cases++;
 msm_dp_display_audio_link_disable(&d);assert(stopped==1&&disabled==1&&!engine);cases++;
 msm_dp_display_post_enable(&d.msm_dp_display);msm_dp_display_audio_link_disable(&d);
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==1&&!d.audio_link_pending);cases++;
 msm_dp_display_post_enable(&d.msm_dp_display);d.plugged=false;
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==1&&!engine&&!d.audio_link_pending);cases++;
 d.plugged=true;msm_dp_display_post_enable(&d.msm_dp_display);d.msm_dp_display.power_on=false;
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==1&&!engine);cases++;
 d.msm_dp_display.power_on=true;msm_dp_display_post_enable(&d.msm_dp_display);d.audio_supported=false;
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==1&&!engine);cases++;
 d.audio_supported=true;error=-22;msm_dp_display_post_enable(&d.msm_dp_display);
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==2);
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==2);cases++;
 msm_dp_display_audio_link_disable(&d);error=0;msm_dp_display_post_enable(&d.msm_dp_display);
 msm_dp_audio_complete_frame(&d.msm_dp_display);assert(started==3&&engine);cases++;
 printf("%d\n",cases);
}
'''
    completion = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <errno.h>
struct msm_kms {int unused;};
struct drm_device {int unused;};
struct crtc_state {bool enable,active;};
struct drm_crtc {struct crtc_state *state;struct drm_device *dev;};
struct drm_encoder {struct drm_crtc *crtc;};
static struct drm_encoder sample_encoder;
static int wait_result,panel_calls,audio_calls,wait_calls;
#define DPU_ERROR(...) ((void)0)
#define DPU_DEBUG(...) ((void)0)
#define trace_dpu_kms_wait_for_commit_done(...) ((void)0)
#define list_for_each_entry(e,l,h) for(int i=0;i<1&&((e)=&sample_encoder);i++)
static bool drm_atomic_crtc_effectively_active(struct crtc_state *s) {return s->active;}
static int dpu_encoder_wait_for_commit_done(struct drm_encoder *e) {wait_calls++;return wait_result;}
static void dpu_kms_complete_panel_first_frame(struct drm_encoder *e) {panel_calls++;}
static void dpu_encoder_complete_dp_audio(struct drm_encoder *e) {audio_calls++;}
'''
    completion += function(kms, 'dpu_kms_wait_for_commit_done') + '\n'
    completion += r'''
int main(void) {
 struct msm_kms kms={0};struct drm_device dev={0};
 struct crtc_state state={.enable=true,.active=true};
 struct drm_crtc crtc={.state=&state,.dev=&dev};sample_encoder.crtc=&crtc;
 dpu_kms_wait_for_commit_done(NULL,&crtc);assert(!audio_calls);
 state.enable=false;dpu_kms_wait_for_commit_done(&kms,&crtc);assert(!wait_calls);
 state.enable=true;state.active=false;dpu_kms_wait_for_commit_done(&kms,&crtc);assert(!wait_calls);
 state.active=true;wait_result=-EWOULDBLOCK;dpu_kms_wait_for_commit_done(&kms,&crtc);assert(!audio_calls);
 wait_result=-ETIMEDOUT;dpu_kms_wait_for_commit_done(&kms,&crtc);assert(!audio_calls);
 wait_result=0;dpu_kms_wait_for_commit_done(&kms,&crtc);assert(audio_calls==1&&panel_calls==1);
 puts("6");
}
'''
    counts = []
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        for name, code in (('frame', frame), ('completion', completion)):
            source = work / (name + '.c')
            source.write_text(code)
            binary = work / name
            subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                            '-Wno-unused-but-set-variable', '-fsanitize=undefined', '-fno-sanitize-recover=all', str(source), '-o', str(binary)], check=True)
            counts.append(int(subprocess.check_output([binary], text=True, timeout=10)))
    result = dict(all_passed=True, cases=sum(counts), actual_kernel_functions_executed=True,
                  unplug_cancels_restore=True,
                  failed_frame_does_not_restore=True,
                  hardware_validation_required=True)
    return result


def check_close(args):
    hdmi = (args.source / 'sound/soc/codecs/hdmi-codec.c').read_text()
    pcm = (args.source / 'sound/soc/soc-pcm.c').read_text()
    q6 = (args.source / 'sound/soc/qcom/qdsp6/q6apm-lpass-dais.c').read_text()
    assert 'q6apm_graph_stop' not in function(q6, 'q6apm_lpass_dai_trigger')
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
struct device {struct device *parent;};
struct snd_soc_component {struct device *dev;bool dpcm_link_managed;};
struct snd_soc_dai {struct device *dev;struct snd_soc_component *component;bool cpu;};
struct snd_pcm_substream {int stream;};
struct ops {void (*audio_shutdown)(struct device *,void *);};
struct hdmi_codec_priv {bool shutdown_pending,busy;int chmap_idx,lock;
 struct {bool no_i2s_capture,no_i2s_playback;struct ops *ops;void *data;} hcd;};
struct snd_soc_pcm_runtime {struct snd_soc_dai *dais[2];struct snd_soc_component *component;};
static struct hdmi_codec_priv hcp;
static struct snd_soc_pcm_runtime *runtime;
static bool cpu_stopped;
static int shutdowns,early_shutdowns;
#define SNDRV_PCM_STREAM_PLAYBACK 0
#define HDMI_CODEC_CHMAP_IDX_UNKNOWN -1
#define guard(t) __attribute__((unused)) int *lock=
static void *snd_soc_dai_get_drvdata(struct snd_soc_dai *d) {return &hcp;}
static void *snd_soc_component_get_drvdata(struct snd_soc_component *c) {return &hcp;}
static void shutdown_audio(struct device *d,void *data) {
 shutdowns++;if(!cpu_stopped)early_shutdowns++;
}
'''
    for name in ('hdmi_codec_shutdown', 'hdmi_codec_pcm_close'):
        code += function(hdmi, name) + '\n'
    code += r'''
#define for_each_rtd_dais(r,i,d) for((i)=0;(i)<2&&((d)=(r)->dais[i]);(i)++)
#define for_each_rtd_dais_reverse(r,i,d) for((i)=1;(i)>=0&&((d)=(r)->dais[i]);(i)--)
#define for_each_rtd_components(r,i,c) for((i)=0;(i)<1&&((c)=(r)->component);(i)++)
static void snd_soc_dpcm_mutex_assert_held(struct snd_soc_pcm_runtime *r) {}
static void snd_soc_runtime_deactivate(struct snd_soc_pcm_runtime *r,int s) {}
static int snd_soc_dai_active(struct snd_soc_dai *d) {return 0;}
static void soc_pcm_set_dai_params(struct snd_soc_dai *d,void *p) {}
static void snd_soc_dai_shutdown(struct snd_soc_dai *d,struct snd_pcm_substream *s,int rollback) {
 if(d->cpu)cpu_stopped=true;else hdmi_codec_shutdown(s,d);
}
static void snd_soc_link_shutdown(struct snd_pcm_substream *s,int rollback) {}
static int soc_pcm_components_close(struct snd_pcm_substream *s,int rollback) {
 return hdmi_codec_pcm_close(runtime->component,s);
}
static void snd_soc_pcm_component_pm_runtime_put(struct snd_soc_pcm_runtime *r,struct snd_pcm_substream *s,int rb) {}
static bool snd_soc_component_active(struct snd_soc_component *c) {return false;}
static void pinctrl_pm_select_sleep_state(struct device *d) {}
'''
    code += function(pcm, 'soc_pcm_clean') + '\n'
    code += r'''
int main(void) {
 int cases=0;
 struct ops ops={.audio_shutdown=shutdown_audio};struct device dev={0};
 struct snd_soc_component component={.dev=&dev,.dpcm_link_managed=true};
 struct snd_soc_dai cpu={.dev=&dev,.component=&component,.cpu=true},codec={.dev=&dev,.component=&component};
 struct snd_soc_pcm_runtime rtd={.dais={&cpu,&codec},.component=&component};runtime=&rtd;
 struct snd_pcm_substream stream={0};hcp.hcd.ops=&ops;hcp.busy=true;hcp.hcd.no_i2s_capture=true;
 soc_pcm_clean(&rtd,&stream,0);
 assert(cpu_stopped&&shutdowns==1&&!early_shutdowns&&!hcp.shutdown_pending&&!hcp.busy);cases++;
 hdmi_codec_pcm_close(&component,&stream);assert(shutdowns==1);cases++;
 cpu_stopped=false;hcp.busy=true;soc_pcm_clean(&rtd,&stream,1);
 assert(shutdowns==2&&!early_shutdowns&&!hcp.shutdown_pending);cases++;
 /* Drivers that never opt into link control keep DAI-shutdown timing. */
 cpu_stopped=false;component.dpcm_link_managed=false;soc_pcm_clean(&rtd,&stream,0);
 assert(shutdowns==3&&early_shutdowns==1);cases++;
 /* Capture is never deferred, even on a component used by managed playback. */
 cpu_stopped=false;component.dpcm_link_managed=true;stream.stream=1;hcp.hcd.no_i2s_capture=false;
 soc_pcm_clean(&rtd,&stream,0);assert(shutdowns==4&&early_shutdowns==2);cases++;
 /* Unsupported streams and repeated close cannot synthesize a shutdown. */
 cpu_stopped=false;hcp.hcd.no_i2s_capture=true;soc_pcm_clean(&rtd,&stream,0);
 assert(shutdowns==4&&!hcp.shutdown_pending);cases++;
 stream.stream=0;cpu_stopped=false;hdmi_codec_shutdown(&stream,&codec);
 assert(hcp.shutdown_pending&&shutdowns==4);cpu_stopped=true;
 hdmi_codec_pcm_close(&component,&stream);assert(shutdowns==5&&!hcp.shutdown_pending);cases++;
 printf("%d\n",cases);
}
'''
    count = int(run_c(code, flags=('-Wno-unused-parameter',), timeout=10))
    result = dict(all_passed=True, cases=count, actual_kernel_functions_executed=True,
                  cpu_shutdown_precedes_engine_disable=True, legacy_and_capture_timing_unchanged=True,
                  duplicate_close_safe=True, hardware_validation_required=True)
    return result


def check_probe(args):
    pcm = (args.source / 'sound/soc/soc-pcm.c').read_text()
    body = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stddef.h>
#include <errno.h>
enum { SNDRV_PCM_STREAM_PLAYBACK, SNDRV_PCM_STREAM_CAPTURE };
enum { snd_soc_dapm_dai_in, snd_soc_dapm_dai_out, unrelated_widget };
struct snd_soc_component { bool dpcm_link_blocked; };
struct snd_soc_dai { struct snd_soc_component *component; };
struct snd_soc_card { int unused; };
struct snd_soc_pcm_runtime {
 struct snd_soc_card *card; struct snd_soc_dai *codecs[2];int num_codecs;
 struct { int be_clients; } dpcm[2];
 int depth;
};
struct snd_pcm_substream { struct snd_soc_pcm_runtime *rtd; int stream; };
struct snd_soc_dapm_widget { int id; struct snd_soc_pcm_runtime *be; };
struct snd_soc_dapm_widget_list { int num; struct snd_soc_dapm_widget *widgets[3]; };
#define snd_soc_substream_to_rtd(s) ((s)->rtd)
#define for_each_rtd_codec_dais(r,i,d) for((i)=0;(i)<(r)->num_codecs&&((d)=(r)->codecs[i]);(i)++)
#define for_each_dapm_widgets(l,i,w) for((i)=0;(i)<(l)->num&&((w)=(l)->widgets[i]);(i)++)
#define list_empty(p) (!*(p))
#define dev_err_once(...) ((void)0)
#define dev_dbg(...) ((void)0)
static struct snd_soc_dapm_widget_list widgets;
static int path_error,add_error,start_error,starts,cleanups,path_puts,clears;
static void snd_soc_dpcm_mutex_lock(struct snd_soc_pcm_runtime *fe) { assert(!fe->depth);fe->depth++; }
static void snd_soc_dpcm_mutex_unlock(struct snd_soc_pcm_runtime *fe) { assert(fe->depth==1);fe->depth--; }
static struct snd_soc_pcm_runtime *dpcm_get_be(struct snd_soc_card *c,struct snd_soc_dapm_widget *w,int s) { return w->be; }
static int dpcm_path_get(struct snd_soc_pcm_runtime *f,int s,struct snd_soc_dapm_widget_list **l) {
 assert(f->depth==1);*l=&widgets;return path_error?path_error:widgets.num;
}
static void dpcm_path_put(struct snd_soc_dapm_widget_list **l) { assert(*l==&widgets);path_puts++; }
static int dpcm_add_paths(struct snd_soc_pcm_runtime *f,int s,struct snd_soc_dapm_widget_list **l) { return add_error; }
static int dpcm_fe_dai_startup(struct snd_pcm_substream *s) { assert(s->rtd->depth==1);starts++;return start_error; }
static void dpcm_fe_dai_cleanup(struct snd_pcm_substream *s) { assert(s->rtd->depth==1);cleanups++;s->rtd->dpcm[s->stream].be_clients=0; }
static void dpcm_clear_pending_state(struct snd_soc_pcm_runtime *f,int s) { assert(f->depth==1);clears++; }
'''
    for name in ('dpcm_be_link_available', 'dpcm_has_blocked_playback_path', 'dpcm_fe_dai_open'):
        body += function(pcm, name) + '\n'
    body += r'''
int main(void) {
 int cases=0;
 struct snd_soc_card card={0};struct snd_soc_component a={0},b={0};
 struct snd_soc_dai da={&a},db={&b};
 struct snd_soc_pcm_runtime be={.card=&card,.num_codecs=2,.codecs={&da,&db}},fe={.card=&card};
 struct snd_pcm_substream sub={&fe,SNDRV_PCM_STREAM_PLAYBACK};
 struct snd_soc_dapm_widget w={snd_soc_dapm_dai_in,&be},unrelated={unrelated_widget,&be};
 widgets.num=1;widgets.widgets[0]=&w;
 /* No active BE and no explicit block remains a missing-route error. */
 assert(dpcm_fe_dai_open(&sub)==-EINVAL&&!starts&&!fe.depth&&path_puts==1);cases++;
 a.dpcm_link_blocked=true;
 assert(!dpcm_fe_dai_open(&sub)&&starts==1&&clears==1&&!fe.depth);cases++;
 b.dpcm_link_blocked=true;a.dpcm_link_blocked=false;
 assert(!dpcm_fe_dai_open(&sub)&&starts==2&&!fe.depth);cases++;
 /* A blocked unrelated widget, missing runtime, or empty graph is not a route. */
 widgets.widgets[0]=&unrelated;
 assert(dpcm_fe_dai_open(&sub)==-EINVAL&&starts==2);cases++;
 widgets.widgets[0]=&w;w.be=NULL;
 assert(dpcm_fe_dai_open(&sub)==-EINVAL&&starts==2);cases++;
 w.be=&be;widgets.num=0;
 assert(dpcm_fe_dai_open(&sub)==-EINVAL&&starts==2);cases++;
 widgets.num=1;sub.stream=SNDRV_PCM_STREAM_CAPTURE;w.id=snd_soc_dapm_dai_out;
 assert(dpcm_fe_dai_open(&sub)==-EINVAL&&starts==2);cases++;
 /* Existing capture and playback BEs still take the ordinary startup path. */
 fe.dpcm[1].be_clients=1;
 assert(!dpcm_fe_dai_open(&sub)&&starts==3&&!fe.depth);cases++;
 sub.stream=SNDRV_PCM_STREAM_PLAYBACK;w.id=snd_soc_dapm_dai_in;
 b.dpcm_link_blocked=false;fe.dpcm[0].be_clients=1;
 assert(!dpcm_fe_dai_open(&sub)&&starts==4&&!fe.depth);cases++;
 /* Allocation/connection and startup failures propagate and clean up. */
 a.dpcm_link_blocked=true;add_error=-ENOMEM;
 assert(dpcm_fe_dai_open(&sub)==-ENOMEM&&cleanups==1&&starts==4&&!fe.depth&&!fe.dpcm[0].be_clients);cases++;
 add_error=0;start_error=-EIO;
 assert(dpcm_fe_dai_open(&sub)==-EIO&&cleanups==2&&starts==5&&!fe.depth);cases++;
 start_error=0;path_error=-EIO;int prior_puts=path_puts;
 assert(dpcm_fe_dai_open(&sub)==-EIO&&path_puts==prior_puts&&starts==5&&!fe.depth);cases++;
 path_error=0;
 /* Repeated no-display probes must be retryable after errors. */
 for(int i=0;i<100;i++)assert(!dpcm_fe_dai_open(&sub)&&!fe.depth);
 assert(starts==105);cases++;
 printf("%d\n",cases);
}
'''
    cases = int(run_c(body, flags=('-Wno-unused-parameter',), timeout=10))
    result = dict(all_passed=True, cases=cases, actual_kernel_functions_executed=True,
                  missing_routes_rejected=True, capture_unchanged=True, errors_cleaned_up=True,
                  hardware_validation_required=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.report:
        args.report.unlink(missing_ok=True)
    checks = {
        'lifecycle': check_lifecycle(args),
        'link': check_link(args),
        'frame': check_frame(args),
        'close': check_close(args),
        'probe': check_probe(args)
    }
    write_report(checks, args.report)


if __name__ == '__main__':
    main()
