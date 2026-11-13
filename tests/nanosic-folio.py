#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the actual folio state/report paths against a fake GPIO and HID sink."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from kernel_test import run_c

PROJECT = Path(__file__).resolve().parents[1]
PATCHES = PROJECT / 'build/kernel/patches/linux-7.3'
DRIVER = 'drivers/hid/hid-nanosic.c'


def function(code, name):
    match = re.search(r'^static [^;\n]*\b' + name + r'\([^;{]*\)\s*\{', code, re.M)
    if not match:
        raise ValueError(name)
    end, depth = match.end(), 1
    while depth:
        depth += (code[end] == '{') - (code[end] == '}')
        end += 1
    return code[match.start():end]


def assemble(root):
    """Rebuild just the driver from the shipped series; no kernel/network needed."""
    recipe = json.loads((PROJECT / 'build/kernel/source.json').read_text())
    patches = [PROJECT / path for path in recipe['target_patches']]
    base = PATCHES / '0001-preserve-liuqin-adaptation.patch'
    assert patches[0] == base
    patch = base.read_text()
    section = patch.split('diff --git a/' + DRIVER + ' b/' + DRIVER + '\n', 1)[1]
    section = section.split('\ndiff --git ', 1)[0]
    source = root / DRIVER
    source.parent.mkdir(parents=True)
    source.write_text('\n'.join(line[1:] for line in section.splitlines()
                                if line.startswith('+') and not line.startswith('+++')) + '\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    for patch in patches[1:]:
        if '+++ b/' + DRIVER not in patch.read_text():
            continue
        subprocess.run(['git', 'apply', '--include=' + DRIVER, str(patch)], cwd=root, check=True)
    return source.read_text()


STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8; typedef uint64_t u64; typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define READ_ONCE(v) (v)
#define WRITE_ONCE(p,v) ((p)=(v))
#define EV_SW 5
#define SW_LID 0
#define SW_TABLET_MODE 1
#define KOBJ_CHANGE 1
#define HID_INPUT_REPORT 0
#define system_wq NULL
#define system_dfl_wq NULL
#define msecs_to_jiffies(v) (v)
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
struct work_struct {bool pending;};
struct delayed_work {struct work_struct work;bool pending;unsigned long delay;};
#define to_delayed_work(p) container_of(p,struct delayed_work,work)
static int held;
struct test_guard {bool active;};
static struct test_guard take(int *p){(void)p;assert(!held);held=1;return (struct test_guard){true};}
static void drop(struct test_guard *g){if(g->active){assert(held);held=0;g->active=false;}}
#define guard(type) struct test_guard guard_ __attribute__((cleanup(drop))) = take
#define scoped_guard(type,p) for(struct test_guard g_ __attribute__((cleanup(drop)))=take(p);g_.active;drop(&g_))
#define lockdep_assert_held(p) do{(void)(p);assert(held);}while(0)
#define dev_dbg(p,...) ((void)(p))
struct device {int kobj;};
struct i2c_client {struct device dev;};
struct input_handle {void *private;};
struct hid_device {int index;};
static int switch_value, switches, events, led_updates, forwarded[4];
static bool pressed[4], contacts[256];
static u8 received[4][21];
struct nanosic {
 struct i2c_client *client;struct hid_device *hid[4];void *tablet_mode_input;
 struct delayed_work posture_work;struct work_struct lid_work;int lock,*tablet_mode_gpio;
 bool suspended,posture_stopping,lid_closed,folio_folded,input_muted,tablet_mode;
 bool connection_known,keyboard_connected;u8 keyboard_status;u64 connection_reports;
 u8 last_touchpad[21];unsigned int bad_magic_count,valid_report_count;
};
static int gpiod_get_value_cansleep(int *p){assert(held);return *p;}
static void mod_delayed_work(void *q,struct delayed_work *w,unsigned long delay){(void)q;w->pending=true;w->delay=delay;}
static void schedule_work(struct work_struct *w){w->pending=true;}
static void input_report_switch(void *p,int code,int v){(void)p;assert(held&&code==SW_TABLET_MODE);switch_value=v;switches++;}
static void input_sync(void *p){(void)p;}
static void kobject_uevent(void *p,int action){(void)p;assert(action==KOBJ_CHANGE);events++;}
static void nanosic_queue_led(struct nanosic *n){(void)n;led_updates++;}
static void nanosic_mark_led_ready(struct nanosic *n){(void)n;}
static int hid_input_report(struct hid_device *hid,int type,u8 *report,int len,int interrupt){
 assert(held&&type==HID_INPUT_REPORT&&interrupt==1);int i=hid->index;
 const int lengths[]={9,3,8,21};const int ids[]={5,6,2,25};
 assert(len==lengths[i]&&report[0]==ids[i]);forwarded[i]++;memcpy(received[i],report,len);
 pressed[i]=false;
 if(i==0){for(int k=1;k<9;k++)pressed[i]|=report[k]!=0;}
 else if(i==1)pressed[i]=(report[1]||report[2]);
 else pressed[i]=report[1]!=0;
 if(i==3){for(int f=0;f<report[20]&&f<3;f++)contacts[report[3+6*f]]=(report[2+6*f]&1)!=0;}
 return 0;
}
'''

CASES = r'''
static struct nanosic nano;static struct i2c_client client;static struct hid_device devices[4];
static int folded;
static void init(void){
 memset(&nano,0,sizeof(nano));memset(pressed,0,sizeof(pressed));memset(contacts,0,sizeof(contacts));
 memset(forwarded,0,sizeof(forwarded));memset(received,0,sizeof(received));
 nano.client=&client;nano.tablet_mode_gpio=&folded;nano.tablet_mode=true;folded=0;
 for(int i=0;i<4;i++){devices[i].index=i;nano.hid[i]=&devices[i];}
 held=0;switch_value=1;switches=events=led_updates=0;
}
static void work(void){nano.posture_work.pending=false;nanosic_posture_work(&nano.posture_work.work);assert(!held);}
static void run_lid_work(void){nano.lid_work.pending=false;nanosic_lid_work(&nano.lid_work);assert(!held);}
static void packet(const u8 *p,int len){
 u8 frame[68]={0x57,0,0x39};assert(len<=65);memcpy(frame+3,p,len);
 scoped_guard(mutex,&nano.lock){nanosic_parse_frame(&nano,frame,len+3);}assert(!held);
}
static void connection(bool attached){u8 p[16]={0x22,0,0x38,0x80,0xa2,0,0,0,0,attached?3:0};packet(p,16);}
static void lid(bool closed){struct input_handle h={&nano};nanosic_lid_event(&h,EV_SW,SW_LID,closed);run_lid_work();}
static void fold(bool back){folded=back;assert(nanosic_fold_irq(1,&nano)==IRQ_HANDLED);assert(nano.posture_work.delay==150);work();}
static void all_down(void){
 const u8 key[9]={5,3,0,4};const u8 media[5]={6,0xe9,0};const u8 mouse[8]={2,1,1};
 u8 touch[21]={25,1};for(int i=0;i<3;i++){touch[2+i*6]=7;touch[3+i*6]=(u8)(4+71*i);}touch[20]=3;
 packet(key,9);packet(media,5);packet(mouse,8);packet(touch,21);
}
static void released(void){
 for(int i=0;i<4;i++)assert(!pressed[i]);
 for(int i=0;i<256;i++)assert(!contacts[i]);
}
static int total(void){return forwarded[0]+forwarded[1]+forwarded[2]+forwarded[3];}
int main(void){
 int cases=0;
 /* All connector/lid/fold combinations: closing does not itself mean tablet mode. */
 for(int c=0;c<2;c++)for(int l=0;l<2;l++)for(int f=0;f<2;f++){
  init();connection(c);lid(l);fold(f);
  assert(switch_value==(f||!c));assert(nano.input_muted==(f||l));
  int before=total();all_down();assert((total()==before)==(f||l||!c));cases++;
 }
 init();connection(true);all_down();for(int i=0;i<4;i++)assert(pressed[i]);
 fold(true);released();assert(switch_value==1);int before=total();all_down();assert(total()==before);cases++;
 /* Transport/status packets still work while input is suppressed. */
 connection(false);assert(!nano.keyboard_connected&&switch_value==1);
 connection(true);assert(nano.keyboard_connected&&switch_value==1);cases++;
 fold(false);assert(switch_value==0);all_down();for(int i=0;i<4;i++)assert(pressed[i]);cases++;
 /* The exact same Ctrl+Shift/key report can be pressed after a release. */
 lid(true);released();assert(switch_value==0);before=total();all_down();assert(total()==before);cases++;
 fold(true);lid(false);assert(nano.input_muted);fold(false);all_down();assert(pressed[0]);cases++;
 connection(false);released();assert(switch_value==1);connection(true);all_down();assert(pressed[0]);cases++;
 /* Idle alone never detaches; repeated state notifications do not re-release. */
 before=total();int switches_before=switches;for(int i=0;i<100;i++)work();
 assert(nano.keyboard_connected&&switches==switches_before&&total()==before);cases++;
 /* A short report cannot flip connection or release a held key. */
 u8 truncated[9]={0x22,0,0x38,0x80,0xa2};packet(truncated,9);
 assert(nano.keyboard_connected&&pressed[0]);cases++;
 /* A lid event is gated even before its deferred work gets CPU time. */
 struct input_handle h={&nano};nanosic_lid_event(&h,EV_SW,SW_LID,1);assert(nano.lid_work.pending);
 all_down();released();before=total();all_down();assert(total()==before);cases++;
 /* Lid work releases keys without pulling a bouncing fold GPIO forward. */
 init();connection(true);all_down();folded=1;nanosic_fold_irq(1,&nano);
 lid(true);released();assert(!nano.folio_folded&&switch_value==0);
 assert(nano.posture_work.pending&&nano.posture_work.delay==150);
 folded=0;nanosic_fold_irq(1,&nano);lid(false);
 assert(nano.posture_work.pending&&nano.posture_work.delay==150);
 work();assert(!nano.folio_folded&&switch_value==0);cases++;
 /* Keep pre-status input and suppress stale reports after a confirmed detach. */
 init();all_down();assert(pressed[0]);connection(false);released();
 before=total();all_down();assert(total()==before);connection(true);
 all_down();assert(pressed[0]);cases++;
 /* An unreadable hall retains the previous safe state. */
 lid(false);fold(true);folded=-EIO;work();assert(nano.folio_folded&&nano.input_muted);cases++;
 /* Old DTs without the optional folded line continue to support connector/lid. */
 init();nano.tablet_mode_gpio=NULL;connection(true);work();all_down();assert(pressed[0]);lid(true);released();cases++;
 /* Suspend does not schedule GPIO work; resume's refresh samples the new posture. */
 init();connection(true);nano.suspended=true;folded=1;
 nanosic_fold_irq(1,&nano);assert(!nano.posture_work.pending);work();assert(!nano.folio_folded);
 nano.suspended=false;scoped_guard(mutex,&nano.lock){nanosic_refresh_posture(&nano);}assert(switch_value==1);cases++;
 nano.posture_stopping=true;nano.posture_work.pending=nano.lid_work.pending=false;
 nanosic_lid_event(&h,EV_SW,SW_LID,1);nanosic_fold_irq(1,&nano);
 assert(!nano.posture_work.pending&&!nano.lid_work.pending);cases++;
 printf("PASS: %d actual-driver folio scenarios (hardware not exercised)\n",cases);return 0;
}
'''


WAKE_STUBS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64; typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define IRQF_ONESHOT 0x2000
#define EPROBE_DEFER 517
#define READ_ONCE(v) (v)
#define WRITE_ONCE(p,v) ((p)=(v))
#define system_dfl_wq NULL
#define msecs_to_jiffies(v) (v)
struct work_struct {bool pending;};
struct delayed_work {struct work_struct work;bool pending;};
struct device {void *data;};
struct i2c_client {struct device dev;};
struct nanosic {
 struct i2c_client *client;int lock,*wake_gpio;
 int data_irq,wakeup_irq;bool data_irq_wake_enabled;
 bool suspended,awake,data_pending,caps_lock_valid,led_transport_ready;
 u64 irq_count,wakeup_irq_count;
 struct delayed_work posture_work,idle_work,status_work,led_work;
 struct work_struct lid_work;unsigned int status_attempts;
};
static int held,wake_level,wake_events,reads,raises,enable_calls,disable_calls;
static int enable_error,disable_error,wake_depth;
static bool may_wakeup;
static void (*cancel_hook)(void);
struct test_guard {bool active;};
static struct test_guard take(int *p){(void)p;assert(!held);held=1;return (struct test_guard){true};}
static void drop(struct test_guard *g){if(g->active){assert(held);held=0;g->active=false;}}
#define guard(type) struct test_guard guard_ __attribute__((cleanup(drop))) = take
#define scoped_guard(type,p) for(struct test_guard g_ __attribute__((cleanup(drop)))=take(p);g_.active;drop(&g_))
static void *dev_get_drvdata(struct device *d){return d->data;}
static bool device_may_wakeup(struct device *d){(void)d;return may_wakeup;}
static void pm_wakeup_event(struct device *d,int ms){(void)d;assert(held&&ms==1000);wake_events++;}
static void nanosic_wake(struct nanosic *n){assert(held&&!n->suspended);n->awake=true;wake_level=1;raises++;n->idle_work.pending=true;}
static void nanosic_receive_frame(struct nanosic *n){assert(held&&!n->suspended);reads++;}
static void nanosic_release_inputs(struct nanosic *n){assert(held&&n->suspended);}
static void nanosic_refresh_posture(struct nanosic *n){assert(held&&!n->suspended);}
static void nanosic_queue_led(struct nanosic *n){assert(held&&!n->suspended);n->led_work.pending=true;}
static void mod_delayed_work(void *q,struct delayed_work *w,unsigned long delay){(void)q;assert(held&&delay==200);w->pending=true;}
static void cancel_delayed_work_sync(struct delayed_work *w){assert(!held);w->pending=false;if(cancel_hook){void (*fn)(void)=cancel_hook;cancel_hook=NULL;fn();}}
static void cancel_work_sync(struct work_struct *w){assert(!held);w->pending=false;}
static void gpiod_set_value_cansleep(int *p,int v){assert(held);*p=v;}
static int enable_irq_wake(int irq){assert(!held&&irq==50);enable_calls++;if(enable_error)return enable_error;wake_depth++;return 0;}
static int disable_irq_wake(int irq){assert(!held&&irq==50&&wake_depth==1);disable_calls++;if(disable_error)return disable_error;wake_depth--;return 0;}
static int data_lookup,wakeup_lookup;
static void *dev_fwnode(struct device *d){return d;}
static int fwnode_irq_get_byname(void *node,const char *name){(void)node;if(!strcmp(name,"data"))return data_lookup;assert(!strcmp(name,"wakeup"));return wakeup_lookup;}
#define dev_err_probe(dev,ret,...) ((void)(dev),(ret))
/* Model resources owned by I2C core separately from reverse-order devres. */
static int pm_irq,owner,request_error,attach_error,action_error;
static int actions[4],action_count,clear_count;
static irqreturn_t (*status_handler)(int,void *);
static void dev_pm_clear_wake_irq(struct device *dev){(void)dev;clear_count++;if(owner==1)owner=0;pm_irq=0;}
static int devm_request_threaded_irq(struct device *dev,int irq,void *hard,
 irqreturn_t (*thread)(int,void *),unsigned long flags,const char *name,void *arg){
 assert(dev==&((struct nanosic*)arg)->client->dev&&irq==46&&!hard);
 assert(flags==IRQF_ONESHOT&&!strcmp(name,"nanosic-wakeup"));
 assert(!owner&&!pm_irq);if(request_error)return request_error;
 owner=2;status_handler=thread;actions[action_count++]=1;return 0;
}
static int devm_pm_set_wake_irq(struct device *dev,int irq){
 assert(irq==46&&owner==2&&!pm_irq);if(attach_error)return attach_error;
 pm_irq=irq;if(action_error){dev_pm_clear_wake_irq(dev);return action_error;}
 actions[action_count++]=2;return 0;
}
static void release_resources(struct device *dev){
 while(action_count){int a=actions[--action_count];if(a==2)dev_pm_clear_wake_irq(dev);
 else {assert(a==1&&owner==2&&!pm_irq);owner=0;status_handler=NULL;}}
 /* I2C probe failure/remove also clears the wake IRQ after devres. */
 dev_pm_clear_wake_irq(dev);assert(!owner&&!pm_irq&&!action_count);
}
'''

WAKE_CASES = r'''
static struct nanosic n;static struct i2c_client c;
static void init(void){
 memset(&n,0,sizeof(n));memset(&c,0,sizeof(c));n.client=&c;c.dev.data=&n;
 n.wake_gpio=&wake_level;n.data_irq=50;n.wakeup_irq=46;n.awake=true;
 wake_level=1;wake_events=reads=raises=enable_calls=disable_calls=0;
 enable_error=disable_error=wake_depth=held=0;may_wakeup=true;cancel_hook=NULL;
 pm_irq=46;owner=1;request_error=attach_error=action_error=0;action_count=clear_count=0;
 status_handler=NULL;data_lookup=50;wakeup_lookup=46;
}
static void transition_irq(void){
 assert(n.suspended);nanosic_wakeup_irq(46,&n);nanosic_data_irq(50,&n);
 assert(!reads&&!raises&&!wake_level&&n.data_pending);
}
int main(void){int cases=0;
 init();assert(!nanosic_irqs_get(&n)&&n.data_irq==50&&n.wakeup_irq==46);cases++;
 init();data_lookup=-EPROBE_DEFER;assert(nanosic_irqs_get(&n)==-EPROBE_DEFER);cases++;
 init();wakeup_lookup=-EPROBE_DEFER;assert(nanosic_irqs_get(&n)==-EPROBE_DEFER);cases++;
 init();wakeup_lookup=-ENXIO;assert(nanosic_irqs_get(&n)==-ENXIO);cases++;
 init();wakeup_lookup=50;assert(nanosic_irqs_get(&n)==-EINVAL);cases++;
 init();assert(!nanosic_request_wakeup_irq(&n));assert(owner==2&&pm_irq==46&&clear_count==1);
 assert(status_handler&&status_handler(46,&n)==IRQ_HANDLED);
 assert(n.wakeup_irq_count==1&&!wake_events&&!reads&&!raises);
 release_resources(&c.dev);assert(clear_count==3);cases++;
 init();request_error=-EBUSY;assert(nanosic_request_wakeup_irq(&n)==-EBUSY);
 release_resources(&c.dev);cases++;
 init();attach_error=-ENOMEM;assert(nanosic_request_wakeup_irq(&n)==-ENOMEM);
 release_resources(&c.dev);cases++;
 init();action_error=-ENOMEM;assert(nanosic_request_wakeup_irq(&n)==-ENOMEM);
 release_resources(&c.dev);cases++;
 init();pm_irq=owner=0;assert(!nanosic_request_wakeup_irq(&n));release_resources(&c.dev);cases++;
 /* Awake status edges neither hold the bridge awake nor fabricate input. */
 init();n.awake=false;wake_level=0;
 for(int i=0;i<8;i++)assert(nanosic_wakeup_irq(46,&n)==IRQ_HANDLED);
 assert(n.wakeup_irq_count==8&&!wake_events&&!reads&&!raises&&!wake_level);cases++;
 init();assert(nanosic_data_irq(50,&n)==IRQ_HANDLED);assert(reads==1&&raises==1&&!wake_events);cases++;
 init();assert(!nanosic_suspend(&c.dev));assert(n.suspended&&!n.awake&&!wake_level&&wake_depth==1);
 nanosic_wakeup_irq(46,&n);assert(wake_events==1&&!reads&&!raises&&!n.data_pending);
 assert(!nanosic_resume(&c.dev)&&!n.suspended&&!wake_depth&&disable_calls==1);
 assert(!reads&&raises==1);cases++;
 init();assert(!nanosic_suspend(&c.dev));nanosic_data_irq(50,&n);
 assert(n.data_pending&&wake_events==1&&!reads&&!raises);
 assert(!nanosic_resume(&c.dev));assert(!n.data_pending&&reads==1&&raises==1&&!wake_depth);cases++;
 init();may_wakeup=false;assert(!nanosic_suspend(&c.dev));transition_irq();
 assert(!wake_events&&!enable_calls&&!wake_depth);assert(!nanosic_resume(&c.dev));
 assert(reads==1&&!disable_calls);cases++;
 /* Edges between the suspend flag and IRQ arming must avoid suspended I2C. */
 init();cancel_hook=transition_irq;assert(!nanosic_suspend(&c.dev));
 assert(wake_events==2&&!reads&&!raises);assert(!nanosic_resume(&c.dev));assert(reads==1);cases++;
 init();cancel_hook=transition_irq;enable_error=-EIO;assert(nanosic_suspend(&c.dev)==-EIO);
 assert(!n.suspended&&!n.data_pending&&!wake_depth&&reads==1&&raises==1);
 assert(n.status_work.pending&&n.led_work.pending);cases++;
 init();for(int i=0;i<6;i++){assert(!nanosic_suspend(&c.dev));assert(!nanosic_resume(&c.dev));}
 assert(enable_calls==6&&disable_calls==6&&!wake_depth);cases++;
 /* Preserve resume errors rather than claiming successful wake disarm. */
 init();assert(!nanosic_suspend(&c.dev));disable_error=-EIO;
 assert(nanosic_resume(&c.dev)==-EIO&&!n.suspended);cases++;
 assert(!held);printf("PASS: %d actual-driver IRQ and suspend scenarios (hardware not exercised)\n",cases);
 return 0;
}
'''


def check_wakeup(code):
    names = ('nanosic_data_irq', 'nanosic_wakeup_irq', 'nanosic_irqs_get',
             'nanosic_request_wakeup_irq', 'nanosic_suspend', 'nanosic_resume')
    output = run_c(WAKE_STUBS + '\n'.join(function(code, name) for name in names) + WAKE_CASES,
                   flags=('-Wno-unused-parameter', '-fsanitize=address,undefined'))
    print(output, end='')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='nanosic-folio-') as temporary:
        root = Path(temporary)
        code = (args.source / DRIVER).read_text() if args.source else assemble(root / 'source')
        constants = '\n'.join(re.findall(r'^#define NANOSIC_(?:FRAME_\w+|ID_\w+|TP_CONTACTS)\s+[^\n]+', code, re.M))
        constants += '\n' + re.search(r'enum nanosic_hid_index \{.*?\n};', code, re.S)[0]
        constants += '\n' + re.search(r'struct nanosic_packet \{.*?\n};', code, re.S)[0]
        constants += '\n' + re.search(r'static const struct nanosic_packet nanosic_packets\[\] = \{.*?\n};', code, re.S)[0]
        names = ('nanosic_packet_by_id', 'nanosic_release_inputs', 'nanosic_report_posture',
                 'nanosic_refresh_posture', 'nanosic_posture_work', 'nanosic_lid_work', 'nanosic_fold_irq',
                 'nanosic_lid_event', 'nanosic_parse_connection_status', 'nanosic_parse_frame')
        program = STUBS + constants + '\n' + '\n'.join(function(code, name) for name in names) + CASES
        source = root / 'folio.c'; source.write_text(program)
        binary = root / 'folio'
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined',
                        '-fno-sanitize-recover=all', str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=15)
        check_wakeup(code)


if __name__ == '__main__':
    main()
