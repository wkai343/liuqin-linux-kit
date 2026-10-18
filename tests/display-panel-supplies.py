#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise OEM supply-request order and partial-enable cleanup with faults."""
from pathlib import Path
import argparse,json,re,subprocess,tempfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--candidate',type=Path,required=True)
parser.add_argument('--baseline-source',type=Path,required=True)
parser.add_argument('--oem-report',type=Path,required=True)
parser.add_argument('--report',type=Path)
args=parser.parse_args()
new=args.candidate.read_text()
old=(args.baseline_source/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text()
start=new.index('/* OEM order for the three supplies currently owned by the panel.')
end=new.index('static int nt36532_off(',start)
helpers=new[start:end]
restored=(new[:start]+new[end:]).replace('ret = nt36532_enable_supplies(ctx);','ret = regulator_bulk_enable(ARRAY_SIZE(ctx->supplies), ctx->supplies);').replace('nt36532_disable_supplies(ctx);','regulator_bulk_disable(ARRAY_SIZE(ctx->supplies), ctx->supplies);')
assert restored==old,'Unrelated panel behavior changed'
rails=json.loads(args.oem_report.read_text())['power']['oem_full_enable_order'][:3]
assert [r['name'] for r in rails]==['iovdd','dvddbuck','dvddldo']
bindings=re.findall(r'ctx->supplies\[(\d+)\]\.supply = "([^"]+)";',new)
assert bindings==[('0','vddio'),('1','dvddbuck'),('2','dvddldo')]
native_names=[name for _,name in bindings]
stubs=r'''
#include <stdio.h>
#include <stdlib.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
struct regulator {int id;};
struct regulator_bulk_data {const char *supply;struct regulator *consumer;};
struct panel_desc {int unused;};
static struct panel_desc liuqin_desc,other_desc;
struct panel {void *dev;};
struct nt36532 {struct panel panel;const struct panel_desc *desc;struct regulator_bulk_data supplies[3];};
static int enable_failure,disable_failure;
static int regulator_enable(struct regulator *r){printf("enable %d\n",r->id);return r->id==enable_failure?-5:0;}
static int regulator_disable(struct regulator *r){printf("disable %d\n",r->id);return r->id==disable_failure?-16:0;}
static void usleep_range(unsigned int min,unsigned int max){printf("wait %u %u\n",min,max);}
static void dev_warn(void *d,const char *format,const char *name,int error){printf("warning %s %d\n",name,error);}
static int regulator_bulk_enable(int count,struct regulator_bulk_data *r){printf("bulk-enable %d\n",count);return 0;}
static int regulator_bulk_disable(int count,struct regulator_bulk_data *r){printf("bulk-disable %d\n",count);return 0;}
'''
main=r'''
int main(int argc,char **argv){
 struct regulator regs[3]={{0},{1},{2}};
 const char *names[3]={"vddio","dvddbuck","dvddldo"};
 struct nt36532 ctx={.desc=atoi(argv[1])?&liuqin_desc:&other_desc};
 enable_failure=atoi(argv[2]);disable_failure=atoi(argv[3]);
 for(int i=0;i<3;i++){ctx.supplies[i].consumer=&regs[i];ctx.supplies[i].supply=names[i];}
 int ret=nt36532_enable_supplies(&ctx);printf("result %d\n",ret);
 if(!ret)nt36532_disable_supplies(&ctx);
 return 0;
}
'''
def delay(ms):return [f'wait {ms*1000} {ms*1000+10}'] if ms else []
def release(indices,fail):
 result=[]
 for i in indices:
  result.append(f'disable {i}')
  if i==fail:result.append(f'warning {native_names[i]} -16')
  result+=delay(rails[i]['post_off_ms'])
 return result
cases=[]
with tempfile.TemporaryDirectory(prefix='liuqin-supply-order-') as tmp:
 src=Path(tmp)/'supplies.c';src.write_text(stubs+helpers+main);prog=Path(tmp)/'supplies'
 subprocess.run(['cc','-std=c11','-Wall','-Werror','-Wno-unused-parameter',str(src),'-o',str(prog)],check=True)
 for board in [0,1]:
  for enable_fail in [-1,0,1,2]:
   for disable_fail in [-1,0,1,2]:
    actual=subprocess.check_output([str(prog),str(board),str(enable_fail),str(disable_fail)],text=True).splitlines()
    expected=[]
    if not board:expected=['bulk-enable 3','result 0','bulk-disable 3']
    else:
     for i in range(3):
      expected.append(f'enable {i}')
      if i==enable_fail:
       expected+=release(range(i-1,-1,-1),disable_fail);expected.append('result -5');break
      expected+=delay(rails[i]['post_on_ms'])
     else:expected+=['result 0']+release(range(2,-1,-1),disable_fail)
    assert actual==expected,(board,enable_fail,disable_fail,actual,expected)
    cases.append(dict(liuqin=bool(board),enable_failure=enable_fail,disable_failure=disable_fail))
result=dict(cases=len(cases),order_and_delays_match_oem_first_three_supplies=True,
 native_binding_order_checked=True,
 rollback_only_releases_successful_votes=True,disable_errors_reported=True,
 other_boards_keep_bulk_operations=True,all_other_panel_code_identical=True,
 physical_rail_sequencing_proven=False,hardware_enabled=False)
if args.report:args.report.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
