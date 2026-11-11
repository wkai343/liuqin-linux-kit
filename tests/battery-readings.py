#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the current kernel battery getter against units, bounds and transport faults.

Supply a fully adapted kernel with --source. The source tree is not modified.
Requires a host C compiler with UndefinedBehaviorSanitizer.
"""
from pathlib import Path
import argparse, hashlib, json, re, subprocess, tempfile

PROJECT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--report', type=Path)
parser.add_argument('--cc', default='cc')
args = parser.parse_args()
if args.report:
    args.report.unlink(missing_ok=True)
relative = Path('drivers/power/supply/qcom_battmgr.c')
s = (args.source / relative).read_text()

def function(text,name):
 m=re.search(r'^static int '+name+r'\([^;]*?\)\n\{',text,re.M);assert m,name
 end=m.end();depth=1
 while depth:depth+=(text[end]=='{')-(text[end]=='}');end+=1
 return text[m.start():end]
body=function(s,'qcom_battmgr_bat_get_property')
helper=function(s,'qcom_battmgr_sm8475_charge_value')
all_props=sorted(set(re.findall(r'POWER_SUPPLY_PROP_[A-Z_]+',body)))
enum='enum power_supply_property {'+','.join(all_props)+'};\n'
structs=[]
for kind,name in [('enum','qcom_battmgr_variant'),('enum','qcom_battmgr_unit'),('struct','qcom_battmgr_info'),('struct','qcom_battmgr_status')]:
 m=re.search('^'+kind+' '+name+r' \{.*?^\};',s,re.M|re.S);assert m;structs.append(m[0])
prefix=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <limits.h>
#include <errno.h>
#include <string.h>
typedef uint32_t u32;
#define BATTMGR_STRING_LEN 128
union power_supply_propval {int intval;const char *strval;};
struct power_supply {void *data;};
'''
setup=r'''
struct qcom_battmgr {enum qcom_battmgr_variant variant;enum qcom_battmgr_unit unit;bool service_up;struct qcom_battmgr_info info;struct qcom_battmgr_status status;};
static unsigned updates,sc_updates;static int failure;
static void *power_supply_get_drvdata(struct power_supply *p){return p->data;}
static int qcom_battmgr_bat_sm8350_update(struct qcom_battmgr *b,enum power_supply_property p){(void)b;(void)p;updates++;return failure;}
static int qcom_battmgr_bat_sc8280xp_update(struct qcom_battmgr *b,enum power_supply_property p){(void)b;(void)p;updates++;sc_updates++;return failure;}
'''
main=r'''
int main(void){
 struct qcom_battmgr b={.service_up=true,.variant=QCOM_BATTMGR_SM8475,.unit=QCOM_BATTMGR_UNIT_mWh,.info={.design_capacity=8600000,.last_full_capacity=7817000,.charge_count=5542,.present=true,.cycle_count=100,.voltage_max=4400000,.technology=1},.status={.status=2,.health=1,.capacity=73,.percent=73,.current_now=172000,.power_now=10000000,.voltage_now=4113000,.voltage_ocv=4140000,.temperature=273,.charge_time=0,.discharge_time=0}};
 struct power_supply psy={.data=&b};union power_supply_propval value;unsigned passed=0;
 /* Explicit public-unit expectations, independent of the getter's switch. */
 struct reading {enum power_supply_property property;int value;};
 const struct reading common[]={
  {POWER_SUPPLY_PROP_STATUS,2},{POWER_SUPPLY_PROP_HEALTH,1},
  {POWER_SUPPLY_PROP_PRESENT,1},{POWER_SUPPLY_PROP_TECHNOLOGY,1},
  {POWER_SUPPLY_PROP_CYCLE_COUNT,100},{POWER_SUPPLY_PROP_CAPACITY,73},
  {POWER_SUPPLY_PROP_VOLTAGE_MAX,4400000},{POWER_SUPPLY_PROP_VOLTAGE_NOW,4113000},
  {POWER_SUPPLY_PROP_VOLTAGE_OCV,4140000},{POWER_SUPPLY_PROP_TEMP,273}
 };
 const enum qcom_battmgr_variant variants[]={QCOM_BATTMGR_SM8350,QCOM_BATTMGR_SC8280XP,QCOM_BATTMGR_X1E80100,QCOM_BATTMGR_SM8475};
 const enum qcom_battmgr_unit units[]={QCOM_BATTMGR_UNIT_mAh,QCOM_BATTMGR_UNIT_mWh};
#define CHECK(prop,rc,expected) do { \
 updates=sc_updates=0;value.intval=123456; \
 assert(qcom_battmgr_bat_get_property(&psy,prop,&value)==(rc)); \
 assert(updates==1 && sc_updates==(unsigned)(b.variant==QCOM_BATTMGR_SC8280XP || b.variant==QCOM_BATTMGR_X1E80100)); \
 if((rc)==0){assert(value.intval==(expected));}passed++; \
} while(0)
 for(unsigned v=0;v<4;v++)for(unsigned u=0;u<2;u++){
  b.variant=variants[v];b.unit=units[u];failure=0;
  for(unsigned i=0;i<sizeof(common)/sizeof(common[0]);i++)CHECK(common[i].property,0,common[i].value);
  CHECK(POWER_SUPPLY_PROP_CURRENT_NOW,0,v==3?-172000:172000);
  CHECK(POWER_SUPPLY_PROP_POWER_NOW,v==3?-ENODATA:0,10000000);
  CHECK(POWER_SUPPLY_PROP_CHARGE_COUNTER,0,v==3?5542000:5542);
  CHECK(POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN,(v==3 || u==0)?0:-ENODATA,8600000);
  CHECK(POWER_SUPPLY_PROP_CHARGE_FULL,(v==3 || u==0)?0:-ENODATA,7817000);
  CHECK(POWER_SUPPLY_PROP_ENERGY_FULL_DESIGN,u==1?0:-ENODATA,8600000);
  CHECK(POWER_SUPPLY_PROP_ENERGY_FULL,u==1?0:-ENODATA,7817000);
  CHECK((enum power_supply_property)-1,-EINVAL,0);
 }
 b.variant=QCOM_BATTMGR_SM8475;b.unit=QCOM_BATTMGR_UNIT_mWh;
 u32 counters[]={0,5542,INT_MAX/1000,INT_MAX/1000+1U,UINT32_MAX};
 for(unsigned i=0;i<sizeof(counters)/sizeof(counters[0]);i++){
  b.info.charge_count=counters[i];value.intval=123456;int rc=qcom_battmgr_bat_get_property(&psy,POWER_SUPPLY_PROP_CHARGE_COUNTER,&value);
  if(counters[i]>INT_MAX/1000)assert(rc==-ERANGE && value.intval==123456);else assert(rc==0 && value.intval==(int)(counters[i]*1000));
  assert(b.info.charge_count==counters[i]);passed++;
 }
 b.info.design_capacity=UINT32_MAX;assert(qcom_battmgr_bat_get_property(&psy,POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN,&value)==-ERANGE);passed++;
 b.info.last_full_capacity=UINT32_MAX;assert(qcom_battmgr_bat_get_property(&psy,POWER_SUPPLY_PROP_CHARGE_FULL,&value)==-ERANGE);passed++;
 b.service_up=false;updates=0;assert(qcom_battmgr_bat_get_property(&psy,POWER_SUPPLY_PROP_CHARGE_COUNTER,&value)==-EAGAIN && !updates);passed++;
 b.service_up=true;failure=-ETIMEDOUT;updates=0;assert(qcom_battmgr_bat_get_property(&psy,POWER_SUPPLY_PROP_CHARGE_FULL,&value)==-ETIMEDOUT && updates==1);passed++;
 printf("%u\n",passed);return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='battery-readings-test-') as td:
 c=Path(td)/'test.c';exe=Path(td)/'test';c.write_text(prefix+enum+'\n'.join(structs)+setup+helper+'\n'+body+'\n'+main)
 subprocess.run([args.cc,'-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-sign-compare','-fsanitize=undefined',str(c),'-o',str(exe)],check=True)
 count=int(subprocess.check_output([str(exe)],text=True))
r={'all_passed':True,'runtime_c_checks':count,'variants_checked':4,'unit_modes_checked':2,'actual_getter_code_executed':True,'overflow_checked':True,'transport_selection_and_failures_checked':True,'source_sha256':hashlib.sha256(s.encode()).hexdigest()}
if args.report:
 args.report.write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps(r,indent=2))
