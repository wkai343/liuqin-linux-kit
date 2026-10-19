#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the NanoSic Caps Lock command and lifecycle paths with a fake I2C bus."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def function(text, name):
    m = re.search(r'^static (?:const char \*|int|void|bool)\s*' + name
                  + r'\([^;{]*\)\s*\{', text, re.M)
    if not m:
        raise ValueError(name)
    end, depth = m.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[m.start():end]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--report', type=Path)
    args = ap.parse_args()
    rel = 'drivers/hid/hid-nanosic.c'
    code = (args.source / rel).read_text()
    old = (args.baseline / rel).read_text()
    for name in ('nanosic_keyboard_rdesc', 'nanosic_packets'):
        pattern = r'static const [^;]+\b' + name + r'\[.*?\n};'
        assert re.search(pattern, code, re.S)[0] == re.search(pattern, old, re.S)[0]
    assert function(code, 'nanosic_hid_raw_request') == function(old, 'nanosic_hid_raw_request')
    assert '.request = nanosic_hid_request,' in code and '.request =' not in old
    assert 'hid_alloc_report_buf(report, GFP_KERNEL)' in function(code, 'nanosic_hid_request')
    probe = function(code, 'nanosic_probe')
    assert probe.index('INIT_DELAYED_WORK(&nano->led_work') < probe.index('nanosic_register_hid(nano)')
    assert probe.index('nano->caps_lock_known = true') < probe.index('nanosic_register_hid(nano)')
    assert probe.index('sysfs_create_group') < probe.index('nano->led_ready = true')
    for name in ('nanosic_led_command',):
        array = re.search(r'static const u8 ' + name + r'\[.*?\n};', code, re.S)[0]
    program = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8; typedef uint64_t u64;
