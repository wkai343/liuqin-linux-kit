#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise Iris secure encoder allocation and ownership failure paths."""
import argparse
from pathlib import Path
import re
from kernel_test import function, run_c

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
args = parser.parse_args()
source = (args.source / 'drivers/media/platform/qcom/iris/iris_secure_memory.c').read_text()
code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <errno.h>
#include <stdio.h>
typedef uint64_t u64;
typedef uint64_t dma_addr_t;
#define BIT_ULL(n) (1ULL << (n))
#define PAGE_ALIGN(n) (((n)+4095)&~4095ULL)
#define QCOM_SCM_VMID_HLOS 3
#define QCOM_SCM_VMID_CP_NON_PIXEL 11
#define QCOM_SCM_PERM_READ 1
#define QCOM_SCM_PERM_RW 3
#define QCOM_SCM_PERM_EXEC 4
#define DMA_BIDIRECTIONAL 0
#define GFP_KERNEL 1
#define __GFP_ZERO 2
#define IRIS_SECURE_IOVA_START 0x01000000ULL
#define IRIS_SECURE_IOVA_END 0x25800000ULL
#define struct_size(p,f,n) (sizeof(*(p))+sizeof((p)->f[0])*(n))
struct device { int refs; };
struct platform_device { struct device dev; };
struct iris_core { struct platform_device *secure_pdev; };
struct iris_buffer { size_t buffer_size; dma_addr_t device_addr;
                     struct iris_secure_buffer *secure; };
