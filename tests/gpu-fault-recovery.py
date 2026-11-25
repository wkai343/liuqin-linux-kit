#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute actual GMU waits with successful, stalled and repeatedly racing dumps."""
import argparse
from pathlib import Path
from kernel_test import function, run_c, write_report

STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
typedef uint64_t ktime_t;
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define WARN_ON_ONCE(x) assert(!(x))
#define msecs_to_jiffies(x) (x)
#define DRM_DEV_ERROR(dev,fmt,...) do { (void)(dev); if (0) printf(fmt, ##__VA_ARGS__); } while (0)
#define REG_A6XX_GMU_HOST2GMU_INTR_SET 0
#define REG_A6XX_GMU_GMU2HOST_INTR_INFO 1
#define REG_A6XX_GMU_GMU2HOST_INTR_CLR 2
#define A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ 1
#define REG_A8XX_GPU_GMU_CX_GMU_RPMH_POWER_STATE 3
#define REG_A6XX_GPU_GMU_CX_GMU_RPMH_POWER_STATE 4
#define REG_A6XX_GPU_GMU_AO_GPU_CX_BUSY_STATUS 5
struct completion { bool done; };
struct adreno_gpu { struct completion fault_coredump_done; struct { bool needs_hw_init; } base; };
struct a6xx_gmu { bool legacy,hung; int dev,lock; };
struct a6xx_gpu { struct adreno_gpu base; struct a6xx_gmu gmu; };
enum a6xx_gmu_oob_state { GMU_OOB_GPU_SET, GMU_OOB_PERFCOUNTER_SET,
 GMU_OOB_BOOT_SLUMBER, GMU_OOB_DCVS_SET };
static const char *a6xx_hfi_msg_id[] = { "test" };
static unsigned polls, waits, clears, elapsed, response_poll;
static bool dump_finishes, new_fault, rgmu;
static bool tracing;
static unsigned traces;
static int traced_ret;
#define a6xx_hfi_trace(g,phase,id,seq,ret,us,info) \
 ((void)(g),(void)(phase),(void)(id),(void)(seq),(void)(ret),(void)(us),(void)(info))
static ktime_t ktime_get(void) { return elapsed * 1000; }
static uint64_t ktime_us_delta(ktime_t end, ktime_t start) { return end - start; }
static bool trace_msm_gmu_oob_enabled(void) { return tracing; }
static bool adreno_is_a8xx(struct adreno_gpu *g) { (void)g; return false; }
static void trace_msm_gmu_oob(u32 state, int ret, uint64_t us, u32 info,
                            u32 power, u32 busy, bool hung, bool needs_init,
                            unsigned long caller) {
 assert(state == GMU_OOB_GPU_SET && us == elapsed * 1000);
 assert(!info && !power && !busy && !hung && !needs_init && caller);
 traces++; traced_ret = ret;
}
static struct completion *pending;
static bool adreno_has_rgmu(struct adreno_gpu *g) { (void)g; return rgmu; }
static bool mutex_is_locked(int *lock) { return *lock; }
static bool completion_done(struct completion *c) { return c->done; }
static unsigned wait_for_completion_timeout(struct completion *c, unsigned timeout) {
 assert(timeout == 1000); assert(++waits == 1);
 if (!dump_finishes) { elapsed += timeout; return 0; }
 c->done = true; return 1;
}
static int poll_result(unsigned timeout) {
 if (new_fault && polls) pending->done = false;
 assert(++polls <= 2); elapsed += timeout / 1000;
 return polls == response_poll ? 0 : -ETIMEDOUT;
}
#define gmu_poll_timeout(g,r,val,cond,us,timeout) ((val)=0, (void)(val), (void)(g), (void)(r), poll_result(timeout))
static void gmu_write(struct a6xx_gmu *g, int reg, u32 value) {
 (void)g; (void)value; if (reg == REG_A6XX_GMU_GMU2HOST_INTR_CLR) clears++;
}
static u32 gmu_read(struct a6xx_gmu *g, int r) { (void)g; (void)r; return 0; }
'''
CASES = r'''
static void check(bool hfi, bool done, bool finishes, unsigned response,
                  bool repeated_fault, int expected, unsigned npolls, unsigned nwaits) {
 struct a6xx_gpu g = { .base.fault_coredump_done.done = done, .gmu.lock = 1 };
 polls=waits=clears=elapsed=traces=0; response_poll=response;
 pending=&g.base.fault_coredump_done;
 dump_finishes=finishes; new_fault=repeated_fault;
 int ret=hfi ? a6xx_hfi_wait_for_msg_interrupt(&g.gmu,0,1) : a6xx_gmu_set_oob(&g.gmu,GMU_OOB_GPU_SET);
 assert(ret==expected && polls==npolls && waits==nwaits);
 assert(elapsed <= (hfi ? 3000 : 1020));
 assert(clears == (!hfi || !ret ? 1u : 0u));
 assert(traces == (!hfi && tracing ? 1u : 0u));
 if (traces) assert(traced_ret == ret);
}
int main(void) {
 for(int enabled=0;enabled<2;enabled++) {
 tracing=enabled;
 for(int hfi=0;hfi<2;hfi++) {
  check(hfi,true,false,1,false,0,1,0); /* no fault; normal response */
  check(hfi,true,false,0,false,-ETIMEDOUT,1,0); /* no dump; timeout */
  check(hfi,false,false,1,false,0,1,0); /* response while dump pending */
  check(hfi,false,false,0,false,-ETIMEDOUT,1,1); /* lock cycle: dump cannot finish */
  check(hfi,false,true,2,false,0,2,1); /* completed dump unblocks GMU */
  check(hfi,false,true,0,false,-ETIMEDOUT,2,1); /* GMU still wedged */
  check(hfi,false,true,0,true,-ETIMEDOUT,2,1); /* next fault must not extend wait */
 }
 }
 puts("PASS: 28 OOB/HFI cases with tracing enabled and disabled");
}
'''

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel',required=True,type=Path)
    p.add_argument('--report',type=Path)
    a=p.parse_args()
    gpu=a.kernel/'drivers/gpu/drm/msm/adreno'
    gmu=(gpu/'a6xx_gmu.c').read_text();hfi=(gpu/'a6xx_hfi.c').read_text()
    table=gmu[gmu.index('struct a6xx_gmu_oob_bits {'):gmu.index('/* Trigger a OOB')]
    # Real functions, synthetic register polling and completion scheduling.
    funcs=function(gmu,'a6xx_gmu_set_oob')+'\n'+function(hfi,'a6xx_hfi_wait_for_msg_interrupt')
    code=STUBS+table+funcs+CASES
    output=run_c(code,flags=('-Wno-sign-compare','-fwrapv'))
    write_report({'gmu_waits':output.strip()},a.report)

if __name__=='__main__':main()