#define BIT(n) (1U<<(n))
#define NANOSIC_ID_KEYBOARD 5
#define NANOSIC_HID_KEYBOARD 0
#define NANOSIC_HID_COUNT 4
#define HID_REQ_SET_REPORT 9
#define HID_OUTPUT_REPORT 1
#define GFP_KERNEL 0
#define SW_TABLET_MODE 1
#define KOBJ_CHANGE 1
#define WRITE_ONCE(p,v) ((p)=(v))
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
#define system_wq NULL
#define system_dfl_wq ((void*)1)
static unsigned long jiffies;
#define msecs_to_jiffies(v) (v)
static void mod_delayed_work(void *q,struct delayed_work *w,unsigned long delay){(void)q;w->pending=true;w->delay=delay;}
static void cancel_delayed_work_sync(struct delayed_work *w){assert(!held);w->pending=false;}
struct device {void *data;int kobj;};
struct i2c_client {unsigned short addr;void *adapter;struct device dev;};
struct i2c_msg {unsigned short addr,flags,len;u8 *buf;};
struct hid_device {struct {struct device *parent;} dev;};
struct hid_report {unsigned int id,type,len;u8 bitmap;};
enum nanosic_i2c_operation {NANOSIC_I2C_NONE,NANOSIC_I2C_DATA_READ,NANOSIC_I2C_VERSION_WRITE,NANOSIC_I2C_VERSION_READ,NANOSIC_I2C_STATUS_WRITE,NANOSIC_I2C_LED_WRITE};
struct nanosic {
 struct i2c_client *client;void *vdd;int *wake_gpio,*reset_gpio;int lock;
 struct hid_device *hid[NANOSIC_HID_COUNT];struct delayed_work idle_work,status_work,led_work;
 unsigned long wake_deadline;bool awake,suspended,data_pending;
 int data_irq;bool data_irq_wake_enabled;
 u64 i2c_transfer_count,i2c_error_count,last_i2c_error_transfer,reset_recovery_count;
 int last_i2c_errno;enum nanosic_i2c_operation last_i2c_operation;
 void *tablet_mode_input;unsigned int status_attempts;
 bool connection_known,keyboard_connected;u8 keyboard_status;u64 connection_reports;
 bool led_ready,led_transport_ready,led_stopping,caps_lock_known,caps_lock_requested,caps_lock_sent,caps_lock_valid;
 unsigned int led_attempts;u64 led_write_count;int last_led_errno;
};
static int tx_result,tx_count,sleep_calls,wake_level,reset_level,alloc_fail,allocations;
static int may_wakeup,enable_error,disable_error,receive_count,regulator_off,switch_value;
static unsigned int requested_size;static u8 last_command[67];
static int nanosic_attr_group;
static void *dev_get_drvdata(struct device *d){return d->data;}
static void *i2c_get_clientdata(struct i2c_client *c){return c->dev.data;}
static void gpiod_set_value_cansleep(int *p,int v){*p=v;}
static void usleep_range(int a,int b){assert(a==25000&&b==25500);sleep_calls++;}
static void msleep(int n){(void)n;}
static void regulator_disable(void *p){(void)p;regulator_off++;}
static void sysfs_remove_group(void *p,void *g){(void)p;(void)g;}
static bool device_may_wakeup(struct device *d){(void)d;return may_wakeup;}
static int enable_irq_wake(int n){(void)n;return enable_error;}
static int disable_irq_wake(int n){(void)n;return disable_error;}
static void nanosic_receive_frame(struct nanosic *n){(void)n;receive_count++;}
static void input_report_switch(void *p,int code,int value){(void)p;assert(code==SW_TABLET_MODE);switch_value=value;}
static void input_sync(void *p){(void)p;}
static void kobject_uevent(void *p,int v){(void)p;(void)v;}
static int i2c_transfer(void *adapter,struct i2c_msg *m,int n){
 (void)adapter;assert(held&&n==1&&m->addr==0x4c&&!m->flags&&m->len==67);
 memcpy(last_command,m->buf,67);tx_count++;return tx_result;
}
static unsigned int hid_report_len(struct hid_report *r){return r->len;}
static u8 *hid_alloc_report_buf(struct hid_report *r,int flags){
 (void)flags;if(alloc_fail)return NULL;requested_size=r->len+7;allocations++;return calloc(1,requested_size);
}
static void hid_output_report(struct hid_report *r,u8 *buf){
 assert(requested_size==9);buf[r->len+6]=0xa5;buf[0]=r->id;buf[1]=r->bitmap;
}
static void kfree(void *p){assert(allocations>0);allocations--;free(p);}
'''
    for name in ('NANOSIC_VERSION_COMMAND_LEN', 'NANOSIC_IDLE_TIMEOUT_MS',
                 'NANOSIC_I2C_RETRY_US', 'NANOSIC_RESET_ASSERT_MS', 'NANOSIC_RESET_READY_MS',
                 'NANOSIC_LED_COMMAND_LEN', 'NANOSIC_LED_RETRIES', 'NANOSIC_LED_RETRY_MS'):
        program += re.search(r'^#define\s+' + name + r'\s+[^\n]+', code, re.M)[0] + '\n'
    program += array + '\n'
    for name in ('nanosic_wake', 'nanosic_i2c_transfer', 'nanosic_queue_led', 'nanosic_mark_led_ready',
                 'nanosic_send_led_command', 'nanosic_led_work', 'nanosic_reset_bridge',
                 'nanosic_parse_connection_status', 'nanosic_hid_request',
                 'nanosic_power_off', 'nanosic_remove', 'nanosic_suspend', 'nanosic_resume'):
        program += function(code, name) + '\n'
    program += r'''
