#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the candidate CABC C code, including failure and blanking races."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(source, name):
    m = re.search(r'^(?:static )?(?:int|void|bool|ssize_t|irqreturn_t)\s+' + name + r'\([^;{]*\)\s*\{', source, re.M)
    assert m, name
    end, depth = m.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[m.start():end]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--rom-proof', type=Path, required=True)
    ap.add_argument('--report', type=Path, required=True)
    ap.add_argument('--cc', default='cc')
    a = ap.parse_args()
    a.report.unlink(missing_ok=True)
    name = 'drivers/gpu/drm/panel/panel-novatek-nt36532.c'
    s, old = (a.source/name).read_text(), (a.baseline/name).read_text()
    for n in ['liuqin_reset', 'nt36532_prepare', 'nt36532_enable', 'nt36532_disable',
              'nt36532_unprepare', 'nt36532_remove', 'liuqin_send_dimming_on',
              'liuqin_dimming_work', 'liuqin_schedule_dimming', 'liuqin_send_oem_cmds',
              'liuqin_esd_irq', 'liuqin_esd_fault']:
        assert function(s, n) == function(old, n), n
    assert function(s, 'nt36532_post_backlight_enable').replace('\tliuqin_cabc_first_frame(ctx);\n', '').replace('ready:\n\tliuqin_esd_start(ctx);\n\tliuqin_schedule_dimming(ctx);', 'ready:\n\tliuqin_schedule_dimming(ctx);\n\tliuqin_esd_start(ctx);') == function(old, 'nt36532_post_backlight_enable')
    assert function(s, 'liuqin_cancel_dimming').replace('\tctx->cabc_ready = false;\n\tctx->cabc_applied = -1;\n\tctx->cabc_cycle = 0;\n', '') == function(old, 'liuqin_cancel_dimming')
    assert function(s, 'nt36532_probe').replace('\tret = liuqin_cabc_init(ctx);\n\tif (ret)\n\t\treturn ret;\n\n', '') == function(old, 'nt36532_probe')
    assert function(s, 'nt36532_probe').index('drm_panel_init') < function(s, 'nt36532_probe').index('liuqin_cabc_init') < function(s, 'nt36532_probe').index('drm_panel_add')
    proof = json.loads(a.rom_proof.read_text())
    for panel in proof['panels'].values():
        for mode, entry in panel['cabc'].items():
            assert entry['packets'] == [dict(type='0x15', payload='ff10', wait_ms=0), dict(type='0x15', payload=f"55{entry['value']:02x}", wait_ms=0)]
            assert entry['state'] == 'dsi_lp_mode'
    cabc = s[s.index('/* Exact M81 CABC modes.'):s.index('/* Exact M81 ROM dimming-on packets')]
    core = (a.baseline/'drivers/gpu/drm/drm_panel.c').read_text()
    prefix = r'''
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
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define READ_ONCE(v) (v)
#define MIPI_DSI_DCS_SHORT_WRITE_PARAM 0x15
#define MIPI_DSI_MSG_USE_LPM 2
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define EXPORT_SYMBOL(...)
struct mutex {pthread_mutex_t m;};
static _Thread_local struct mutex *held,*lock_stack[3];
static _Thread_local unsigned int lock_depth;
static _Thread_local bool off_actor;
static pthread_barrier_t off_attempt,tx_entered,tx_release;
static void mutex_lock(struct mutex *m){assert(lock_depth<3&&held!=m);if(off_actor){assert(pthread_mutex_trylock(&m->m)==EBUSY);pthread_barrier_wait(&off_attempt);off_actor=false;}assert(!pthread_mutex_lock(&m->m));lock_stack[lock_depth++]=held;held=m;}
static void mutex_unlock(struct mutex *m){assert(held==m&&lock_depth);held=lock_stack[--lock_depth];assert(!pthread_mutex_unlock(&m->m));}
#define lockdep_assert_held(m) assert(held==(m))
struct device {void *data;};
struct device_attribute {int attr;};
struct attribute {int unused;};
struct attribute_group {struct attribute **attrs;};
#define DEVICE_ATTR_RW(n) struct device_attribute dev_attr_##n
#define DEVICE_ATTR_RO(n) struct device_attribute dev_attr_##n
#define dev_get_drvdata(d) ((d)->data)
#define sysfs_emit(buf,...) snprintf(buf,4096,__VA_ARGS__)
static int match(const char *const *a,size_t n,const char *s){size_t z=strlen(s);if(z&&s[z-1]=='\n')z--;for(size_t i=0;i<n;i++)if(strlen(a[i])==z&&!strncmp(a[i],s,z))return i;return -EINVAL;}
#define sysfs_match_string(a,s) match(a,ARRAY_SIZE(a),s)
static int group_result,group_calls,errors;
static int devm_device_add_group(struct device*d,const struct attribute_group*g){assert(d&&g);group_calls++;return group_result;}
#define dev_warn(...) ((void)0)
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)errors++)
#define DRM_DEV_INFO(...) ((void)0)
struct backlight_device {bool blank;int brightness;};
static bool backlight_is_blank(const struct backlight_device*b){return b->blank;}
static int backlight_enable(struct backlight_device*b){b->blank=false;return 0;}
static int backlight_disable(struct backlight_device*b){b->blank=true;return 0;}
struct drm_panel;
struct drm_panel_funcs {int (*disable)(struct drm_panel*);};
struct drm_panel {struct device *dev;struct mutex backlight_lock;bool enabled,backlight_pending,backlight_after_first_frame;unsigned int backlight_cookie;struct backlight_device *backlight;const struct drm_panel_funcs *funcs;};
struct mipi_dsi_host;
struct mipi_dsi_msg {u8 channel,type;unsigned short flags;size_t tx_len;const void *tx_buf;size_t rx_len;void *rx_buf;};
struct mipi_dsi_host_ops {ssize_t (*transfer)(struct mipi_dsi_host*,const struct mipi_dsi_msg*);};
struct mipi_dsi_host {const struct mipi_dsi_host_ops *ops;};
struct mipi_dsi_device {struct mipi_dsi_host *host;u8 channel;};
struct liuqin_oem_panel {unsigned int dimming_delay_ms;};
struct liuqin_oem_cmd {u8 type,flags,wait_ms,len,data[17];};
struct nt36532 {struct drm_panel panel;struct mipi_dsi_device *dsi[2];const struct liuqin_oem_panel *oem_panel;struct mutex esd_lock;bool esd_available,esd_enabled,esd_stopping;unsigned int esd_cycle,esd_armed,dimming_cookie;unsigned long esd_started;int esd_arm_work;bool dimming_stopping,dimming_pending,esd_pending,cabc_ready;int cabc_requested,cabc_applied,cabc_error,dimming_work;unsigned int cabc_cycle;};
static struct liuqin_oem_panel variant;
static struct nt36532 c;static struct device dev;static struct backlight_device bl;
static struct mipi_dsi_host_ops ops;static struct mipi_dsi_host host;static struct mipi_dsi_device d;
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532*c){return c->dsi[1]?c->dsi[1]:c->dsi[0];}
static struct nt36532 *to_nt36532(struct drm_panel*p){return (struct nt36532*)((char*)p - offsetof(struct nt36532,panel));}
static void cancel_delayed_work_sync(int*w){assert(!held);(void)w;}
static int transfers,fail_at,short_at,cases;static bool hold_tx,inject_fault,payload_return;
static u8 trace[64][2];
static ssize_t tx(struct mipi_dsi_host*h,const struct mipi_dsi_msg*m){
 assert(h==&host&&held==&c.panel.backlight_lock&&transfers<64);assert(c.panel.enabled&&!bl.blank);assert(m->type==0x15&&m->flags==2&&m->tx_len==2&&m->channel==d.channel);
 int i=transfers++;memcpy(trace[i],m->tx_buf,2);
 if(hold_tx&&i==0){pthread_barrier_wait(&tx_entered);pthread_barrier_wait(&tx_release);}
 if(inject_fault&&i==1)c.esd_pending=true;
 if(i==fail_at)return -ETIMEDOUT;if(i==short_at)return 1;int wire=native_count(m);assert(wire<0||wire==4);return wire<0?wire:(payload_return?2:wire);
}
'''
    # Attribute pointers need the same field type as the kernel declaration.
    prefix = prefix.replace('struct device_attribute {int attr;};\nstruct attribute {int unused;};', 'struct attribute {int unused;};\nstruct device_attribute {struct attribute attr;};')
    host_source = (a.baseline/'drivers/gpu/drm/msm/dsi/dsi_host.c').read_text()
    packet_source = (a.baseline/'drivers/gpu/drm/drm_mipi_dsi.c').read_text()
    native_prefix = r"""
#define BIT(n) (1U << (n))
#define MIPI_DSI_MODE_VIDEO 1
#define pr_err(...) ((void)0)
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define PTR_ERR(p) ((long)(p))
struct mipi_dsi_packet {size_t size;u8 header[4];size_t payload_length;const u8 *payload;};
static bool mipi_dsi_packet_format_is_short(u8 t){return t==0x15;}
static bool mipi_dsi_packet_format_is_long(u8 t){return t==0x39;}
struct msm_dsi_host;
struct native_ops {u8 *(*tx_buf_get)(struct msm_dsi_host*);void (*tx_buf_put)(struct msm_dsi_host*);};
struct msm_dsi_cfg_handler {const struct native_ops *ops;};
struct native_mode {int hdisplay;};
struct msm_dsi_host {const struct msm_dsi_cfg_handler *cfg_hnd;int tx_size,format,mode_flags;struct native_mode *mode;};
static u8 native_buffer[32];static int native_dma_result;
static u8 *native_buf(struct msm_dsi_host*h){(void)h;return native_buffer;}
static int mipi_dsi_pixel_format_to_bpp(int format){assert(format==0);return 24;}
static int dsi_cmd_dma_tx(struct msm_dsi_host*h,int len){(void)h;assert(len==4);return native_dma_result?native_dma_result:len;}
"""
    native_body = '\n'.join([function(packet_source, 'mipi_dsi_create_packet'),
                              function(host_source, 'dsi_cmd_dma_add'),
                              function(host_source, 'dsi_cmds2buf_tx')])
    native_suffix = r"""
static int native_count(const struct mipi_dsi_msg*m){
 static const struct native_ops nops={native_buf,NULL};static const struct msm_dsi_cfg_handler cfg={&nops};
 struct native_mode mode={1800};struct msm_dsi_host mh={&cfg,32,0,MIPI_DSI_MODE_VIDEO,&mode};
 int ret=dsi_cmds2buf_tx(&mh,m);if(ret>=0){const u8 *d=m->tx_buf;assert(native_buffer[0]==d[0]&&native_buffer[1]==d[1]&&native_buffer[3]==BIT(7));}return ret;
}
"""
    prefix = prefix.replace('static ssize_t tx(', native_prefix+native_body+native_suffix+'\nstatic ssize_t tx(')
    prefix += r"""
static bool esd_monitor=true,oem_delayed_dimming=true,oem_normal_display=false;
static unsigned long jiffies=10;static int system_wq;
static unsigned long msecs_to_jiffies(unsigned int n){return n;}
static bool mod_delayed_work(int w,int*d,unsigned long delay){(void)w;(void)d;(void)delay;assert(lock_depth);return true;}
static void cancel_delayed_work(int*d){(void)d;}
static void liuqin_esd_mask_locked(struct nt36532*c){assert(held==&c->esd_lock);c->esd_armed=0;}
#define sysfs_notify(...) ((void)0)
static const struct liuqin_oem_cmd liuqin_oem_normal_display[]={{0x15,0,0,2,{0xff,0x10}},{0x39,0,0,2,{0x13,0}}};
"""
    bodies = '\n'.join(function(s,n) for n in ['liuqin_esd_start','liuqin_esd_stop',
        'liuqin_schedule_dimming','liuqin_cancel_dimming','nt36532_post_backlight_enable'])
    suffix = r'''
static int disabled(struct drm_panel*p){liuqin_esd_stop(to_nt36532(p),false);liuqin_cancel_dimming(to_nt36532(p),false);return 0;}
static const struct drm_panel_funcs funcs={disabled};
static void reset(void){memset(&c,0,sizeof(c));memset(trace,0,sizeof(trace));assert(!pthread_mutex_init(&c.panel.backlight_lock.m,NULL));assert(!pthread_mutex_init(&c.esd_lock.m,NULL));dev.data=&c;bl=(struct backlight_device){false,1500};ops=(struct mipi_dsi_host_ops){tx};host=(struct mipi_dsi_host){&ops};d=(struct mipi_dsi_device){&host,0};c.dsi[0]=c.dsi[1]=&d;c.oem_panel=&variant;c.esd_available=true;c.esd_enabled=true;c.panel.backlight_after_first_frame=true;c.panel.dev=&dev;c.panel.enabled=true;c.panel.backlight_cookie=1;c.panel.backlight=&bl;c.panel.funcs=&funcs;transfers=errors=group_calls=group_result=native_dma_result=0;fail_at=short_at=-1;hold_tx=inject_fault=payload_return=false;oem_cabc=-1;assert(!liuqin_cabc_init(&c));assert(c.cabc_requested==-1&&c.cabc_applied==-1&&group_calls==1);cases++;}
static void first_frame(void){mutex_lock(&c.panel.backlight_lock);liuqin_cabc_first_frame(&c);mutex_unlock(&c.panel.backlight_lock);c.panel.backlight_pending=false;}
static ssize_t set(const char*s){return cabc_mode_store(&dev,NULL,s,strlen(s));}
static void finish(void){liuqin_cancel_dimming(&c,true);assert(!pthread_mutex_destroy(&c.panel.backlight_lock.m));assert(!pthread_mutex_destroy(&c.esd_lock.m));assert(!lock_depth);}
static void check_mode(int m){assert(c.cabc_requested==m&&c.cabc_applied==m&&c.cabc_cycle==c.panel.backlight_cookie&&!c.cabc_error);assert(transfers>=2&&!memcmp(trace[transfers-2],(u8[]){0xff,0x10},2)&&!memcmp(trace[transfers-1],(u8[]){0x55,m},2));}
static atomic_bool off_done;
static void *off_thread(void*p){off_actor=true;drm_panel_disable(p);atomic_store(&off_done,true);return NULL;}
static void *write_thread(void*p){assert(set(p)==(ssize_t)strlen(p));return NULL;}
int main(void){char buf[4096];
 reset();first_frame();assert(!transfers&&c.cabc_ready);cabc_mode_show(&dev,NULL,buf);assert(!strcmp(buf,"unchanged\n"));cabc_status_show(&dev,NULL,buf);assert(strstr(buf,"programmed=unknown")&&strstr(buf,"ready=1"));finish();
 const char *names[]={"off","ui","still","movie"};
 for(int m=0;m<4;m++){reset();first_frame();assert(set(names[m])==(ssize_t)strlen(names[m]));check_mode(m);int before=transfers;assert(set(names[m])==(ssize_t)strlen(names[m])&&transfers==before);cabc_status_show(&dev,NULL,buf);assert(strstr(buf,"error=0"));finish();}
 for(int m=0;m<4;m++){reset();payload_return=true;first_frame();assert(set(names[m])==(ssize_t)strlen(names[m]));check_mode(m);finish();}
 for(int m=0;m<4;m++){reset();oem_cabc=m;assert(!liuqin_cabc_init(&c));c.panel.backlight_pending=true;first_frame();check_mode(m);finish();}
 for(int m=-2;m<6;m++){reset();oem_cabc=m;assert(!liuqin_cabc_init(&c));assert(c.cabc_requested==((m>=0&&m<4)?m:-1));finish();}
 reset();c.oem_panel=NULL;group_calls=0;assert(!liuqin_cabc_init(&c)&&!group_calls);assert(set("ui")==-ENODEV);finish();
 reset();group_result=-ENOMEM;assert(liuqin_cabc_init(&c)==-ENOMEM);finish();
 const char *bad[]={"","-1","unchanged","4","UI","ui junk","ui\n\n","ui ","\n"};
 for(size_t i=0;i<ARRAY_SIZE(bad);i++){reset();first_frame();assert(set(bad[i])==-EINVAL&&!transfers&&c.cabc_requested==-1);finish();}
 reset();first_frame();assert(set("ui\n")==3);check_mode(1);finish();
 for(int state=0;state<7;state++){reset();first_frame();if(state==0)c.cabc_ready=false;if(state==1)c.esd_pending=true;if(state==2)c.panel.enabled=false;if(state==3)c.panel.backlight_pending=true;if(state==4)c.panel.backlight=NULL;if(state==5)bl.blank=true;if(state==6)c.dimming_stopping=true;ssize_t ret=set("movie");assert(ret==(state==6?-ENODEV:5)&&!transfers);if(state!=6)assert(c.cabc_requested==3);c.panel.backlight=&bl;finish();}
 reset();first_frame();bl.brightness=0;assert(set("off")==3);check_mode(0);finish();
 for(int kind=0;kind<2;kind++)for(int at=0;at<2;at++){reset();first_frame();if(!kind)fail_at=at;if(kind==1)short_at=at;assert(set("ui")==(!kind?-ETIMEDOUT:-EIO)&&transfers==at+1&&c.cabc_requested==1&&c.cabc_applied==-1&&c.cabc_error<0);fail_at=short_at=-1;assert(set("ui")==2);check_mode(1);finish();}
 for(int kind=0;kind<2;kind++){reset();first_frame();native_dma_result=kind?1:-ETIMEDOUT;assert(set("ui")== (kind?-EIO:-ETIMEDOUT)&&c.cabc_applied==-1);native_dma_result=0;assert(set("ui")==2);check_mode(1);finish();}
 for(int kind=0;kind<2;kind++){reset();first_frame();if(kind)ops.transfer=NULL;else host.ops=NULL;assert(set("ui")==-EOPNOTSUPP&&c.cabc_applied==-1);finish();}
 reset();first_frame();inject_fault=true;assert(set("still")==-EAGAIN&&c.cabc_applied==-1&&c.cabc_error==-EAGAIN);finish();
 reset();c.cabc_requested=2;fail_at=0;first_frame();assert(errors==1&&c.cabc_ready&&c.cabc_error==-ETIMEDOUT&&c.cabc_applied==-1);finish();
 reset();c.cabc_requested=2;c.esd_pending=true;first_frame();assert(!transfers&&!c.cabc_ready);finish();
 reset();first_frame();assert(set("ui")==2);check_mode(1);drm_panel_disable(&c.panel);assert(!c.cabc_ready&&c.cabc_applied==-1&&!c.cabc_cycle&&bl.blank);assert(set("movie")==5&&transfers==2);c.panel.enabled=true;backlight_enable(&bl);c.panel.backlight_cookie++;c.panel.backlight_pending=true;first_frame();check_mode(3);assert(transfers==4);finish();
 reset();first_frame();c.dsi[1]=NULL;d.channel=1;assert(set("still")==5);check_mode(2);finish();
 reset();first_frame();hold_tx=true;assert(!pthread_barrier_init(&tx_entered,NULL,2)&&!pthread_barrier_init(&tx_release,NULL,2)&&!pthread_barrier_init(&off_attempt,NULL,2));pthread_t writer,off;atomic_init(&off_done,false);assert(!pthread_create(&writer,NULL,write_thread,"movie"));pthread_barrier_wait(&tx_entered);assert(!pthread_create(&off,NULL,off_thread,&c.panel));pthread_barrier_wait(&off_attempt);assert(!atomic_load(&off_done));pthread_barrier_wait(&tx_release);assert(!pthread_join(writer,NULL)&&!pthread_join(off,NULL));assert(atomic_load(&off_done)&&!c.panel.enabled&&bl.blank&&transfers==2&&c.cabc_applied==-1);pthread_barrier_destroy(&tx_entered);pthread_barrier_destroy(&tx_release);pthread_barrier_destroy(&off_attempt);finish();

 /* A stopped old fault stays pending until the real first-frame ESD start. */
 reset();first_frame();assert(set("ui")==2);c.esd_pending=true;drm_panel_disable(&c.panel);assert(c.esd_pending&&!c.cabc_ready);c.panel.enabled=true;backlight_enable(&bl);c.panel.backlight_cookie++;c.panel.backlight_pending=true;mutex_lock(&c.panel.backlight_lock);assert(!nt36532_post_backlight_enable(&c.panel));mutex_unlock(&c.panel.backlight_lock);c.panel.backlight_pending=false;assert(!c.esd_pending&&c.esd_enabled&&c.esd_cycle==c.panel.backlight_cookie);check_mode(1);finish();
 /* A new fault during mode transfer must not be cleared by a later ESD start. */
 reset();c.cabc_requested=1;c.esd_pending=true;c.esd_enabled=false;c.panel.backlight_cookie=2;c.panel.backlight_pending=true;inject_fault=true;mutex_lock(&c.panel.backlight_lock);assert(!nt36532_post_backlight_enable(&c.panel));mutex_unlock(&c.panel.backlight_lock);c.panel.backlight_pending=false;assert(c.esd_pending&&c.cabc_applied==-1&&c.cabc_error==-EAGAIN&&errors==1);finish();
 printf("%d\n",cases);return 0;}
'''
    with tempfile.TemporaryDirectory(prefix='liuqin-cabc-test-') as tmp:
        src, exe = Path(tmp)/'test.c', Path(tmp)/'test'
        src.write_text(prefix+cabc+'\n'+bodies+'\n'+function(core, 'drm_panel_disable')+'\n'+suffix)
        subprocess.run([a.cc, '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-Wno-sign-compare', '-Wno-misleading-indentation', '-fsanitize=undefined', '-fno-sanitize-recover=all', '-pthread', str(src), '-o', str(exe)], check=True)
        cases = int(subprocess.check_output([str(exe)], text=True))
    result = dict(all_passed=True, cases=cases, source_sha256=hashlib.sha256(s.encode()).hexdigest(),
                  actual_candidate_c_executed=True, exact_rom_packets=True, concurrent_blanking_checked=True,
                  actual_msm_packet_length_path_executed=True, payload_and_padded_counts_accepted=True,
                  actual_esd_lifecycle_and_post_backlight_callback_executed=True, old_fault_cleared_before_replay=True, fresh_fault_during_replay_preserved=True,
                  policy_failure_does_not_block_first_frame=True, replay_after_suspend_and_recovery=True,
                  default_sends_no_extra_commands=True, existing_on_off_dimming_esd_functions_unchanged=True)
    a.report.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
