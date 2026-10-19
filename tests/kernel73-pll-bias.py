#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check real PHY bias-vote code with mocked MMIO and nested clock lifetimes."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(text, name):
    m = re.search(r'^static (?:int|void)\s+' + name + r'\([^;{]*\)\s*\{', text, re.M)
    if not m:
        raise ValueError('missing function: ' + name)
    end, depth = m.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[m.start():end]


def block(text, name):
    f = function(text, name)
    begin = f.index('\tspin_lock_irqsave(&pll->pll_enable_lock, flags);')
    end = f.index('\tspin_unlock_irqrestore(&pll->pll_enable_lock, flags);', begin)
    return f[begin:end] + '\tspin_unlock_irqrestore(&pll->pll_enable_lock, flags);\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--report', type=Path)
    a = ap.parse_args()
    rel = 'drivers/gpu/drm/msm/dsi/phy/dsi_phy_7nm.c'
    current = (a.source / rel).read_text()
    old = (a.baseline / rel).read_text()
    for name in ['dsi_pll_enable_pll_bias', 'dsi_pll_disable_pll_bias']:
        assert function(current, name) == function(old, name), name
    normalized = current.replace('\t/* The initial bias vote must program the same mux as later votes. */\n', '')
    normalized = normalized.replace('\twritel(0xc0, phy->pll_base + REG_DSI_7nm_PHY_PLL_SYSTEM_MUXES);\n', '', 1)
    normalized = normalized.replace('\twritel(0, phy->pll_base + REG_DSI_7nm_PHY_PLL_SYSTEM_MUXES);\n', '', 1)
    assert normalized == old, 'unexpected change outside PHY mux initialization'
    code = r'''
#include <assert.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
#define __iomem
#define BIT(n) (1U << (n))
#define REG_DSI_7nm_PHY_CMN_CTRL_0 0
#define REG_DSI_7nm_PHY_PLL_SYSTEM_MUXES 0
#define DSI_7nm_PHY_CMN_CTRL_0_PLL_SHUTDOWNB BIT(5)
#define DSI_7nm_PHY_CMN_CTRL_0_DIGTOP_PWRDN_B BIT(6)
struct msm_dsi_phy {void *base,*pll_base,*pll_data;};
struct dsi_pll_7nm {struct msm_dsi_phy *phy;int pll_enable_cnt;int pll_enable_lock;};
#define spin_lock_irqsave(lock,flags) do{assert(!*(lock));*(lock)=1;(flags)=0;}while(0)
#define spin_unlock_irqrestore(lock,flags) do{(void)(flags);assert(*(lock)==1);*(lock)=0;}while(0)
static u32 readl(void *p){return *(u32*)p;}
static void writel(u32 value,void *p){*(u32*)p=value;}
#define ndelay(n) ((void)(n))
#define WARN_ON(x) assert(!(x))
static int errors;
#define DRM_DEV_ERROR_RATELIMITED(dev,...) do{errors++;}while(0)
'''
    code += function(current, 'dsi_pll_enable_pll_bias') + '\n' + function(current, 'dsi_pll_disable_pll_bias')
    for suffix, text in [('fixed', current), ('old', old)]:
        for name, target in [('on', 'dsi_7nm_phy_enable'), ('off', 'dsi_7nm_phy_disable')]:
            code += '\nstatic void phy_' + name + '_' + suffix + '(struct msm_dsi_phy *phy){\n'
            code += 'struct dsi_pll_7nm *pll=phy->pll_data;void *base=phy->base;unsigned long flags;\n'
            if name == 'on':
                code += 'u32 data;\n'
            code += block(text, target) + '}\n'
    code += r'''
int main(void){
 u32 registers[2]={0};unsigned int cases=0;
 struct msm_dsi_phy phy={.base=&registers[0],.pll_base=&registers[1]};
 struct dsi_pll_7nm pll={.phy=&phy};phy.pll_data=&pll;
 /* Reproduce the real zero-mux/positive-vote contradiction. */
 phy_on_old(&phy);assert(pll.pll_enable_cnt==1 && registers[1]==0);cases++;
 dsi_pll_enable_pll_bias(&pll);dsi_pll_disable_pll_bias(&pll);
 assert(pll.pll_enable_cnt==1 && registers[1]==0);cases++;
 registers[1]=0xc0;phy_off_old(&phy);
 assert(!pll.pll_enable_cnt && !registers[0] && registers[1]==0xc0);cases++;
 memset(registers,0,sizeof(registers));
 for(int pass=0;pass<32;pass++){
  phy_on_fixed(&phy);assert(pll.pll_enable_cnt==1 && registers[1]==0xc0 && registers[0]==0x60);
  dsi_pll_enable_pll_bias(&pll);assert(pll.pll_enable_cnt==2 && registers[1]==0xc0);
  dsi_pll_enable_pll_bias(&pll);assert(pll.pll_enable_cnt==3 && registers[1]==0xc0);
  dsi_pll_disable_pll_bias(&pll);assert(pll.pll_enable_cnt==2 && registers[1]==0xc0);
  dsi_pll_disable_pll_bias(&pll);assert(pll.pll_enable_cnt==1 && registers[1]==0xc0);
  phy_off_fixed(&phy);assert(!pll.pll_enable_cnt && !registers[0] && !registers[1]);
  /* Reading an off PLL temporarily powers it, then returns it fully off. */
  dsi_pll_enable_pll_bias(&pll);assert(pll.pll_enable_cnt==1 && registers[1]==0xc0 && registers[0]==0x20);
  dsi_pll_disable_pll_bias(&pll);assert(!pll.pll_enable_cnt && !registers[0] && !registers[1]);
  assert(!pll.pll_enable_lock && !errors);
 }
 cases+=9;
 printf("%u\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='kernel73-bias-') as directory:
        work = Path(directory)
        (work / 'test.c').write_text(code)
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined',
                        '-fno-sanitize-recover=all', str(work / 'test.c'), '-o', str(work / 'test')], check=True)
        cases = int(subprocess.check_output([str(work / 'test')], text=True, timeout=10))
    result = {'all_passed': True, 'cases': cases, 'cycles': 32, 'actual_bias_code_executed': True,
              'old_active_zero_mux_reproduced': True, 'old_off_stale_mux_reproduced': True,
              'nested_prepare_and_rate_queries_preserved': True, 'only_two_mux_writes_added': True,
              'hardware_validated': False}
    if a.report:
        a.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
