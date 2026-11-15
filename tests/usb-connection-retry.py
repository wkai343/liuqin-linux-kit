#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute UCSI status admission, event replay and retry lifetime from real sources."""
import argparse
import importlib.util
from pathlib import Path
import re
import unittest

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,required=True)
args,rest=parser.parse_known_args()
spec=importlib.util.spec_from_file_location('common',Path(__file__).with_name('kernel-common-fixes.py'))
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)

FIXTURE=r'''
typedef uint8_t u8;
typedef uint16_t u16;
#define EPROBE_DEFER 517
#define HZ 100
#define system_long_wq NULL
#define EVENT_PENDING 0
#define UCSI_ENABLE_NTFY_CONNECTOR_CHANGE 1
#define UCSI_SET_NOTIFICATION_ENABLE 1
#define UCSI_GET_CONNECTOR_STATUS 2
#define UCSI_CONNECTOR_NUMBER(n) ((n)<<8)
#define UCSI_GET_CONNECTOR_STATUS_SIZE (32*sizeof(unsigned long)*8)
#define DECLARE_BITMAP(n,s) unsigned long n[32]
#define bitmap_copy(a,b,n) memcpy((a),(b),sizeof(unsigned long)*32)
#define UCSI_MAX_DATA_LENGTH(u) (sizeof(unsigned long)*32)
#define min(a,b) ((a)<(b)?(a):(b))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define smp_load_acquire(p) (*(p))
#define UCSI_CONSTAT(c,k) ((c)->status[FIELD_##k])
enum { FIELD_CHANGE, FIELD_CONNECTED, FIELD_PWR_DIR, FIELD_PARTNER_TYPE,
 FIELD_PARTNER_FLAG_USB, FIELD_PARTNER_FLAG_USB4_GEN3, FIELD_PARTNER_FLAG_USB4_GEN4,
 FIELD_PWR_OPMODE, FIELD_PWR_READING_READY_V2_1, FIELD_CURRENT_SCALE_V2_1,
 FIELD_VOLTAGE_SCALE_V2_1, FIELD_PEAK_CURRENT_V2_1, FIELD_AVG_CURRENT_V2_1,
 FIELD_VBUS_VOLTAGE_V2_1 };
#define UCSI_CONSTAT_PARTNER_TYPE_UFP 1
#define UCSI_CONSTAT_PARTNER_TYPE_CABLE_AND_UFP 2
#define UCSI_CONSTAT_PARTNER_TYPE_CABLE 3
#define UCSI_CONSTAT_PARTNER_TYPE_DFP 4
#define UCSI_CONSTAT_PARTNER_TYPE_AUDIO 5
#define UCSI_CONSTAT_PARTNER_TYPE_DEBUG 6
#define UCSI_CONSTAT_CONNECT_CHANGE (1<<14)
#define UCSI_CONSTAT_POWER_DIR_CHANGE (1<<12)
#define UCSI_CONSTAT_PARTNER_CHANGE (1<<11)
#define UCSI_CONSTAT_POWER_OPMODE_CHANGE (1<<2)
#define UCSI_CONSTAT_POWER_LEVEL_CHANGE (1<<6)
#define UCSI_CONSTAT_PDOS_CHANGE (1<<5)
#define UCSI_CONSTAT_CAM_CHANGE (1<<8)
#define UCSI_CONSTAT_BC_CHANGE (1<<9)
#define UCSI_CONSTAT_SINK_PATH_CHANGE (1<<13)
#define UCSI_CAP_GET_PD_MESSAGE 1
#define UCSI_CAP_CABLE_DETAILS 2
#define UCSI_CONSTAT_PWR_OPMODE_PD 3
#define UCSI_CONSTAT_CURR_SCALE_MULT 5
#define UCSI_CONSTAT_VOLT_SCALE_MULT 50
#define UCSI_VERSION_2_0 0x200
#define UCSI_VERSION_2_1 0x210
#define UCSI_VERSION_3_0 0x300
#define UCSI_USB4_IMPLIES_USB 1
#define TYPEC_HOST 1
#define TYPEC_DEVICE 2
#define TYPEC_MODE_DEBUG 3
#define TYPEC_MODE_AUDIO 4
#define TYPEC_STATE_USB 5
#define USB_MODE_USB4 4
#define fallthrough __attribute__((fallthrough))
#define dev_err(...) ((void)0)
#define dev_err_once(...) ((void)0)
#define dev_dbg(...) ((void)0)
#define dev_warn_ratelimited(...) ((void)0)
#define trace_ucsi_connector_change(...) ((void)0)
enum usb_role {USB_ROLE_NONE,USB_ROLE_HOST,USB_ROLE_DEVICE};
enum typec_role {TYPEC_SINK,TYPEC_SOURCE};
struct work_struct {bool queued;};
struct delayed_work {struct work_struct work; bool disabled;};
struct ucsi;
struct ucsi_connector {
 struct ucsi *ucsi; int num,lock,complete;
 struct work_struct work; struct delayed_work connection_work;
 bool connection_pending; unsigned connection_retries; u16 pending_changes;
 unsigned long status[32]; void *wq,*port,*partner,*usb_role_sw,*partner_altmode[1];
 u32 peak_current,avg_current,vbus_voltage;
};
struct ops {int (*prepare_connection)(struct ucsi_connector *); void (*connector_status)(struct ucsi_connector *);};
struct ucsi {
 struct ops *ops; struct ucsi_connector *connector; void *dev;
 struct {unsigned features; int num_connectors;} cap;
 unsigned version,quirks; unsigned long flags; u64 ntfy;
 struct delayed_work work; struct work_struct resume_work;
};
static unsigned long firmware[32];
static int prepare_error,query_error,role_calls,actual_role=-1,reads,power_updates;
static int retry_queues,callback_calls,work_cancels,resume_error;
static int prepare(struct ucsi_connector *c) {assert(c->lock); return UCSI_CONSTAT(c,CONNECTED)?prepare_error:0;}
static void status_callback(struct ucsi_connector *c) {assert(c->lock); callback_calls++;}
static void mutex_lock(int *p) {assert(!*p); *p=1;}
static void mutex_unlock(int *p) {assert(*p); *p=0;}
static bool test_and_set_bit(int bit,unsigned long *p) {bool v=*p&1;*p|=1;return v;}
static void clear_bit(int bit,unsigned long *p) {*p&=~1UL;}
static int ucsi_send_command_common(struct ucsi *u,u64 cmd,void *out,size_t len,void *data,size_t n,bool ack) {
 reads++; if(ack)u->flags=0; if(query_error)return query_error;
 assert(len==sizeof(firmware)); memcpy(out,firmware,len); return len;
}
static int ucsi_send_command(struct ucsi *u,u64 cmd,void *out,size_t len) {return resume_error;}
static unsigned msecs_to_jiffies(unsigned ms) {return ms;}
static void mod_delayed_work(void *wq,struct delayed_work *w,unsigned delay) {
 assert(delay==500); if(!w->disabled){w->work.queued=true;retry_queues++;}
}
static void cancel_delayed_work(struct delayed_work *w) {w->work.queued=false;}
static void cancel_delayed_work_sync(struct delayed_work *w) {w->work.queued=false;}
static void disable_delayed_work_sync(struct delayed_work *w) {w->disabled=true;w->work.queued=false;}
static void enable_delayed_work(struct delayed_work *w) {assert(w->disabled);w->disabled=false;}
static void cancel_work_sync(struct work_struct *w) {w->queued=false;work_cancels++;}
static void schedule_work(struct work_struct *w) {w->queued=true;}
static void queue_work(void *wq,struct work_struct *w) {w->queued=true;}
static int usb_role_switch_set_role(void *sw,enum usb_role role) {role_calls++;actual_role=role;return 0;}
static void typec_set_data_role(void *p,int role) {}
static void typec_set_pwr_role(void *p,int role) {power_updates++;}
static void typec_set_mode(void *p,int mode) {}
static void typec_partner_set_usb_mode(void *p,int mode) {}
static bool completion_done(int *c) {return *c;}
static void complete(int *c) {*c=1;}
static void ucsi_port_psy_changed(struct ucsi_connector *c) {}
static int ucsi_register_partner(struct ucsi_connector *c) {c->partner=c;return 0;}
static void ucsi_unregister_partner(struct ucsi_connector *c) {c->partner=NULL;}
static void ucsi_pwr_opmode_change(struct ucsi_connector *c) {}
static void ucsi_orientation(struct ucsi_connector *c) {}
static void ucsi_altmode_update_active(struct ucsi_connector *c) {}
static int ucsi_get_partner_identity(struct ucsi_connector *c) {return 0;}
static int ucsi_check_cable(struct ucsi_connector *c) {return 0;}
static int ucsi_register_partner_pdos(struct ucsi_connector *c) {return 0;}
static int ucsi_check_connector_capability(struct ucsi_connector *c) {return 0;}
static int ucsi_check_altmodes(struct ucsi_connector *c) {return 0;}
static int ucsi_partner_task(struct ucsi_connector *c,int (*cb)(struct ucsi_connector *),int count,unsigned delay) {return 0;}
static void ucsi_connector_change(struct ucsi *,u8);
'''


