#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute UCSI recovery, retry scheduling and port ownership with failed transport."""
import argparse
import importlib.util
from pathlib import Path
import re
import sys
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path)
args, rest = parser.parse_known_args()
spec = importlib.util.spec_from_file_location('common_fixtures', Path(__file__).with_name('kernel-common-fixes.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

FIXTURE = r'''
#define HZ 100
#define EPROBE_DEFER 517
#define SERVREG_SERVICE_STATE_UP 1
#define UCSI_SET_NOTIFICATION_ENABLE 5
#define UCSI_GET_CAPABILITY 6
#define UCSI_GET_CAPABILITY_SIZE 8
#define UCSI_ENABLE_NTFY_CMD_COMPLETE 1
#define UCSI_ENABLE_NTFY_ERROR 2
#define UCSI_ENABLE_NTFY_CONNECTOR_CHANGE 4
#define UCSI_CCI 4
#define UCSI_CCI_CONNECTOR(x) (((x) >> 1) & 127)
#define UCSI_ROLE_SWITCH_WAIT_COUNT 100
#define UCSI_ROLE_SWITCH_INTERVAL (HZ / 10)
#define EVENT_PENDING 0
#define GFP_KERNEL 0
#define BITS_TO_BYTES(x) (((x) + 7) / 8)
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31 - (h))))
#define WRITE_ONCE(p,v) ((p)=(v))
#define smp_load_acquire(p) (*(p))
#define smp_store_release(p,v) (*(p)=(v))
#define system_dfl_long_wq NULL
#define system_wq NULL
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_delayed_work(w) container_of(w,struct delayed_work,work)
typedef uint8_t u8;
struct work_struct { bool disabled; };
struct delayed_work { struct work_struct work; bool pending; int delay; };
struct ucsi;
struct ucsi_connector { struct work_struct work; int lock_key, num; void *port; struct ucsi *ucsi; };
struct ops {
 int (*read_version)(struct ucsi *, unsigned short *);
 int (*read_cci)(struct ucsi *, u32 *);
 int (*async_control)(struct ucsi *, u64);
};
struct ucsi {
 struct delayed_work work; struct work_struct resume_work;
 const struct ops *ops; void *dev; unsigned short version;
 struct { int num_connectors; } cap;
 struct ucsi_connector *connector;
 unsigned int work_count; unsigned long flags; u64 ntfy; int ppm_lock;
};
struct device { void *data; };
struct auxiliary_device { struct device dev; };
struct pmic_glink_ucsi {
 struct delayed_work register_work; struct work_struct notify_work; int state_lock;
 bool pd_running, ucsi_registered, stopping; struct ucsi *ucsi; void *dev;
 u32 service_generation, registered_generation; unsigned int register_attempts;
};
static int spin_depth, read_error, read_calls, queues, registrations, unregistrations;
static int releases, ports, keys, cancels, controls, allocations, port_regs, scheduled;
static int read_cci_error, command_error, errors, resets;
static u32 observed_cci, notification_cci;
static unsigned short version=0x100;
static bool array_live;
static void (*read_hook)(void);
static struct ucsi_connector connectors[3];
#define spin_lock_irqsave(l,f) do { assert(!spin_depth); spin_depth++; (f)=0; } while(0)
#define spin_unlock_irqrestore(l,f) do { assert(spin_depth==1); spin_depth--; (void)(f); } while(0)
#define dev_dbg(...) ((void)0)
#define dev_err_probe(...) ((void)0)
#define dev_err(...) do { errors++; } while(0)
#define dev_warn(...) ((void)0)
#define dev_warn_ratelimited(...) ((void)0)
static void mutex_lock(int *p) { assert(!spin_depth && !*p); *p=1; }
static void mutex_unlock(int *p) { assert(*p==1); *p=0; }
static void *dev_get_drvdata(struct device *d) { return d->data; }
static void clear_bit(int bit, unsigned long *value) { *value &= ~(1UL<<bit); }
static bool test_and_set_bit(int bit, unsigned long *value) {
 bool old=!!(*value & (1UL<<bit)); *value |= 1UL<<bit; return old;
}
static int read_version(struct ucsi *u, unsigned short *out) {
 assert(!spin_depth); read_calls++;
 if (read_hook) { void (*hook)(void)=read_hook; read_hook=NULL; hook(); }
 if (read_error) return read_error;
 *out=version; return 0;
}
static int read_cci(struct ucsi *u, u32 *out) {
 assert(!u->connector && u->ppm_lock); *out=2; return read_cci_error;
}
static int control(struct ucsi *u, u64 cmd) {
 assert(!spin_depth && cmd==UCSI_SET_NOTIFICATION_ENABLE); controls++; return 0;
}
static void queue_delayed_work(void *q, struct delayed_work *w, int delay) {
 assert(!spin_depth); w->pending=true; w->delay=delay; queues++;
}
static void mod_delayed_work(void *q, struct delayed_work *w, int delay) {
 assert(spin_depth==1); if (w->work.disabled) return; w->pending=true; w->delay=delay;
}
static void cancel_delayed_work_sync(struct delayed_work *w) {
 assert(!spin_depth); w->pending=false; cancels++;
}
static void cancel_work_sync(struct work_struct *w) { assert(!spin_depth); cancels++; }
static void disable_delayed_work_sync(struct delayed_work *w) {
 assert(!spin_depth); w->work.disabled=true; w->pending=false;
}
static void disable_work_sync(struct work_struct *w) { assert(!spin_depth); w->disabled=true; }
static void schedule_work(struct work_struct *w) { assert(array_live); scheduled++; }
static void ucsi_debugfs_register(struct ucsi *u) { registrations++; }
static void ucsi_debugfs_unregister(struct ucsi *u) { unregistrations++; }
static void ucsi_unregister_port(struct ucsi_connector *c) {
 assert(array_live && c>=connectors && c<connectors+2); ports++; c->port=NULL;
}
static void lockdep_register_key(int *key) { assert(array_live); }
static void lockdep_unregister_key(int *key) { assert(array_live); keys++; }
static void kfree(void *p) {
 assert(p==connectors && array_live); array_live=false; releases++;
}
static void *allocate(size_t n) {
 assert(!array_live && n==3); memset(connectors,0,sizeof(connectors)); array_live=true; allocations++; return connectors;
}
#define kzalloc_objs(obj,n) allocate(n)
static int ucsi_register_port(struct ucsi *u, struct ucsi_connector *c) {
 c->ucsi=u; c->port=(void *)1; port_regs++; return 0;
}
static int ucsi_reset_ppm(struct ucsi *u) { resets++; return 0; }
static u64 ucsi_get_supported_notifications(struct ucsi *u) { return 7; }
static int ucsi_send_command(struct ucsi *u, u64 cmd, void *data, size_t len) {
 if (command_error) return command_error;
 if (cmd==UCSI_GET_CAPABILITY) u->cap.num_connectors=2;
 return 0;
}
static void ucsi_connector_change(struct ucsi *, u8);
static void ucsi_notify_common(struct ucsi *u, u32 cci) {
 assert(spin_depth==1); observed_cci=cci;
 if (UCSI_CCI_CONNECTOR(cci)) ucsi_connector_change(u,UCSI_CCI_CONNECTOR(cci));
}
static int pmic_glink_ucsi_read(struct ucsi *u, unsigned int off, void *out, size_t len) {
 assert(off==UCSI_CCI && len==4 && !spin_depth); *(u32 *)out=notification_cci; return 0;
}
'''


@unittest.skipUnless(args.source, '--source enables compiled UCSI lifecycle checks')
class Tests(unittest.TestCase):
    def code(self):
        core = (args.source / 'drivers/usb/typec/ucsi/ucsi.c').read_text()
        glink = (args.source / 'drivers/usb/typec/ucsi/ucsi_glink.c').read_text()
        constants = '\n'.join(re.findall(r'^#define PMIC_GLINK_UCSI_REGISTER_.*$', glink, re.M))
        core_funcs = ['ucsi_connector_change', 'ucsi_init', 'ucsi_init_work', 'ucsi_register', 'ucsi_unregister']
        glink_funcs = ['pmic_glink_ucsi_notify', 'pmic_glink_ucsi_unregister',
                       'pmic_glink_ucsi_register_retry', 'pmic_glink_ucsi_register',
                       'pmic_glink_ucsi_pdr_notify', 'pmic_glink_ucsi_remove']
        # The exported connector helper needs no external linkage in the fixture.
        functions = '\n'.join(common.function(core, f) for f in core_funcs)
        functions = functions.replace('void ucsi_connector_change(', 'static void ucsi_connector_change(', 1)
        return common.COMMON + FIXTURE + constants + '\n' + functions + '\n' + '\n'.join(common.function(glink, f) for f in glink_funcs) + r'''
static const struct ops ops={read_version,read_cci,control};
static void tick(struct pmic_glink_ucsi *g) {
 assert(g->register_work.pending && !g->register_work.work.disabled);
 g->register_work.pending=false; pmic_glink_ucsi_register(&g->register_work.work);
}
static void initialize(struct ucsi *u) {
 assert(u->work.pending); u->work.pending=false; ucsi_init_work(&u->work.work);
}
'''

    def test_version_errors_and_zero_version_retry_without_another_service_event(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u};
 pmic_glink_ucsi_pdr_notify(&g,1);
 read_error=-ETIMEDOUT; tick(&g);
 assert(!g.ucsi_registered && !queues && g.register_work.pending && g.register_work.delay==HZ);
 read_error=-EIO; tick(&g); read_error=0; version=0; tick(&g);
 assert(!g.ucsi_registered && !unregistrations && !controls);
 version=0x100; tick(&g);
 assert(g.ucsi_registered && read_calls==4 && registrations==1 && queues==1);
 pmic_glink_ucsi_pdr_notify(&g,1); assert(!g.register_work.pending);
 return 0;
}
''')

    def test_budget_exhaustion_duplicate_up_and_new_service_budget(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u}; read_error=-ECONNRESET;
 pmic_glink_ucsi_pdr_notify(&g,1);
 for (int i=0;i<PMIC_GLINK_UCSI_REGISTER_ATTEMPTS;i++) tick(&g);
 assert(read_calls==PMIC_GLINK_UCSI_REGISTER_ATTEMPTS && !g.register_work.pending && errors==1);
 pmic_glink_ucsi_pdr_notify(&g,1); assert(!g.register_work.pending);
 pmic_glink_ucsi_pdr_notify(&g,0); pmic_glink_ucsi_pdr_notify(&g,1); read_error=0; tick(&g);
 assert(g.ucsi_registered && g.register_attempts==1);
 return 0;
}
''')

    def test_down_replaces_delayed_retry_and_permanent_failure_stops(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u}; read_error=-EAGAIN;
 pmic_glink_ucsi_pdr_notify(&g,1); tick(&g);
 pmic_glink_ucsi_pdr_notify(&g,0); assert(g.register_work.delay==0); tick(&g);
 assert(read_calls==1 && !g.register_work.pending && !unregistrations);
 pmic_glink_ucsi_pdr_notify(&g,1); read_error=-EINVAL; tick(&g);
 assert(!g.register_work.pending && read_calls==2);
 return 0;
}
''')

    def test_coalesced_down_up_releases_old_ports_and_registers_new_generation(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u};
 pmic_glink_ucsi_pdr_notify(&g,1); tick(&g); initialize(&u);
 assert(u.connector && ports==0 && allocations==1);
 pmic_glink_ucsi_pdr_notify(&g,0); pmic_glink_ucsi_pdr_notify(&g,1); tick(&g);
 assert(!u.connector && releases==1 && ports==2 && keys==2 && registrations==2);
 assert(g.registered_generation==g.service_generation && u.work_count==0);
 initialize(&u); assert(u.connector && allocations==2);
 pmic_glink_ucsi_pdr_notify(&g,0); tick(&g);
 assert(!u.connector && releases==2 && ports==4 && !g.ucsi_registered);
 return 0;
}
''')

    def test_service_change_during_version_read_discards_old_success_or_failure(self):
        common.execute(self.code() + r'''
static struct pmic_glink_ucsi *active;
static void bounce(void) { pmic_glink_ucsi_pdr_notify(active,0); pmic_glink_ucsi_pdr_notify(active,1); }
int main(void) {
 for (int fail=0;fail<2;fail++) {
  struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u}; active=&g;
  pmic_glink_ucsi_pdr_notify(&g,1); read_hook=bounce; read_error=fail ? -ETIMEDOUT : 0; tick(&g);
  assert(!g.ucsi_registered && !u.work.pending && g.register_work.pending);
  assert(g.register_work.delay==0 && g.register_attempts==0);
  read_error=0; tick(&g); assert(g.ucsi_registered && g.register_attempts==1);
  pmic_glink_ucsi_pdr_notify(&g,0); tick(&g);
 }
 return 0;
}
''')

    def test_late_init_timeout_cleans_ports_before_retry_and_budget_is_fresh(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops,.work_count=999,.ntfy=7,.flags=1};
 assert(!ucsi_register(&u)); assert(!u.work_count && !u.ntfy && !u.flags);
 read_cci_error=-ETIMEDOUT; initialize(&u);
 assert(!u.connector && !array_live && releases==1 && ports==2 && u.work_count==1);
 assert(u.work.pending && u.work.delay==UCSI_ROLE_SWITCH_INTERVAL);
 read_cci_error=0; initialize(&u);
 assert(u.connector && allocations==2 && port_regs==4 && scheduled==1);
 ucsi_unregister(&u); assert(!u.connector && !u.ntfy && releases==2);
 /* A later early initialization failure never refers to the freed array. */
 assert(!ucsi_register(&u)); command_error=-EINVAL; initialize(&u); ucsi_unregister(&u);
 assert(!u.connector && releases==2 && ports==4);
 return 0;
}
''')

    def test_stale_notifications_keep_command_bits_but_never_dispatch_old_ports(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u};
 notification_cci=2 | (1U<<31);
 pmic_glink_ucsi_notify(&g.notify_work); assert(observed_cci==(1U<<31) && !scheduled);
 pmic_glink_ucsi_pdr_notify(&g,1); tick(&g);
 pmic_glink_ucsi_notify(&g.notify_work); assert(observed_cci==notification_cci && !scheduled);
 initialize(&u); assert(scheduled==1);
 pmic_glink_ucsi_pdr_notify(&g,0);
 pmic_glink_ucsi_notify(&g.notify_work); assert(observed_cci==(1U<<31) && scheduled==1);
 tick(&g); assert(!u.connector);
 pmic_glink_ucsi_notify(&g.notify_work); assert(scheduled==1);
 return 0;
}
''')

    def test_remove_disables_retries_and_ignores_late_service_callbacks(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u={.ops=&ops}; struct pmic_glink_ucsi g={.ucsi=&u};
 struct auxiliary_device dev={.dev.data=&g};
 pmic_glink_ucsi_pdr_notify(&g,1); tick(&g); initialize(&u);
 pmic_glink_ucsi_remove(&dev); assert(!g.ucsi_registered && !u.connector && releases==1);
 pmic_glink_ucsi_pdr_notify(&g,0); pmic_glink_ucsi_pdr_notify(&g,1);
 assert(g.stopping && g.register_work.work.disabled && g.notify_work.disabled && !g.register_work.pending);
 pmic_glink_ucsi_register(&g.register_work.work); assert(read_calls==1);
 return 0;
}
''')


@unittest.skipUnless(args.source, '--source enables compiled UCSI response checks')
class ResponseLengthTests(unittest.TestCase):
    def code(self):
        root = args.source / 'drivers/usb/typec/ucsi'
        glink = (root / 'ucsi_glink.c').read_text()
        header = (root / 'ucsi.h').read_text()
        definitions = re.findall(
            r'^#define (?:UCSI_MESSAGE_IN|UCSI_MESSAGE_OUT|UCSIv2_MESSAGE_OUT|UCSI_VERSION_2_0)\b.*$',
            header, re.M)
        definitions += re.findall(r'^#define UCSI_BUF_V[12]_SIZE\b.*$', glink, re.M)
        return common.COMMON + '\n'.join(definitions) + r'''

