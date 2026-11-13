#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise four-microphone routing, shared ADC clocks and HPF/offline lifetimes."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile
import hashlib
import importlib.util
import struct

from kernel_test import function, run_c, write_report


def stock_adc_transport(path):
    """Read UC0 from the actual tablet ROM, not the generic Cape source."""
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87'
    spec = importlib.util.spec_from_file_location('display', Path(__file__).with_name('display-refresh-rates.py'))
    display = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(display)
    nodes = display.dtb_tree(path)
    assert nodes['/']['xiaomi,miboard-id'] == struct.pack('>II', 0x10, 0)
    props = [p for p in nodes.values() if 'qcom,swr-tx-port-params' in p]
    assert len(props) == 1
    params = struct.unpack('>32I', props[0]['qcom,swr-tx-port-params'])
    return tuple((slave, params[2 * slave + 1], params[2 * slave]) for slave in range(2))


def check_transport(source, adc_transport=((0, 1, 0), (1, 2, 0))):
    """Follow WCD's fixed slave-port/channel masks through the board map."""
    dts = (source / 'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts').read_text()
    match = re.search(r'qcom,tx-port-mapping\s*=\s*<([^>]+)>', dts)
    ports = [int(x, 0) for x in match[1].split()]
    # ADC1/2 occupy slave port 1 bits 0/1; ADC3/4 occupy port 2 bits 0/1.
    # Mainline cannot merge those identical bit masks into one master port.
    assert ports[0] != ports[1], 'ADC slave ports collide at the master'
    tx = dts[dts.index('&swr2 {'):]
    def values(prop):
        m = re.search(re.escape(prop) + r'\s*=\s*/bits/ 8\s*<([^>]+)>', tx)
        assert m, prop
        return [int(x, 0) for x in m[1].split()]
    lanes = values('qcom,ports-lane-control')
    offsets = values('qcom,ports-offset1')
    intervals = values('qcom,ports-sinterval-low')
    ucm = (Path(__file__).resolve().parents[1] / 'device/board-overlay/usr/share/alsa/ucm2/Qualcomm/sm8450/Xiaomi-Pad-6-Pro/HiFi.conf').read_text()
    smic = {int(dec): int(inp) for dec, inp in re.findall(r"TX SMIC MUX(\d)' SWR_MIC(\d+)", ucm)}
    assert 'CaptureMixerElem' not in ucm, 'ADC port switches must not be desktop volume controls'
    # Liuqin OS2.0.6.0.VMYCNXM DTBO entry 15, qcom,swr-tx-port-params UC0.
    # The generic Cape source uses a different second ADC data lane/offset.
    for slave, lane, offset in adc_transport:
        port = ports[slave]
        assert 1 <= port <= 4
        assert (lanes[port-1], offsets[port-1], intervals[port-1]) == (lane, offset, 1)
        for channel in range(2):
            adc = slave * 2 + channel + 1
            inp = (port - 1) * 4 + channel
            assert f'"TX SWR_INPUT{inp}", "ADC{adc}_OUTPUT"' in dts
            assert smic[adc-1] == inp, 'UCM selects a different SoundWire input'


def defines(source):
    lines = iter(source.splitlines())
    result = []
    for line in lines:
        if not line.startswith('#define'):
            continue
        result.append(line)
        while line.endswith('\\'):
            line = next(lines)
            result.append(line)
    return '\n'.join(result) + '\n'


COMMON = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint16_t u16;
#define BIT(b) (1UL << (b))
#define GENMASK(h,l) ((~0UL << (l)) & (~0UL >> (63-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define test_bit(b,p) (!!(*(p)&BIT(b)))
#define set_bit(b,p) (*(p)|=BIT(b))
#define clear_bit(b,p) (*(p)&=~BIT(b))
#define for_each_set_bit(b,p,n) for ((b)=0;(b)<(n);(b)++) if (test_bit(b,p))
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
enum { SND_SOC_DAPM_PRE_PMU=1, SND_SOC_DAPM_POST_PMU=2,
       SND_SOC_DAPM_PRE_PMD=4, SND_SOC_DAPM_POST_PMD=8 };