struct scatterlist { u64 phys, dma_address; size_t length; };
struct sg_table { struct scatterlist *sgl; int orig_nents; };
struct qcom_scm_vmperm { int vmid, perm; };
#define for_each_sg(head,sg,n,i) for ((i)=0;(i)<(n)&&((sg)=&(head)[i]);(i)++)
#define sg_phys(sg) ((sg)->phys)
#define sg_dma_address(sg) ((sg)->dma_address)
#define dev_err(...) ((void)0)
static int dma_live, mem_live, grants, reclaims, grant_fail, reclaim_fail;
static bool dma_fail, meta_fail;
static u64 next_iova=0x25700000, protected_pa[128];
static int protected_count;
static struct sg_table *dma_alloc_noncontiguous(struct device *d, size_t size,
                                               int dir, int gfp, int attrs)
{
    (void)d; assert(!dir && gfp==(GFP_KERNEL|__GFP_ZERO) && !attrs);
    if (dma_fail) return NULL;
    struct sg_table *s=calloc(1,sizeof(*s));
    s->sgl=calloc(3,sizeof(*s->sgl)); s->orig_nents=3;
    for (int i=0;i<3;i++) s->sgl[i]=(struct scatterlist){
        .phys=0x80000000+next_iova+i*0x10000,
        .dma_address=next_iova,.length=i==2?size-8192:4096};
    next_iova-=0x40000; dma_live++; return s;
}
static void dma_free_noncontiguous(struct device *d,size_t size,struct sg_table *s,int dir)
{
    (void)d; assert(size && !dir);
    for (int i=0;i<3;i++) for (int j=0;j<protected_count;j++)
        assert(s->sgl[i].phys!=protected_pa[j]);
    free(s->sgl); free(s); dma_live--;
}
static void *kzalloc(size_t size,int flags)
{ (void)flags; if(meta_fail)return NULL; mem_live++; return calloc(1,size); }
static void kfree(void *p) { mem_live--; free(p); }
static struct device *get_device(struct device *d) { d->refs++; return d; }
static void put_device(struct device *d) { assert(d->refs>0); d->refs--; }
static int qcom_scm_assign_mem(u64 pa,size_t size,u64 *owner,
                               const struct qcom_scm_vmperm *p,unsigned n)
{
    assert(n==1 && !(pa&4095) && size && !(size&4095));
    if (p->vmid==QCOM_SCM_VMID_CP_NON_PIXEL) {
        assert(*owner==BIT_ULL(QCOM_SCM_VMID_HLOS) && p->perm==QCOM_SCM_PERM_RW);
        if (++grants==grant_fail) return -EIO;
        assert(protected_count<128); protected_pa[protected_count++]=pa;
    } else {
        assert(p->vmid==QCOM_SCM_VMID_HLOS && p->perm==7);
        assert(*owner==BIT_ULL(QCOM_SCM_VMID_CP_NON_PIXEL));
        if (++reclaims==reclaim_fail) return -EIO;
        int j; for(j=0;j<protected_count;j++)if(protected_pa[j]==pa)break;
        assert(j<protected_count); protected_pa[j]=protected_pa[--protected_count];
    }
    *owner=BIT_ULL(p->vmid); return 0;
}
'''
code += re.search(r'struct iris_secure_buffer \{.*?\n\};', source, re.S).group()+'\n'
for name in ('iris_secure_buffer_reclaim', 'iris_secure_buffer_free', 'iris_secure_buffer_alloc'):
    code += function(source, name)+'\n'
code += r'''
static struct platform_device pdev;
static struct iris_core core={.secure_pdev=&pdev};
static void clean(void)
{
    assert(!dma_live && !mem_live && !protected_count && !pdev.dev.refs);
    grants=reclaims=grant_fail=reclaim_fail=0;
    dma_fail=meta_fail=false; next_iova=0x25700000;
}
int main(void)
{
    struct iris_buffer a={.buffer_size=204800}, b={.buffer_size=204800};
    clean();
    assert(!iris_secure_buffer_alloc(&core,&a));
    assert(!iris_secure_buffer_alloc(&core,&b));
    assert(a.device_addr!=b.device_addr && a.secure!=b.secure);
    iris_secure_buffer_free(a.secure); iris_secure_buffer_free(b.secure); clean();
    for(int fail=1;fail<=3;fail++) {
        a.secure=NULL; grant_fail=fail;
        assert(iris_secure_buffer_alloc(&core,&a)==-EIO && !a.secure);
        assert(reclaims==fail-1); clean();
    }
    for(int fail=1;fail<=3;fail++) {
        assert(!iris_secure_buffer_alloc(&core,&a)); reclaim_fail=fail;
        iris_secure_buffer_free(a.secure);
        assert(dma_live==1 && mem_live==1 && protected_count==1 && pdev.dev.refs==1);
        reclaim_fail=0; iris_secure_buffer_free(a.secure); clean();
    }
    for(int fail=1;fail<=2;fail++) {
        a.secure=NULL; dma_fail=fail==1; meta_fail=fail==2;
        assert(iris_secure_buffer_alloc(&core,&a)==-ENOMEM && !a.secure); clean();
    }
    for(int i=0;i<3;i++) {
        next_iova=i==0?0:i==1?IRIS_SECURE_IOVA_START-4096:IRIS_SECURE_IOVA_END-4096;
        assert(iris_secure_buffer_alloc(&core,&a)==-ERANGE && !grants); clean();
    }
    next_iova=IRIS_SECURE_IOVA_START;
    assert(!iris_secure_buffer_alloc(&core,&a)); iris_secure_buffer_free(a.secure); clean();
    next_iova=IRIS_SECURE_IOVA_END-PAGE_ALIGN(a.buffer_size);
    assert(!iris_secure_buffer_alloc(&core,&a)); iris_secure_buffer_free(a.secure); clean();
    core.secure_pdev=NULL;
    assert(iris_secure_buffer_alloc(&core,&a)==-ENODEV); clean();
    puts("PASS: independent sessions, partial grants, failed reclaims, allocation failures and IOVA bounds");
}
'''
print(run_c(code), end='')

smmu_source = (args.source / 'drivers/iommu/arm/arm-smmu/arm-smmu-qcom.c').read_text()

def static_function(text, name):
    pos = text.index(name + '(')
    start = text.rfind('\nstatic ', 0, pos) + 1
    end = text.index('{', pos) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]

code = r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
typedef uint64_t u64;
typedef unsigned gfp_t;
#define PAGE_SIZE 4096UL
#define BIT_ULL(n) (1ULL << (n))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define GFP_KERNEL 1
#define GFP_ATOMIC 2
#define __GFP_ZERO 4
#define QCOM_SCM_VMID_HLOS 3
#define QCOM_SCM_VMID_CP_NON_PIXEL 11
#define QCOM_SCM_PERM_READ 1
#define QCOM_SCM_PERM_RW 3
#define QCOM_SCM_PERM_EXEC 4
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
struct device { int refs; };
struct work_struct { void (*run)(struct work_struct *); };
struct arm_smmu_device { struct device *dev; };
struct arm_smmu_domain { struct arm_smmu_device *smmu; };
struct qcom_smmu { struct arm_smmu_device smmu; int *secure_wq; };
struct qcom_scm_vmperm { int vmid,perm; };
#define to_qcom_smmu(p) container_of(p,struct qcom_smmu,smmu)
#define dev_err(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define INIT_WORK(w,f) ((w)->run=(f))
#define virt_to_phys(p) ((uintptr_t)(p))
static bool entry_fail, page_fail, assign_fail;
static int page_count, entry_count, calls;
static size_t allocation_size;
static u64 live_owner;
static struct work_struct *pending;
static bool gfpflags_allow_blocking(gfp_t gfp) { return gfp&GFP_KERNEL; }
static unsigned get_order(size_t size) {
    unsigned order=0; while ((PAGE_SIZE<<order)<size) order++; return order;
}
static unsigned long __get_free_pages(gfp_t gfp,unsigned order) {
    assert(gfp&__GFP_ZERO); if(page_fail)return 0;
    allocation_size=PAGE_SIZE<<order;
    void *p=aligned_alloc(PAGE_SIZE,allocation_size); assert(p);
    memset(p,0,allocation_size); page_count++; live_owner=BIT_ULL(QCOM_SCM_VMID_HLOS);
    return (unsigned long)p;
}
static void free_pages(unsigned long addr,unsigned order) {
    assert(live_owner==BIT_ULL(QCOM_SCM_VMID_HLOS));
    assert((PAGE_SIZE<<order)==allocation_size); page_count--; free((void *)addr);
}
static int qcom_scm_assign_mem(uintptr_t pa,size_t size,u64 *owner,
                               const struct qcom_scm_vmperm *p,unsigned n) {
    calls++; assert(!(pa&(PAGE_SIZE-1)) && size==allocation_size && *owner==live_owner);
    u64 dest=0;
    if(n==2) {
        assert(p[0].vmid==QCOM_SCM_VMID_HLOS && p[0].perm==QCOM_SCM_PERM_RW);
        assert(p[1].vmid==QCOM_SCM_VMID_CP_NON_PIXEL && p[1].perm==QCOM_SCM_PERM_READ);
    } else {
        assert(n==1 && p[0].vmid==QCOM_SCM_VMID_HLOS && p[0].perm==7);
    }
    if(assign_fail)return -EIO;
    for(unsigned i=0;i<n;i++)dest|=BIT_ULL(p[i].vmid);
    *owner=live_owner=dest; return 0;
}
static void *test_kmalloc(size_t size,gfp_t gfp) {
    assert(gfp==GFP_ATOMIC); if(entry_fail)return NULL;
    entry_count++; return malloc(size);
}
#define kmalloc_obj(obj,gfp) test_kmalloc(sizeof(obj),gfp)
static void kfree(void *p) { entry_count--; free(p); }
static struct device *get_device(struct device *d) { d->refs++; return d; }
static void put_device(struct device *d) { assert(d->refs>0); d->refs--; }
static void queue_work(int *wq,struct work_struct *w) {
    assert(wq && !pending); pending=w;
}
'''
code += re.search(r'struct qcom_smmu_secure_free \{.*?\n\};', smmu_source, re.S).group() + '\n'
for name in ('qcom_smmu_secure_alloc', 'qcom_smmu_secure_free_work', 'qcom_smmu_secure_free'):
    code += static_function(smmu_source, name) + '\n'
