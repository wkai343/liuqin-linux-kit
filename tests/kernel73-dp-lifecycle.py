#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute DP detection and failed power-on ownership paths from kernel source."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(text, name):
    match = re.search(r'^(?:static )?(?:int|void|enum drm_connector_status)\s+'
                      + name + r'\([^;{]*\)\s*\{', text, re.M)
    if not match:
        raise ValueError(name)
    end, depth = match.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]


def execute(root, name, code):
    source = root / (name + '.c')
    source.write_text(code)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=undefined', '-fno-sanitize-recover=all',
                    str(source), '-o', str(root / name)], check=True)
    return int(subprocess.check_output([str(root / name)], text=True, timeout=10))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--report', type=Path)
    args = ap.parse_args()
    common = '''#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
'''
    relative = 'drivers/gpu/drm/msm/dp/dp_display.c'
    old, new = [(tree / relative).read_text() for tree in [args.baseline, args.source]]
    detect = common + r'''
typedef uint8_t u8;
enum drm_connector_status {connector_status_connected=1,connector_status_disconnected=2};
#define DP_RECEIVER_CAP_SIZE 16
struct drm_dp_desc {int unused;};
struct drm_connector {int unused;};
struct platform_device {int dev;};
static struct platform_device device;
struct msm_dp {struct platform_device *pdev;};
struct msm_dp_display_private {struct msm_dp msm_dp_display;int plugged_lock;
 bool plugged,phy_initialized;void *ctrl,*aux;};
struct msm_dp_bridge {struct msm_dp *msm_dp_display;};
struct drm_bridge {struct msm_dp_bridge *dp;};
#define to_dp_bridge(b) ((b)->dp)
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
#define drm_dbg_dp(...) ((void)0)
#define DRM_DEBUG_DP(...) ((void)0)
#define DRM_ERROR(...) ((void)0)
static void ignore_lock(void *p){(void)p;}
#define guard(type) ignore_lock
static int pm_result,init_result,hpd,caps_result,desc_result,sink_cap,sink_count;
static int votes,inits,exits,aux_reads,aux_enable,aux_disable;
static int pm_runtime_resume_and_get(void *p){(void)p;if(pm_result<0)return pm_result;votes++;return 0;}
static void pm_runtime_put_sync(void *p){(void)p;votes--;assert(votes>=0);}
static int msm_dp_ctrl_phy_init(void *p){(void)p;inits++;return init_result;}
static void msm_dp_display_host_phy_exit(struct msm_dp_display_private *p){
 if(p->phy_initialized){p->phy_initialized=false;exits++;}}
static void msm_dp_aux_enable_xfers(void *p,bool enable){(void)p;if(enable)aux_enable++;else aux_disable++;}
static int msm_dp_aux_is_link_connected(void *p){(void)p;return hpd;}
static int drm_dp_read_dpcd_caps(void *p,u8 *data){(void)p;(void)data;aux_reads++;return caps_result;}
static int drm_dp_is_branch(u8 *p){(void)p;return 0;}
static int drm_dp_read_desc(void *p,struct drm_dp_desc *d,int branch){(void)p;(void)d;(void)branch;aux_reads++;return desc_result;}
static bool drm_dp_read_sink_count_cap(struct drm_connector *c,u8 *p,struct drm_dp_desc *d){(void)c;(void)p;(void)d;return sink_cap;}
static int drm_dp_read_sink_count(void *p){(void)p;return sink_count;}
'''
    detect += function(new, 'msm_dp_display_host_phy_init')
    detect += function(old, 'msm_dp_bridge_detect').replace('msm_dp_bridge_detect(', 'old_detect(', 1)
    detect += function(new, 'msm_dp_bridge_detect')
    detect += r'''
static void reset(struct msm_dp_display_private *p){
 memset(p,0,sizeof(*p));p->msm_dp_display.pdev=&device;
 pm_result=init_result=hpd=caps_result=desc_result=sink_cap=0;
 sink_count=1;votes=inits=exits=aux_reads=aux_enable=aux_disable=0;
}
int main(void){
 int cases=0;struct msm_dp_display_private p;
 struct msm_dp_bridge dp={.msm_dp_display=&p.msm_dp_display};struct drm_bridge b={.dp=&dp};
 struct drm_connector c={0};
 /* Old int-to-bool caller both powers an unplugged PHY and leaks its ownership. */
 reset(&p);assert(old_detect(&b,&c)==connector_status_disconnected);
 assert(inits==1&&p.phy_initialized&&exits==0);cases++;
 reset(&p);hpd=1;init_result=-ETIMEDOUT;
 assert(old_detect(&b,&c)==connector_status_connected&&aux_reads==2);cases++;
 reset(&p);assert(msm_dp_bridge_detect(&b,&c)==connector_status_disconnected);
 assert(!inits&&!aux_enable&&!aux_reads&&!votes&&!p.phy_initialized);cases++;
 reset(&p);pm_result=-EIO;assert(msm_dp_bridge_detect(&b,&c)==connector_status_disconnected);
 assert(!inits&&!aux_reads&&!votes);cases++;
 for(int owned=0;owned<2;owned++){
  reset(&p);hpd=1;p.plugged=owned;init_result=-ETIMEDOUT;
  assert(msm_dp_bridge_detect(&b,&c)==connector_status_disconnected);
  assert(inits==1&&!aux_reads&&!aux_enable&&!p.plugged&&!p.phy_initialized&&!votes);cases++;
 }
 for(int error=0;error<2;error++)for(int initialized=0;initialized<2;initialized++){
  reset(&p);hpd=1;p.phy_initialized=initialized;
  if(error)caps_result=-EIO;else desc_result=-ENODATA;
  assert(msm_dp_bridge_detect(&b,&c)==connector_status_disconnected);
  assert(inits==!initialized&&exits==!initialized&&p.phy_initialized==initialized&&!votes);
  assert(aux_disable==!initialized);cases++;
 }
 for(int initialized=0;initialized<2;initialized++){
  reset(&p);hpd=1;p.phy_initialized=initialized;
  assert(msm_dp_bridge_detect(&b,&c)==connector_status_connected);
  assert(p.plugged&&p.phy_initialized&&votes==1&&aux_reads==2&&inits==!initialized&&!exits);cases++;
 }
 /* A previously plugged sink may require AUX even if HPD currently reads low. */
 reset(&p);p.plugged=true;p.phy_initialized=true;
 assert(msm_dp_bridge_detect(&b,&c)==connector_status_connected&&aux_reads==2&&!inits);cases++;
 printf("%d\n",cases);return 0;
}
'''
    relative = 'drivers/phy/qualcomm/phy-qcom-qmp-combo.c'
    old, new = [(tree / relative).read_text() for tree in [args.baseline, args.source]]
    power = common + r'''
#define __iomem
#define DP_PHY_PD_CTL_PSR_PWRDN 0x02
#define QSERDES_DP_PHY_PD_CTL 0
struct qmp_combo;
struct qmp_phy_cfg {void *dp_tx_tbl;int dp_tx_tbl_num;
 void (*configure_dp_tx)(struct qmp_combo*);int (*configure_dp_phy)(struct qmp_combo*);};
struct qmp_combo {const struct qmp_phy_cfg *cfg;void *dp_tx,*dp_tx2,*dp_dp_phy;int phy_mutex;bool dp_powered_on;};
struct phy {struct qmp_combo *qmp;};
static int serdes_error,config_error,tx_calls,config_calls,depth;
#define phy_get_drvdata(p) ((p)->qmp)
#define dev_dbg(...) ((void)0)
static void mutex_lock(int *p){(void)p;assert(depth++==0);}
static void mutex_unlock(int *p){(void)p;assert(--depth==0);}
#define qmp_configure_lane(dev,base,table,n,lane) do{(void)(base);(void)(table);(void)(n);(void)(lane);}while(0)
static int qmp_combo_dp_serdes_init(struct qmp_combo *q){(void)q;return serdes_error;}
static void writel(uint32_t v,void *p){*(uint32_t*)p=v;}
static void config_tx(struct qmp_combo *q){(void)q;tx_calls++;}
static int config_phy(struct qmp_combo *q){(void)q;config_calls++;return config_error;}
'''
    power += function(old, 'qmp_combo_dp_power_on').replace('qmp_combo_dp_power_on(', 'old_power_on(', 1)
    power += function(new, 'qmp_combo_dp_power_on')
    power += r'''
int main(void){
 int cases=0;uint32_t pd=0;
 struct qmp_phy_cfg cfg={.configure_dp_tx=config_tx,.configure_dp_phy=config_phy};
 struct qmp_combo q={.cfg=&cfg,.dp_dp_phy=&pd};struct phy phy={.qmp=&q};
 serdes_error=-ETIMEDOUT;assert(old_power_on(&phy)==-ETIMEDOUT&&q.dp_powered_on);cases++;
 for(int initial=0;initial<2;initial++)for(int stage=0;stage<3;stage++){
  q.dp_powered_on=initial;pd=0;tx_calls=config_calls=depth=0;
  serdes_error=stage==1?-ETIMEDOUT:0;config_error=stage==2?-EIO:0;
  int ret=qmp_combo_dp_power_on(&phy);
  assert(ret==(stage==1?-ETIMEDOUT:stage==2?-EIO:0));
  assert(q.dp_powered_on==(stage==0)&&!depth);
  assert(pd==(stage?DP_PHY_PD_CTL_PSR_PWRDN:0));
  assert(tx_calls==(stage!=1)&&config_calls==(stage!=1));cases++;
 }
 printf("%d\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='kernel73-dp-') as temp:
        root = Path(temp)
        cases = {'detection': execute(root, 'detect', detect),
                 'power_failure': execute(root, 'power', power)}
    report = {'all_passed': True, 'actual_source_functions_executed': True,
              'cases': cases, 'old_ownership_and_error_bugs_reproduced': True,
              'unplugged_port_skips_phy': True, 'hardware_validated': False}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