#define SND_SOC_DAPM_EVENT_ON(e) ((e)&3)
struct regmap { unsigned value[0x4000];bool cache_only,dirty;int hardware,writes,fail_reg; };
struct snd_soc_component { void *priv;struct regmap *map; };
struct snd_soc_dapm_context { struct snd_soc_component *component; };
struct snd_soc_dapm_widget { struct snd_soc_dapm_context *dapm;unsigned shift;const char *name; };
struct snd_kcontrol { int unused; };
#define snd_soc_dapm_to_component(d) ((d)->component)
#define snd_soc_component_get_drvdata(c) ((c)->priv)
static bool board=true,ready=true;
static bool liuqin_board_quirks(void) { return board; }
static bool liuqin_audio_transport_ready(void) { return ready; }
static void regcache_cache_only(struct regmap *m,bool on) { m->cache_only=on; }
static void regcache_mark_dirty(struct regmap *m) { m->dirty=true; }
struct operation { unsigned reg,mask,value; };
static struct operation operations[8192];static unsigned nops;
static int snd_soc_component_update_bits(struct snd_soc_component *c,unsigned r,unsigned mask,unsigned v) {
 struct regmap *m=c->map;assert(r<ARRAY_SIZE(m->value));
 if(board && !ready)assert(m->cache_only);
 if(!m->cache_only)m->hardware++;
 if(m->fail_reg==(int)r){m->fail_reg=0;return -EIO;}
 unsigned old=m->value[r];m->value[r]=(old&~mask)|(v&mask);m->writes++;
 assert(nops<ARRAY_SIZE(operations));operations[nops++]=(struct operation){r,mask,v};
 return old!=m->value[r];
}
static int snd_soc_component_write_field(struct snd_soc_component *c,unsigned r,unsigned m,unsigned v) {
 assert(m);return snd_soc_component_update_bits(c,r,m,v<<__builtin_ctzl(m));
}
static unsigned delays,delay_low,delay_high;
static void usleep_range(unsigned low,unsigned high) {
 assert(low && high>=low);delays++;delay_low=low;delay_high=high;
 assert(nops<ARRAY_SIZE(operations));operations[nops++]=(struct operation){0xffff,low,high};
}
'''


def wcd_fixture(source, header):
    code = COMMON + defines(header) + defines(source)
    for start in ('WCD_ADC1', 'ADC_MODE_INVALID', 'AIF1_PB'):
        code += re.search(r'enum \{\s*' + start + r'\b[^}]*};', source)[0] + '\n'
    code += re.search(r'static u8 tx_mode_bit\[\] = \{[^}]*};', source)[0] + '\n'
    code += r'''
#include <pthread.h>
struct mutex { pthread_mutex_t lock; };
static struct mutex *take(struct mutex *m) { assert(!pthread_mutex_lock(&m->lock));return m; }
static void release(struct mutex **m) { assert(!pthread_mutex_unlock(&(*m)->lock)); }
#define guard(type) __attribute__((cleanup(release))) struct mutex *held=take
struct sdw_slave { int bank,reads; };
struct wcd938x_sdw_priv { struct sdw_slave *sdev; };
struct wcd938x_priv {
 struct regmap *regmap;unsigned long status_mask;unsigned tx_mode[4];
 struct wcd938x_sdw_priv *sdw_priv[2];struct mutex micb_lock;
 int micb_ref[4],pullup_ref[4];void *wcd_mbhc;
};
enum { MIC_BIAS_1=1,MIC_BIAS_2,MIC_BIAS_3,MIC_BIAS_4 };
enum { MICB_PULLUP_ENABLE,MICB_PULLUP_DISABLE,MICB_ENABLE,MICB_DISABLE };
enum { WCD_EVENT_POST_MICBIAS_2_ON,WCD_EVENT_POST_DAPM_MICBIAS_2_ON,
 WCD_EVENT_PRE_MICBIAS_2_OFF,WCD_EVENT_POST_MICBIAS_2_OFF,WCD_EVENT_POST_DAPM_MICBIAS_2_OFF };
static void wcd_mbhc_event_notify(void *mbhc,int event) { (void)mbhc;(void)event; }
static int sdw_slave_get_current_bank(struct sdw_slave *s) {
 assert(!board || ready);s->reads++;return s->bank;
}
static const char *strnstr(const char *s,const char *needle,size_t n) {
 const char *p=strstr(s,needle);return p && (size_t)(p-s)+strlen(needle)<=n?p:NULL;
}
'''
    for name in ('wcd938x_get_clk_rate', 'wcd938x_set_swr_clk_rate',
                 'wcd938x_capture_prepare', 'wcd938x_adcs_active', 'wcd938x_tx_swr_ctrl',
                 'wcd938x_get_adc_mode', 'wcd938x_codec_enable_adc',
                 'wcd938x_tx_channel_config', 'wcd938x_adc_enable_req',
                 'wcd938x_micbias_control_unlocked', 'wcd938x_micbias_control',
                 'wcd938x_codec_enable_micbias', 'wcd938x_codec_enable_micbias_pullup'):
        code += function(source, name) + '\n'
    code += r'''
