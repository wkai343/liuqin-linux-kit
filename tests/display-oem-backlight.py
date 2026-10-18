#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the actual backlight state machine against fault-injected I2C."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile

def function(s,n):
 m=re.search(r'^static (?:int|bool)\s+'+n+r'\([^;{]*\)\s*\{',s,re.M);assert m,n
 p=m.end();d=1
 while d:d+=(s[p]=='{')-(s[p]=='}');p+=1
 return s[m.start():p]

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--cc',default='cc');p.add_argument('--report',type=Path,required=True);a=p.parse_args()
 s=(a.source/'drivers/video/backlight/ktz8866.c').read_text();old=(a.baseline/'drivers/video/backlight/ktz8866.c').read_text()
 for key in ['DEFAULT_BRIGHTNESS','MAX_BRIGHTNESS','LCD_BIAS_EN','PWM_HYST']:
  assert re.search(r'^#define '+key+r'.*$',s,re.M).group()==re.search(r'^#define '+key+r'.*$',old,re.M).group()
 dts=(a.source/'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts').read_text()
 assert 'current-num-sinks = <6>;' in dts and 'kinetic,blanked-current-sink-mask = <0x1f>;' in dts and 'kinetic,backlight-disable-delay-ms = <10>;' in dts
 assert '.precious_reg = ktz8866_precious_reg' in s
 defs='\n'.join(re.findall(r'^#define [^\n]+',s,re.M))
 struct=re.search(r'struct ktz8866 \{.*?\n\};',s,re.S).group()
 core='\n'.join(function(s,n) for n in ['ktz8866_precious_reg','ktz8866_write','ktz8866_set_brightness','ktz8866_backlight_update_status','ktz8866_init'])
 cfg=re.search(r'static const struct regmap_config ktz8866_regmap_config = \{.*?\n\};',s,re.S).group()
 ops=re.search(r'static const struct backlight_ops ktz8866_backlight_ops = \{.*?\n\};',s,re.S).group()
 prefix=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;typedef uint32_t u32;
#define BIT(n) (1UL<<(n))
#define GFP_KERNEL 0
#define GPIOD_OUT_HIGH 1
#define BACKLIGHT_RAW 1
#define BACKLIGHT_POWER_OFF 4
#define BACKLIGHT_SCALE_LINEAR 1
#define BL_CORE_SUSPENDRESUME 1
#define EPROBE_DEFER 517
#define IS_ERR(x) ((intptr_t)(x)<0 && (intptr_t)(x)>-4096)
#define PTR_ERR(x) ((int)(intptr_t)(x))
static void mock_dev_info(const char *fmt, ...) __attribute__((format(printf,1,2)));
static void mock_dev_info(const char *fmt, ...) {(void)fmt;}
#define dev_info(dev, ...) mock_dev_info(__VA_ARGS__)
#define dev_err_probe(dev,error,...) (error)
static unsigned int ilog2(unsigned int n){assert(n);return 31-__builtin_clz(n);}
struct device_node {int sinks,mask,delay,ramp,led_ramp;bool bias,initial_off;};
struct device {struct device_node *of_node;};
struct regmap {int id;unsigned int values[0x16];};
struct i2c_device_id {unsigned long driver_data;};
struct i2c_client {struct device dev;struct i2c_device_id id;struct regmap *map;void *data;};
struct gpio_desc {int unused;};
struct ktz8866;
struct backlight_properties {int type,max_brightness,brightness,power,scale;};
struct backlight_device;
struct backlight_ops {int options;int (*update_status)(struct backlight_device*);};
struct backlight_device {struct backlight_properties props;struct ktz8866 *data;const struct backlight_ops *ops;};
struct regmap_config {int reg_bits,val_bits,max_register;bool (*precious_reg)(struct device*,unsigned int);};
struct op {char kind;unsigned int map,reg,val;};
static struct op trace[256];static int ntrace,writes,fail_at=-1,fail_after,reg_fail,published,cases;
static struct regmap maps[2];static struct device_node node;static struct i2c_client client;
static struct gpio_desc gpio;static struct backlight_device bl;static unsigned int published_mask,published_brightness;
static void *allocations[64];static int nalloc;
static int regmap_write(struct regmap *m,unsigned int reg,unsigned int val){
 assert(reg<=0x15 && reg!=0x0d && reg!=0x0e);assert(ntrace<256);trace[ntrace++]=(struct op){'W',m->id,reg,val};
 bool error=writes++==fail_at;if(!error || fail_after)m->values[reg]=val;return error?-EIO:0;
}
static void usleep_range(unsigned long lo,unsigned long hi){assert(ntrace<256);trace[ntrace++]=(struct op){'D',0,lo,hi};}
static int of_property_read_u32(struct device_node *n,const char *name,unsigned int *out){
 int v=-1;if(!strcmp(name,"current-num-sinks"))v=n->sinks;
 if(!strcmp(name,"kinetic,blanked-current-sink-mask"))v=n->mask;
 if(!strcmp(name,"kinetic,backlight-disable-delay-ms"))v=n->delay;
 if(!strcmp(name,"kinetic,current-ramp-delay-ms"))v=n->ramp;
 if(!strcmp(name,"kinetic,led-enable-ramp-delay-ms"))v=n->led_ramp;
 if(v<0)return -EINVAL;*out=v;return 0;
}
static bool of_property_read_bool(struct device_node *n,const char *name){return !strcmp(name,"kinetic,enable-lcd-bias")?n->bias:n->initial_off;}
static struct ktz8866 *bl_get_data(struct backlight_device *b){return b->data;}
static unsigned int backlight_get_brightness(struct backlight_device *b){return b->props.power?0:b->props.brightness;}
static const struct i2c_device_id *i2c_client_get_device_id(struct i2c_client *c){return &c->id;}
static void *devm_kzalloc(struct device *d,size_t size,int flag){(void)d;(void)flag;assert(nalloc<64);void *v=calloc(1,size);assert(v);allocations[nalloc++]=v;return v;}
static struct regmap *devm_regmap_init_i2c(struct i2c_client *c,const struct regmap_config *cfg){assert(cfg->precious_reg(&c->dev,15));assert(!cfg->precious_reg(&c->dev,8));return c->map;}
static int devm_regulator_get_enable(struct device *d,const char *s){(void)d;(void)s;return 0;}
static struct gpio_desc *devm_gpiod_get_optional(struct device *d,const char *s,int flag){(void)d;(void)s;(void)flag;return &gpio;}
static struct backlight_device *devm_backlight_device_register(struct device *d,const char *name,struct device *parent,struct ktz8866 *ktz,const struct backlight_ops *ops,const struct backlight_properties *props){
 (void)d;(void)name;(void)parent;published_mask=maps[0].values[8];published_brightness=maps[0].values[4]|(maps[0].values[5]<<3);
 if(reg_fail)return (void*)(intptr_t)-ENOMEM;published++;bl=(struct backlight_device){.props=*props,.data=ktz,.ops=ops};return &bl;
}
static void i2c_set_clientdata(struct i2c_client *c,void *data){c->data=data;}
'''
 cases=r'''
