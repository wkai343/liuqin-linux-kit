#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the actual curve and register-read C against exact-ROM vectors and I/O faults."""
from pathlib import Path
import argparse,hashlib,json,re,struct,subprocess,tempfile


def function(s,n):
    m=re.search(r'^static (?:int|unsigned int)\s+'+n+r'\([^;{]*\)\s*\{',s,re.M);assert m,n
    end=m.end();depth=1
    while depth:depth+=(s[end]=='{')-(s[end]=='}');end+=1
    return s[m.start():end]


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--rom-proof',type=Path,required=True);ap.add_argument('--report',type=Path,required=True);ap.add_argument('--cc',default='cc');a=ap.parse_args();a.report.unlink(missing_ok=True)
    name='drivers/video/backlight/ktz8866.c';s=(a.source/name).read_text();old=(a.baseline/name).read_text();header=(a.source/'drivers/video/backlight/ktz8866-liuqin-curve.h').read_text();proof=json.loads(a.rom_proof.read_text())
    table=[int(v) for v in re.findall(r'\d+',header.split('= {',1)[1].split('};',1)[0])]
    assert table==proof['curve_values'] and hashlib.sha256(struct.pack('<2048I',*table)).hexdigest()==proof['curve_sha256']
    for n in ['ktz8866_write','ktz8866_set_brightness','ktz8866_init']:assert function(s,n)==function(old,n),n
    assert 'oem_brightness_curve && id->driver_data == 0 &&' in s
    assert '"xiaomi,liuqin-brightness-curve"' in s and '.precious_reg = ktz8866_precious_reg' in s
    expected=[table[1700*v//2047 if v<=2047 else 1700+347*(v-2048)//2047] for v in range(4096)]
    prefix=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <errno.h>
#include <limits.h>
typedef uint16_t u16;
#define MAX_BRIGHTNESS 2047
#define DEFAULT_BRIGHTNESS 1500
#define PWM2DIG_LSBS 0x12
#define PWM2DIG_MSBS 0x13
#define BL_BRT_LSB 4
#define BL_BRT_MSB 5
struct mutex {int held;};
static struct mutex *held;
static void mutex_lock(struct mutex *m){assert(!held && !m->held);held=m;m->held=1;}
static void mutex_unlock(struct mutex *m){assert(held==m && m->held);held=NULL;m->held=0;}
struct regmap {unsigned int regs[0x16];};
struct ktz8866 {bool oem_curve;struct regmap *regmap;};
struct backlight_device {struct ktz8866 *ktz;struct mutex update_lock;unsigned int level;bool off;};
static int reads,fail_read=-1,calls,raw_value;
static unsigned int addresses[2];
static int regmap_read(struct regmap *map,unsigned int reg,unsigned int *value){assert(held && reg!=15 && reads<2);addresses[reads]=reg;if(reads++==fail_read)return -EIO;*value=map->regs[reg];return 0;}
static struct ktz8866 *bl_get_data(struct backlight_device *bl){return bl->ktz;}
static unsigned int backlight_get_brightness(struct backlight_device *bl){return bl->off?0:bl->level;}
static int ktz8866_set_brightness(struct ktz8866 *ktz,unsigned int value){(void)ktz;assert(value<=2047);raw_value=value;calls++;return 0;}
'''
    code=prefix+header+'\n'+''.join(function(s,n)+'\n' for n in ['ktz8866_map_brightness','ktz8866_default_brightness','ktz8866_backlight_update_status','ktz8866_read_level'])
    code+='static const unsigned int expected[4096]={'+','.join(map(str,expected))+'};\n'
    code+=r'''
int main(void){
 struct regmap regs={0};struct ktz8866 ktz={true,&regs};struct backlight_device bl={.ktz=&ktz};unsigned int checks=0;
 for(unsigned int v=0;v<4096;v++){assert(ktz8866_map_brightness(&ktz,v)==(int)expected[v]);bl.level=v;calls=0;assert(!ktz8866_backlight_update_status(&bl) && calls==1 && raw_value==(int)expected[v]);checks++;}
 assert(ktz8866_default_brightness(&ktz)==510 && ktz8866_map_brightness(&ktz,510)==1500);checks++;
 for(unsigned int v=4096;v<4100;v++){calls=0;bl.level=v;assert(ktz8866_backlight_update_status(&bl)==-EINVAL && !calls);checks++;}
 assert(ktz8866_map_brightness(&ktz,UINT_MAX)==-EINVAL);checks++;
 bl.off=true;bl.level=4095;assert(!ktz8866_backlight_update_status(&bl) && !raw_value);checks++;bl.off=false;
 ktz.oem_curve=false;for(unsigned int v=0;v<2048;v++){assert(ktz8866_map_brightness(&ktz,v)==(int)v);checks++;}
 assert(ktz8866_map_brightness(&ktz,2048)==-EINVAL && ktz8866_default_brightness(&ktz)==1500);checks++;
 for(int pwm=0;pwm<2;pwm++)for(unsigned int v=0;v<2048;v++){
  regs.regs[pwm?0x12:4]=pwm?(v&255):(v&7);regs.regs[pwm?0x13:5]=pwm?(v>>8):(v>>3);reads=0;fail_read=-1;
  assert(ktz8866_read_level(&bl,pwm)==(int)v && !held && reads==2);assert(addresses[0]==(unsigned)(pwm?0x12:4) && addresses[1]==(unsigned)(pwm?0x13:5));checks++;
 }
 for(int pwm=0;pwm<2;pwm++)for(int fail=0;fail<2;fail++){reads=0;fail_read=fail;assert(ktz8866_read_level(&bl,pwm)==-EIO && !held && reads==fail+1);checks++;}
 printf("%u\n",checks);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='liuqin-curve-') as tmp:
        c=Path(tmp)/'test.c';exe=Path(tmp)/'test';c.write_text(code)
        subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-fsanitize=undefined','-fno-sanitize-recover=all',str(c),'-o',str(exe)],check=True)
        checks=int(subprocess.check_output([str(exe)],text=True,timeout=15))
    r={'all_passed':True,'checks':checks,'all_4096_oem_levels_match_rom':True,'legacy_2048_levels_unchanged':True,'raw_and_pwm_all_11bit_values_checked':True,'i2c_errors_propagated_without_lock_leak':True,'raw_1500_default_preserved':True,'supply_pwm_mode_and_raw_write_state_machine_unchanged':True,'source_sha256':hashlib.sha256(s.encode()).hexdigest(),'rom_proof_sha256':hashlib.sha256(a.rom_proof.read_bytes()).hexdigest()};a.report.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))


if __name__=='__main__':main()
