#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Replay panel power votes with failures, boot-on state and touch-probe guards."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile

def function(s,name):
    m=re.search(r'^static [^;{]+\b'+name+r'\([^;{]*\)\s*\{',s,re.M);assert m,name
    end=m.end();depth=1
    while depth:
        depth+=(s[end]=='{')-(s[end]=='}');end+=1
    return s[m.start():end]

def main():
    a=argparse.ArgumentParser();a.add_argument('--source',type=Path,required=True);a.add_argument('--baseline',type=Path,required=True);a.add_argument('--report',type=Path,required=True);args=a.parse_args()
    rel='drivers/gpu/drm/panel/panel-novatek-nt36532.c'
    s=(args.source/rel).read_text();old=(args.baseline/rel).read_text()
    for name in ['pipa_init_sequence','yudi_init_sequence','liuqin_reset','pipa_reset','yudi_reset','liuqin_init_sequence','liuqin_send_oem_cmds','nt36532_off','nt36532_enable','nt36532_disable']:
        assert function(s,name)==function(old,name),name
    assert s[s.index('/* OEM payloads'):s.index('static const struct drm_display_mode pipa_mode_120')]==old[old.index('/* OEM payloads'):old.index('static const struct drm_display_mode pipa_mode_120')]
    probe=function(s,'nt36532_probe');assert probe.index('liuqin_hold_boot_supplies(ctx)')<probe.index('drm_panel_add(&ctx->panel)')
    assert 'ctx->keep_iovdd = liuqin_retained_power && ctx->oem_panel;' in probe
    rel='drivers/input/touchscreen/nt36523/nt36xxx.c';touch=(args.source/rel).read_text();old_touch=(args.baseline/rel).read_text()
    tp=function(touch,'nvt_ts_probe');assert tp.index('nvt_wait_liuqin_panel(')<tp.index('kzalloc(')<tp.index('nvt_ts_check_chip_ver_trim(')
    for name in ['nvt_ts_suspend','nvt_ts_resume','panel_prepared','panel_unpreparing','nvt_ts_check_chip_ver_trim']:
        assert function(touch,name)==function(old_touch,name),name
    rel='arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts'
    dt=(args.source/rel).read_text();old_dt=(args.baseline/rel).read_text()
    strip=lambda x:re.sub(r'/\*.*?\*/','',x,flags=re.S)
    expected=strip(old_dt)
    for name in ['display_panel_dvdd_buck','display_panel_dvdd_ldo']:
        m=re.search(r'\t'+name+r':.*?\n\t};',expected,re.S);assert m
        block=m[0];assert block.count('regulator-always-on;')==1 and 'regulator-boot-on;' in block
        expected=expected[:m.start()]+block.replace('\n\t\tregulator-always-on;','')+expected[m.end():]
    assert strip(dt)==expected,'Unexpected device-tree electrical changes'
    for name in ['backlight_gpio_regulator_vsp','backlight_gpio_regulator_vsn']:
        assert re.search(r'\t'+name+r':.*?\n\t};',dt,re.S)[0]==re.search(r'\t'+name+r':.*?\n\t};',old_dt,re.S)[0]
    core=(args.baseline/'drivers/gpu/drm/drm_panel.c').read_text();unprepare=core[core.index('void drm_panel_unprepare('):core.index('EXPORT_SYMBOL(drm_panel_unprepare);')]
    assert unprepare.index('follower->funcs->panel_unpreparing')<unprepare.index('panel->funcs->unprepare(panel)')
    helpers=s[s.index('/* Track the panel'):s.index('static int nt36532_off(')]
    actual=helpers+'\n'+function(s,'nt36532_prepare')+'\n'+function(s,'nt36532_unprepare')+'\n'+function(touch,'nvt_wait_liuqin_panel')
    stubs=r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#define BIT(i) (1U<<(i))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define MIPI_DSI_MODE_LPM 1