static struct ktz8866 ktz,slave;
static void reset(bool oem,bool dual){
 ktz_b=NULL;for(int i=0;i<nalloc;i++)free(allocations[i]);nalloc=0;
 memset(maps,0,sizeof(maps));maps[0].id=0;maps[1].id=1;
 node=(struct device_node){.sinks=oem?6:5,.mask=oem?31:-1,.delay=oem?10:-1,.ramp=128,.led_ramp=1,.bias=true,.initial_off=true};
 client=(struct i2c_client){.dev={&node},.map=&maps[0]};
 ktz=(struct ktz8866){.client=&client,.regmap=&maps[0]};slave=(struct ktz8866){.client=&client,.regmap=&maps[1]};
 if(dual)ktz_b=&slave;ntrace=writes=reg_fail=published=fail_after=0;fail_at=-1;cases++;
}
static void clear_trace(void){ntrace=writes=0;fail_at=-1;fail_after=0;}
static void ready(bool dual){reset(true,dual);assert(!ktz8866_init(&ktz));clear_trace();}
static void check_dark(int count){for(int i=0;i<count;i++){assert(maps[i].values[8]==31);assert(!maps[i].values[4] && !maps[i].values[5]);}}
static void check_lit(int count,unsigned int val){for(int i=0;i<count;i++){assert(maps[i].values[8]==127);assert((maps[i].values[4]|maps[i].values[5]<<3)==val);}}
int main(void){
 reset(true,false);assert(!ktz8866_init(&ktz));assert(ktz.active_sink_mask==63 && ktz.blanked_sink_mask==31 && ktz.disable_delay_ms==10);assert(writes==4 && maps[0].values[3]==0xbd && maps[0].values[20]==0x11 && maps[0].values[9]==0x9f);clear_trace();
 assert(!ktz8866_set_brightness(&ktz,1500));check_lit(1,1500);assert(ntrace==3 && trace[0].reg==8 && trace[0].val==127);clear_trace();
 assert(!ktz8866_set_brightness(&ktz,500));check_lit(1,500);assert(ntrace==2 && trace[0].reg==4);clear_trace();
 assert(!ktz8866_set_brightness(&ktz,0));check_dark(1);assert(ntrace==4 && trace[0].val==31 && trace[1].kind=='D' && trace[1].reg==10000 && trace[1].val==10010 && trace[2].reg==4);clear_trace();
 assert(!ktz8866_set_brightness(&ktz,0));assert(ntrace==2);assert(ktz.enable_state_valid && !ktz.led_on);
 reset(false,false);assert(!ktz8866_init(&ktz));clear_trace();assert(!ktz8866_set_brightness(&ktz,1500));assert(maps[0].values[8]==0x5f);clear_trace();assert(!ktz8866_set_brightness(&ktz,0));assert(maps[0].values[8]==31 && ntrace==3);
 reset(false,false);node.sinks=-1;assert(!ktz8866_init(&ktz));assert(ktz.active_sink_mask==63 && ktz.blanked_sink_mask==63);
 for(int dual=0;dual<2;dual++)for(int after=0;after<2;after++)for(int off=0;off<2;off++)for(int i=0;i<3*(dual+1);i++){
  ready(dual);if(off){assert(!ktz8866_set_brightness(&ktz,1500));clear_trace();}
  fail_at=i;fail_after=after;assert(ktz8866_set_brightness(&ktz,off?0:1500)==-EIO);assert(!ktz.enable_state_valid);assert(writes==i+1);
  clear_trace();assert(!ktz8866_set_brightness(&ktz,off?700:0));assert(ktz.enable_state_valid && ktz.led_on==!!off);
  if(off)check_lit(dual+1,700);else check_dark(dual+1);
  assert(trace[0].reg==8 && trace[0].val==(off?127U:31U));
 }
 for(int dual=0;dual<2;dual++)for(int i=0;i<4*(dual+1);i++){
  reset(true,dual);fail_at=i;assert(ktz8866_init(&ktz)==-EIO);assert(writes==i+1 && !ktz.enable_state_valid);
 }
 for(int i=0;i<5;i++){
  reset(true,false);if(i==0)node.sinks=0;if(i==1)node.sinks=7;if(i==2)node.mask=64;if(i==3){node.sinks=5;node.mask=63;}if(i==4)node.delay=101;
  assert(ktz8866_init(&ktz)==-EINVAL && !writes);
 }
 reset(true,false);node.ramp=0;assert(ktz8866_init(&ktz)==-EINVAL && writes==1);
 reset(true,false);node.led_ramp=16385;assert(ktz8866_init(&ktz)==-EINVAL && writes==2);
 ready(false);assert(ktz8866_set_brightness(&ktz,2048)==-EINVAL && !writes);assert(!ktz8866_set_brightness(&ktz,2047));check_lit(1,2047);assert(!(maps[0].values[8]&128));
 reset(true,false);assert(ktz8866_precious_reg(&client.dev,15));for(int i=0;i<=21;i++)if(i!=15)assert(!ktz8866_precious_reg(&client.dev,i));
 reset(true,false);assert(!ktz8866_probe(&client));assert(published==1 && published_mask==31 && !published_brightness);assert(bl.props.max_brightness==2047 && bl.props.brightness==1500 && bl.props.power==4 && client.data==&bl);
 clear_trace();bl.props.power=0;bl.props.brightness=1000;assert(!bl.ops->update_status(&bl));check_lit(1,1000);
 for(int i=0;i<6;i++){reset(true,false);fail_at=i;fail_after=1;assert(ktz8866_probe(&client)==-EIO && !published);}
 reset(false,false);node.initial_off=false;reg_fail=1;assert(ktz8866_probe(&client)==-ENOMEM && !published);check_dark(1);
 reset(false,false);node.initial_off=false;fail_at=6;fail_after=1;assert(ktz8866_probe(&client)==-EIO && !published);check_dark(1);
 reset(false,false);client.id.driver_data=1;assert(ktz8866_probe(&client)==-EPROBE_DEFER && !writes && !published);
 for(int i=0;i<nalloc;i++)free(allocations[i]);printf("%d\n",cases);return 0;
}
'''
 with tempfile.TemporaryDirectory(prefix='oem-backlight-tests-') as tmp:
  src=Path(tmp)/'test.c';exe=Path(tmp)/'test'
  src.write_text(prefix+defs+'\n'+struct+'\nstruct ktz8866 *ktz_b;\n'+core+'\n'+cfg+'\n'+ops+'\n'+function(s,'ktz8866_probe')+cases)
  subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-misleading-indentation','-fsanitize=undefined','-fno-sanitize-recover=all',str(src),'-o',str(exe)],check=True)
  count=int(subprocess.check_output([str(exe)],text=True,timeout=20))
 report={'cases':count,'all_passed':True,'actual_candidate_c_executed':True,'oem_on_7f_blank_1f_and_10ms_wait_verified':True,'single_and_dual_chip_partial_write_errors_checked':True,'reverse_direction_retry_restores_enable_state':True,'probe_publishes_only_after_complete_initialization':True,'failed_probe_cleanup_checked':True,'flag_read_to_clear_excluded_from_debug_dumps':True,'raw_brightness_range_and_bias_voltages_unchanged':True,'generic_five_sink_success_path_preserved':True,'hardware_validation_required':True}
 a.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
if __name__=='__main__':main()
