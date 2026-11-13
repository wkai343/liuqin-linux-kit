#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute bonded-video CTL kickoff and enable code from an assembled kernel."""
import argparse
from pathlib import Path
import subprocess

from kernel_test import function, run_c, write_report


STUBS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <limits.h>
#include <stddef.h>
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define ENC_ROLE_SOLO 0
#define ENC_ROLE_MASTER 1
#define ENC_ROLE_SLAVE 2
#define DPU_ENC_DISABLED 0
#define DPU_ENC_ENABLING 1
#define DPU_ENC_ENABLED 2
#define DRM_MODE_ENCODER_VIRTUAL 5
#define CDM_CDWN_OUTPUT_HDMI 0
#define CTL_0 1
#define WARN_ON(x) (x)
#define DPU_DEBUG(...) ((void)0)
#define DPU_DEBUG_VIDENC(...) ((void)0)
#define DPU_ERROR(...) assert(0)
#define trace_dpu_enc_trigger_flush(...) ((void)0)
#define spin_lock_irqsave(lock,flags) ((void)0)
#define spin_unlock_irqrestore(lock,flags) ((void)0)
#define atomic_set(p,v) (*(p)=(v))
#define set_bit(i,p) (*(p)|=BIT(i))
struct dpu_encoder_virt;
struct drm_encoder {int encoder_type;};
struct msm_format {int dummy;};
struct dpu_hw_ctl {
 unsigned int idx, pending, intfs, submitted, flushes;
 struct {void (*trigger_flush)(struct dpu_hw_ctl *);
  u32 (*get_pending_flush)(struct dpu_hw_ctl *);
  void (*update_pending_flush)(struct dpu_hw_ctl *,u32);
  void (*update_pending_flush_intf)(struct dpu_hw_ctl *,u32);
  void (*update_pending_flush_merge_3d)(struct dpu_hw_ctl *,u32);
  void (*update_pending_flush_cdm)(struct dpu_hw_ctl *,u32);
  void (*update_pending_flush_periph)(struct dpu_hw_ctl *,u32);
 } ops;
};
struct dpu_hw_intf {u32 idx;struct {void (*enable_timing)(void);} ops;};
struct dpu_hw_wb {u32 idx;};
struct dpu_hw_cdm {u32 idx;};
struct merge {u32 idx;};
struct dpu_hw_pingpong {struct merge *merge_3d;};
struct version {int core_major_ver;};
struct catalog {struct version *mdss_ver;};
struct dpu_kms {int base;struct catalog *catalog;};
struct dpu_encoder_phys {
 struct drm_encoder *parent;struct dpu_hw_ctl *hw_ctl;
 struct dpu_hw_pingpong *hw_pp;struct dpu_hw_intf *hw_intf;
 struct dpu_hw_wb *hw_wb;struct dpu_hw_cdm *hw_cdm;struct dpu_kms *dpu_kms;
 int enable_state,split_role,underrun_cnt,pending;
 struct {bool (*needs_single_flush)(struct dpu_encoder_phys *);} ops;
};
struct dpu_encoder_virt {
 struct drm_encoder base;struct dpu_encoder_phys *phys_encs[2],*cur_master;
 unsigned int num_phys_encs;int enc_spinlock;u32 cwb_mask;unsigned long frame_busy_mask[1];
};
static unsigned starts;
static struct dpu_encoder_virt *to_dpu_encoder_virt(struct drm_encoder *e){return (void*)e;}
static int dpu_encoder_phys_inc_pending(struct dpu_encoder_phys *p){return ++p->pending;}
static void _dpu_encoder_trigger_start(struct dpu_encoder_phys *p){assert(p);starts++;}
static bool dpu_encoder_phys_vid_is_master(struct dpu_encoder_phys *p){return p->split_role!=ENC_ROLE_SLAVE;}
static u32 dpu_encoder_get_drm_fmt(struct dpu_encoder_phys *p){return 0;}
static const struct msm_format *mdp_get_format(int *k,u32 f,int m){static struct msm_format fmt;return &fmt;}
static u32 dpu_encoder_helper_get_3d_blend_mode(struct dpu_encoder_phys *p){return 0;}
static void dpu_encoder_helper_split_config(struct dpu_encoder_phys *p,u32 i){}
static void dpu_encoder_helper_phys_setup_cdm(struct dpu_encoder_phys *p,const struct msm_format *f,int t){}
static void dpu_encoder_phys_vid_setup_timing_engine(struct dpu_encoder_phys *p){}
static bool dpu_encoder_needs_periph_flush(struct dpu_encoder_phys *p){return false;}
static void timing(void){}
static void flush(struct dpu_hw_ctl *c){c->submitted=c->pending;c->flushes++;}
static u32 pending(struct dpu_hw_ctl *c){return c->pending;}
static void update(struct dpu_hw_ctl *c,u32 bits){c->pending|=bits;}
static void intf(struct dpu_hw_ctl *c,u32 i){c->intfs|=BIT(i);c->pending|=BIT(31);}
'''

CASES = r'''
int main(void) {
 for(int ver=4;ver<=10;ver++)for(int bonded=0;bonded<=1;bonded++) {
  struct version v={ver};struct catalog cat={&v};struct dpu_kms kms={.catalog=&cat};
  struct dpu_hw_ctl ctl[2]={0};
  for(int i=0;i<2;i++){
   ctl[i].idx=i+1;ctl[i].pending=BIT(i);
   ctl[i].ops.trigger_flush=flush;ctl[i].ops.get_pending_flush=pending;
   ctl[i].ops.update_pending_flush=update;ctl[i].ops.update_pending_flush_intf=intf;
  }
  struct dpu_hw_pingpong pp[2]={0};
  struct dpu_hw_intf intfs[2]={{.idx=1,.ops={timing}},{.idx=2,.ops={timing}}};
  struct dpu_encoder_virt enc={.num_phys_encs=bonded?2:1};
  struct dpu_encoder_phys phys[2]={0};
  for(unsigned int i=0;i<enc.num_phys_encs;i++){
   phys[i].parent=&enc.base;phys[i].hw_ctl=&ctl[(ver<5)?i:0];
   phys[i].hw_pp=&pp[i];phys[i].hw_intf=&intfs[i];phys[i].dpu_kms=&kms;
   phys[i].split_role=bonded?(i?ENC_ROLE_SLAVE:ENC_ROLE_MASTER):ENC_ROLE_SOLO;
   phys[i].ops.needs_single_flush=dpu_encoder_phys_vid_needs_single_flush;
   enc.phys_encs[i]=&phys[i];
   dpu_encoder_phys_vid_enable(&phys[i]);
   assert(phys[i].enable_state==DPU_ENC_ENABLING);
  }
  enc.cur_master=&phys[0];starts=0;
  _dpu_encoder_kickoff_phys(&enc);
  /* One hardware transaction, with one master completion to wait for. */
  if(ctl[0].flushes!=1 || ctl[1].flushes!=0 || starts!=1)return 20;
  assert(phys[0].pending==1 && phys[1].pending==0);
  assert(enc.frame_busy_mask[0]==1);
  assert(ctl[0].intfs==(BIT(1)|((bonded&&ver>=5)?BIT(2):0)));
  assert(!ctl[1].intfs);
  assert(ctl[0].submitted==(BIT(0)|BIT(31)|((bonded&&ver<5)?BIT(1):0)));
  /* Disabled encoders must not contribute another submission. */
  for(unsigned int i=0;i<enc.num_phys_encs;i++)phys[i].enable_state=DPU_ENC_DISABLED;
  _dpu_encoder_kickoff_phys(&enc);
  assert(ctl[0].flushes==1 && ctl[1].flushes==0);
 }
 return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = args.source / 'drivers/gpu/drm/msm/disp/dpu1'
    vid = (root / 'dpu_encoder_phys_vid.c').read_text()
    encoder = (root / 'dpu_encoder.c').read_text()
    single = function(vid, 'dpu_encoder_phys_vid_needs_single_flush')
    code = '\n'.join([function(vid, 'dpu_encoder_phys_vid_enable'),
                      function(encoder, '_dpu_encoder_trigger_flush'),
                      function(encoder, '_dpu_encoder_kickoff_phys')])
    flags = ('-Wno-unused-parameter', '-Wno-unused-variable', '-Wno-unused-but-set-variable')
    run_c(STUBS + single + code + CASES, flags=flags)
    old = single.replace('return phys_enc->split_role != ENC_ROLE_SOLO;',
                         'return phys_enc->dpu_kms->catalog->mdss_ver->core_major_ver < 5 && '
                         'phys_enc->split_role != ENC_ROLE_SOLO;')
    assert old != single
    try:
        run_c(STUBS + old + code + CASES, flags=flags)
    except subprocess.CalledProcessError as error:
        assert error.returncode == 20, error
    else:
        raise AssertionError('Old active-CTL exclusion did not reproduce duplicate flush')
    write_report({'actual_kernel_functions_executed': True, 'topology_cases': 14,
                  'one_flush_and_one_master_completion': True,
                  'both_active_ctl_interfaces_flushed': True,
                  'solo_and_legacy_behavior_preserved': True,
                  'disabled_encoders_skipped': True,
                  'old_callback_reproduces_duplicate_flush': True}, args.report)


if __name__ == '__main__':
    main()