static struct nanosic nano;static struct i2c_client client;static struct hid_device devices[4];
static struct hid_report report;
static void init(void){
 memset(&nano,0,sizeof(nano));memset(&client,0,sizeof(client));memset(devices,0,sizeof(devices));
 client.addr=0x4c;client.dev.data=&nano;nano.client=&client;nano.led_ready=true;nano.led_transport_ready=true;
 nano.wake_gpio=&wake_level;nano.reset_gpio=&reset_level;nano.connection_known=true;nano.keyboard_connected=true;nano.keyboard_status=3;
 for(int i=0;i<4;i++){devices[i].dev.parent=&client.dev;nano.hid[i]=&devices[i];}
 report=(struct hid_report){.id=5,.type=HID_OUTPUT_REPORT,.len=2};
 tx_result=1;tx_count=sleep_calls=held=alloc_fail=allocations=0;
 may_wakeup=1;enable_error=disable_error=receive_count=regulator_off=0;
 memset(last_command,0xff,sizeof(last_command));
}
static void request(u8 bits){report.bitmap=bits;nanosic_hid_request(&devices[0],&report,HID_REQ_SET_REPORT);assert(!held&&!allocations);}
static void run_led(void){assert(nano.led_work.pending);nano.led_work.pending=false;nanosic_led_work(&nano.led_work.work);assert(!held);}
static void expected(bool on){
 const u8 prefix[]={0x4c,0x32,0,0x4e,0x31,0x80,0x38,0x26,1};
 assert(!memcmp(prefix,last_command,sizeof(prefix)));assert(last_command[9]==on&&last_command[10]==0x5e + on);
 for(int i=11;i<67;i++)assert(last_command[i]==0);
}
static bool status(u8 flags,size_t len){
 u8 p[10]={0x22,0,0x38,0x80,0xa2,0,0,0,0,flags};bool ret;
 scoped_guard(mutex,&nano.lock){ret=nanosic_parse_connection_status(&nano,p,len);}return ret;
}
static void ready(void){scoped_guard(mutex,&nano.lock){nanosic_mark_led_ready(&nano);}}
int main(void){
 int cases=0;init();request(2);assert(nano.led_work.pending&&!tx_count);run_led();expected(true);
 assert(nano.caps_lock_valid&&nano.caps_lock_sent&&nano.led_write_count==1&&sleep_calls==1);cases++;
 request(0);run_led();expected(false);assert(sleep_calls==1);cases++;
 request(0x1d);assert(!nano.led_work.pending&&tx_count==2);cases++; /* Other LED bits do not write. */
 for(int n=0;n<32;n++){request(2);request(0);request(2);run_led();expected(true);request(0);run_led();expected(false);}
 assert(allocations==0);cases++;
 init();nano.caps_lock_known=true;scoped_guard(mutex,&nano.lock){nanosic_queue_led(&nano);}run_led();expected(false);cases++;
 for(int malformed=0;malformed<6;malformed++){
  init();struct hid_device *h=&devices[0];int req=HID_REQ_SET_REPORT;
  if(malformed==0)h=&devices[1];if(malformed==1)req=1;if(malformed==2)report.type=2;
  if(malformed==3)report.id=0;if(malformed==4)report.len=1;if(malformed==5)report.len=3;
  nanosic_hid_request(h,&report,req);assert(!tx_count&&!nano.led_work.pending&&!nano.caps_lock_known&&!allocations);cases++;
 }
 init();alloc_fail=1;request(2);assert(!nano.caps_lock_known&&!nano.led_work.pending);cases++;
 init();nano.led_ready=false;request(2);assert(nano.caps_lock_known&&!nano.led_work.pending&&!tx_count);
 nano.led_ready=true;scoped_guard(mutex,&nano.lock){nanosic_queue_led(&nano);}run_led();expected(true);cases++;
 init();nano.awake=true;request(2);run_led();assert(!sleep_calls);cases++;
 init();client.addr=0x4d;request(2);for(int i=0;i<3;i++)run_led();assert(!tx_count&&!nano.led_work.pending&&nano.last_led_errno==-ENXIO);cases++;
 for(int err=0;err<2;err++){
  init();tx_result=err?-ETIMEDOUT:0;request(2);
  for(int attempt=0;attempt<3;attempt++){run_led();assert(!nano.caps_lock_valid);}
  assert(tx_count==3&&!nano.led_work.pending&&nano.last_led_errno==(err?-ETIMEDOUT:-EIO));cases++;
  tx_result=1;request(2);run_led();assert(nano.caps_lock_valid&&nano.caps_lock_sent&&nano.last_led_errno==0);cases++;
 }
 init();request(2);tx_result=-EIO;run_led();assert(nano.led_work.pending);
 request(0);tx_result=1;run_led();expected(false);assert(nano.led_attempts==1);cases++;
 init();request(2);run_led();assert(status(0,10));assert(!nano.caps_lock_valid&&switch_value==1);
 request(2);assert(!nano.led_work.pending);assert(status(3,10));run_led();expected(true);cases++;
 assert(status(3,10));assert(!nano.led_work.pending);cases++;
 assert(status(1,10));run_led();assert(status(3,10));run_led();expected(true);cases++;
 assert(!status(0,9));assert(nano.keyboard_connected&&!nano.led_work.pending);cases++;
 init();request(2);assert(status(0,10));run_led();assert(!tx_count);cases++;
 init();request(2);assert(!nanosic_suspend(&client.dev));assert(nano.suspended&&!nano.led_work.pending&&!nano.caps_lock_valid);
 request(0);assert(!nano.led_work.pending&&!tx_count);nano.data_pending=true;
 assert(!nanosic_resume(&client.dev));assert(!nano.led_work.pending);ready();run_led();expected(false);assert(receive_count==1);cases++;
 init();request(2);run_led();assert(!nanosic_suspend(&client.dev));assert(!nanosic_resume(&client.dev));assert(!nano.led_work.pending);ready();run_led();expected(true);assert(nano.led_write_count==2);cases++;
 init();request(2);enable_error=-EIO;assert(nanosic_suspend(&client.dev)==-EIO);
 assert(!nano.suspended&&!nano.led_work.pending);ready();run_led();expected(true);cases++;
 init();may_wakeup=0;request(2);assert(!nanosic_suspend(&client.dev));assert(!nano.led_work.pending);assert(!nanosic_resume(&client.dev));ready();run_led();expected(true);cases++;
 init();request(2);run_led();scoped_guard(mutex,&nano.lock){nanosic_reset_bridge(&nano);}assert(!nano.led_work.pending);ready();run_led();assert(nano.reset_recovery_count==1);expected(true);cases++;
 init();request(2);nanosic_remove(&client);request(0);assert(nano.led_stopping&&!nano.led_work.pending&&!tx_count);cases++;
 init();request(2);nanosic_power_off(&nano);request(0);assert(regulator_off==1&&nano.led_stopping&&!nano.led_work.pending&&!tx_count);cases++;
 /* Initial HID state is retained until an application response arrives. */
 init();nano.led_transport_ready=false;request(2);request(0);assert(!tx_count&&!nano.led_work.pending);
 assert(!status(3,9));assert(!nano.led_transport_ready&&!nano.led_work.pending);cases++;
 assert(status(3,10));assert(nano.led_transport_ready);run_led();expected(false);cases++;
 /* A valid application report only starts one replay, not one per key. */
 ready();assert(!nano.led_work.pending);cases++;
 /* A stale work item also checks readiness before touching the bus. */
 init();request(2);nano.led_transport_ready=false;run_led();assert(!tx_count&&!nano.caps_lock_valid);cases++;
 ready();run_led();expected(true);cases++;
 printf("%d\n",cases);return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='nanosic-led-') as temp:
        root = Path(temp); c = root / 'led.c'; c.write_text(program)
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined',
                        '-fno-sanitize-recover=all', str(c), '-o', str(root / 'led')], check=True)
        cases = int(subprocess.check_output([str(root / 'led')], text=True, timeout=15))
    result = {'all_passed': True, 'actual_driver_functions_executed': True, 'cases': cases,
              'coalescing_cycles': 32, 'descriptor_and_raw_request_unchanged': True,
              'command_and_padding_verified': True, 'hardware_validated': False}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
