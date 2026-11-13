#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise WCD/SoundWire IRQ, FIFO, clock stop and reset lifetimes."""
import argparse
from pathlib import Path
import re
import hashlib
import importlib.util
import struct

from kernel_test import function, run_c, write_report


spec = importlib.util.spec_from_file_location('dtb', Path(__file__).with_name('display-refresh-rates.py'))
dtb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dtb)


def cells(value):
    return struct.unpack('>' + str(len(value) // 4) + 'I', value)


def strings(value):
    return value.rstrip(b'\0').decode().split('\0')


def settings(nodes, root):
    result = {}
    for name, props in nodes.items():
        if name == root or name.startswith(root + '/'):
            for pin in strings(props['pins']) if 'pins' in props else []:
                result.setdefault(pin, {}).update({k: v for k, v in props.items()
                                                  if k not in ['pins', 'phandle']})
    return result


def bus_states(nodes, address):
    name = next(n for n in nodes if n.endswith('/soundwire@' + address))
    props = nodes[name]
    assert strings(props['pinctrl-names']) == ['default', 'sleep']
    handles = {cells(p['phandle'])[0]: n for n, p in nodes.items() if 'phandle' in p}
    result = []
    for index in range(2):
        state = {}
        for handle in cells(props['pinctrl-' + str(index)]):
            state.update(settings(nodes, handles[handle]))
        result.append(state)
    return result


def check_pins(args):
    assert hashlib.sha256(args.stock_overlay.read_bytes()).hexdigest() == \
        '295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87'
    stock = dtb.dtb_tree(args.stock_overlay)
    candidate = dtb.dtb_tree(args.dtb)
    checked = []
    for address, prefix, pins in [('3210000', 'rx', ['gpio3', 'gpio4', 'gpio5']),
                                   ('33b0000', 'tx', ['gpio0', 'gpio1', 'gpio2', 'gpio14'])]:
        active, sleep = bus_states(candidate, address)
        assert set(active) == set(sleep) == set(pins)
        original = {}
        for name in stock:
            if name.rsplit('/', 1)[-1].startswith(prefix + '_swr_') and name.endswith('_sleep'):
                original.update(settings(stock, name))
        assert set(original) == set(pins)
        for pin in pins:
            # OEM calls mux function 1 "func1"; mainline uses the signal name.
            assert strings(original[pin]['function']) == ['func1']
            function = 'swr_' + prefix + ('_clk' if pin == pins[0] else '_data')
            assert strings(active[pin]['function']) == strings(sleep[pin]['function']) == [function]
            for key in ['drive-strength', 'input-enable', 'bias-pull-down', 'bias-bus-hold']:
                assert sleep[pin].get(key) == original[pin].get(key), (pin, key)
            assert cells(active[pin]['slew-rate']) == (1,)
            assert ('bias-disable' if pin == pins[0] else 'bias-bus-hold') in active[pin]
            checked.append(pin)
    result = {'status': 'PASS', 'pins': checked, 'sleep_states_match_shipping_rom': True,
              'active_mux_bias_and_slew_present': True}
    return result


def check_irq_and_clock_stop(args):
    common = (args.source / 'sound/soc/codecs/wcd-common.c').read_text()
    codec = (args.source / 'sound/soc/codecs/wcd938x-sdw.c').read_text()
    master = (args.source / 'drivers/soundwire/qcom.c').read_text().replace('__maybe_unused ', '')
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
#include <string.h>
typedef uint32_t u32;
enum { IRQ_HANDLED=1, SDW_SCP_INT1_IMPL_DEF=4,
 WCD938X_DIGITAL_INTR_STATUS_0=0, WCD938X_DIGITAL_INTR_STATUS_1=1,
 WCD938X_DIGITAL_INTR_STATUS_2=2 };
struct regmap { int unused; };
struct irq_domain { unsigned int mapping; };
struct device { void *data; struct regmap *regmap; };
struct sdw_slave { struct device dev; };
struct sdw_slave_intr_status { unsigned int control_port; };
struct wcd938x_sdw_priv { bool is_tx; struct irq_domain *slave_irq; };
static void *dev_get_drvdata(struct device *d) { return d->data; }
static struct regmap *dev_get_regmap(struct device *d, void *unused) {
 (void)unused;return d->regmap;
}
static unsigned int irq_find_mapping(struct irq_domain *d, int n) {
 assert(d && n==0);return d->mapping;
}
static int nested,reads,fail_read,clear_after,errors;
#define dev_err_ratelimited(d,fmt,...) ((void)(d),(void)(fmt),errors++)
static void handle_nested_irq(unsigned int irq) { assert(irq==17);nested++; }
static int regmap_read(struct regmap *map, unsigned int reg, u32 *value) {
 assert(map && reg<3);reads++;
 if(reads==fail_read)return -EIO;
 *value=nested<clear_after;return 0;
}
'''
    code += function(common, 'wcd_interrupt_callback') + '\n'
    code += function(codec, 'wcd9380_interrupt_callback') + '\n'
    code += r'''
enum { SWRM_COMP_SW_RESET, SWRM_LINK_MANAGER_EE, SWRM_EE_CPU,
 SWRM_MCP_BUS_CTRL, SWRM_MCP_BUS_CLK_START, SWRM_V2_0_CLK_CTRL,
 SWRM_V2_0_CLK_CTRL_CLK_START, SWRM_REG_INTERRUPT_CLEAR,
 SWRM_REG_INTERRUPT_MASK_ADDR, SWRM_REG_INTERRUPT_CPU_EN,
 SWRM_REG_FRAME_GEN_ENABLED=10, SWRM_FRM_GEN_ENABLED=1,
 SWRM_LINK_STATUS_RETRY_CNT=100,
 SWRM_INTERRUPT_STATUS_MASTER_CLASH_DET=8,
 SWRM_VERSION_1_7_0=17, SWRM_VERSION_2_0_0=20, TIMEOUT_MS=100,
 SWRM_COMP_CFG_ADDR=80, SWRM_COMP_CFG_ENABLE_MSK=1 };
struct bus { int slaves;struct { int curr_bank,next_bank; } params; };
static bool list_empty(int *v) { return !*v; }
struct qcom_swrm_ctrl {
 struct device *dev;
 int wake_irq,irq,version,enumeration,audio_cgcr;
 unsigned int intr_mask;
 unsigned int audio_generation,slave_status;
 bool clock_stop_not_supported;
 void *hclk;
 int reg_layout[16];
 struct bus bus;
 int status[8];
 int (*reg_write)(struct qcom_swrm_ctrl *,int,int);
 int (*reg_read)(struct qcom_swrm_ctrl *,int,void *);
};
static bool board,transport_ready=true,frame_failure,enum_failure;
static unsigned int generation=1;
static bool liuqin_board_quirks(void) { return board; }
static bool liuqin_audio_transport_ready(void) { return transport_ready; }
static unsigned int liuqin_audio_generation(void) { return generation; }
static bool clock_on,irq_on,wake_on,late_irq;
static bool frame_running,frame_target,frame_pending,reset_path;
static int frame_reads;
static int clk_fail,prep_result,stop_result,exit_result,disable_count,enable_count;
static int writes,drained;
static int pin_state=1,pin_hw=1,pin_fail,pin_switches;
/* Selecting the already-current state does not write hardware. */
static int pinctrl_pm_select_sleep_state(struct device *d) {
 (void)d;assert(!irq_on);pin_switches++;
 if(pin_fail==1)return -EIO;
 if(pin_state!=0){pin_state=0;pin_hw=0;}return 0;
}
static int pinctrl_pm_select_default_state(struct device *d) {
 (void)d;assert(!irq_on);pin_switches++;
 if(pin_fail==2)return -EIO;
 if(pin_state!=1){pin_state=1;pin_hw=1;}return 0;
}
static int clk_prepare_enable(void *c) {
 assert(c);if(clk_fail)return clk_fail;clock_on=true;return 0;
}
static void clk_disable_unprepare(void *c) {
 assert(c && clock_on && !irq_on && !late_irq);
 assert(reset_path || !transport_ready || !frame_running);clock_on=false;frame_running=false;
}
static void enable_irq(int irq) {
 if(irq==7){wake_on=true;return;}
 assert(irq==11 && clock_on && !irq_on);
 assert(reset_path || (frame_running && !frame_pending));
 irq_on=true;enable_count++;
}
static void disable_irq(int irq) {
 assert(irq==11 && clock_on && irq_on);irq_on=false;disable_count++;
 if(late_irq){drained++;late_irq=false;}
}
static void disable_irq_nosync(int irq) { assert(irq==7);wake_on=false; }
static int irq_get_irq_data(int irq) { return irq; }
static bool irqd_irq_disabled(int irq) { assert(irq==7);return !wake_on; }
static int write_reg(struct qcom_swrm_ctrl *c,int r,int v) {
 (void)c;(void)v;assert(clock_on);writes++;
 if(r==SWRM_MCP_BUS_CTRL || r==SWRM_V2_0_CLK_CTRL) {
  assert(reset_path || !irq_on);frame_pending=true;frame_target=true;frame_reads=0;
 }
 return 0;
}
static int read_reg(struct qcom_swrm_ctrl *c,int r,void *value) {
 u32 *v=value;(void)c;assert(clock_on);
 if(r==SWRM_COMP_CFG_ADDR){*v=SWRM_COMP_CFG_ENABLE_MSK;return 0;}
 assert(r==100+SWRM_REG_FRAME_GEN_ENABLED);
 if(frame_pending && !frame_target)assert(!irq_on && !late_irq);
 if(frame_pending && ++frame_reads==3) {
  frame_running=frame_target;frame_pending=false;
 }
 *v=frame_running?SWRM_FRM_GEN_ENABLED:0;return 0;
}
static void reinit_completion(int *v) { *v=0; }
static void usleep_range(int a,int b) { assert(a<=b); }
static int msecs_to_jiffies(int t) { return t; }
static int wait_for_completion_timeout(int *v,int t) {
 (void)v;(void)t;assert(irq_on);return !enum_failure;
}
static void qcom_swrm_init(struct qcom_swrm_ctrl *c) {
 assert(clock_on && irq_on);
 if(board && reset_path)assert(c->slave_status==0 && pin_hw==1);
 frame_running=!frame_failure;frame_pending=false;
}
static void qcom_swrm_get_device_status(struct qcom_swrm_ctrl *c) { (void)c; }
static void sdw_handle_slave_status(struct bus *b,int *s) { (void)b;(void)s;assert(irq_on || reset_path); }
static void reset_control_reset(int r) { (void)r;assert(clock_on); }
static int sdw_bus_exit_clk_stop(struct bus *b) { (void)b;assert(clock_on && irq_on);return exit_result; }
static void swrm_wait_for_wr_fifo_done(struct qcom_swrm_ctrl *c) { (void)c;assert(clock_on && irq_on); }
static int sdw_bus_prep_clk_stop(struct bus *b) { (void)b;assert(clock_on && irq_on);return prep_result; }
static int sdw_bus_clk_stop(struct bus *b) {
 (void)b;assert(clock_on && irq_on);late_irq=true;
 if(stop_result==0 || stop_result==-ENODATA) {
  frame_pending=true;frame_target=false;frame_reads=0;
 }
 return stop_result;
}
#define dev_err(d,fmt,...) ((void)(d),(void)(fmt),errors++)
#define dev_info(d,fmt,...) ((void)(d),(void)(fmt))
'''
    code += function(master, 'swrm_wait_for_frame_gen') + '\n'
    code += function(master, 'swrm_runtime_resume') + '\n'
    code += function(master, 'swrm_runtime_suspend') + '\n'
    code += r'''
static void reset_irq_test(void) { nested=reads=fail_read=errors=0;clear_after=1; }
int main(void) {
 struct regmap map={0};struct irq_domain domain={17};
 struct wcd938x_sdw_priv priv={false,&domain};
 struct sdw_slave slave={.dev={&priv,NULL}};
 struct sdw_slave_intr_status status={4};
 reset_irq_test();
 /* RX alerts used to reach regmap_read(NULL), including before bind. */
 assert(wcd9380_interrupt_callback(&slave,&status)==0 && !nested && !reads);
 priv.slave_irq=NULL;
 assert(wcd9380_interrupt_callback(&slave,&status)==0 && !nested);
 priv.is_tx=true;priv.slave_irq=&domain;
 assert(wcd9380_interrupt_callback(&slave,&status)==-ENODEV && !nested);
 slave.dev.regmap=&map;priv.slave_irq=NULL;
 assert(wcd9380_interrupt_callback(&slave,&status)==-ENODEV && !nested);
 priv.slave_irq=&domain;domain.mapping=0;
 assert(wcd9380_interrupt_callback(&slave,&status)==-ENXIO && !nested);
 domain.mapping=17;status.control_port=3;
 assert(wcd9380_interrupt_callback(&slave,&status)==0 && !reads);
 status.control_port=7;
 for(int n=1;n<=8;n++) {
  reset_irq_test();clear_after=n;
  assert(wcd9380_interrupt_callback(&slave,&status)==IRQ_HANDLED);
  assert(nested==n && reads==3*n && !errors);
 }
 for(int n=1;n<=3;n++) {
  reset_irq_test();fail_read=n;
  assert(wcd9380_interrupt_callback(&slave,&status)==-EIO);
  assert(reads==n && nested==1 && errors==1);
 }
 reset_irq_test();clear_after=99;
 assert(wcd9380_interrupt_callback(&slave,&status)==-ETIMEDOUT);
 assert(nested==8 && reads==24 && errors==1);
 int versions[]={13,17,20};
 for(int v=0;v<3;v++)for(int reset=0;reset<2;reset++)for(int wake=0;wake<2;wake++) {
  struct qcom_swrm_ctrl c={.irq=11,.wake_irq=wake?7:0,.version=versions[v],
   .hclk=&c,.clock_stop_not_supported=reset,.reg_write=write_reg,.reg_read=read_reg};
  struct device d={.data=&c};
  for(int i=0;i<16;i++)c.reg_layout[i]=100+i;
  clock_on=irq_on=true;wake_on=late_irq=false;
  reset_path=reset;frame_running=true;frame_pending=false;
  clk_fail=prep_result=stop_result=exit_result=disable_count=enable_count=drained=0;
  for(int cycle=0;cycle<5;cycle++) {
   assert(!swrm_runtime_suspend(&d));
   assert(!clock_on && !irq_on && !late_irq);
   assert(disable_count==cycle+1 && drained==(reset?0:cycle+1));
   assert(!swrm_runtime_resume(&d));
   assert(clock_on && irq_on && !wake_on && enable_count==cycle+1);
  }
  assert(!swrm_runtime_suspend(&d));clk_fail=-EIO;
  assert(swrm_runtime_resume(&d)==-EIO && !clock_on && !irq_on && enable_count==5);
  assert(wake_on==!!wake);
  clk_fail=0;assert(!swrm_runtime_resume(&d));
  if(!reset) {
   prep_result=-EIO;
   assert(swrm_runtime_suspend(&d)==-EIO && clock_on && irq_on);
   prep_result=0;stop_result=-EIO;
   assert(swrm_runtime_suspend(&d)==-EIO && clock_on && irq_on);
   stop_result=-ENODATA;prep_result=-ENODATA;
   assert(!swrm_runtime_suspend(&d) && !clock_on && !irq_on && !late_irq);
  }
 }
 /* LPASS restart with retained software handles requires full bus init. */
 struct qcom_swrm_ctrl c={.irq=11,.wake_irq=7,.version=17,.hclk=&c,
  .reg_write=write_reg,.reg_read=read_reg,.audio_generation=1,.slave_status=4,.bus={.slaves=1}};
 struct device d={.data=&c};
 for(int i=0;i<16;i++)c.reg_layout[i]=100+i;
 clock_on=irq_on=late_irq=frame_running=frame_pending=false;wake_on=true;
 clk_fail=prep_result=stop_result=0;board=true;reset_path=true;generation=2;
 transport_ready=false;int saved=writes;
 assert(swrm_runtime_resume(&d)==-EBUSY && writes==saved && wake_on);
 transport_ready=true;pin_hw=0;pin_state=1;
 for(pin_fail=1;pin_fail<=2;pin_fail++) {
  assert(swrm_runtime_resume(&d)==-EIO && !clock_on && !irq_on && wake_on);
  assert(c.audio_generation==1);
 }
 pin_fail=0;
 assert(!swrm_runtime_resume(&d) && c.audio_generation==2);
 assert(pin_hw==1 && pin_state==1 && pin_switches==5);
 assert(c.bus.params.curr_bank==0 && c.bus.params.next_bank==1);
 transport_ready=false;assert(!swrm_runtime_suspend(&d));
 transport_ready=true;generation++;frame_failure=true;
 assert(swrm_runtime_resume(&d)==-ETIMEDOUT && !clock_on && !irq_on && wake_on);
 assert(c.audio_generation==2);frame_failure=false;enum_failure=true;
 assert(swrm_runtime_resume(&d)==-ETIMEDOUT && !clock_on && !irq_on && wake_on);
 assert(c.audio_generation==2);enum_failure=false;
 assert(!swrm_runtime_resume(&d) && c.audio_generation==3);
}
'''
    run_c(code, timeout=10)
    result = dict(all_passed=True, actual_kernel_functions_executed=True,
                  rx_null_regmap_avoided=True, tx_errors_and_stuck_status_covered=True,
                  runtime_pm_combinations=12, successful_stop_start_cycles=60,
                  late_irq_drained_before_clock_off=True, hardware_validation_required=True)
    result.update(frame_stop_before_clock_gate=True,
                  mode0_irq_after_frame_restart=True, wake_irq_restored_on_clock_failure=True)
    result.update(reinitialize_after_dsp_restart=True, offline_resume_refused=True,
                  failed_reinitialization_retried=True,
                  lost_pin_mux_restored_before_enumeration=True,
                  pin_restore_failure_unwinds_clocks_and_retries=True)
    return result


def check_fifo(args):
    s = (args.source/'drivers/soundwire/qcom.c').read_text()
    code = r'''
    #include <assert.h>
    #include <stdbool.h>
    #include <stdint.h>
    #include <stddef.h>
    #include <errno.h>
    #include <stdio.h>
    typedef uint8_t u8;
    typedef uint16_t u16;
    typedef uint32_t u32;
    typedef int irqreturn_t;
    #define BIT(n) (1U << (n))
    #define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
    #define FIELD_GET(mask,value) (((value) & (mask)) >> __builtin_ctz(mask))
    enum { SDW_BROADCAST_DEV_NUM=15, SDW_CMD_OK=0, SDW_CMD_FAIL_OTHER=1,
     SDW_CMD_IGNORED=2, IRQ_HANDLED=1, IRQ_NONE=0 };
    '''
    code += ''.join(line+'\n' for line in s.replace('\\\n','').splitlines() if line.startswith('#define'))
    code += re.search(r'enum \{\n\s*SWRM_REG_FRAME_GEN_ENABLED,.*?\n\};',s,re.S).group(0)
    for name in ['swrm_v1_3_reg_layout', 'swrm_v1_7_reg_layout', 'swrm_v2_0_reg_layout']:
        code += re.search(r'static const unsigned int '+name+r'\[\] = \{.*?\n\};',s,re.S).group(0)
    code += r'''
    struct bus { int unused; };
    struct qcom_swrm_ctrl {
     void *dev,*hclk;
     struct bus bus;
     unsigned int version,intr_mask,slave_status;
     int status[16],broadcast;
     u8 wcmd_id,rcmd_id;
     u32 wr_fifo_depth;
     const unsigned int *reg_layout;
     int (*reg_read)(struct qcom_swrm_ctrl *,int,u32 *);
     int (*reg_write)(struct qcom_swrm_ctrl *,int,int);
    };
    static int errors,enumerated,slave_alerts,clocks;
    static u32 pending,acked,last_write,last_read,read_response,cpu_mask;
    static bool frame_running;
    static bool liuqin_audio_transport_ready(void){return true;}
    static irqreturn_t qcom_swrm_irq_handler(int irq,void *data);
    static int clk_prepare_enable(void *p){(void)p;clocks++;return 0;}
    static void clk_disable_unprepare(void *p){(void)p;clocks--;}
    static void complete(int *p){(*p)++;}
    static void reinit_completion(int *p){*p=0;}
    static int wait_for_completion_timeout(int *p,int ms){(void)ms;return *p;}
    static int msecs_to_jiffies(int n){return n;}
    static void usleep_range(int a,int b){(void)a;(void)b;}
    #define dev_err(d,fmt,...) ((void)(d),(void)(fmt),errors++)
    #define dev_err_ratelimited(d,fmt,...) dev_err(d,fmt,##__VA_ARGS__)
    #define dev_dbg(d,fmt,...) ((void)(d),(void)(fmt))
    #define dev_dbg_ratelimited(d,fmt,...) dev_dbg(d,fmt,##__VA_ARGS__)
    static int qcom_swrm_get_alert_slave_dev_num(struct qcom_swrm_ctrl *c){(void)c;return 1;}
    static void sdw_handle_slave_status(struct bus *b,int *s){(void)b;(void)s;slave_alerts++;}
    static void qcom_swrm_get_device_status(struct qcom_swrm_ctrl *c){(void)c;}
    static void qcom_swrm_enumerate(struct bus *b){(void)b;enumerated++;}
    static int read_reg(struct qcom_swrm_ctrl *c,int reg,u32 *value){
     if(reg==(int)c->reg_layout[SWRM_REG_INTERRUPT_STATUS])*value=pending;
     else if(reg==(int)c->reg_layout[SWRM_REG_CMD_FIFO_STATUS])*value=BIT(16);
     else if(reg==(int)c->reg_layout[SWRM_REG_CMD_FIFO_RD_FIFO_ADDR])*value=read_response;
     else if(reg==SWRM_MCP_SLV_STATUS)*value=4;
     else assert(0);
     return 0;
    }
    static int write_reg(struct qcom_swrm_ctrl *c,int reg,int value){
     if(reg==(int)c->reg_layout[SWRM_REG_INTERRUPT_CLEAR]){acked|=value;pending&=~value;}
     else if(reg==(int)c->reg_layout[SWRM_REG_INTERRUPT_CPU_EN])cpu_mask=value;
     else if(reg==(int)c->reg_layout[SWRM_REG_CMD_FIFO_WR_CMD]){
      last_write=reg;
      if((value & 0x00ff0000)==0x00ff0000){
       if(c->version==SWRM_VERSION_1_7_0){
        assert(reg==0x31c);pending|=BIT(20);
       }else pending|=BIT(10);
       if((u32)value==0x02ff0044)frame_running=false;
       qcom_swrm_irq_handler(1,c);
      }
     }else if(reg==(int)c->reg_layout[SWRM_REG_CMD_FIFO_RD_CMD]){
      last_read=reg;read_response=0xab|(c->rcmd_id<<8);
     }else assert(reg==SWRM_CMD_FIFO_CMD);
     return 0;
    }
    '''
    for name in ['swrm_get_packed_reg_val','swrm_wait_for_rd_fifo_avail',
                 'swrm_wait_for_wr_fifo_avail','swrm_wait_for_wr_fifo_done',
                 'qcom_swrm_irq_status','qcom_swrm_irq_mask','qcom_swrm_irq_handler',
                 'qcom_swrm_cmd_fifo_wr_cmd','qcom_swrm_cmd_fifo_rd_cmd']:
        match=re.search(r'^static (?:int|bool|u32|irqreturn_t)\s+'+name+r'\([^;{]*\)\s*\{',s,re.M)
        assert match, name
        end=match.end();depth=1
        while depth:
            depth+=(s[end]=='{')-(s[end]=='}');end+=1
        code += s[match.start():end]+'\n'
    code += r'''
    int main(void){
     struct qcom_swrm_ctrl c={.reg_read=read_reg,.reg_write=write_reg,.wr_fifo_depth=16};
     unsigned int versions[]={SWRM_VERSION_1_3_0,SWRM_VERSION_1_7_0,SWRM_VERSION_2_0_0};
     const unsigned int *layouts[]={swrm_v1_3_reg_layout,swrm_v1_7_reg_layout,swrm_v2_0_reg_layout};
     for(unsigned int v=0;v<3;v++){
      c.version=versions[v];c.reg_layout=layouts[v];c.intr_mask=qcom_swrm_irq_mask(&c);
      for(int n=0;n<12;n++){
       errors=clocks=0;acked=pending=0;frame_running=true;
       assert(qcom_swrm_cmd_fifo_wr_cmd(&c,2,15,0x44)==SDW_CMD_OK);
       assert(!frame_running && c.broadcast==1 && !errors && !clocks);
       assert(acked==(v==1?BIT(20):BIT(10)));
       u8 byte=0;assert(qcom_swrm_cmd_fifo_rd_cmd(&c,1,0x41,1,&byte)==0);
       assert(byte==0xab);
       if(v==1){assert(last_write==0x31c && last_read==0x320);assert(c.reg_layout[SWRM_REG_CMD_FIFO_STATUS]==0x328);}
      }
      errors=0;pending=v==1?BIT(18)|BIT(19):BIT(4)|BIT(5);acked=0;
      qcom_swrm_irq_handler(1,&c);assert(errors==2 && !pending && !clocks);
      assert(acked==(v==1?BIT(18)|BIT(19):BIT(4)|BIT(5)));
      errors=0;pending=BIT(3);qcom_swrm_irq_handler(1,&c);
      assert(errors==1 && !(c.intr_mask & BIT(3)) && cpu_mask==c.intr_mask);
      if(v==1){
       c.broadcast=0;pending=BIT(10);qcom_swrm_irq_handler(1,&c);
       assert(c.broadcast==0 && !pending);
      }
      errors=0;slave_alerts=0;pending=BIT(0);qcom_swrm_irq_handler(1,&c);
      assert(slave_alerts==1 && !errors && !clocks);
     }
     return 0;
    }
    '''
    run_c(code, flags=('-Wno-unused-parameter',), timeout=15)
    report=dict(all_passed=True,actual_kernel_functions_executed=True,
                versions=["1.3","1.7","2.0"],broadcast_cycles=36,
                cpu1_fifo_and_completion=True,cpu0_completion_ignored_on_v17=True,
                fifo_errors_and_active_bus_clash_reported=True,
                original_hardware_status_acknowledged=True,hardware_validation_required=True)
    return report


def check_codec_reset(args):
    source = (args.source/'sound/soc/codecs/wcd938x.c').read_text()
    code = r'''
#include <assert.h>
#include <stdbool.h>
struct regmap { bool cache_only, dirty; };
struct wcd938x_priv { struct regmap *regmap;unsigned int audio_generation;bool mbhc_disabled; };
static bool board=true;
static unsigned int generation=1;
static int resets,settles;
static bool liuqin_board_quirks(void) { return board; }
static unsigned int liuqin_audio_generation(void) { return generation; }
static void regcache_cache_only(struct regmap *m,bool only) { m->cache_only=only; }
static void regcache_mark_dirty(struct regmap *m) { m->dirty=true; }
static int wcd938x_reset(struct wcd938x_priv *w) {
 assert(w->regmap->cache_only && w->regmap->dirty);resets++;return 0;
}
static void usleep_range(int low,int high) { assert(low>=10000 && high>=low);settles++; }
'''
    code += function(source, 'wcd938x_prepare_recovery')
    code += function(source, 'wcd938x_headphone_irqs_enabled')
    code += r'''
int main(void) {
 struct regmap map={0};struct wcd938x_priv w={.regmap=&map,.audio_generation=1};
 assert(wcd938x_headphone_irqs_enabled(&w));w.mbhc_disabled=true;
 assert(!wcd938x_headphone_irqs_enabled(&w));board=false;
 assert(wcd938x_headphone_irqs_enabled(&w));board=true;
 wcd938x_prepare_recovery(&w);assert(!resets && !settles);
 /* Ordinary sleep must preserve the codec and its runtime stream state. */
 for(int i=0;i<100;i++)wcd938x_prepare_recovery(&w);
 assert(!resets);
 generation=2;wcd938x_prepare_recovery(&w);assert(resets==1 && settles==1);
 /* A failed rebind has not committed the new generation and must retry. */
 wcd938x_prepare_recovery(&w);assert(resets==2 && settles==2);
 w.audio_generation=2;wcd938x_prepare_recovery(&w);assert(resets==2);
 generation=3;board=false;wcd938x_prepare_recovery(&w);assert(resets==2);
 return 0;
}
'''
    run_c(code)
    result = {'status':'PASS','actual_kernel_functions_executed':True,
              'sleep_does_not_reset_codec':True,'reset_precedes_rebind':True,
              'failed_rebind_retries_reset':True,'other_boards_unchanged':True}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--dtb', type=Path, help='Compiled candidate DTB for pin checks')
    parser.add_argument('--stock-overlay', type=Path, help='Pinned Liuqin DTBO entry 15')
    args = parser.parse_args()
    if args.report:
        args.report.unlink(missing_ok=True)
    if bool(args.dtb) != bool(args.stock_overlay):
        parser.error('--dtb and --stock-overlay must be provided together')
    checks = {
        'irq_and_clock_stop': check_irq_and_clock_stop(args),
        'fifo': check_fifo(args),
        'codec_reset': check_codec_reset(args)
    }
    if args.dtb:
        checks['pins'] = check_pins(args)
    write_report(checks, args.report)


if __name__ == '__main__':
    main()
