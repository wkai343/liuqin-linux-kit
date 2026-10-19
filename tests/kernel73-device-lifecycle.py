#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise migrated audio constraints and USB power ownership using real C functions."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(text, name):
    m = re.search(r'^static (?:inline )?(?:int|void)\s+' + name + r'\([^;{]*\)\s*\{', text, re.M)
    if not m:
        raise ValueError(name)
    end, depth = m.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[m.start():end]


def run_c(root, name, code):
    src = root / (name + '.c')
    src.write_text(code)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=undefined', '-fno-sanitize-recover=all', str(src),
                    '-o', str(root / name)], check=True)
    return int(subprocess.check_output([str(root / name)], text=True, timeout=10))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--report', type=Path)
    a = ap.parse_args()
    common = '#include <assert.h>\n#include <stdint.h>\n#include <stdbool.h>\n#include <stdio.h>\n#include <string.h>\n#include <errno.h>\n'
    rel = 'sound/soc/qcom/sc8280xp.c'
    old, new = [(tree / rel).read_text() for tree in [a.baseline, a.source]]
    assert new.replace('\t\tsnd_mask_none(fmt);\n', '', 1) == old
    audio = common + r'''
typedef unsigned int snd_pcm_format_t;
struct snd_interval {unsigned int min,max;};
struct snd_mask {uint32_t bits[2];};
struct snd_pcm_hw_params {struct snd_interval rate,channels;struct snd_mask fmt;};
struct snd_soc_dai {int id;};
struct snd_soc_card {const char *name;};
struct snd_soc_pcm_runtime {struct snd_soc_card *card;struct snd_soc_dai *cpu;};
#define snd_soc_rtd_to_cpu(rtd,i) ((rtd)->cpu)
#define SNDRV_PCM_HW_PARAM_RATE 0
#define SNDRV_PCM_HW_PARAM_CHANNELS 1
#define SNDRV_PCM_HW_PARAM_FORMAT 2
#define SNDRV_PCM_FORMAT_S16_LE 2
#define SNDRV_PCM_FORMAT_S24_LE 6
#define TERTIARY_TDM_RX_0 200
#define TX_CODEC_DMA_TX_0 300
#define TX_CODEC_DMA_TX_1 301
#define TX_CODEC_DMA_TX_2 302
#define TX_CODEC_DMA_TX_3 303
#define hw_param_interval(p,n) ((n)==0?&(p)->rate:&(p)->channels)
#define hw_param_mask(p,n) (&(p)->fmt)
#define MASK_OFS(i) ((i)>>5)
#define MASK_BIT(i) (1U<<((i)&31))
'''
    helpers = (a.source / 'include/sound/pcm_params.h').read_text()
    for name in ['snd_mask_none', 'snd_mask_set', 'snd_mask_set_format']:
        audio += function(helpers, name) + '\n'
    audio += function(old, 'sc8280xp_be_hw_params_fixup').replace('sc8280xp_be_hw_params_fixup(', 'before_fixup(', 1)
    audio += function(new, 'sc8280xp_be_hw_params_fixup')
    audio += r'''
int main(void){
 unsigned int cases=0;struct snd_soc_dai dai={.id=TERTIARY_TDM_RX_0};
 struct snd_soc_card card={.name="Xiaomi-Pad-6-Pro"};struct snd_soc_pcm_runtime r={.card=&card,.cpu=&dai};
 struct snd_pcm_hw_params params={0};
 before_fixup(&r,&params);assert((params.fmt.bits[0]&(1U<<2))&&(params.fmt.bits[0]&(1U<<6)));cases++;
 for(unsigned int mask=0;mask<4;mask++){
  memset(&params,0,sizeof(params));params.fmt.bits[0]=(mask&1?1U<<2:0)|(mask&2?1U<<10:0);
  assert(!sc8280xp_be_hw_params_fixup(&r,&params));
  assert(params.fmt.bits[0]==(1U<<6)&&!params.fmt.bits[1]);
  assert(params.rate.min==48000&&params.rate.max==48000&&params.channels.min==4&&params.channels.max==4);cases++;
 }
 for(unsigned int model=0;model<2;model++)for(unsigned int port=0;port<3;port++){
  card.name=model?"Other-Board":"Xiaomi-Pad-6-Pro";
  dai.id=port==0?TERTIARY_TDM_RX_0:port==1?TX_CODEC_DMA_TX_0:999;
  if(!model&&!port)continue;
  struct snd_pcm_hw_params old={0},new={0};before_fixup(&r,&old);sc8280xp_be_hw_params_fixup(&r,&new);
  assert(!memcmp(&old,&new,sizeof(old)));cases++;
 }
 printf("%u\n",cases);return 0;
}
'''
    rel = 'drivers/phy/qualcomm/phy-qcom-qmp-combo.c'
    old, new = [(tree / rel).read_text() for tree in [a.baseline, a.source]]
    assert function(old, 'qmp_combo_usb_power_off') == function(new, 'qmp_combo_usb_power_off')
    usb = common + r'''
#define __iomem
#define QPHY_PCS_POWER_DOWN_CONTROL 0
#define QPHY_SW_RESET 1
#define QPHY_START_CTRL 2
#define QPHY_PCS_STATUS 3
#define SW_PWRDN 1U
#define SW_RESET 1U
#define SERDES_START 1U
#define PCS_START 2U
#define PHYSTATUS 64U
#define PHY_INIT_COMPLETE_TIMEOUT 10000
struct qmp_phy_cfg {unsigned int regs[4];bool has_pwrdn_delay;
 void *serdes_tbl,*tx_tbl,*rx_tbl,*pcs_tbl,*pcs_misc_tbl,*pcs_usb_tbl;
 int serdes_tbl_num,tx_tbl_num,rx_tbl_num,pcs_tbl_num,pcs_misc_tbl_num,pcs_usb_tbl_num;};
struct qmp_combo {const struct qmp_phy_cfg *cfg;void *dev,*serdes,*tx,*rx,*tx2,*rx2,*pcs,*pcs_usb,*pcs_misc,*pipe_clk;};
struct phy {struct qmp_combo *qmp;};
#define phy_get_drvdata(p) ((p)->qmp)
static int pipe_votes,force_timeout;
static void qphy_setbits(void *p,unsigned int off,unsigned int bits){*(uint32_t*)((char*)p+off)|=bits;}
static void qphy_clrbits(void *p,unsigned int off,unsigned int bits){*(uint32_t*)((char*)p+off)&=~bits;}
#define qmp_configure(dev,base,table,n) do{(void)(base);(void)(table);(void)(n);}while(0)
#define qmp_configure_lane(dev,base,table,n,lane) qmp_configure(dev,base,table,n)
#define usleep_range(a,b) do{(void)(a);(void)(b);}while(0)
#define dev_err(dev,...) ((void)(dev))
static void clk_disable_unprepare(void *clk){(void)clk;pipe_votes--;}
#define readl_poll_timeout(status,val,condition,delay,maxwait) ({ \
 (void)(status);(void)(delay);(void)(maxwait); \
 (val)=force_timeout||!(*(uint32_t*)qmp->pcs&SW_PWRDN)?PHYSTATUS:0; \
 (condition)?0:-ETIMEDOUT; })
'''
    usb += function(new, 'qmp_combo_usb_power_off')
    usb += function(old, 'qmp_combo_usb_power_on').replace('qmp_combo_usb_power_on(', 'before_power_on(', 1)
    usb += function(new, 'qmp_combo_usb_power_on')
    usb += r'''
int main(void){
 unsigned int cases=0;uint32_t regs[4]={0};
 const struct qmp_phy_cfg cfg={.regs={0,4,8,12},.has_pwrdn_delay=true};
 struct qmp_combo q={.cfg=&cfg,.pcs=regs};struct phy phy={.qmp=&q};
 /* Fresh COM init already powers USB: both implementations work. */
 pipe_votes=1;regs[0]=SW_PWRDN;assert(!before_power_on(&phy)&&pipe_votes==1);cases++;
 /* DP holds COM while USB has exited: old path leaves USB off and consumes DP's clock. */
 assert(!qmp_combo_usb_power_off(&phy));
 assert(before_power_on(&phy)==-ETIMEDOUT&&pipe_votes==0);cases++;
 pipe_votes=1;assert(!qmp_combo_usb_power_on(&phy)&&pipe_votes==1&&(regs[0]&SW_PWRDN));cases++;
 for(int pass=0;pass<32;pass++){
  assert(!qmp_combo_usb_power_off(&phy));assert(!(regs[0]&SW_PWRDN));
  assert(!qmp_combo_usb_power_on(&phy));assert((regs[0]&SW_PWRDN)&&!(regs[1]&SW_RESET));
  assert((regs[2]&(SERDES_START|PCS_START))==(SERDES_START|PCS_START)&&pipe_votes==1);
 }
 cases+=3;
 force_timeout=1;
 assert(qmp_combo_usb_power_on(&phy)==-ETIMEDOUT);cases++;
 assert(pipe_votes==1);cases++;
 assert(!(regs[0]&SW_PWRDN)&&(regs[1]&SW_RESET)&&!(regs[2]&(SERDES_START|PCS_START)));cases++;
 /* The caller may release its COM vote exactly once after this error. */
 clk_disable_unprepare(q.pipe_clk);assert(pipe_votes==0);cases++;
 printf("%u\n",cases);return 0;
}
'''
    touch = (a.source / 'drivers/input/touchscreen/nt36523/nt36xxx.c').read_text()
    call = re.search(r'alloc_workqueue\("nvt_esd_check_wq",\s*([^,]+),\s*1\)', touch)[1]
    assert {x.strip() for x in call.split('|')} == {'WQ_MEM_RECLAIM', 'WQ_PERCPU'}
    with tempfile.TemporaryDirectory(prefix='kernel73-lifecycle-') as tmp:
        root = Path(tmp)
        results = {'audio_constraints': run_c(root, 'audio', audio), 'usb_shared_power': run_c(root, 'usb', usb)}
    result = {'all_passed': True, 'actual_source_functions_executed': True, 'cases': results,
              'usb_cycles': 32, 'old_audio_mask_bug_reproduced': True,
              'old_usb_off_and_clock_bug_reproduced': True, 'touch_esd_explicit_percpu': True,
              'hardware_validated': False}
    if a.report:
        a.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