typedef uint8_t u8;
typedef uint16_t u16;
struct pmic_glink_hdr { u32 owner, type, opcode; };
struct ucsi_write_buf_resp_msg { struct pmic_glink_hdr hdr; u32 ret_code; };
struct completion { unsigned int count; };
struct ucsi { u16 version; };
struct pmic_glink_ucsi {
 struct ucsi *ucsi;
 bool ucsi_registered;
 struct completion read_ack, write_ack;
 u8 read_buf[UCSI_BUF_V2_SIZE];
};
#define READ_ONCE(value) (value)
#define dev_err(...) ((void)0)
#define dev_warn_ratelimited(...) ((void)0)
static void complete(struct completion *ack) { ack->count++; }
''' + '\n'.join(common.function(glink, name) for name in (
            'pmic_glink_ucsi_read_ack', 'pmic_glink_ucsi_write_ack'))

    def test_read_requires_a_complete_layout_and_copies_only_its_payload(self):
        common.execute(self.code() + r'''
int main(void) {
 const struct { u16 version; bool registered; size_t first, second; } formats[] = {
  {0, false, 64, 544}, {0, true, 0, 0}, {0x0100, true, 64, 0}, {0x0200, true, 544, 0},
 };
 const size_t lengths[] = {12, 16, 63, 64, 65, 543, 544, 545};
 for (size_t i = 0; i < sizeof(formats) / sizeof(formats[0]); i++) {
  for (size_t j = 0; j < sizeof(lengths) / sizeof(lengths[0]); j++) {
   size_t len = lengths[j];
   bool accepted = len == formats[i].first || len == formats[i].second;
   size_t payload = accepted ? len - 16 : 0;
   struct ucsi u = {.version = formats[i].version};
   struct pmic_glink_ucsi g = {.ucsi = &u, .ucsi_registered = formats[i].registered};
   u8 response[545] = {0};
   memset(response + 12, 0x5a, payload);
   memset(g.read_buf, 0xa5, sizeof(g.read_buf));
   pmic_glink_ucsi_read_ack(&g, response, len);
   assert(g.read_ack.count == accepted);
   for (size_t n = 0; n < sizeof(g.read_buf); n++)
    assert(g.read_buf[n] == (n < payload ? 0x5a : 0xa5));
  }
 }
 return 0;
}
''')

    def test_write_requires_exact_length_and_complete_errors_are_not_acked(self):
        common.execute(self.code() + r'''
