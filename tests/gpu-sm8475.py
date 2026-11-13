#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check Cape GPU OPPs and execute the GMU CX dependency/HFI code offline."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location('folio', Path(__file__).with_name('nanosic-folio.py'))
folio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(folio)

# OS2.0.6.0 vendor_boot DTB 02, qcom,kgsl-3d0 power levels, ascending.
# MHz, GX RPMh corner, CX RPMh corner, ACD control word.
OEM = [
    (220, 48, 64, 0xc8285ffd), (285, 56, 64, 0xc02c5ffd),
    (324, 64, 64, 0xc02a5ffd), (364, 80, 128, 0xc0285ffd),
    (439, 96, 128, 0xc0285ffd), (515, 128, 128, 0x882e5ffd),
    (580, 144, 192, 0x882e5ffd), (645, 192, 192, 0x882d5ffd),
    (710, 224, 256, 0x882c5ffd), (765, 256, 256, 0x882c5ffd),
    (815, 320, 320, 0x882b5ffd), (862, 384, 384, 0x882b5ffd),
    (900, 416, 416, 0x882b5ffd),
]


def read_dtb(path):
    data = path.read_bytes()
    magic, total, pos, strings = struct.unpack_from('>4I', data)
    assert magic == 0xd00dfeed and total <= len(data)
    nodes, stack = {}, []
    while True:
        token = struct.unpack_from('>I', data, pos)[0]
        pos += 4
        if token == 1:
            end = data.index(0, pos)
            stack.append(data[pos:end].decode())
            pos = (end + 4) & ~3
            nodes['/'.join(stack) or '/'] = {}
        elif token == 2:
            stack.pop()
        elif token == 3:
            size, offset = struct.unpack_from('>2I', data, pos)
            pos += 8
            begin = strings + offset
            name = data[begin:data.index(0, begin)].decode()
            nodes['/'.join(stack) or '/'][name] = data[pos:pos + size]
            pos = (pos + size + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            return nodes
        else:
            raise AssertionError(token)


STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32; typedef uint16_t u16;
#define GMU_MAX_GX_FREQS 16
#define EPROBE_DEFER 517
#define __packed __attribute__((packed))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define ERR_PTR(e) ((void*)(intptr_t)(e))
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
#define dev_err_probe(dev,err,...) (err)
#define HFI_H2F_MSG_PERF_TABLE 4
struct device {int id;};
struct platform_device {struct device dev;};
struct msm_gpu {struct platform_device *pdev;};
struct adreno_gpu {struct msm_gpu base;};
struct a6xx_gmu {
 int nr_gpu_freqs,nr_gmu_freqs;
 unsigned long gpu_freqs[16],gmu_freqs[4];
 u32 gx_arc_votes[16],gx_cx_votes[16],cx_arc_votes[4];
};
struct a6xx_gpu {struct adreno_gpu base;struct a6xx_gmu gmu;};
struct device_node {bool present;u32 level;int error;};
struct dev_pm_opp {struct device_node node;};
static struct a6xx_gpu gpu;
static struct platform_device pdev;
static struct dev_pm_opp opps[16];
static bool a730=true;
static int opp_refs,node_refs,lookup_error,db_error,db_reads,send_error;
static size_t db_size;
static u16 cx[] = {0,16,64,128,192,256,384,416,432,448,0,0,0,0,0,0};
static bool adreno_is_a730(struct adreno_gpu *p){(void)p;return a730;}
static bool adreno_is_a8xx(struct adreno_gpu *p){(void)p;return false;}
static struct dev_pm_opp *dev_pm_opp_find_freq_exact(struct device *d,unsigned long f,bool available){
 (void)d;assert(available);if(lookup_error)return ERR_PTR(lookup_error);
 for(int i=1;i<gpu.gmu.nr_gpu_freqs;i++)if(gpu.gmu.gpu_freqs[i]==f){opp_refs++;return &opps[i];}
 return ERR_PTR(-ENOENT);
}
static struct device_node *dev_pm_opp_get_of_node(struct dev_pm_opp *o){node_refs++;return &o->node;}
static void dev_pm_opp_put(struct dev_pm_opp *o){(void)o;assert(opp_refs>0);opp_refs--;}
static void of_node_put(struct device_node *n){(void)n;assert(node_refs>0);node_refs--;}
static bool of_property_present(struct device_node *n,const char *name){assert(!strcmp(name,"qcom,opp-cx-level"));return n->present;}
static int of_property_read_u32(struct device_node *n,const char *name,u32 *v){assert(!strcmp(name,"qcom,opp-cx-level"));if(n->error)return n->error;*v=n->level;return 0;}
static const void *cmd_db_read_aux_data(const char *name,size_t *size){
 assert(!strcmp(name,"cx.lvl"));db_reads++;if(db_error)return ERR_PTR(db_error);*size=db_size;return cx;
}
'''

CASES = r'''
static struct a6xx_hfi_msg_perf_table sent;
static int a8xx_hfi_send_perf_table(struct a6xx_gmu *g){(void)g;assert(false);return -EINVAL;}
static int a6xx_hfi_send_msg(struct a6xx_gmu *g,int id,void *msg,size_t size,void *reply,int reply_size){
 (void)g;assert(id==4&&size==sizeof(sent)&&!reply&&!reply_size);memcpy(&sent,msg,size);return send_error;
}
FUNCTIONS
static void init(bool properties){
 assert(!opp_refs&&!node_refs);memset(&gpu,0,sizeof(gpu));memset(opps,0,sizeof(opps));
 gpu.base.base.pdev=&pdev;gpu.gmu.nr_gpu_freqs=14;gpu.gmu.nr_gmu_freqs=3;
 gpu.gmu.gmu_freqs[1]=200000000;gpu.gmu.gmu_freqs[2]=500000000;
 const unsigned long mhz[]={0,220,285,324,364,439,515,580,645,710,765,815,862,900};
 const u32 levels[]={0,64,64,64,128,128,128,192,192,256,256,320,384,416};
 for(int i=1;i<14;i++){gpu.gmu.gpu_freqs[i]=mhz[i]*1000000;opps[i].node=(struct device_node){properties,levels[i],0};}
 a730=true;lookup_error=db_error=db_reads=send_error=0;db_size=sizeof(cx);
}
int main(void){
 int cases=0;
 _Static_assert(sizeof(struct perf_gx_level)==12,"HFI ABI");
 _Static_assert(offsetof(struct perf_gx_level,cx_vote)==4,"CX wire word");
 _Static_assert(offsetof(struct perf_gx_level,freq)==8,"frequency wire word");
 init(false);assert(!a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)&&!db_reads);
 assert(!a6xx_hfi_send_perf_table(&gpu.gmu));for(int i=0;i<14;i++)assert(sent.gx_votes[i].cx_vote==UINT32_MAX);cases++;
 init(true);assert(!a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)&&db_reads==1);
 const u32 expected[]={0,2,2,2,3,3,3,4,4,5,5,6,6,7};
 assert(!a6xx_hfi_send_perf_table(&gpu.gmu)&&sent.num_gpu_levels==14&&sent.num_gmu_levels==3);
 for(int i=0;i<14;i++){assert(sent.gx_votes[i].cx_vote==expected[i]);assert(sent.gx_votes[i].freq==gpu.gmu.gpu_freqs[i]/1000);}
 assert(sent.cx_votes[1].freq==200000&&sent.cx_votes[2].freq==500000);cases++;
 send_error=-EIO;assert(a6xx_hfi_send_perf_table(&gpu.gmu)==-EIO);cases++;
 init(true);opps[4].node.present=false;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EINVAL&&!db_reads);cases++;
 init(true);a730=false;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EINVAL&&!db_reads);cases++;
 init(false);a730=false;assert(!a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu));cases++;
 init(true);lookup_error=-ENOENT;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-ENOENT);cases++;
 init(true);opps[3].node.error=-EOVERFLOW;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EOVERFLOW);cases++;
 init(true);db_error=-EPROBE_DEFER;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EPROBE_DEFER);cases++;
 init(true);db_size=0;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EINVAL);cases++;
 init(true);db_size=3;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EINVAL);cases++;
 init(true);opps[13].node.level=1000;assert(a6xx_gmu_rpmh_gx_cx_votes_init(&gpu.gmu)==-EINVAL);cases++;
 assert(!opp_refs&&!node_refs);printf("PASS: %d actual GMU/HFI cases, OEM CX corner indices and wire layout\n",cases);
 return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--dtb', type=Path)
    parser.add_argument('--stock-base', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    base = args.source / 'drivers/gpu/drm/msm/adreno'
    gmu = (base / 'a6xx_gmu.c').read_text()
    hfi = (base / 'a6xx_hfi.c').read_text()
    header = (base / 'a6xx_hfi.h').read_text()
    structs = '\n'.join(re.search(r'struct ' + name + r' \{.*?\} __packed;', header, re.S)[0]
                        for name in ('perf_level', 'perf_gx_level', 'a6xx_hfi_msg_perf_table'))
    functions = folio.function(gmu, 'a6xx_gmu_rpmh_gx_cx_votes_init') + '\n' + folio.function(hfi, 'a6xx_hfi_send_perf_table')
    assert re.search(r'ret = a6xx_gmu_pwrlevels_probe\(gmu\);\s*if \(ret\)\s*goto detach_gxpd;', gmu)
    with tempfile.TemporaryDirectory(prefix='gpu-sm8475-') as temp:
        c = Path(temp) / 'test.c'
        c.write_text(STUBS + structs + CASES.replace('FUNCTIONS', functions))
        binary = c.with_suffix('')
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
                        '-fsanitize=undefined', '-fno-sanitize-recover=all', str(c), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=15)
    if args.stock_base:
        assert hashlib.sha256(args.stock_base.read_bytes()).hexdigest() == '527344cd52497d0b8b9311f1ab82ac3418b86b95c25e7786890f38f40054e467'
        nodes = read_dtb(args.stock_base)
        rows = []
        for path, props in nodes.items():
            if path.startswith('/soc/qcom,kgsl-3d0@3d00000/qcom,gpu-pwrlevels/'):
                rows.append(tuple(int.from_bytes(props[name], 'big') // (1000000 if name == 'qcom,gpu-freq' else 1)
                                  for name in ('qcom,gpu-freq', 'qcom,level', 'qcom,cx-level', 'qcom,acd-level')))
        assert sorted(rows) == OEM
        print('PASS: matrix matches the digest-verified OEM GPU power table')
    if args.dtb:
        nodes = read_dtb(args.dtb)
        rows = []
        for path, props in nodes.items():
            if path.startswith('/soc@0/gpu@3d00000/opp-table/opp-'):
                rows.append(tuple(int.from_bytes(props[name], 'big') // (1000000 if name == 'opp-hz' else 1)
                                  for name in ('opp-hz', 'opp-level', 'qcom,opp-cx-level', 'qcom,opp-acd-level')))
        assert sorted(rows) == OEM, rows
        gmu_opps = sorted((int.from_bytes(props['opp-hz'], 'big'), int.from_bytes(props['opp-level'], 'big'))
                          for path, props in nodes.items() if path.startswith('/soc@0/gmu@3d6a000/opp-table/opp-'))
        assert gmu_opps == [(200000000, 64), (500000000, 128)], gmu_opps
        print('PASS: compiled DT has exactly 13 OEM GPU OPPs and the OEM 200/500 MHz GMU OPPs')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(dict(status='PASS',gpu_opps=OEM,actual_c_cases=12,
                                             dtb_checked=bool(args.dtb),oem_checked=bool(args.stock_base),
                                             hardware_exercised=False),indent=2)+'\n')


if __name__ == '__main__':
    main()
