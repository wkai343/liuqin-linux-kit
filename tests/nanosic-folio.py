#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the actual folio state/report paths against a fake GPIO and HID sink."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

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
#define msecs_to_jiffies(v) (v)
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
struct work_struct {int unused;};
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
 struct delayed_work posture_work;int lock,*tablet_mode_gpio;
 bool suspended,posture_stopping,lid_closed,folio_folded,input_muted,tablet_mode;
 bool connection_known,keyboard_connected;u8 keyboard_status;u64 connection_reports;
 u8 last_touchpad[21];unsigned int bad_magic_count,valid_report_count;
};
static int gpiod_get_value_cansleep(int *p){assert(held);return *p;}
static void mod_delayed_work(void *q,struct delayed_work *w,unsigned long delay){(void)q;w->pending=true;w->delay=delay;}
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
static void packet(const u8 *p,int len){
 u8 frame[68]={0x57,0,0x39};assert(len<=65);memcpy(frame+3,p,len);
 scoped_guard(mutex,&nano.lock){nanosic_parse_frame(&nano,frame,len+3);}assert(!held);
}
static void connection(bool attached){u8 p[16]={0x22,0,0x38,0x80,0xa2,0,0,0,0,attached?3:0};packet(p,16);}
static void lid(bool closed){struct input_handle h={&nano};nanosic_lid_event(&h,EV_SW,SW_LID,closed);work();}
static void fold(bool back){folded=back;assert(nanosic_fold_irq(1,&nano)==IRQ_HANDLED);assert(nano.posture_work.delay==40);work();}
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
  int before=total();all_down();assert((total()==before)==(f||l));cases++;
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
 struct input_handle h={&nano};nanosic_lid_event(&h,EV_SW,SW_LID,1);assert(nano.posture_work.pending);
 all_down();released();before=total();all_down();assert(total()==before);cases++;
 /* An unreadable hall retains the previous safe state. */
 lid(false);fold(true);folded=-EIO;work();assert(nano.folio_folded&&nano.input_muted);cases++;
 /* Old DTs without the optional folded line continue to support connector/lid. */
 init();nano.tablet_mode_gpio=NULL;connection(true);work();all_down();assert(pressed[0]);lid(true);released();cases++;
 /* Suspend does not schedule GPIO work; resume's refresh samples the new posture. */
 init();connection(true);nano.suspended=true;folded=1;
 nanosic_fold_irq(1,&nano);assert(!nano.posture_work.pending);work();assert(!nano.folio_folded);
 nano.suspended=false;scoped_guard(mutex,&nano.lock){nanosic_refresh_posture(&nano);}assert(switch_value==1);cases++;
 nano.posture_stopping=true;nano.posture_work.pending=false;
 nanosic_lid_event(&h,EV_SW,SW_LID,1);nanosic_fold_irq(1,&nano);assert(!nano.posture_work.pending);cases++;
 printf("PASS: %d actual-driver folio scenarios (hardware not exercised)\n",cases);return 0;
}
'''


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
                 'nanosic_refresh_posture', 'nanosic_posture_work', 'nanosic_fold_irq',
                 'nanosic_lid_event', 'nanosic_parse_connection_status', 'nanosic_parse_frame')
        program = STUBS + constants + '\n' + '\n'.join(function(code, name) for name in names) + CASES
        source = root / 'folio.c'; source.write_text(program)
        binary = root / 'folio'
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined',
                        '-fno-sanitize-recover=all', str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=15)


if __name__ == '__main__':
    main()
