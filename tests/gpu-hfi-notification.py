#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the actual GMU fault IRQ against concurrent polled HFI replies."""
import argparse
from pathlib import Path
from kernel_test import function, run_c, write_report

STUBS = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
typedef uint32_t u32;
#define IRQ_HANDLED 1
#define REG_A6XX_GMU_GMU2HOST_INTR_INFO 1
#define REG_A6XX_GMU_GMU2HOST_INTR_CLR 2
#define A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ 1u
#define A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT 4u
#define dev_err_ratelimited(...) ((void)0)
#define a6xx_hfi_trace(...) ((void)0)
struct a6xx_gmu { u32 pending, observed, cleared; unsigned faults; };
static u32 gmu_read(struct a6xx_gmu *g, unsigned reg) {
 assert(reg == REG_A6XX_GMU_GMU2HOST_INTR_INFO);
 g->observed = g->pending;
 return g->pending;
}
static void gmu_write(struct a6xx_gmu *g, unsigned reg, u32 bits) {
 assert(reg == REG_A6XX_GMU_GMU2HOST_INTR_CLR);
 g->cleared |= bits; g->pending &= ~bits;
}
static void a6xx_gmu_fault(struct a6xx_gmu *g) { g->faults++; }
'''
CASES = r'''
int main(void) {
 /* A spurious IRQ can observe a reply even though only FAULT is unmasked. */
 for (u32 pending=0; pending<8; pending++) {
  struct a6xx_gmu g = {.pending=pending};
  assert(a6xx_hfi_irq(0,&g) == IRQ_HANDLED);
  assert(g.observed == pending);
  assert((g.pending & 1u) == (pending & 1u));
  assert((g.pending & 2u) == (pending & 2u));
  assert(!(g.pending & 4u));
  assert(g.cleared == (pending & 4u));
  assert(g.faults == !!(pending & 4u));
 }
 puts("PASS: 8 IRQ status combinations preserve polled replies and report firmware faults");
}
'''

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path, required=True)
    p.add_argument('--report', type=Path)
    a = p.parse_args()
    source = (a.kernel/'drivers/gpu/drm/msm/adreno/a6xx_gmu.c').read_text()
    handler = function(source.replace('static irqreturn_t a6xx_hfi_irq',
                                     'static int a6xx_hfi_irq'), 'a6xx_hfi_irq')
    output = run_c(STUBS + handler + CASES, flags=('-Wno-unused-parameter',))
    write_report({'hfi_irq_ownership': output.strip()}, a.report)

if __name__ == '__main__':
    main()