#define dev_info(dev,...) ((void)(dev))
#define dev_warn(dev,...) ((void)(dev))
#define dev_err(dev,...) ((void)(dev))
#define EPROBE_DEFER 517
struct device_node {bool liuqin;};
struct device {struct device_node *of_node;};
struct drm_panel {struct device *dev;};
struct regulator {int id,refs,physical;};
struct regulator_bulk_data {const char *supply;struct regulator *consumer;};
struct mipi_dsi_device {struct device dev;unsigned mode_flags;};
struct nt36532;
struct panel_desc {int (*init_sequence)(struct nt36532 *);void (*reset_sequence)(struct nt36532 *);};
struct nt36532 {struct drm_panel panel;struct mipi_dsi_device *dsi[2];const struct panel_desc *desc;void *oem_panel;struct regulator_bulk_data supplies[3];unsigned supply_votes;bool keep_iovdd;int reset_gpio,dsc;};
struct drm_dsc_picture_parameter_set {int unused;};
static struct panel_desc liuqin_desc,other_desc;
static int fail_on=-1,fail_off=-1,init_result,pps_result,bulk_on,bulk_off,cases;
static int events[1000],event_count;
static void event(int n){assert(event_count<1000);events[event_count++]=n;}
static int regulator_enable(struct regulator *r){event(100+r->id);if(r->id==fail_on)return -EIO;r->refs++;r->physical=1;return 0;}
static int regulator_disable(struct regulator *r){event(200+r->id);assert(r->refs>0);if(r->id==fail_off)return -EBUSY;r->refs--;if(!r->refs)r->physical=0;return 0;}
static int regulator_is_enabled(struct regulator *r){return r->physical;}
static int regulator_bulk_enable(int n,struct regulator_bulk_data *r){(void)n;(void)r;bulk_on++;return 0;}
static int regulator_bulk_disable(int n,struct regulator_bulk_data *r){(void)n;(void)r;bulk_off++;return 0;}
static void usleep_range(unsigned a,unsigned b){assert(b>a);event((int)a);}
static void msleep(unsigned a){event(100000+(int)a);}
static void gpiod_set_value_cansleep(int g,int v){(void)g;event(300+v);}
static void reset(struct nt36532 *c){(void)c;event(400);}
static int init(struct nt36532 *c){(void)c;event(500);return init_result;}
static struct nt36532 *to_nt36532(struct drm_panel *p){return (struct nt36532 *)p;}
static struct mipi_dsi_device *nt36532_cmd_dsi(struct nt36532 *c){return c->dsi[1];}
static void drm_dsc_pps_payload_pack(struct drm_dsc_picture_parameter_set *p,int *d){(void)p;(void)d;}
static int mipi_dsi_picture_parameter_set(struct mipi_dsi_device *d,struct drm_dsc_picture_parameter_set *p){(void)d;(void)p;event(600);return pps_result;}
static int mipi_dsi_compression_mode(struct mipi_dsi_device *d,bool b){(void)d;(void)b;event(700);return 0;}
static int puts_node,finds_panel,panel_find_result;
static struct device_node *of_parse_phandle(struct device_node *n,const char *s,int i){assert(!strcmp(s,"panel") && !i);return n;}
static bool of_device_is_compatible(struct device_node *n,const char *s){assert(!strcmp(s,"xiaomi,liuqin-nt36532"));return n->liuqin;}
static void of_node_put(struct device_node *n){assert(n);puts_node++;}
static struct drm_panel *of_drm_find_panel(struct device_node *n){assert(n);finds_panel++;return (struct drm_panel *)(intptr_t)(panel_find_result?panel_find_result:1);}
#define PTR_ERR_OR_ZERO(p) ((intptr_t)(p)<0?(int)(intptr_t)(p):0)
'''
    scenarios=r'''