code += r'''
static void run_pending(void) {
    struct work_struct *w=pending; assert(w); pending=NULL; w->run(w);
}
int main(void) {
    struct device dev={0}; int queue;
    struct qcom_smmu qs={.smmu.dev=&dev,.secure_wq=&queue};
    struct arm_smmu_domain domain={.smmu=&qs.smmu};
    assert(!qcom_smmu_secure_alloc(&domain,4096,GFP_ATOMIC) && !calls);
    page_fail=true; assert(!qcom_smmu_secure_alloc(&domain,64,GFP_KERNEL)); page_fail=false;
    assign_fail=true; assert(!qcom_smmu_secure_alloc(&domain,64,GFP_KERNEL));
    assign_fail=false; assert(!page_count);
    for(size_t size=64;size<=8192;size*=128) {
        void *p=qcom_smmu_secure_alloc(&domain,size,GFP_KERNEL); assert(p);
        assert(live_owner==(BIT_ULL(3)|BIT_ULL(11)));
        int previous=calls; qcom_smmu_secure_free(&domain,p,size);
        assert(calls==previous && page_count==1 && dev.refs==1);
        run_pending(); assert(!page_count && !entry_count && !dev.refs);
    }
    void *p=qcom_smmu_secure_alloc(&domain,4096,GFP_KERNEL); assert(p);
    entry_fail=true; qcom_smmu_secure_free(&domain,p,4096);
    assert(!pending && page_count==1 && !entry_count);
    entry_fail=false; qcom_smmu_secure_free(&domain,p,4096); run_pending();
    assert(!page_count);
    p=qcom_smmu_secure_alloc(&domain,4096,GFP_KERNEL); assert(p);
    qcom_smmu_secure_free(&domain,p,4096); assign_fail=true; run_pending();
    assert(page_count==1 && !entry_count && !dev.refs && live_owner==(BIT_ULL(3)|BIT_ULL(11)));
    assign_fail=false; qcom_smmu_secure_free(&domain,p,4096); run_pending();
    assert(!page_count && !entry_count && !dev.refs);
    puts("PASS: table permissions, allocation granularity, atomic rejection, deferred reclaim and protected-page retention");
}
'''
print(run_c(code, flags=('-Wno-unused-parameter',)), end='')

