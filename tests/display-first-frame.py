#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise actual DRM/DPU deferred-backlight functions, including cancel races."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import tempfile


def function(text, name):
    match = re.search(r'^(?:static )?(?:unsigned int|void|bool)\s+' + name + r'\([^;{]*\)\s*\{', text, re.M)
    assert match, name
    end = match.end()
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    panel = (args.source / 'drivers/gpu/drm/drm_panel.c').read_text()
    bridge = (args.source / 'drivers/gpu/drm/bridge/panel.c').read_text()
    kms = (args.source / 'drivers/gpu/drm/msm/disp/dpu1/dpu_kms.c').read_text()
    nt = (args.source / 'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text()
    old_nt = (args.baseline / 'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text()
    # The only board-driver changes are the opt-in parameter and its probe gate.
    restored = re.sub(r'static bool first_frame_backlight = true;\nmodule_param.*?\n\n(?=struct nt36532)', '', nt, count=1, flags=re.S)
    restored = restored.replace('''\tif (ctx->desc == &liuqin_desc) {
\t\tliuqin_select_oem_panel(ctx);
\t\tctx->panel.backlight_after_first_frame =
\t\t\tfirst_frame_backlight && ctx->oem_panel;
\t}
''', '''\tif (ctx->desc == &liuqin_desc)
\t\tliuqin_select_oem_panel(ctx);
''')
    assert restored == old_nt, 'panel commands/power/reset changed'
    wait = function(kms, 'dpu_kms_wait_for_commit_done')
    assert 'if (!ret)\n\t\t\tdpu_kms_complete_panel_first_frame(encoder);' in wait
    assert wait.index('dpu_encoder_wait_for_commit_done(encoder)') < wait.index('dpu_kms_complete_panel_first_frame(encoder)')
    assert 'wait_for' not in function(panel, 'drm_panel_enable')
    assert function(bridge, 'panel_bridge_atomic_enable') == function(
        (args.baseline / 'drivers/gpu/drm/bridge/panel.c').read_text(), 'panel_bridge_atomic_enable')
    helpers = '\n'.join(function(panel, name) for name in [
        'drm_panel_enable', 'drm_panel_disable', 'drm_panel_get_first_frame_cookie',
        'drm_panel_complete_first_frame'])
    helpers += '\n' + '\n'.join(function(bridge, name) for name in [
        'drm_bridge_is_panel', 'drm_panel_bridge_get_first_frame_cookie',
        'drm_panel_bridge_complete_first_frame'])
    helpers += '\n' + function(kms, 'dpu_kms_complete_panel_first_frame')
    stubs = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define dev_info(dev,...) ((void)(dev))
#define dev_warn(dev,...) ((void)(dev))
#define dev_err_ratelimited(dev,...) ((void)(dev))
#define dev_warn_ratelimited(dev,...) ((void)(dev))
#define DRM_DEV_INFO(dev,...) ((void)(dev))
struct mutex { pthread_mutex_t value; };
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->value));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->value));}
struct backlight_device {bool on;int enables,disables,error;};
struct drm_panel;
struct drm_panel_funcs {int (*enable)(struct drm_panel *);int (*disable)(struct drm_panel *);};
struct drm_panel {
    void *dev;const struct drm_panel_funcs *funcs;struct backlight_device *backlight;
    bool enabled,backlight_after_first_frame,backlight_pending;
    unsigned int backlight_cookie;struct mutex backlight_lock;
};
static int enable_result,disable_result,enable_calls,disable_calls;
static bool hold_backlight;
static pthread_barrier_t inside_update,finish_update;
static atomic_bool disable_started;
static void barrier(pthread_barrier_t *b){int r=pthread_barrier_wait(b);assert(!r || r==PTHREAD_BARRIER_SERIAL_THREAD);}
static int backlight_enable(struct backlight_device *b){
    if(!b)return 0;
    if(hold_backlight){barrier(&inside_update);barrier(&finish_update);}
    b->enables++;if(b->error)return b->error;b->on=true;return 0;
}
static int backlight_disable(struct backlight_device *b){if(b){b->disables++;b->on=false;}return 0;}
static int driver_enable(struct drm_panel *p){(void)p;enable_calls++;return enable_result;}
static int driver_disable(struct drm_panel *p){(void)p;disable_calls++;return disable_result;}
static const struct drm_panel_funcs funcs={driver_enable,driver_disable};
static const int panel_bridge_bridge_funcs,other_bridge_funcs;
struct drm_bridge {const void *funcs;struct drm_bridge *next;};
struct panel_bridge {struct drm_bridge bridge;struct drm_panel *panel;};
static struct panel_bridge *drm_bridge_to_panel_bridge(struct drm_bridge *b){return (struct panel_bridge *)b;}
struct drm_device {void *dev;};
struct drm_encoder {struct drm_device *dev;struct drm_bridge *first;};
#define drm_for_each_bridge_in_chain(e,b) for((b)=(e)->first;(b);(b)=(b)->next)
static int tx_result,tx_calls;
static bool replace_cycle_during_tx;
static struct drm_panel *tx_panel;
void drm_panel_enable(struct drm_panel *p);
void drm_panel_disable(struct drm_panel *p);
static int dpu_encoder_wait_for_tx_complete(struct drm_encoder *e){
    (void)e;tx_calls++;
    if(replace_cycle_during_tx){
        replace_cycle_during_tx=false;drm_panel_disable(tx_panel);drm_panel_enable(tx_panel);
    }
    return tx_result;
}
'''
    cases = r'''
