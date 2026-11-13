#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the A7xx power-up list builder and check its firmware ABI boundaries."""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
args = parser.parse_args()
spec = importlib.util.spec_from_file_location('folio', Path(__file__).with_name('nanosic-folio.py'))
folio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(folio)
base = args.source / 'drivers/gpu/drm/msm/adreno'
header = (base / 'a6xx_gpu.h').read_text()
lock = re.search(r'struct cpu_gpu_lock \{.*?^\};', header, re.S | re.M)[0]
function = folio.function((base / 'a6xx_gpu.c').read_text(), 'a7xx_patch_pwrup_reglist')
stub = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
#define BIT(n) (1U<<(n))
#define PIPE_BR 1
#define PIPE_BV 2
#define PIPE_NONE 0
#define REG_A6XX_TPL1_NC_MODE_CNTL 0xb602
#define REG_A6XX_SP_NC_MODE_CNTL 0xae02
#define REG_A7XX_CP_APERTURE_CNTL_HOST 0x79f
#define A7XX_CP_APERTURE_CNTL_HOST_PIPE(p) ((p)<<12)
struct adreno_reglist_list {const u32 *regs; int count;};
struct pipe_reg {u32 pipe, offset;};
struct adreno_reglist_pipe_list {const struct pipe_reg *regs; int count;};
struct a6xx_info {const struct adreno_reglist_list *ifpc_reglist, *pwrup_reglist;
const struct adreno_reglist_pipe_list *dyn_pwrup_reglist;};
struct adreno_info {const struct a6xx_info *a6xx;};
struct msm_gpu {int unused;};
struct adreno_gpu {struct msm_gpu base; const struct adreno_info *info; bool a730;};
struct a6xx_gpu {struct adreno_gpu base;void *pwrup_reglist_ptr;u32 dynamic_sel_reglist_offset;};
static struct adreno_gpu *to_adreno_gpu(struct msm_gpu *gpu){return (void*)gpu;}
static struct a6xx_gpu *to_a6xx_gpu(struct adreno_gpu *gpu){return (void*)gpu;}
static bool adreno_is_a730(struct adreno_gpu *gpu){return gpu->a730;}
static u32 aperture;
static u32 gpu_read(struct msm_gpu *gpu,u32 reg){(void)gpu;return reg ^ aperture ^ 0xa5a00000;}
static void gpu_write(struct msm_gpu *gpu,u32 reg,u32 value){(void)gpu;assert(reg==REG_A7XX_CP_APERTURE_CNTL_HOST);aperture=value;}
'''
cases = r'''
static void run(bool a730,bool ifpc,bool dynamic){
 const u32 static_regs[]={0x22,0x33,0x44};
 const u32 ifpc_regs[]={0x55,0x66};
 const struct pipe_reg dyn_regs[]={{BIT(PIPE_BR)|BIT(PIPE_BV),0x77},{BIT(PIPE_BR),0x88}};
 const struct adreno_reglist_list s={static_regs,3},i={ifpc_regs,2};
 const struct adreno_reglist_pipe_list d={dyn_regs,2};
 const struct a6xx_info config={ifpc?&i:NULL,&s,dynamic?&d:NULL};
 const struct adreno_info info={&config};
 uint64_t storage[128];memset(storage,0xcc,sizeof(storage));
 struct cpu_gpu_lock *lock=(void*)storage;memset(lock,0,sizeof(*lock));
 struct a6xx_gpu gpu={.base={.info=&info,.a730=a730},.pwrup_reglist_ptr=lock};
 lock->gpu_req=lock->cpu_req=lock->turn=123;aperture=0;
 a7xx_patch_pwrup_reglist(&gpu.base.base);
 assert(!lock->gpu_req&&!lock->cpu_req&&!lock->turn&&!aperture);
 assert(lock->ifpc_list_len==(ifpc?2:0));
 assert(lock->preemption_list_len==(a730?5:3));
 assert(lock->dynamic_list_len==(dynamic?3:0));
 assert(gpu.dynamic_sel_reglist_offset==lock->dynamic_list_len);
 u32 *v=(void*)lock->regs;size_t n=0;
 #define PAIR(reg) do{assert(v[n++]==(reg));assert(v[n++]==((reg)^0xa5a00000));}while(0)
 if(ifpc){PAIR(0x55);PAIR(0x66);}PAIR(0x22);PAIR(0x33);PAIR(0x44);
 if(a730){PAIR(0xb602);PAIR(0xae02);}
 if(dynamic){
  const u32 expected[]={0x1000,0x77,0xa5a01077,0x1000,0x88,0xa5a01088,0x2000,0x77,0xa5a02077};
  assert(!memcmp(v+n,expected,sizeof(expected)));n+=9;
 }
 assert(n==2U*(lock->ifpc_list_len+lock->preemption_list_len)+3U*lock->dynamic_list_len);
 for(size_t j=sizeof(*lock)+4*n;j<sizeof(storage);j++)assert(((unsigned char*)storage)[j]==0xcc);
}
int main(void){
 _Static_assert(offsetof(struct cpu_gpu_lock,regs)==16,"firmware header size");
 _Static_assert(offsetof(struct cpu_gpu_lock,preemption_list_len)==13,"firmware pair-count offset");
 for(int a=0;a<2;a++)for(int i=0;i<2;i++)for(int d=0;d<2;d++)run(a,i,d);
 puts("PASS: 8 actual power-up list ABI/layout cases, including A730/non-A730 and dynamic triplet boundaries");
}
'''
with tempfile.TemporaryDirectory(prefix='gpu-reglist-') as temp:
    c = Path(temp) / 'test.c'
    c.write_text(stub + lock + '\n' + function + '\n' + cases)
    binary = c.with_suffix('')
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined',
                    '-fno-sanitize-recover=all', str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)