# A real Iris2 encoder DRAIN acknowledgement has flags=0. Treating it as an
# error races CAPTURE QBUF against the final frame and can truncate the stream.
response = (args.source / 'drivers/media/platform/qcom/iris/iris_hfi_gen2_response.c').read_text()
code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#define ENCODER 1
#define DECODER 2
#define HFI_FW_FLAGS_SUCCESS 1
#define HFI_FW_FLAGS_INFORMATION 2
#define HFI_FW_FLAGS_SESSION_ERROR 4
#define HFI_FW_FLAGS_SYSTEM_ERROR 8
#define IRIS_INST_ERROR 5
#define IRIS_INST_SUB_DRAIN 8
#define IRIS_INST_SUB_INPUT_PAUSE 16
struct platform { bool enc_drain_no_success; };
struct iris_core { struct platform *iris_platform_data; };
struct iris_inst { struct iris_core *core; unsigned domain, state, sub_state; };
struct iris_hfi_packet { unsigned flags; };
static int iris_inst_change_state(struct iris_inst *i, unsigned state)
{ i->state=state; return 0; }
static int iris_inst_change_sub_state(struct iris_inst *i, unsigned clear, unsigned set)
{ i->sub_state=(i->sub_state&~clear)|set; return 0; }
'''
code += function(response, 'iris_hfi_gen2_handle_session_drain') + '\n'
code += r'''
int main(void)
{
    struct platform p={0}; struct iris_core core={.iris_platform_data=&p};
    const unsigned errors[]={HFI_FW_FLAGS_INFORMATION, HFI_FW_FLAGS_SESSION_ERROR,
        HFI_FW_FLAGS_SYSTEM_ERROR, HFI_FW_FLAGS_SUCCESS|HFI_FW_FLAGS_SESSION_ERROR,
        HFI_FW_FLAGS_SUCCESS|HFI_FW_FLAGS_SYSTEM_ERROR};
    for (int legacy=1;legacy>=0;legacy--) {
        p.enc_drain_no_success=legacy;
        for (unsigned domain=ENCODER;domain<=DECODER;domain++) {
            struct iris_inst i={.core=&core,.domain=domain,.state=4,
                .sub_state=IRIS_INST_SUB_DRAIN};
            struct iris_hfi_packet pkt={.flags=0};
            assert(!iris_hfi_gen2_handle_session_drain(&i,&pkt));
            if (legacy && domain==ENCODER) {
                assert(i.state==4 && i.sub_state==(IRIS_INST_SUB_DRAIN|IRIS_INST_SUB_INPUT_PAUSE));
            } else {
                assert(i.state==IRIS_INST_ERROR && i.sub_state==IRIS_INST_SUB_DRAIN);
            }
            i.state=4; i.sub_state=IRIS_INST_SUB_DRAIN; pkt.flags=HFI_FW_FLAGS_SUCCESS;
            assert(!iris_hfi_gen2_handle_session_drain(&i,&pkt));
            assert(i.state==4 && (i.sub_state&IRIS_INST_SUB_INPUT_PAUSE));
            for (unsigned n=0;n<sizeof(errors)/sizeof(errors[0]);n++) {
                i.state=4; i.sub_state=IRIS_INST_SUB_DRAIN; pkt.flags=errors[n];
                assert(!iris_hfi_gen2_handle_session_drain(&i,&pkt));
                assert(i.state==IRIS_INST_ERROR && i.sub_state==IRIS_INST_SUB_DRAIN);
            }
        }
    }
    p.enc_drain_no_success=true;
    struct iris_inst i={.core=&core,.domain=ENCODER,.state=4};
    struct iris_hfi_packet pkt={.flags=0};
    assert(!iris_hfi_gen2_handle_session_drain(&i,&pkt));
    assert(i.state==4 && !i.sub_state);
    puts("PASS: Iris2 encoder drain ACK, decoder/newer-firmware rejection and genuine error flags");
}
'''
print(run_c(code), end='')