int main(void) {
 struct ucsi u = {.version = 0x0100};
 struct pmic_glink_ucsi g = {.ucsi = &u, .ucsi_registered = true};
 struct { struct ucsi_write_buf_resp_msg ack; u8 extra; } response = {0};
 u8 read_v1[64] = {0}, read_v2[544] = {0};
 u32 error = 1;

 pmic_glink_ucsi_write_ack(&g, &response.ack, 12);
 pmic_glink_ucsi_write_ack(&g, &response.ack, 15);
 pmic_glink_ucsi_write_ack(&g, &response.ack, 17);
 response.ack.ret_code = error;
 pmic_glink_ucsi_write_ack(&g, &response.ack, 16);
 assert(!g.write_ack.count);
 response.ack.ret_code = 0;
 pmic_glink_ucsi_write_ack(&g, &response.ack, 16);
 assert(g.write_ack.count == 1);

 memcpy(read_v1 + sizeof(read_v1) - sizeof(error), &error, sizeof(error));
 memcpy(read_v2 + sizeof(read_v2) - sizeof(error), &error, sizeof(error));
 pmic_glink_ucsi_read_ack(&g, read_v1, sizeof(read_v1));
 u.version = 0x0200;
 pmic_glink_ucsi_read_ack(&g, read_v2, sizeof(read_v2));
 assert(!g.read_ack.count);
 return 0;
}
''')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