static struct drm_panel p;
static struct backlight_device bl;
static struct drm_device dev;
static struct panel_bridge pb;
static struct drm_bridge other;
static struct drm_encoder encoder;
static int cases;
static void setup(bool deferred){
    memset(&p,0,sizeof(p));memset(&bl,0,sizeof(bl));
    assert(!pthread_mutex_init(&p.backlight_lock.value,NULL));
    p.backlight=&bl;p.funcs=&funcs;p.backlight_after_first_frame=deferred;
    enable_calls=disable_calls=enable_result=disable_result=tx_calls=tx_result=0;
    hold_backlight=replace_cycle_during_tx=false;tx_panel=&p;
    pb=(struct panel_bridge){.bridge={.funcs=&panel_bridge_bridge_funcs},.panel=&p};
    other=(struct drm_bridge){.funcs=&other_bridge_funcs,.next=&pb.bridge};
    encoder=(struct drm_encoder){.dev=&dev,.first=&other};
    cases++;
}
static void finish(void){assert(!pthread_mutex_destroy(&p.backlight_lock.value));}
static void *complete_thread(void *arg){drm_panel_complete_first_frame(&p,*(unsigned int *)arg);return NULL;}
static void *disable_thread(void *arg){(void)arg;atomic_store(&disable_started,true);drm_panel_disable(&p);return NULL;}
int main(void){
    unsigned int first,second;
    setup(true);assert(!drm_panel_get_first_frame_cookie(NULL));
    drm_panel_complete_first_frame(NULL,1);drm_panel_enable(NULL);drm_panel_disable(NULL);
    assert(!drm_panel_get_first_frame_cookie(&p));dpu_kms_complete_panel_first_frame(&encoder);
    assert(!tx_calls && !bl.enables);finish();

    setup(false);drm_panel_enable(&p);assert(p.enabled && bl.on && bl.enables==1);
    assert(!drm_panel_get_first_frame_cookie(&p));dpu_kms_complete_panel_first_frame(&encoder);
    assert(!tx_calls);drm_panel_disable(&p);assert(!p.enabled && !bl.on && disable_calls==1);finish();

    setup(true);drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);
    assert(first && p.enabled && enable_calls==1 && !bl.on && !bl.enables);
    dpu_kms_complete_panel_first_frame(&encoder);assert(bl.on && bl.enables==1 && tx_calls==1);
    assert(!drm_panel_get_first_frame_cookie(&p));dpu_kms_complete_panel_first_frame(&encoder);
    assert(bl.enables==1 && tx_calls==1);finish();

    setup(true);drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);
    drm_panel_enable(&p);assert(drm_panel_get_first_frame_cookie(&p)==first && enable_calls==1);finish();

    setup(true);enable_result=-EIO;drm_panel_enable(&p);
    assert(!p.enabled && !drm_panel_get_first_frame_cookie(&p) && !bl.enables);
    dpu_kms_complete_panel_first_frame(&encoder);assert(!tx_calls);finish();

    setup(true);drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);
    drm_panel_disable(&p);assert(disable_calls==1 && !p.enabled && !bl.on);
    drm_panel_complete_first_frame(&p,first);assert(!bl.enables);finish();

    setup(true);drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);
    drm_panel_disable(&p);drm_panel_enable(&p);second=drm_panel_get_first_frame_cookie(&p);
    assert(second && second!=first);drm_panel_complete_first_frame(&p,first);assert(!bl.on);
    drm_panel_complete_first_frame(&p,second);assert(bl.on && bl.enables==1);finish();

    setup(true);drm_panel_enable(&p);drm_panel_complete_first_frame(&p,0);
    assert(!bl.on && p.backlight_pending);finish();

    setup(true);drm_panel_enable(&p);tx_result=-ETIMEDOUT;dpu_kms_complete_panel_first_frame(&encoder);
    assert(!bl.on && p.backlight_pending && tx_calls==1);
    tx_result=0;dpu_kms_complete_panel_first_frame(&encoder);assert(bl.on && tx_calls==2);finish();

    setup(true);drm_panel_enable(&p);tx_result=-EWOULDBLOCK;dpu_kms_complete_panel_first_frame(&encoder);
    assert(!bl.on && p.backlight_pending && !bl.enables);finish();

    setup(true);drm_panel_enable(&p);bl.error=-EIO;dpu_kms_complete_panel_first_frame(&encoder);
    assert(!bl.on && p.backlight_pending && bl.enables==1);
    bl.error=0;dpu_kms_complete_panel_first_frame(&encoder);assert(bl.on && !p.backlight_pending && bl.enables==2);finish();

    setup(true);drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);
    disable_result=-EIO;drm_panel_disable(&p);assert(p.enabled && !p.backlight_pending);
    drm_panel_complete_first_frame(&p,first);assert(!bl.on);
    disable_result=0;drm_panel_disable(&p);assert(!p.enabled);finish();

    setup(true);drm_panel_enable(&p);replace_cycle_during_tx=true;
    dpu_kms_complete_panel_first_frame(&encoder);assert(!bl.on && p.backlight_pending);
    assert(p.backlight_cookie==2);dpu_kms_complete_panel_first_frame(&encoder);assert(bl.on);finish();

    setup(true);p.backlight_cookie=UINT_MAX;drm_panel_enable(&p);
    assert(drm_panel_get_first_frame_cookie(&p)==1);finish();

    setup(true);p.backlight=NULL;drm_panel_enable(&p);
    assert(p.enabled && !p.backlight_pending && !drm_panel_get_first_frame_cookie(&p));finish();

    setup(true);drm_panel_enable(&p);other.next=NULL;dpu_kms_complete_panel_first_frame(&encoder);
    drm_panel_bridge_complete_first_frame(&other,p.backlight_cookie);
    assert(!tx_calls && !bl.on);finish();

    setup(true);drm_panel_enable(&p);first=drm_panel_get_first_frame_cookie(&p);
    hold_backlight=true;atomic_store(&disable_started,false);
    assert(!pthread_barrier_init(&inside_update,NULL,2));assert(!pthread_barrier_init(&finish_update,NULL,2));
    pthread_t a,b;assert(!pthread_create(&a,NULL,complete_thread,&first));barrier(&inside_update);
    assert(!pthread_create(&b,NULL,disable_thread,NULL));
    while(!atomic_load(&disable_started)){sched_yield();}
    barrier(&finish_update);
    assert(!pthread_join(a,NULL));assert(!pthread_join(b,NULL));
    assert(!bl.on && !p.enabled && !p.backlight_pending && bl.enables==1 && bl.disables==1);
    assert(!pthread_barrier_destroy(&inside_update));assert(!pthread_barrier_destroy(&finish_update));finish();
    printf("%d\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='first-frame-tests-') as temp:
        root = Path(temp)
        path = root / 'test.c'
        path.write_text(stubs + helpers + cases)
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                        str(path), '-o', str(root / 'test')], check=True)
        count = int(subprocess.check_output([str(root / 'test')], text=True, timeout=20))
    result = {'runtime_cases': count, 'actual_kernel_functions_exercised': True,
              'concurrent_completion_and_disable_serialized': True,
              'stale_completions_rejected': True, 'timeout_and_disabled_encoder_stay_dark': True,
              'panel_callbacks_not_deferred': True, 'other_panels_immediate': True,
              'panel_commands_power_reset_unchanged': True, 'hardware_validation_required': True}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