static struct regmap map;
static struct sdw_slave slave;
static struct wcd938x_sdw_priv swr={&slave};
static struct wcd938x_priv wcd={.regmap=&map,.sdw_priv={NULL,&swr},.micb_lock={PTHREAD_MUTEX_INITIALIZER}};
static struct snd_soc_component c={&wcd,&map};
static struct snd_soc_dapm_context dapm={&c};
static struct snd_soc_dapm_widget widget={&dapm,0,"ADC1_MIXER"};
static unsigned clocks(void) { return map.value[WCD938X_DIGITAL_CDC_ANA_CLK_CTL]&0x18; }
static void on(unsigned ch) {
 widget.shift=ch;
 assert(!wcd938x_adc_enable_req(&widget,NULL,SND_SOC_DAPM_PRE_PMU));
 assert(!wcd938x_tx_swr_ctrl(&widget,NULL,SND_SOC_DAPM_PRE_PMU));
 assert(!wcd938x_codec_enable_adc(&widget,NULL,SND_SOC_DAPM_PRE_PMU));
 assert(clocks()==0x18);
}
static void off(unsigned ch) {
 widget.shift=ch;
 assert(!wcd938x_codec_enable_adc(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert(!wcd938x_adc_enable_req(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert(!wcd938x_tx_swr_ctrl(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert(clocks()==((wcd.status_mask&15)?0x18:0));
 assert((wcd.status_mask&~15UL)==BIT(8));
}
int main(void) {
 /* Every nonempty subset, all 24 removal orders, duplicate notifications. */
 for(unsigned mask=1;mask<16;mask++)
 for(unsigned a=0;a<4;a++)for(unsigned b=0;b<4;b++)
 for(unsigned d=0;d<4;d++)for(unsigned e=0;e<4;e++) {
  if(a==b||a==d||a==e||b==d||b==e||d==e)continue;
  nops=0;wcd.status_mask=BIT(8);memset(&map,0,sizeof(map));
  for(unsigned ch=0;ch<4;ch++)if(mask&BIT(ch)){on(ch);on(ch);}
  unsigned order[]={a,b,d,e};
  for(unsigned i=0;i<4;i++)if(mask&BIT(order[i])){off(order[i]);off(order[i]);}
  assert(wcd.status_mask==BIT(8));
 }
 /* An online bus failure remains an error and cannot acquire an ADC vote. */
 widget.shift=0;map.fail_reg=WCD938X_DIGITAL_CDC_ANA_CLK_CTL;
 assert(wcd938x_codec_enable_adc(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-EIO);
 assert(wcd.status_mask==BIT(8));
 map.fail_reg=WCD938X_DIGITAL_SWR_TX_CLK_RATE;
 assert(wcd938x_tx_swr_ctrl(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-EIO);
 nops=0;
 for(unsigned ch=0;ch<4;ch++)on(ch);
 widget.shift=MIC_BIAS_3;
 for(int i=0;i<2;i++)assert(!wcd938x_codec_enable_micbias(&widget,NULL,SND_SOC_DAPM_PRE_PMU));
 assert(!wcd938x_codec_enable_micbias_pullup(&widget,NULL,SND_SOC_DAPM_PRE_PMU));
 assert(wcd.micb_ref[2]==2 && wcd.pullup_ref[2]==1);
 ready=false;int hardware=map.hardware,banks=slave.reads;
 assert(wcd938x_codec_enable_micbias(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-ENETRESET);
 assert(wcd938x_codec_enable_micbias_pullup(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-ENETRESET);
 assert(wcd.micb_ref[2]==2 && wcd.pullup_ref[2]==1);
 widget.shift=0;
 assert(wcd938x_codec_enable_adc(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-ENETRESET);
 assert(wcd938x_adc_enable_req(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-ENETRESET);
 assert(wcd938x_tx_swr_ctrl(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-ENETRESET);
 for(unsigned ch=0;ch<4;ch++)off(ch);
 widget.shift=MIC_BIAS_3;
 assert(!wcd938x_codec_enable_micbias(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert(wcd.micb_ref[2]==1 && (map.value[WCD938X_ANA_MICB3]&0xc0)==0x40);
 assert(!wcd938x_codec_enable_micbias(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert((map.value[WCD938X_ANA_MICB3]&0xc0)==0x80);
 for(int i=0;i<3;i++) {
  assert(!wcd938x_codec_enable_micbias_pullup(&widget,NULL,SND_SOC_DAPM_POST_PMD));
  assert(!wcd938x_codec_enable_micbias(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 }
 assert(!wcd.micb_ref[2] && !wcd.pullup_ref[2] && !(map.value[WCD938X_ANA_MICB3]&0xc0));
 assert(map.cache_only && map.dirty && map.hardware==hardware && slave.reads==banks);
 ready=true;map.cache_only=false;
 for(unsigned ch=0;ch<4;ch++)on(ch);
 for(unsigned ch=0;ch<4;ch++)off(ch);
 /* Board quirk remains scoped; legacy per-channel clock behavior unchanged. */
 board=false;on(0);on(1);widget.shift=0;
 assert(!wcd938x_codec_enable_adc(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert(!(clocks()&0x08));
 return 0;
}
'''
    return code


def tx_fixture(source):
    code = COMMON + defines(source)
    code += r'''
struct work_struct { int unused; };
struct delayed_work { struct work_struct work;bool pending;void (*fn)(struct work_struct *); };
#define to_delayed_work(w) container_of(w,struct delayed_work,work)
struct tx_macro;
'''
    for name in ('tx_mute_work', 'hpf_work'):
        code += re.search(r'struct ' + name + r' \{[^}]*};', source)[0] + '\n'
    code += r'''
struct tx_macro_data { unsigned ver; };
struct snd_soc_dai { struct snd_soc_component *component; unsigned id; };
struct tx_macro {
 struct regmap *regmap;struct snd_soc_component *component;struct tx_macro_data *data;
 struct hpf_work tx_hpf_work[NUM_DECIMATORS];struct tx_mute_work tx_mute_dwork[NUM_DECIMATORS];
 int dec_mode[NUM_DECIMATORS];bool bcs_enable,bcs_clk_en;
 bool dec_active[NUM_DECIMATORS],four_channel_clock;
 unsigned long dec_clock_mask,active_ch_mask[4];
};
enum { TX_MACRO_AIF1_CAP=1, TX_MACRO_DEC_MAX=8 };
enum { MSM_DMIC,SWR_MIC,ANC_FB_TUNE1 };
#define LPASS_VER_9_0_0 90
static unsigned snd_soc_component_read(struct snd_soc_component *c,unsigned r) {
 assert(r<ARRAY_SIZE(c->map->value));return c->map->value[r];
}
static unsigned snd_soc_component_read_field(struct snd_soc_component *c,unsigned r,unsigned mask) {
 return (snd_soc_component_read(c,r)&mask)>>__builtin_ctzl(mask);
}
static int snd_soc_component_write(struct snd_soc_component *c,unsigned r,unsigned v) {
 return snd_soc_component_update_bits(c,r,~0U,v);
}
static bool run_unmute_on_cancel;
static bool cancel_delayed_work_sync(struct delayed_work *d) {
 bool pending=d->pending;
 if(run_unmute_on_cancel && d->pending){d->fn(&d->work);run_unmute_on_cancel=false;}
 d->pending=false;return pending;
}
static void queue_delayed_work(int queue,struct delayed_work *d,unsigned delay) {
 (void)queue;assert(delay && !d->pending);d->pending=true;
}
#define system_freezable_wq 0
#define msecs_to_jiffies(n) (n)
'''
    for name in ('is_amic_enabled', 'tx_macro_capture_prepare', 'tx_macro_amic_hpf_delay',
                 'tx_macro_restore_hpf', 'tx_macro_tx_hpf_corner_freq_callback',
                 'tx_macro_mute_update_callback', 'tx_macro_dec_clock',
                 'tx_macro_enable_dec', 'tx_macro_digital_mute', 'tx_macro_component_remove'):
        code += function(source, name) + '\n'
    assert '.remove = tx_macro_component_remove,' in source
    code += r'''
static struct regmap map;
static struct tx_macro_data data={91};
static struct tx_macro tx={.regmap=&map,.data=&data};
static struct snd_soc_component c={&tx,&map};
static struct snd_soc_dapm_context dapm={&c};
static struct snd_soc_dapm_widget widget={&dapm,0,"DEC0"};
static void init(void) {
 memset(&map,0,sizeof(map));memset(tx.tx_hpf_work,0,sizeof(tx.tx_hpf_work));
 memset(tx.tx_mute_dwork,0,sizeof(tx.tx_mute_dwork));tx.component=&c;nops=0;delays=0;
 memset(tx.dec_active,0,sizeof(tx.dec_active));memset(tx.active_ch_mask,0,sizeof(tx.active_ch_mask));
 tx.dec_clock_mask=0;tx.four_channel_clock=false;
 for(int i=0;i<NUM_DECIMATORS;i++) {
  tx.tx_hpf_work[i].tx=&tx;tx.tx_hpf_work[i].decimator=i;
  tx.tx_hpf_work[i].dwork.fn=tx_macro_tx_hpf_corner_freq_callback;
  tx.tx_mute_dwork[i].tx=&tx;tx.tx_mute_dwork[i].decimator=i;
  tx.tx_mute_dwork[i].dwork.fn=tx_macro_mute_update_callback;
  map.value[CDC_TX_INP_MUX_ADC_MUXn_CFG1(i)]=SWR_MIC;
  map.value[CDC_TXn_TX_PATH_CTL(i)]=4;
 }
}
static void start(unsigned dec) {
 widget.shift=dec;
 assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_PRE_PMU));
 assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_POST_PMU));
 assert(tx.tx_hpf_work[dec].restore_pending && tx.tx_hpf_work[dec].dwork.pending);
 assert(tx.tx_mute_dwork[dec].dwork.pending);
}
static void stop(unsigned dec) {
 widget.shift=dec;
 assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_PRE_PMD));
 assert(map.value[CDC_TXn_TX_PATH_CTL(dec)]&CDC_TXn_PGA_MUTE_MASK);
 assert(!tx.tx_hpf_work[dec].dwork.pending && !tx.tx_mute_dwork[dec].dwork.pending);
 assert(!tx.tx_hpf_work[dec].restore_pending);
 assert(!snd_soc_component_read_field(&c,CDC_TXn_TX_PATH_CFG0(dec),CDC_TXn_HPF_CUT_FREQ_MASK));
 assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_POST_PMD));
 assert(!(map.value[CDC_TXn_TX_PATH_CTL(dec)]&CDC_TXn_CLK_EN_MASK));
}
int main(void) {
 init();
 unsigned rates[]={0,1,3,4,5,6,2,15},lows[]={125,62,31,20,10,5,125,125};
 unsigned highs[]={130,65,32,21,11,6,130,130};
 for(unsigned i=0;i<ARRAY_SIZE(rates);i++) {
  map.value[CDC_TXn_TX_PATH_CTL(0)]=rates[i];nops=0;delays=0;
  tx.tx_hpf_work[0].restore_pending=true;
  tx_macro_tx_hpf_corner_freq_callback(&tx.tx_hpf_work[0].dwork.work);
  assert(delays==1 && delay_low==lows[i] && delay_high==highs[i]);
  assert(nops==4 && operations[1].value==2 && operations[2].reg==0xffff && operations[3].value==1);
  assert(!tx.tx_hpf_work[0].restore_pending);
 }
 /* Rapid close must join an already-running unmute before applying mute. */
 for(unsigned d=0;d<NUM_DECIMATORS;d++) {
  init();start(d);run_unmute_on_cancel=true;stop(d);assert(!run_unmute_on_cancel);
 }
 /* Offline callbacks do nothing; teardown still restores the saved cutoff. */
 init();for(unsigned d=0;d<4;d++)start(d);
 int hardware=map.hardware;ready=false;nops=0;delays=0;
 for(unsigned d=0;d<4;d++) {
  tx_macro_tx_hpf_corner_freq_callback(&tx.tx_hpf_work[d].dwork.work);
  tx_macro_mute_update_callback(&tx.tx_mute_dwork[d].dwork.work);
  /* The work has completed, so cancel returns false. The cutoff is still owed. */
  tx.tx_hpf_work[d].dwork.pending=false;tx.tx_mute_dwork[d].dwork.pending=false;
 }
 assert(!nops && !delays);
 widget.shift=0;
 assert(tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_PRE_PMU)==-ENETRESET);
 assert(tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_POST_PMU)==-ENETRESET);
 for(unsigned d=0;d<4;d++)stop(d);
 assert(map.hardware==hardware && map.cache_only && map.dirty && !delays);
 ready=true;init();for(unsigned d=0;d<4;d++)start(d);
 ready=false;hardware=map.hardware;delays=0;tx_macro_component_remove(&c);
 assert(!tx.component && map.hardware==hardware && !delays);
 for(unsigned d=0;d<NUM_DECIMATORS;d++)
  assert(!tx.tx_hpf_work[d].dwork.pending && !tx.tx_mute_dwork[d].dwork.pending && !tx.tx_hpf_work[d].restore_pending);
 /* Rebinding and a normal long recording restore/unmute all channels again. */
 ready=true;init();
 for(unsigned d=0;d<4;d++) {
  start(d);tx_macro_mute_update_callback(&tx.tx_mute_dwork[d].dwork.work);
  assert(!(map.value[CDC_TXn_TX_PATH_CTL(d)]&CDC_TXn_PGA_MUTE_MASK));
  tx_macro_tx_hpf_corner_freq_callback(&tx.tx_hpf_work[d].dwork.work);stop(d);
 }
 /* The fixed four-slot carrier must survive every partial physical ADC mask. */
 for(unsigned selected=1;selected<16;selected++) {
  init();tx.active_ch_mask[TX_MACRO_AIF1_CAP]=15;
  for(unsigned d=0;d<4;d++)if(selected&BIT(d))start(d);
  assert(tx.dec_clock_mask==15);
  struct snd_soc_dai dai={&c,TX_MACRO_AIF1_CAP};
  assert(!tx_macro_digital_mute(&dai,0,1));
  for(unsigned d=0;d<4;d++)if(!(selected&BIT(d)))
   assert((map.value[CDC_TXn_TX_PATH_CTL(d)]&0x30)==0x30);
  for(unsigned d=0;d<4;d++)if(selected&BIT(d)) {
   widget.shift=d;assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_PRE_PMD));
   assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_POST_PMD));
   unsigned remaining=selected&(~0U<<(d+1));
   assert(tx.dec_clock_mask==(remaining?15:0));
   if(remaining) {
    assert(!tx_macro_digital_mute(&dai,1,1));
    assert(!tx_macro_digital_mute(&dai,0,1));
    assert((map.value[CDC_TXn_TX_PATH_CTL(d)]&0x30)==0x30);
   }
  }
  assert(!tx.four_channel_clock);
 }
 init();tx.active_ch_mask[TX_MACRO_AIF1_CAP]=15;
 for(unsigned d=0;d<4;d++)start(d);
 for(unsigned d=0;d<4;d++) {
  widget.shift=d;assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_PRE_PMD));
  assert(!tx_macro_enable_dec(&widget,NULL,SND_SOC_DAPM_POST_PMD));
  assert(tx.dec_clock_mask==15);start(d);
 }
 hardware=map.hardware;ready=false;tx_macro_component_remove(&c);ready=true;
 assert(map.hardware==hardware && !tx.dec_clock_mask && !tx.four_channel_clock);
 /* Partial clock acquisition failure cannot turn off already-owned clocks. */
 for(unsigned failed=0;failed<4;failed++) {
  init();tx.active_ch_mask[TX_MACRO_AIF1_CAP]=15;
  map.fail_reg=CDC_TXn_TX_PATH_CTL(failed);
  assert(tx_macro_dec_clock(&tx,0,true)==-EIO);
  assert(!tx.dec_clock_mask && !tx.dec_active[0] && !tx.four_channel_clock);
  for(unsigned d=0;d<4;d++)assert(!(map.value[CDC_TXn_TX_PATH_CTL(d)]&CDC_TXn_CLK_EN_MASK));
 }
 /* Non-Liuqin analog HPF handling retains its original zero-delay sequence. */
 init();board=false;tx_macro_tx_hpf_corner_freq_callback(&tx.tx_hpf_work[0].dwork.work);
 assert(!delays);tx_macro_component_remove(&c);assert(tx.component==&c);
 return 0;
}
'''
    return code


def check_routes(args):
    transport = stock_adc_transport(args.stock_overlay) if args.stock_overlay else ((0, 1, 0), (1, 2, 0))
    check_transport(args.source, transport)
    if args.baseline:
        try:
            check_transport(args.baseline)
        except AssertionError as error:
            assert str(error) == 'ADC slave ports collide at the master'
        else:
            raise AssertionError('The old port collision was not detected')
    source = (args.source / 'sound/soc/codecs/lpass-tx-macro.c').read_text()
    body = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
enum { TX_MACRO_DEC_MAX=8, TX_MACRO_MAX_DAIS=4, CDC_TXn_PGA_MUTE_MASK=16 };
struct tx_macro { unsigned long active_ch_mask[4], active_ch_cnt[4]; bool four_channel_clock, dec_active[8]; };
struct snd_soc_component { struct tx_macro *tx; unsigned writes, mask, muted, connected[4]; };
struct snd_soc_dapm_context { struct snd_soc_component *component; };
struct snd_soc_dapm_widget { struct snd_soc_dapm_context *dapm; unsigned shift; };
struct soc_mixer_control { unsigned shift; };
struct snd_kcontrol { struct snd_soc_dapm_widget *widget; uintptr_t private_value; };
struct snd_ctl_elem_value { struct { struct { long value[1]; } integer; } value; };
struct snd_soc_dapm_update { int unused; };
struct snd_soc_dai { struct snd_soc_component *component; int id; };
#define snd_soc_dapm_kcontrol_to_widget(k) ((k)->widget)
#define snd_soc_dapm_to_component(d) ((d)->component)
#define snd_soc_component_get_drvdata(c) ((c)->tx)
#define test_bit(b,m) (!!(*(m)&(1UL<<(b))))
#define set_bit(b,m) (*(m)|=(1UL<<(b)))
#define clear_bit(b,m) (*(m)&=~(1UL<<(b)))
#define for_each_set_bit(b,m,n) for((b)=0;(b)<(n);(b)++) if(test_bit(b,m))
#define CDC_TXn_TX_PATH_CTL(n) (n)
static int updates;
static bool board=true;
static bool liuqin_board_quirks(void) { return board; }
static void snd_soc_dapm_mixer_update_power(struct snd_soc_dapm_context *d, struct snd_kcontrol *k, u32 e, struct snd_soc_dapm_update *u) {
 struct soc_mixer_control *mc=(void *)k->private_value;
 if(e)d->component->connected[k->widget->shift]|=1U<<mc->shift;
 else d->component->connected[k->widget->shift]&=~(1U<<mc->shift);
 updates++;
}
static void snd_soc_component_write_field(struct snd_soc_component *c, unsigned r, unsigned m, unsigned v) {
 assert(r<8 && m==CDC_TXn_PGA_MUTE_MASK && v<=1);
 c->writes++;c->mask|=1U<<r;
 if(v)c->muted|=1U<<r;else c->muted&=~(1U<<r);
}
'''
    for name in ('tx_macro_tx_mixer_get', 'tx_macro_tx_mixer_put', 'tx_macro_digital_mute',
                 'tx_macro_reset_capture_routes'):
        body += function(source, name) + '\n'
    body += r'''
int main(void) {
 struct tx_macro tx={0};struct snd_soc_component c={.tx=&tx};
 struct snd_soc_dapm_context dapm={&c};struct snd_soc_dapm_widget w={&dapm,1};
 struct soc_mixer_control mc={0};struct snd_kcontrol k={&w,(uintptr_t)&mc};
 struct snd_ctl_elem_value value={0},actual={0};struct snd_soc_dai dai={&c,1};
 unsigned cases=0, seed=123456789, expected[4]={0};
 /* All 256 masks: every selected channel participates in stream mute. */
 for(unsigned mask=0;mask<256;mask++) {
  for(unsigned bit=0;bit<8;bit++) {
   mc.shift=bit;value.value.integer.value[0]=!!(mask&(1U<<bit));
   int old=!!(tx.active_ch_mask[1]&(1U<<bit)), before=updates;
   assert(tx_macro_tx_mixer_put(&k,&value)==(old!=value.value.integer.value[0]));
   assert(updates-before==(old!=value.value.integer.value[0]));
   before=updates;assert(!tx_macro_tx_mixer_put(&k,&value)&&updates==before);
   assert(!tx_macro_tx_mixer_get(&k,&actual));assert(actual.value.integer.value[0]==value.value.integer.value[0]);
  }
  assert(tx.active_ch_mask[1]==mask&&tx.active_ch_cnt[1]==__builtin_popcount(mask));
  c.writes=c.mask=0;assert(!tx_macro_digital_mute(&dai,1,1));
  assert(c.mask==mask && c.writes==__builtin_popcount(mask) && (c.muted&mask)==mask);
  c.writes=c.mask=0;assert(!tx_macro_digital_mute(&dai,0,1));
  assert(c.mask==mask && c.writes==__builtin_popcount(mask) && !(c.muted&mask));cases++;
 }
 expected[1]=255;
 /* Interleaved DAIs and arbitrary order, including duplicate disables. */
 for(unsigned i=0;i<10000;i++) {
  seed=seed*1664525U+1013904223U;unsigned id=(seed>>28)&3,bit=(seed>>20)&7,on=(seed>>12)&1;
  w.shift=id;mc.shift=bit;value.value.integer.value[0]=on;
  tx_macro_tx_mixer_put(&k,&value);
  if(on)expected[id]|=1U<<bit;else expected[id]&=~(1U<<bit);
  for(unsigned d=0;d<4;d++)assert(tx.active_ch_mask[d]==expected[d]&&tx.active_ch_cnt[d]==__builtin_popcount(expected[d]));
  cases++;
 }
 /* Reverse-order UCM teardown must empty every mask and count. */
 for(unsigned d=0;d<4;d++) {
  w.shift=d;value.value.integer.value[0]=0;
  for(int bit=7;bit>=0;bit--){mc.shift=bit;tx_macro_tx_mixer_put(&k,&value);}
  assert(!tx.active_ch_mask[d]&&!tx.active_ch_cnt[d]);cases++;
 }
 /* A new card's NOPM paths are disconnected while the macro object survives. */
 for(unsigned n=0;n<100;n++) {
  w.shift=1;value.value.integer.value[0]=1;
  for(unsigned bit=0;bit<4;bit++){mc.shift=bit;tx_macro_tx_mixer_put(&k,&value);}
  assert(tx.active_ch_mask[1]==15 && tx.active_ch_cnt[1]==4);
  memset(c.connected,0,sizeof(c.connected));
  for(unsigned bit=0;bit<4;bit++){mc.shift=bit;assert(!tx_macro_tx_mixer_put(&k,&value));}
  assert(!c.connected[1]); /* reproduce lost DAPM power with a RUNNING PCM */
  tx_macro_reset_capture_routes(&tx);
  for(unsigned d=0;d<4;d++)assert(!tx.active_ch_mask[d] && !tx.active_ch_cnt[d]);
  for(unsigned bit=0;bit<4;bit++){mc.shift=bit;assert(tx_macro_tx_mixer_put(&k,&value)==1);}
  assert(c.connected[1]==15 && tx.active_ch_cnt[1]==4);cases++;
 }
 board=false;tx_macro_reset_capture_routes(&tx);assert(tx.active_ch_mask[1]==15);
 printf("%u\n",cases);
}
'''
    cases = int(run_c(body, flags=('-Wno-unused-parameter', '-Wno-sign-compare'), timeout=10))
    result = dict(all_passed=True, cases=cases, actual_kernel_functions_executed=True,
                  all_channel_masks=True, repeated_enable_disable=True, independent_dais=True,
                  stale_card_routes_reproduced=True, recreated_card_routes_restored=True,
                  separate_adc_transport_verified=True,
                  shipping_rom_checked=bool(args.stock_overlay),
                  collision_baseline_rejected=bool(args.baseline),
                  hardware_validation_required=True)
    return result


def check_channel_map(args):
    source = args.source / 'sound/soc/qcom/qdsp6'
    mapping = (source / 'audioreach.c').read_text()
    driver = (source / 'q6apm-dai.c').read_text()
    assert 'q6apm_dai_set_channel_mapping(&cfg);' in function(driver, 'q6apm_dai_prepare')
    body = r'''
#include <assert.h>
#include <stdbool.h>
#include <string.h>
typedef unsigned char u8;
enum { PCM_CHANNEL_FL=1, PCM_CHANNEL_FR=2, PCM_CHANNEL_FC=3,
       PCM_CHANNEL_LS=4, PCM_CHANNEL_RS=5, SNDRV_PCM_STREAM_CAPTURE=1 };
struct audioreach_module_config { u8 channel_map[32]; int num_channels, direction; };
static bool board;
static bool liuqin_board_quirks(void) { return board; }
'''
    body += function(mapping, 'audioreach_set_default_channel_mapping') + '\n'
    body += function(driver, 'q6apm_dai_set_channel_mapping') + '\n'
    body += r'''
int main(void) {
 int sizes[]={1,2,4};
 for (int b=0;b<2;b++) for (int stream=0;stream<2;stream++) for (int n=0;n<3;n++) {
  struct audioreach_module_config cfg={.num_channels=sizes[n],.direction=stream};
  u8 expected[32]={0};board=b;
  audioreach_set_default_channel_mapping(expected,sizes[n]);
  if(b && stream==SNDRV_PCM_STREAM_CAPTURE && sizes[n]==4)
   for(int i=0;i<4;i++)expected[i]=i+1;
  q6apm_dai_set_channel_mapping(&cfg);
  assert(!memcmp(cfg.channel_map,expected,sizeof(expected)));
 }
 /* The codec-DMA source labels raw channels 1/2/3/4. A converter matches
  * these identities; requesting 1/2/4/5 loses mic 3 and inserts silence. */
 int input[]={0,101,202,303,404,0};
 u8 old[32]={0};audioreach_set_default_channel_mapping(old,4);
 assert(input[old[2]]==404 && input[old[3]]==0);
 board=true;
 struct audioreach_module_config cfg={.num_channels=4,.direction=SNDRV_PCM_STREAM_CAPTURE};
 q6apm_dai_set_channel_mapping(&cfg);
 for(int i=0;i<4;i++)assert(input[cfg.channel_map[i]]==(i+1)*101);
}
'''
    run_c(body, timeout=10)
    result = dict(all_passed=True, actual_kernel_functions_executed=True,
                  scope_cases=12, raw_microphone_identity_preserved=True,
                  old_missing_channel_reproduced=True, hardware_validation_required=True)
    return result


def check_lifecycle(args):
    codecs = args.source/'sound/soc/codecs'
    fixtures = {
        'wcd': wcd_fixture((codecs/'wcd938x.c').read_text(), (codecs/'wcd938x.h').read_text()),
        'tx': tx_fixture((codecs/'lpass-tx-macro.c').read_text()),
    }
    with tempfile.TemporaryDirectory(prefix='liuqin-mic-lifecycle-') as tmp:
        tmp = Path(tmp)
        for name, code in fixtures.items():
            path = tmp/(name+'.c');path.write_text(code)
            subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                            '-fsanitize=undefined','-pthread',str(path),'-o',str(tmp/name)],check=True)
            subprocess.run([str(tmp/name)],check=True,timeout=20)
    result = {'status':'PASS','actual_kernel_functions_executed':True,
              'adc_subsets_and_removal_orders':360,'shared_clocks_survive_partial_shutdown':True,
              'offline_rejects_new_votes_and_retires_cached_votes':True,
              'online_bus_errors_propagated':True,'oem_hpf_rate_delays':True,
              'rapid_close_joins_unmute':True,'offline_hpf_cutoff_restored':True,
              'four_slot_carrier_keeps_muted_channels_clocked':True,
              'component_remove_drains_work':True,'other_boards_unchanged':True}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--baseline', type=Path, help='Old source with colliding ADC ports')
    parser.add_argument('--stock-overlay', type=Path, help='Pinned Liuqin DTBO entry 15')
    args = parser.parse_args()
    if args.report:
        args.report.unlink(missing_ok=True)
    checks = {
        'routes': check_routes(args),
        'channel_map': check_channel_map(args),
        'lifecycle': check_lifecycle(args)
    }
    write_report(checks, args.report)


if __name__ == '__main__':
    main()