class Tests(unittest.TestCase):
    def code(self):
        core=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        names=('ucsi_retry_connection','ucsi_cancel_connection_work','ucsi_connection_work','ucsi_get_connector_status',
               'ucsi_partner_change','ucsi_check_connection','ucsi_handle_connector_change',
               'ucsi_connector_change','ucsi_suspend','ucsi_resume','ucsi_resume_work')
        constants=re.search(r'^#define UCSI_CONNECTION_RETRIES .*$',core,re.M).group()
        functions='\n'.join(common.function(core,n) for n in names)
        functions=functions.replace('void ucsi_connector_change(', 'static void ucsi_connector_change(',1)
        register=common.function(core,'ucsi_register_port')
        a=register.index('\tret = ucsi_get_connector_status(con, false);')
        b=register.index('\n\tif (con->partner &&',a)
        initial='\nstatic int initial(struct ucsi *ucsi,struct ucsi_connector *con) {\nint ret; enum usb_role u_role=USB_ROLE_NONE;\n'
        initial+=register[a:b]+'\nout: return ret;\n}\n'
        return common.COMMON+FIXTURE+constants+'\n'+functions+initial+r'''
static struct ops ops={prepare,status_callback};
static struct ucsi_connector connectors[2];
static struct ucsi u={.ops=&ops,.connector=connectors,.ntfy=UCSI_ENABLE_NTFY_CONNECTOR_CHANGE,.cap={.num_connectors=1}};
static struct ucsi_connector *c=&connectors[0];
static void setup(void) {
 c->ucsi=&u; c->num=1; c->port=c;
 firmware[FIELD_CONNECTED]=1;firmware[FIELD_PARTNER_FLAG_USB]=1;
 firmware[FIELD_PARTNER_TYPE]=UCSI_CONSTAT_PARTNER_TYPE_UFP;
 firmware[FIELD_CHANGE]=UCSI_CONSTAT_CONNECT_CHANGE;
}
static void event(void) {u.flags=1;ucsi_handle_connector_change(&c->work);}
static void tick(void) {
 assert(c->connection_work.work.queued);c->connection_work.work.queued=false;
 ucsi_connection_work(&c->connection_work.work);
 assert(c->work.queued);c->work.queued=false;ucsi_handle_connector_change(&c->work);
}
'''

    def test_initial_preparation_failure_never_reaches_role_and_defers_registration(self):
        common.execute(self.code()+r'''
int main(void) {
 setup(); c->lock=1; prepare_error=-EAGAIN;
 assert(initial(&u,c)==-EPROBE_DEFER && !role_calls && !callback_calls && !c->partner);
 prepare_error=-EIO; assert(initial(&u,c)==-EPROBE_DEFER && !role_calls);
 prepare_error=0; assert(!initial(&u,c) && role_calls==1 && actual_role==USB_ROLE_HOST);
 return 0;
}
''')

    def test_acknowledged_attach_is_replayed_after_preparation_recovers(self):
        common.execute(self.code()+r'''
int main(void) {
 setup(); prepare_error=-EIO; event();
 assert(!role_calls && !c->partner && c->connection_pending && !UCSI_CONSTAT(c,CONNECTED));
 assert(c->pending_changes==UCSI_CONSTAT_CONNECT_CHANGE && retry_queues==1);
 firmware[FIELD_CHANGE]=0; prepare_error=0; tick();
 assert(role_calls==1 && actual_role==USB_ROLE_HOST && c->partner);
 assert(!c->pending_changes && !c->connection_pending && !c->connection_work.work.queued);
 return 0;
}
''')

    def test_retry_reads_detach_instead_of_replaying_stale_host_role(self):
        common.execute(self.code()+r'''
int main(void) {
 setup(); prepare_error=-EAGAIN;event();
 memset(firmware,0,sizeof(firmware));tick();
 assert(actual_role==USB_ROLE_NONE && role_calls==1 && !c->partner);
 assert(!c->connection_pending && !c->pending_changes); return 0;
}
''')

    def test_retry_budget_is_bounded_and_transport_failure_keeps_pending_changes(self):
        common.execute(self.code()+r'''
int main(void) {
 setup();prepare_error=-ETIMEDOUT;event();
 query_error=-ETIMEDOUT;tick();assert(!role_calls && c->pending_changes);
 query_error=0;
 while(c->connection_work.work.queued)tick();
 assert(retry_queues==UCSI_CONNECTION_RETRIES && !role_calls);
 prepare_error=0;firmware[FIELD_CHANGE]=0;event();
 assert(actual_role==USB_ROLE_HOST && !c->connection_retries);return 0;
}
''')

    def test_suspend_blocks_rearming_and_resume_retries_pending_connection(self):
        common.execute(self.code()+r'''
int main(void) {
 setup();prepare_error=-EIO;event();assert(!ucsi_suspend(&u));
 assert(c->connection_work.disabled && !c->connection_work.work.queued && work_cancels==1);
 ucsi_retry_connection(c);assert(!c->connection_work.work.queued);
 assert(!ucsi_resume(&u));ucsi_resume_work(&u.resume_work);
 assert(!c->connection_work.disabled && c->connection_work.work.queued);
 prepare_error=0;firmware[FIELD_CHANGE]=0;tick();assert(actual_role==USB_ROLE_HOST);return 0;
}
''')

    def test_failed_resume_and_teardown_cannot_leave_retry_work_live(self):
        common.execute(self.code()+r'''
int main(void) {
 setup();prepare_error=-EIO;event();assert(!ucsi_suspend(&u));
 resume_error=-ETIMEDOUT;assert(!ucsi_resume(&u));ucsi_resume_work(&u.resume_work);
 assert(!c->connection_work.disabled && !c->connection_work.work.queued);
 assert(!ucsi_suspend(&u));resume_error=0;assert(!ucsi_resume(&u));
 ucsi_resume_work(&u.resume_work);assert(c->connection_work.work.queued);
 ucsi_cancel_connection_work(c);
 ucsi_retry_connection(c);
 assert(c->connection_work.disabled && !c->connection_work.work.queued && !c->work.queued);
 return 0;
}
''')


if __name__=='__main__':
    unittest.main(argv=[__file__,*rest])
