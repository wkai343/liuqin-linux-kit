#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Apply the battery patch in a temporary directory and exercise the real C getter.

Supply an unpatched kernel with --baseline. Optionally use --source to
also check the getter against a separately built candidate. Neither tree
is modified. Requires a host C compiler with UndefinedBehaviorSanitizer.
"""
from pathlib import Path
import argparse, hashlib, json, re, subprocess, tempfile

PROJECT = Path(__file__).resolve().parents[1]
PATCH = PROJECT / 'build/kernel/patches/0033-liuqin-battery-telemetry.patch'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline', type=Path, required=True)
parser.add_argument('--source', type=Path)
parser.add_argument('--report', type=Path)
parser.add_argument('--cc', default='cc')
args = parser.parse_args()
if args.report:
    args.report.unlink(missing_ok=True)
relative = Path('drivers/power/supply/qcom_battmgr.c')
old = (args.baseline / relative).read_text()
with tempfile.TemporaryDirectory(prefix='liuqin-battery-patch-') as tmp:
    tree = Path(tmp)
    path = tree / relative
    path.parent.mkdir(parents=True)
    path.write_text(old)
    subprocess.run(['git', 'apply', str(PATCH)], cwd=tree, check=True)
    s = path.read_text()

def function(text,name):
 m=re.search(r'^static int '+name+r'\([^;]*?\)\n\{',text,re.M);assert m,name
 end=m.end();depth=1
 while depth:depth+=(text[end]=='{')-(text[end]=='}');end+=1
 return text[m.start():end]
if args.source:
 candidate = (args.source / relative).read_text()
 for name in ('qcom_battmgr_bat_get_property', 'qcom_battmgr_sm8475_charge_value'):
  assert function(candidate, name) == function(s, name), name

# Guard the whole driver outside the getter, helper and required include.
restored=s.replace('#include <linux/limits.h>\n', '', 1)
restored=restored.replace('/* M81 full capacities are uAh; its remaining counter is reported in mAh. */\n'+function(s,'qcom_battmgr_sm8475_charge_value')+'\n\n','',1)
restored=restored.replace(function(s,'qcom_battmgr_bat_get_property'),function(old,'qcom_battmgr_bat_get_property'),1)
assert restored == old, 'Unexpected change outside battery getter'

body=function(s,'qcom_battmgr_bat_get_property');baseline=function(old,'qcom_battmgr_bat_get_property').replace('qcom_battmgr_bat_get_property(','baseline_get_property(',1);helper=function(s,'qcom_battmgr_sm8475_charge_value')
for name in ['qcom_battmgr_raw_property','qcom_battmgr_bat_sm8350_update','qcom_battmgr_bat_sc8280xp_update','qcom_battmgr_xm_set_value_locked']:
 assert function(s,name)==function(old,name)
props=sorted(set(re.findall(r'POWER_SUPPLY_PROP_[A-Z_]+',body)));enum='enum power_supply_property {'+','.join(props)+'};\n'
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
static unsigned updates;static int failure;
static void *power_supply_get_drvdata(struct power_supply *p){return p->data;}
static int qcom_battmgr_bat_sm8350_update(struct qcom_battmgr *b,enum power_supply_property p){(void)b;(void)p;updates++;return failure;}
static int qcom_battmgr_bat_sc8280xp_update(struct qcom_battmgr *b,enum power_supply_property p){(void)b;(void)p;updates++;return failure;}
'''
main=r'''
int main(void){
 struct qcom_battmgr b={.service_up=true,.variant=QCOM_BATTMGR_SM8475,.unit=QCOM_BATTMGR_UNIT_mWh,.info={.design_capacity=8600000,.last_full_capacity=7817000,.charge_count=5542,.present=true,.cycle_count=100,.voltage_max=4400000,.technology=1},.status={.status=2,.health=1,.capacity=73,.percent=73,.current_now=172000,.power_now=10000000,.voltage_now=4113000,.voltage_ocv=4140000,.temperature=273,.charge_time=0,.discharge_time=0}};
 struct power_supply psy={.data=&b};union power_supply_propval value,previous;unsigned passed=0;
 enum power_supply_property props[]={PROPS};
 for(unsigned variant=0;variant<3;variant++)for(unsigned unit=0;unit<2;unit++)for(unsigned i=0;i<sizeof(props)/sizeof(props[0]);i++){
  b.variant=variant;b.unit=unit;value.intval=previous.intval=123456;updates=0;failure=0;
  int oldrc=baseline_get_property(&psy,props[i],&previous);assert(updates==1);updates=0;
  int newrc=qcom_battmgr_bat_get_property(&psy,props[i],&value);assert(updates==1);
  if(variant==QCOM_BATTMGR_SM8475 && props[i]==POWER_SUPPLY_PROP_CHARGE_COUNTER){assert(newrc==0 && value.intval==5542000 && b.info.charge_count==5542);}
  else if(variant==QCOM_BATTMGR_SM8475 && props[i]==POWER_SUPPLY_PROP_CHARGE_FULL_DESIGN){assert(newrc==0 && value.intval==8600000);}
  else if(variant==QCOM_BATTMGR_SM8475 && props[i]==POWER_SUPPLY_PROP_CHARGE_FULL){assert(newrc==0 && value.intval==7817000);}
  else if(variant==QCOM_BATTMGR_SM8475 && props[i]==POWER_SUPPLY_PROP_POWER_NOW){assert(newrc==-ENODATA);}
  else{assert(oldrc==newrc);if(!oldrc){if(props[i]==POWER_SUPPLY_PROP_MODEL_NAME || props[i]==POWER_SUPPLY_PROP_MANUFACTURER || props[i]==POWER_SUPPLY_PROP_SERIAL_NUMBER)assert(value.strval==previous.strval);else assert(value.intval==previous.intval);}}
  passed++;
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
'''.replace('PROPS',','.join(props))
with tempfile.TemporaryDirectory(prefix='battery-readings-test-') as td:
 c=Path(td)/'test.c';exe=Path(td)/'test';c.write_text(prefix+enum+'\n'.join(structs)+setup+helper+'\n'+baseline+'\n'+body+'\n'+main)
 subprocess.run([args.cc,'-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-sign-compare','-fsanitize=undefined',str(c),'-o',str(exe)],check=True)
 count=int(subprocess.check_output([str(exe)],text=True));assert count==len(props)*6+9
r={'all_passed':True,'runtime_c_checks':count,'getter_properties_compared':len(props),'variants_compared':3,'unit_modes_compared':2,'actual_getter_code_executed':True,'other_variant_behavior_preserved':True,'overflow_checked':True,'firmware_request_path_unchanged':True,'source_sha256':hashlib.sha256(s.encode()).hexdigest(),'automatic_writes_added':False}
r['patch_sha256']=hashlib.sha256(PATCH.read_bytes()).hexdigest()
if args.report:
 args.report.write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps(r,indent=2))
