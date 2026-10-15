#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the actual OEM wait functions and patched level-based kernel poll.

No hardware, credentials, images or templates are accessed by these host tests.
"""
from pathlib import Path
import os,subprocess,tempfile
PROJECT=Path(__file__).resolve().parents[1]
source=(PROJECT/'device/fingerprint/oem/src/fingerprint/oem/fpc_build_info.c').read_text()
waits=source[source.index('static int wait_sensor_irq('):source.index('/* OEM HAL store_template_db_host:')]
harness=r'''
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>
static volatile sig_atomic_t operation_cancelled;
enum query_kind {SENSOR_WAIT_FINGER_DOWN,SENSOR_CHECK_FINGER_LOST,
 SENSOR_WAIT_FINGER_LOST,SENSOR_CHECK_FINGER_STATE,CAPTURE_ENROL};
static int scenario,level,armed,polls,captures,qualified,lifted;
static int64_t now_ms;
static int fake_clock_gettime(clockid_t clock,struct timespec *now){
 assert(clock==CLOCK_MONOTONIC);now->tv_sec=now_ms/1000;now->tv_nsec=now_ms%1000*1000000;return 0;
}
static int fake_usleep(useconds_t duration){now_ms+=scenario==5?60000:duration/1000;return 0;}
static int fake_poll(struct pollfd *event,nfds_t count,int timeout){
 assert(count==1);polls++;event->revents=level?POLLIN:0;
 if(!level)now_ms+=timeout;
 return level?1:0; /* Physical level is not consumed by polling. */
}
static int query(int fd,uint32_t session,enum query_kind kind){
 (void)fd;(void)session;
 if(kind==SENSOR_WAIT_FINGER_DOWN){armed++;level=scenario==7?0:1;return 0;}
 if(kind==SENSOR_WAIT_FINGER_LOST){armed++;level=1;return 0;}
 if(kind==SENSOR_CHECK_FINGER_STATE){qualified++;level=0;return scenario==3&&qualified==1?0:1;}
 if(kind==SENSOR_CHECK_FINGER_LOST){lifted++;return 1;}
 assert(kind==CAPTURE_ENROL);captures++;level=0;
 if(scenario==4)return captures<=50?1:0;
 if(scenario==5)return 1;
 if(scenario==6)return 4; /* A genuine quality error must remain a retry. */
 return 0;
}
#define poll fake_poll
#define clock_gettime fake_clock_gettime
#define usleep fake_usleep
'''+waits+r'''
int main(void){
 scenario=1;assert(wait_finger_down(1,1,1,120000,1)==0);assert(armed==1&&polls==1);
 assert(wait_sensor_irq(1,"held",10)==0);assert(level==1); /* Durable high IRQ. */
 scenario=2;armed=polls=qualified=lifted=0;
 assert(wait_finger_lost(1,1,1)==0);assert(armed==1&&qualified==1&&lifted==1);
 scenario=3;armed=polls=qualified=lifted=0;
 assert(wait_finger_lost(1,1,1)==0);assert(armed==2&&qualified==2&&lifted==1);
 scenario=4;captures=armed=0;
 assert(capture_with_wait(1,1,1)==0);assert(captures==51&&armed==51);
 scenario=5;captures=armed=0;
 assert(capture_with_wait(1,1,1)==-1);assert(captures==2); /* One retry deadline. */
 scenario=6;captures=0;assert(capture_with_wait(1,1,1)==4);assert(captures==1);
 scenario=7;captures=0;assert(capture_with_wait(1,1,1)==-1);assert(captures==0);
 operation_cancelled=1;level=1;assert(wait_sensor_irq(1,"cancel",10)==-1);
 puts("OEM IRQ sequencing, WAIT_TIME, quality rejection, deadline and cancel: passed");
}
'''
patch=(PROJECT/'build/kernel/patches/0002-fpc-irq-level.patch').read_text()
reconstructed='\n'.join(line[1:] for line in patch.splitlines() if line.startswith((' ', '+')) and not line.startswith('+++'))
start=reconstructed.index('static __poll_t fpc1020_poll(');end=reconstructed.index('\n}',start)+2
poll_function=reconstructed[start:end]
kernel_harness=r'''
#include <assert.h>
#include <sys/epoll.h>
#include <stdio.h>
typedef unsigned int __poll_t;
struct poll_table_struct {int unused;};
struct file {void *private_data;};
struct fpc1020_data {void *irq_gpio;int irq_wait;};
static int raw_level,registered;
static int gpiod_get_raw_value_cansleep(void *gpio){(void)gpio;return raw_level;}
static void poll_wait(struct file *file,int *queue,struct poll_table_struct *wait){
 (void)file;(void)queue;(void)wait;registered++;
}
'''+poll_function+r'''
int main(void){
 struct fpc1020_data data={0};struct file file={&data};struct poll_table_struct wait={0};
 raw_level=0;assert(fpc1020_poll(&file,&wait)==0); /* An ended pulse isn't ready. */
 raw_level=1;assert(fpc1020_poll(&file,&wait)&EPOLLIN);assert(fpc1020_poll(&file,&wait)&EPOLLIN);
 raw_level=0;assert(fpc1020_poll(&file,&wait)==0);
 raw_level=-5;assert(fpc1020_poll(&file,&wait)==EPOLLERR);
 assert(registered==5);puts("Kernel poll follows physical GPIO level without consuming it: passed");
}
'''
with tempfile.TemporaryDirectory(prefix='liuqin-irq-') as t:
 for name,code in [('client',harness),('kernel',kernel_harness)]:
  p=Path(t);(p/(name+'.c')).write_text(code)
  subprocess.run([os.environ.get('CC','cc'),'-Wall','-Wextra','-Werror','-o',str(p/name),str(p/(name+'.c'))],check=True)
  r=subprocess.run([str(p/name)],capture_output=True,text=True)
  if r.returncode:print(r.stderr);r.check_returncode()
  print(r.stdout.splitlines()[-1])