static struct nt36532 c;
static struct regulator regs[3];
static struct mipi_dsi_device dsi;
static struct device dev;
static void setup(bool retained){
    memset(&c,0,sizeof(c));memset(regs,0,sizeof(regs));
    for(int i=0;i<3;i++){regs[i].id=i;regs[i].physical=1;c.supplies[i].consumer=&regs[i];c.supplies[i].supply="rail";}
    liuqin_desc=(struct panel_desc){init,reset};other_desc=liuqin_desc;
    c.panel.dev=&dev;c.desc=&liuqin_desc;c.keep_iovdd=retained;c.oem_panel=&c;
    c.dsi[0]=c.dsi[1]=&dsi;event_count=bulk_on=bulk_off=init_result=pps_result=0;
    fail_on=fail_off=-1;cases++;
}
static void votes(unsigned mask){assert(c.supply_votes==mask);for(int i=0;i<3;i++)assert(regs[i].refs==!!(mask&BIT(i)));}
static int count(int x){int n=0;for(int i=0;i<event_count;i++)n+=events[i]==x;return n;}
static int indexof(int x){for(int i=0;i<event_count;i++)if(events[i]==x)return i;return -1;}
static void boot(void){assert(!liuqin_hold_boot_supplies(&c));votes(7);}
int main(void){
    setup(true);boot();assert(count(100)==1 && count(101)==1 && count(102)==1 && !count(10000));
    event_count=0;assert(!nt36532_prepare(&c.panel));votes(7);assert(!count(100) && !count(101) && !count(102));
    assert(count(100120)==3 && count(400)==1 && count(500)==1 && count(600)==1 && !count(700));
    event_count=0;assert(!nt36532_unprepare(&c.panel));votes(1);
    assert(regs[0].physical==1 && regs[1].physical==0 && regs[2].physical==0);
    assert(indexof(202)<indexof(201) && count(200)==0 && events[0]==301);
    event_count=0;assert(!nt36532_prepare(&c.panel));votes(7);
    assert(events[0]==300 && indexof(300)<indexof(101) && indexof(101)<indexof(102));
    assert(count(100120)==3 && !count(100));

    setup(false);boot();event_count=0;nt36532_unprepare(&c.panel);votes(6);
    assert(regs[0].physical==0 && regs[1].physical==1 && regs[2].physical==1);
    assert(count(200)==1 && !count(201) && !count(202));
    event_count=0;assert(!nt36532_prepare(&c.panel));votes(7);assert(!count(300) && count(100)==1 && !count(101) && !count(102));

    for(int fail=0;fail<3;fail++){
        setup(true);fail_on=fail;assert(liuqin_hold_boot_supplies(&c)==-EIO);votes(0);
        for(int i=0;i<fail;i++)assert(count(200+i)==1);
    }
    for(int fail=1;fail<3;fail++){
        setup(true);boot();nt36532_unprepare(&c.panel);fail_on=fail;event_count=0;
        assert(nt36532_prepare(&c.panel)==-EIO);votes(1);assert(!count(100) && !count(200) && !count(500));
        fail_on=-1;assert(!nt36532_prepare(&c.panel));votes(7);
    }
    setup(false);boot();nt36532_unprepare(&c.panel);fail_on=0;
    assert(nt36532_prepare(&c.panel)==-EIO);votes(6);

    setup(true);boot();nt36532_unprepare(&c.panel);fail_on=2;fail_off=1;
    assert(nt36532_prepare(&c.panel)==-EIO);votes(3);
    fail_on=fail_off=-1;event_count=0;assert(!nt36532_prepare(&c.panel));votes(7);
    assert(!count(101) && count(102)==1);

    for(int fail=1;fail<3;fail++){
        setup(true);boot();fail_off=fail;nt36532_unprepare(&c.panel);votes(1|BIT(fail));
        fail_off=-1;nt36532_unprepare(&c.panel);votes(1);
    }
    setup(false);boot();fail_off=0;nt36532_unprepare(&c.panel);votes(7);
    fail_off=-1;nt36532_unprepare(&c.panel);votes(6);

    setup(true);boot();init_result=-EIO;assert(nt36532_prepare(&c.panel)==-EIO);votes(1);assert(!count(600));
    setup(true);boot();pps_result=-EIO;assert(nt36532_prepare(&c.panel)==-EIO);votes(1);
    setup(false);boot();init_result=-EIO;assert(nt36532_prepare(&c.panel)==-EIO);votes(6);

    setup(true);boot();liuqin_release_boot_supplies(&c);votes(0);
    event_count=0;liuqin_release_boot_supplies(&c);assert(!event_count);
    setup(true);liuqin_release_boot_supplies(&c);votes(0);assert(!event_count);

    setup(true);regs[1].physical=0;boot();assert(count(10000)==1 && regs[1].physical==1);
    setup(true);regs[0].physical=0;boot();assert(count(10000)==1);

    setup(true);c.desc=&other_desc;assert(!nt36532_enable_supplies(&c));nt36532_disable_supplies(&c);
    assert(bulk_on==1 && bulk_off==1 && !c.supply_votes && !event_count);

    struct device_node node={.liuqin=true};struct device t={0};
    cases++;puts_node=finds_panel=0;assert(!nvt_wait_liuqin_panel(&t) && !puts_node && !finds_panel);
    cases++;t.of_node=&node;node.liuqin=false;assert(!nvt_wait_liuqin_panel(&t) && puts_node==1 && !finds_panel);
    cases++;node.liuqin=true;panel_find_result=-EPROBE_DEFER;assert(nvt_wait_liuqin_panel(&t)==-EPROBE_DEFER && puts_node==2 && finds_panel==1);
    cases++;panel_find_result=0;assert(!nvt_wait_liuqin_panel(&t) && puts_node==3 && finds_panel==2);
    printf("%d\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='retained-power-tests-') as temp:
        root=Path(temp);(root/'test.c').write_text(stubs+actual+scenarios)
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',str(root/'test.c'),'-o',str(root/'test')],check=True)
        count=int(subprocess.check_output([str(root/'test')],text=True,timeout=20))
    report={'cases':count,'actual_power_and_prepare_functions_executed':True,'retained_and_legacy_states_verified':True,'preexisting_votes_survive_failures':True,'failed_disable_votes_tracked':True,'probe_por_settle_checked':True,'touch_probe_defer_checked':True,'panel_commands_reset_waits_unchanged':True,'touch_firmware_and_follower_callbacks_unchanged':True,'device_tree_changes_only_two_always_on_flags':True,'bias_constraints_unchanged':True,'hardware_validation_required':True}
    args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))

if __name__=='__main__':main()
