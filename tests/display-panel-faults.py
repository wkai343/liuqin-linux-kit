#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run actual panel fault state-machine C with IRQ depth, timers and concurrent events."""
from pathlib import Path
import argparse,hashlib,json,re,subprocess,tempfile


def function(s,n):
    m=re.search(r'^static (?:void|bool|irqreturn_t|ssize_t)\s+'+n+r'\([^;{]*\)\s*\{',s,re.M);assert m,n
    i=m.end();depth=1
    while depth:depth+=(s[i]=='{')-(s[i]=='}');i+=1
    return s[m.start():i]


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,required=True);ap.add_argument('--report',type=Path,required=True);ap.add_argument('--cc',default='cc');a=ap.parse_args();a.report.unlink(missing_ok=True)
    s=(a.source/'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text()
    assert 'IRQF_TRIGGER_HIGH | IRQF_ONESHOT | IRQF_NO_AUTOEN' in s
    assert 'esd_confirm_level' in s
    assert '"error-gpios"' in s and 'static DEVICE_ATTR_RO(esd_status)' in s and 'static DEVICE_ATTR_WO(esd_inject)' in s
    names=['liuqin_esd_mask_locked','liuqin_esd_fault','liuqin_esd_irq','liuqin_esd_arm_work','liuqin_esd_start','liuqin_esd_stop','liuqin_esd_shutdown','esd_status_show','esd_inject_store']
    body='\n'.join(function(s,n) for n in names)
    assert 'mipi_dsi_' not in body and 'gpiod_set' not in body
    prefix=r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <sys/types.h>
typedef uint64_t u64;typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define BIT(i) (1U<<(i))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
struct mutex {pthread_mutex_t lock;int rank;};
static _Thread_local struct mutex *stack[4];static _Thread_local int depth;
static void mutex_lock(struct mutex *m){assert(depth<4 && (!depth || stack[depth-1]->rank<m->rank));assert(!pthread_mutex_lock(&m->lock));stack[depth++]=m;}
static void mutex_unlock(struct mutex *m){assert(depth && stack[depth-1]==m);depth--;assert(!pthread_mutex_unlock(&m->lock));}
#define lockdep_assert_held(m) assert(depth && stack[depth-1]==(m))
struct work_struct {int unused;};struct delayed_work {struct work_struct work;bool queued;unsigned long delay;};
#define to_delayed_work(p) container_of(p,struct delayed_work,work)
static int system_wq,queue_count,cancel_count,sync_count,irq_depth[2],gpio_reads,cases;
static unsigned long jiffies;
#define time_before(a,b) ((long)((a)-(b))<0)
static unsigned long msecs_to_jiffies(unsigned int n){return n;}
static unsigned int jiffies_to_msecs(unsigned long n){return n;}
static void mod_delayed_work(int w,struct delayed_work *d,unsigned long delay){(void)w;assert(depth && stack[depth-1]->rank==1);d->queued=true;d->delay=delay;queue_count++;}
static void cancel_delayed_work(struct delayed_work *d){assert(!depth);d->queued=false;cancel_count++;}
static void cancel_delayed_work_sync(struct delayed_work *d){assert(!depth);d->queued=false;cancel_count++;}
static void enable_irq(int irq){assert(depth && stack[depth-1]->rank==1 && irq>=10 && irq<=11);assert(irq_depth[irq-10]==1);irq_depth[irq-10]--;}
static void disable_irq_nosync(int irq){assert(depth && stack[depth-1]->rank==1 && irq>=10 && irq<=11);assert(irq_depth[irq-10]==0);irq_depth[irq-10]++;}
static void synchronize_irq(int irq){assert(!depth && irq>=10 && irq<=11);sync_count++;}
struct gpio_desc {int level;};
static int gpiod_get_value_cansleep(struct gpio_desc *g){assert(depth && stack[depth-1]->rank==1);gpio_reads++;return g->level;}
struct device {int kobj;void *data;};struct device_attribute {int unused;};
static void *dev_get_drvdata(struct device *d){return d->data;}
static atomic_int notifications,warnings,traces;
static void sysfs_notify(void *k,const char *dir,const char *name){(void)k;assert(!dir && !strcmp(name,"esd_status"));atomic_fetch_add(&notifications,1);}
static void dev_warn(void *dev,const char *fmt,...) __attribute__((format(printf,2,3)));
static void dev_warn(void *dev,const char *fmt,...){(void)dev;(void)fmt;atomic_fetch_add(&warnings,1);}
#define dev_warn_ratelimited dev_warn
static void dev_info_ratelimited(void *dev,const char *fmt,...){(void)dev;(void)fmt;atomic_fetch_add(&traces,1);}
static ssize_t sysfs_emit(char *b,const char *fmt,...) __attribute__((format(printf,2,3)));
static ssize_t sysfs_emit(char *b,const char *fmt,...){va_list a;va_start(a,fmt);int n=vsnprintf(b,4096,fmt,a);va_end(a);return n;}
static int kstrtouint(const char *s,unsigned int base,unsigned int *out){char *end;unsigned long n=strtoul(s,&end,base);if(end==s || (*end && *end!='\n') || n>UINT_MAX || *s=='-')return -EINVAL;*out=n;return 0;}
struct drm_panel {struct mutex backlight_lock;struct device *dev;bool enabled,backlight_pending,backlight_after_first_frame;unsigned int backlight_cookie;};
struct nt36532 {struct drm_panel panel;struct mutex esd_lock;struct delayed_work esd_arm_work;struct gpio_desc *esd_gpio[2];int esd_irq[2];bool esd_available,esd_enabled,esd_stopping,esd_pending;unsigned int esd_armed,esd_cycle,esd_last_line;unsigned long esd_started;u64 esd_events;};
static bool esd_monitor=true,esd_confirm_level=true;
'''
    suffix=r'''
static struct nt36532 ctx;static struct device device;static struct gpio_desc gpios[2];
static void setup(void){assert(!depth);memset(&ctx,0,sizeof(ctx));device.data=&ctx;ctx.panel.dev=&device;ctx.panel.backlight_lock.rank=0;ctx.esd_lock.rank=1;assert(!pthread_mutex_init(&ctx.panel.backlight_lock.lock,NULL));assert(!pthread_mutex_init(&ctx.esd_lock.lock,NULL));for(int i=0;i<2;i++){gpios[i].level=0;ctx.esd_gpio[i]=&gpios[i];ctx.esd_irq[i]=10+i;irq_depth[i]=1;}ctx.esd_available=true;ctx.panel.enabled=true;ctx.panel.backlight_after_first_frame=true;ctx.panel.backlight_cookie=1;jiffies=10000;queue_count=cancel_count=sync_count=gpio_reads=0;atomic_store(&notifications,0);atomic_store(&warnings,0);atomic_store(&traces,0);esd_monitor=true;esd_confirm_level=true;cases++;}
static void start(void){mutex_lock(&ctx.panel.backlight_lock);liuqin_esd_start(&ctx);mutex_unlock(&ctx.panel.backlight_lock);}
static void arm(void){ctx.esd_arm_work.queued=false;liuqin_esd_arm_work(&ctx.esd_arm_work.work);}
static void finish(void){liuqin_esd_shutdown(&ctx);assert(irq_depth[0]==1 && irq_depth[1]==1 && !ctx.esd_enabled && !ctx.esd_arm_work.queued && !depth);assert(!pthread_mutex_destroy(&ctx.esd_lock.lock));assert(!pthread_mutex_destroy(&ctx.panel.backlight_lock.lock));}
static void *irq_thread(void *arg){int irq=10+(int)(intptr_t)arg;assert(liuqin_esd_irq(irq,&ctx)==IRQ_HANDLED);return NULL;}
int main(void){
 setup();start();assert(ctx.esd_enabled && !ctx.esd_pending && ctx.esd_cycle==1 && ctx.esd_arm_work.delay==1000);arm();assert(!gpio_reads && !ctx.esd_armed && ctx.esd_arm_work.delay==1000);jiffies+=999;arm();assert(!gpio_reads && ctx.esd_arm_work.delay==1);jiffies++;arm();assert(ctx.esd_armed==3 && !irq_depth[0] && !irq_depth[1]);finish();
 setup();gpios[1].level=1;start();jiffies+=1000;arm();assert(ctx.esd_armed==1 && irq_depth[1]==1 && !ctx.esd_pending && ctx.esd_arm_work.delay==2000);jiffies+=2000;arm();assert(ctx.esd_armed==1);gpios[1].level=0;jiffies+=2000;arm();assert(ctx.esd_armed==3);finish();
 setup();gpios[0].level=-EIO;gpios[1].level=1;start();jiffies+=1000;arm();assert(!ctx.esd_armed && !ctx.esd_events && ctx.esd_arm_work.queued);finish();
 setup();start();jiffies+=1000;arm();gpios[1].level=1;assert(liuqin_esd_fault(&ctx,1,false));assert(ctx.esd_pending && ctx.esd_events==1 && ctx.esd_last_line==1 && !ctx.esd_armed);assert(!liuqin_esd_fault(&ctx,0,false) && !liuqin_esd_fault(&ctx,2,true));arm();assert(!ctx.esd_armed);finish();
 setup();start();jiffies+=1000;arm();gpios[0].level=gpios[1].level=1;pthread_t t[32];for(int i=0;i<32;i++)assert(!pthread_create(&t[i],NULL,irq_thread,(void*)(intptr_t)(i%2)));for(int i=0;i<32;i++)assert(!pthread_join(t[i],NULL));assert(ctx.esd_events==1 && ctx.esd_pending && atomic_load(&warnings)==1);finish();
 setup();start();jiffies+=1000;arm();liuqin_esd_stop(&ctx,false);arm();assert(!ctx.esd_enabled && !ctx.esd_armed && !liuqin_esd_fault(&ctx,0,false));ctx.panel.backlight_cookie=2;start();assert(ctx.esd_cycle==2);arm();assert(!ctx.esd_armed && ctx.esd_arm_work.delay==1000);jiffies+=1000;arm();assert(ctx.esd_armed==3);finish();
 for(int bad=0;bad<6;bad++){setup();start();jiffies+=1000;if(bad==0)esd_monitor=false;if(bad==1)ctx.esd_enabled=false;if(bad==2)ctx.esd_stopping=true;if(bad==3)ctx.panel.enabled=false;if(bad==4)ctx.panel.backlight_pending=true;if(bad==5)ctx.panel.backlight_cookie=2;arm();assert(!ctx.esd_armed && !gpio_reads);finish();}
 for(int bad=0;bad<5;bad++){setup();if(bad==0)esd_monitor=false;if(bad==1)ctx.esd_available=false;if(bad==2)ctx.esd_stopping=true;if(bad==3)ctx.panel.backlight_after_first_frame=false;if(bad==4)ctx.panel.backlight_cookie=0;start();assert(!ctx.esd_enabled && !queue_count);finish();}
 setup();start();assert(esd_inject_store(&device,NULL,"1\n",2)==2);assert(ctx.esd_events==1 && ctx.esd_last_line==2);assert(esd_inject_store(&device,NULL,"1\n",2)==-EBUSY);assert(esd_inject_store(&device,NULL,"2",1)==-EINVAL);assert(esd_inject_store(&device,NULL,"bad",3)==-EINVAL);char buf[4096];esd_status_show(&device,NULL,buf);assert(strstr(buf,"pending=1") && strstr(buf,"events=1") && strstr(buf,"last=2"));liuqin_esd_stop(&ctx,false);assert(esd_inject_store(&device,NULL,"1",1)==-EBUSY);finish();
 setup();jiffies=ULONG_MAX-50;start();arm();assert(!ctx.esd_armed);jiffies+=1000;arm();assert(ctx.esd_armed==3);finish();

 setup();start();jiffies+=1000;arm();assert(liuqin_esd_irq(10,&ctx)==IRQ_HANDLED);assert(!ctx.esd_pending && !ctx.esd_events && ctx.esd_armed==3 && !irq_depth[0] && !irq_depth[1]);assert(atomic_load(&traces)==1);gpios[0].level=1;assert(liuqin_esd_irq(10,&ctx)==IRQ_HANDLED);assert(ctx.esd_pending && ctx.esd_events==1);finish();
 setup();start();jiffies+=1000;arm();gpios[0].level=-EIO;assert(liuqin_esd_irq(10,&ctx)==IRQ_HANDLED);assert(!ctx.esd_pending && !ctx.esd_events && !ctx.esd_armed && ctx.esd_arm_work.queued && ctx.esd_arm_work.delay==2000);gpios[0].level=0;jiffies+=2000;arm();assert(ctx.esd_armed==3);gpios[1].level=1;assert(liuqin_esd_irq(11,&ctx)==IRQ_HANDLED);assert(ctx.esd_events==1 && ctx.esd_last_line==1);finish();
 setup();esd_confirm_level=false;start();jiffies+=1000;arm();assert(liuqin_esd_irq(10,&ctx)==IRQ_HANDLED);assert(ctx.esd_pending && ctx.esd_events==1 && atomic_load(&traces)==0);finish();
 setup();start();jiffies+=1000;arm();for(int i=0;i<32;i++)assert(!pthread_create(&t[i],NULL,irq_thread,(void*)(intptr_t)(i%2)));for(int i=0;i<32;i++)assert(!pthread_join(t[i],NULL));assert(!ctx.esd_events && !ctx.esd_pending && ctx.esd_armed==3 && atomic_load(&traces)==32);finish();
 printf("%d\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='liuqin-panel-faults-') as tmp:
        c=Path(tmp)/'test.c';exe=Path(tmp)/'test';c.write_text(prefix+body+suffix)
        subprocess.run([a.cc,'-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-pthread','-fsanitize=undefined','-fno-sanitize-recover=all',str(c),'-o',str(exe)],check=True)
        cases=int(subprocess.check_output([str(exe)],text=True,timeout=20))
    r={'all_passed':True,'cases':cases,'inactive_level_irqs_do_not_latch_faults':True,'gpio_read_errors_mask_and_retry_without_fault':True,'confirmed_high_irqs_still_recover':True,'legacy_comparison_switch_checked':True,'actual_candidate_state_machine_executed':True,'initial_high_irq_not_assumed_fault':True,'no_autoenable_and_mask_depth_balanced':True,'concurrent_irq_events_coalesced':True,'startup_grace_stale_work_and_generation_checked':True,'no_reset_or_dcs_transfer_in_irq':True,'synchronous_cleanup_holds_no_worker_locks':True,'software_injection_requires_active_generation':True,'timer_wrap_checked':True,'source_sha256':hashlib.sha256(s.encode()).hexdigest()};a.report.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))


if __name__=='__main__':main()
