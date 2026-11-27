#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the real MSM submit function with PM and hardware-init failures."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_test import function, run_c, write_report

STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stdio.h>
struct device { int unused; };
struct platform_device { struct device dev; };
struct drm_device { struct device *dev; };
struct msm_ringbuffer { int submit_lock, submits, cur_ctx_seqno, fctx; };
struct context { int seqno; };
struct queue { struct context *ctx; };
struct fence { int seqno; };
struct msm_gem_submit { struct msm_ringbuffer *ring; struct fence *hw_fence;
 struct queue *queue; int seqno,node; };
struct msm_gpu;
struct funcs { void (*submit)(struct msm_gpu *, struct msm_gem_submit *); };
struct msm_gpu { struct platform_device *pdev; struct drm_device *dev;
 struct funcs *funcs; int lock, active_lock, active_submits; };
static int pm_error,init_error,refs,gets,put_count,syncputs,hw_calls,submits,queued,timers,init_calls,fence_inits;
#define WARN_ON(x) assert(!(x))
#define DRM_DEV_ERROR(dev,fmt,...) do { (void)(dev); if(0) printf(fmt, ##__VA_ARGS__); } while(0)
#define spin_lock_irqsave(lock,flags) do { (void)(lock); (flags)=0; } while(0)
#define spin_unlock_irqrestore(lock,flags) do { (void)(lock); (void)(flags); } while(0)
static int mutex_is_locked(int *lock) { return *lock; }
static void mutex_lock(int *lock) { (void)lock; }
static void mutex_unlock(int *lock) { (void)lock; }
static int pm_runtime_resume_and_get(struct device *dev) { (void)dev; gets++; if(pm_error)return pm_error;refs++;return 0; }
static void pm_runtime_get(struct device *dev) { (void)dev;refs++; }
static void pm_runtime_put(struct device *dev) { (void)dev;refs--;put_count++; }
static void pm_runtime_put_sync(struct device *dev) { (void)dev;refs--;syncputs++; }
static int msm_gpu_hw_init(struct msm_gpu *gpu) { (void)gpu;init_calls++;return init_error; }
static void msm_fence_init(struct fence *f, int ctx) { (void)ctx; assert(!pm_error&&!init_error); fence_inits++; f->seqno=42; }
static void msm_gem_submit_get(struct msm_gem_submit *s) { (void)s;submits++; }
static void list_add_tail(int *n,int *h) { (void)n;(void)h;queued++; }
static void msm_devfreq_active(struct msm_gpu *g) { (void)g; }
static void hangcheck_timer_reset(struct msm_gpu *g) { (void)g;timers++; }
static void hw_submit(struct msm_gpu *g,struct msm_gem_submit *s) { (void)g;(void)s;assert(!pm_error&&!init_error);hw_calls++; }
'''
CASES = r'''
static void check(int pm,int init,int active) {
 struct device d={0};struct drm_device drm={&d};struct platform_device pd={0};
 struct funcs f={hw_submit};struct msm_gpu gpu={.pdev=&pd,.dev=&drm,.funcs=&f,.lock=1,.active_submits=active};
 struct msm_ringbuffer ring={0};struct context ctx={7};struct queue q={&ctx};struct fence fence={42};
 struct msm_gem_submit s={.ring=&ring,.hw_fence=&fence,.queue=&q};
 pm_error=pm;init_error=init;refs=gets=put_count=syncputs=hw_calls=submits=queued=timers=init_calls=fence_inits=0;
 int ret=msm_gpu_submit(&gpu,&s);
 assert(ret==(pm?pm:init));assert(gets==1);
 if(pm||init) {
  assert(refs==0&&put_count==0&&hw_calls==0&&submits==0&&queued==0&&timers==0&&fence_inits==0);
  assert(gpu.active_submits==active&&ring.cur_ctx_seqno==0);
  assert(init_calls==(!pm)&&syncputs==(!pm));
 } else {
  assert(hw_calls==1&&submits==1&&queued==1&&timers==1&&put_count==1&&!syncputs&&fence_inits==1);
  assert(refs==(!active)&&gpu.active_submits==active+1&&ring.cur_ctx_seqno==7&&s.seqno==42);
 }
}
int main(void) {
 check(-EIO,0,0);check(-ETIMEDOUT,0,0);check(0,-ETIMEDOUT,0);
 check(0,-EIO,0);check(0,0,0);check(0,0,2);
 puts("PASS: PM/init failures acquire no submit references and emit no GPU commands; successful idle/active paths preserved");
}
'''

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path, required=True)
    p.add_argument('--report', type=Path)
    a = p.parse_args()
    source = (a.kernel/'drivers/gpu/drm/msm/msm_gpu.c').read_text()
    output = run_c(STUBS + function(source, 'msm_gpu_submit') + CASES)
    write_report({'submit_error': output.strip()}, a.report)

if __name__ == '__main__':
    main()
