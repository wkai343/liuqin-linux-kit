#!/usr/bin/env python3
"""Execute GLINK endpoint ownership, unbind, remote close and edge teardown."""
import argparse
import importlib.util
from pathlib import Path
import re
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
args, rest = parser.parse_known_args()
spec = importlib.util.spec_from_file_location('common', Path(__file__).resolve().parents[1] / 'kernel/kernel-common-fixes.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
typedef uint32_t __be32;
#define SZ_1K 1024
#define HZ 100
#define RPMSG_ADDR_ANY (~0U)
#define GLINK_CMD_CLOSE 3
#define NATIVE_DSR_SIG (1U<<31)
#define NATIVE_CTS_SIG (1U<<30)
#define READ_ONCE(x) (x)
#define cpu_to_le16(x) (x)
#define cpu_to_le32(x) (x)
#define cpu_to_be32(x) (x)
#define be32_to_cpup(x) (*(x))
#define ALIGN(x,y) (((x)+(y)-1)&~((y)-1))
#define IS_ERR(p) (!(p))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_glink_channel(p) container_of(p,struct glink_channel,ept)
#define WARN(x,...) (x)
#define dev_warn(...) ((void)0)
#define dev_err(...) ((void)0)
#define trace_qcom_glink_cmd_close_tx(...) ((void)0)
#define trace_qcom_glink_cmd_close_rx(...) ((void)0)
#define trace_qcom_glink_cmd_close_ack_rx(...) ((void)0)
#define trace_qcom_glink_cmd_signal_rx(...) ((void)0)
struct device_node {int unused;};
struct rpmsg_device;
struct glink_channel;
struct device {struct device_node *of_node; struct glink_channel *channel;};
typedef int (*rpmsg_rx_cb_t)(struct rpmsg_device *,void *,int,void *,u32);
struct rpmsg_endpoint {
 struct rpmsg_device *rpdev; rpmsg_rx_cb_t cb;
 void (*flow_cb)(struct rpmsg_device *,void *,bool); void *priv; const void *ops;
};
struct rpmsg_device {struct rpmsg_endpoint *ept; struct {char name[32];} id; struct device dev; bool bound;};
struct rpmsg_channel_info {char name[32]; u32 src,dst;};
struct idr {struct glink_channel *slots[16];};
struct qcom_glink {
 const char *label; bool abort_tx,intentless; int idr_lock,tx_lock,tx_avail_notify;
 struct idr lcids,rcids; struct device *dev;
};
struct kref {int count;};
struct glink_channel {
 struct rpmsg_endpoint ept; struct rpmsg_device *rpdev; struct qcom_glink *glink;
 const char *name; unsigned int lcid,rcid; int recv_lock,open_ack,intent_work;
 struct kref refcount; bool keepalive,ept_attached,close_sent,remote_close,intents_announced;
};
struct glink_msg {uint16_t cmd,param1;uint32_t param2;};
struct property {__be32 *value;int length;};
struct glink_core_rx_intent {int unused;};
static bool board=true,fail_open,fail_send_open,close_during_open;
static int opens,closes,acks,releases,allocations,advertisements,callbacks,flow_callbacks,cancels;
static int glink_endpoint_ops;
static struct glink_core_rx_intent intent;
#define spin_lock_irqsave(p,f) do {assert(!*(p));*(p)=1;(f)=0;} while(0)
#define spin_unlock_irqrestore(p,f) do {assert(*(p));*(p)=0;(void)(f);} while(0)
#define idr_for_each_entry(p,c,i) for((i)=0,(c)=NULL;(i)<16?((c)=(p)->slots[i],true):((c)=NULL,false);(i)++) if(c)
static void *idr_find(struct idr *p,int id) {assert(id>=0 && id<16);return p->slots[id];}
static void idr_remove(struct idr *p,int id) {assert(id>=0 && id<16);p->slots[id]=NULL;}
static void idr_destroy(struct idr *p) {memset(p,0,sizeof(*p));}
static bool liuqin_board_quirks(void) {return board;}
static bool completion_done(int *p) {return *p;}
static void kref_get(struct kref *p) {assert(p->count>0);p->count++;}
static void qcom_glink_channel_release(struct kref *p) {assert(!p->count);releases++;}
static void kref_put(struct kref *p,void (*release)(struct kref *)) {assert(p->count>0);if(!--p->count)release(p);}
static int qcom_glink_tx(struct qcom_glink *g,const struct glink_msg *r,int n,void *d,int len,bool wait) {
 assert(r->cmd==GLINK_CMD_CLOSE && r->param1);closes++;return g->abort_tx?-ECANCELED:0;
}
static void qcom_glink_rx_advance(struct qcom_glink *g,int n) {(void)g;}
static void wake_up_all(int *p) {(void)p;}
static void cancel_work_sync(int *p) {cancels++;}
static void qcom_glink_cancel_rx_work(struct qcom_glink *g) {cancels++;}
static void qcom_glink_intent_req_abort(struct glink_channel *c) {(void)c;}
static const struct property *of_find_property(struct device_node *n,const char *key,void *len) {return NULL;}
static struct glink_core_rx_intent *qcom_glink_alloc_intent(struct qcom_glink *g,struct glink_channel *c,int size,bool reuse) {assert(size==1024 && reuse);allocations++;return &intent;}
static void qcom_glink_advertise_intent(struct qcom_glink *g,struct glink_channel *c,struct glink_core_rx_intent *i) {advertisements++;}
static void qcom_glink_send_close_ack(struct qcom_glink *g,struct glink_channel *c) {assert(c->rcid);acks++;}
static void strscpy_pad(char *to,const char *from,size_t n) {snprintf(to,n,"%s",from);}
static void qcom_glink_send_close_req(struct qcom_glink *,struct glink_channel *);
static void qcom_glink_destroy_ept(struct rpmsg_endpoint *);
static int qcom_glink_announce_destroy(struct rpmsg_device *);
static void qcom_glink_send_open_ack(struct qcom_glink *g,struct glink_channel *c) {(void)c;}
static int qcom_glink_send_open_req(struct qcom_glink *g,struct glink_channel *c) {
 opens++;assert(!c->lcid);kref_get(&c->refcount);
 if(fail_send_open)return -EIO;
 c->lcid=5;g->lcids.slots[5]=c;
 c->open_ack=!fail_open;if(close_during_open)c->remote_close=true;return 0;
}
static int wait_for_completion_timeout(int *p,int timeout) {return *p;}
static struct glink_channel *qcom_glink_create_local(struct qcom_glink *g,const char *n) {assert(!"unexpected local create");return NULL;}
static void unregister_client(struct glink_channel *c) {
 if(c->rpdev && c->rpdev->bound) {
  qcom_glink_announce_destroy(c->rpdev);
  if(c->keepalive)assert(!c->ept.cb && !c->ept.flow_cb && !c->ept.priv);
  c->rpdev->bound=false;qcom_glink_destroy_ept(&c->ept);
 }
}
static int rpmsg_unregister_device(struct device *d,struct rpmsg_channel_info *i) {
 assert(!strcmp(d->channel->name,i->name));unregister_client(d->channel);return 0;
}
static void device_unregister(struct device *d) {unregister_client(d->channel);}
static int device_for_each_child(struct device *d,void *data,int (*fn)(struct device *,void *)) {return fn(d,data);}
static int receive(struct rpmsg_device *p,void *data,int len,void *priv,u32 src) {assert(p && priv==(void *)123);callbacks++;return 0;}
static void flow(struct rpmsg_device *p,void *priv,bool enable) {assert(p && priv==(void *)123);flow_callbacks++;}
'''


def function(source, name):
    match = re.search(r'^(?:static )?[^\n]*\b' + name + r'\([^;{]*\)\s*\{', source, re.M)
    if not match:
        raise ValueError(name)
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


class Tests(unittest.TestCase):
    def code(self):
        source = (args.source / 'drivers/rpmsg/qcom_glink_native.c').read_text()
        names = ['qcom_glink_keepalive', 'qcom_glink_send_close_req', 'qcom_glink_create_remote', 'qcom_glink_create_ept',
                 'qcom_glink_announce_create', 'qcom_glink_announce_destroy',
                 'qcom_glink_remove_rpmsg_device', 'qcom_glink_destroy_ept',
                 'qcom_glink_handle_signals', 'qcom_glink_rx_close', 'qcom_glink_rx_close_ack',
                 'qcom_glink_remove_device', 'qcom_glink_native_remove']
        return '#include <stdio.h>\n' + FIXTURE + '\n'.join(function(source, n) for n in names) + r'''
static struct device dev;
static struct qcom_glink g={.label="lpass",.dev=&dev};
static struct glink_channel c;
static struct rpmsg_device p;
static struct rpmsg_channel_info info={.name="PMIC_LOGS_ADSP_APPS"};
static void init(void) {
 c=(struct glink_channel){.glink=&g,.rpdev=&p,.name=info.name,.rcid=7,.refcount={1},.keepalive=true};
 p=(struct rpmsg_device){.ept=&c.ept};strcpy(p.id.name,info.name);dev.channel=&c;
 g.rcids.slots[7]=&c;
}
static void bind_client(void) {
 assert(qcom_glink_create_ept(&p,receive,(void *)123,info)==&c.ept);
 p.bound=true;c.ept.flow_cb=flow;assert(!qcom_glink_announce_create(&p));
}
'''

    def check(self, body):
        common.execute(self.code() + '\nint main(void) {init();\n' + body + '\nreturn 0;}\n')

    def test_scoped_board_edge_and_channel(self):
        self.check(r'''
assert(qcom_glink_keepalive(&g,info.name));
board=false;assert(!qcom_glink_keepalive(&g,info.name));board=true;
g.label="cdsp";assert(!qcom_glink_keepalive(&g,info.name));g.label="lpass";
assert(!qcom_glink_keepalive(&g,"PMIC_RTR_ADSP_APPS"));
assert(!qcom_glink_keepalive(&g,"PMIC_LOGS_ADSP_APPS_extra"));
''')

    def test_rebind_reuses_transport_and_intents_and_drains_callbacks(self):
        self.check(r'''
for(int i=0;i<100;i++) {
 bind_client();assert(opens==1 && !closes && c.refcount.count==2);
 assert(allocations==5 && advertisements==5);
 qcom_glink_handle_signals(&g,7,NATIVE_DSR_SIG);assert(flow_callbacks==i+1);
 unregister_client(&c);
 assert(!c.ept_attached && !c.ept.cb && !c.ept.priv && !c.ept.flow_cb && !c.ept.rpdev);
 assert(c.lcid==5 && c.rcid==7 && !closes && !releases && c.refcount.count==2);
 qcom_glink_handle_signals(&g,7,NATIVE_DSR_SIG);assert(flow_callbacks==i+1);
}
''')

    def test_refuse_duplicate_ctrl_and_removed_channels(self):
        self.check(r'''
bind_client();assert(!qcom_glink_create_ept(&p,receive,NULL,info));
assert(c.ept.priv==(void *)123 && c.refcount.count==2 && opens==1);
strcpy(p.id.name,"rpmsg_ctrl");assert(!qcom_glink_create_ept(&p,receive,NULL,info));
assert(c.refcount.count==2 && opens==1 && !closes);strcpy(p.id.name,info.name);
unregister_client(&c);g.rcids.slots[7]=NULL;
assert(!qcom_glink_create_ept(&p,receive,NULL,info));assert(opens==1 && !closes);
''')

    def test_racing_open_cannot_reenable_flow_callback_during_remove(self):
        self.check(r'''
bind_client();qcom_glink_announce_destroy(&p);
assert(!c.ept_attached && !c.ept.cb && !c.ept.priv);
/* rpmsg_eptdev_open may install flow_cb while remove waits for ept_lock. */
c.ept.flow_cb=flow;qcom_glink_handle_signals(&g,7,NATIVE_DSR_SIG);
assert(!flow_callbacks);
qcom_glink_destroy_ept(&c.ept);assert(!c.ept.flow_cb && !closes);
''')

    def test_remote_close_releases_bound_and_unbound_transports(self):
        for bound in (True, False):
            self.check('bind_client();\n' + ('' if bound else 'unregister_client(&c);\n') + r'''
qcom_glink_rx_close(&g,7);
assert(closes==1 && acks==1 && c.remote_close && !c.ept_attached);
assert(!c.rcid && !g.rcids.slots[7] && c.refcount.count==1 && !releases);
assert(!qcom_glink_create_ept(&p,receive,NULL,info));
qcom_glink_rx_close_ack(&g,5);
assert(!c.lcid && !g.lcids.slots[5] && releases==1 && !c.refcount.count && closes==1);
''')

    def test_failed_or_interrupted_handshake_does_not_publish_callbacks(self):
        for trigger in ('fail_open', 'close_during_open'):
            self.check(trigger + r'''=true;
assert(!qcom_glink_create_ept(&p,receive,(void *)123,info));
assert(!c.ept_attached && !c.ept.cb && c.refcount.count==2);
assert(!qcom_glink_create_ept(&p,receive,NULL,info));assert(opens==1);
qcom_glink_rx_close(&g,7);qcom_glink_rx_close_ack(&g,5);
assert(releases==1 && closes==1);
''')
        self.check(r'''
g.abort_tx=true;assert(!qcom_glink_create_ept(&p,receive,NULL,info));
assert(!opens && !closes && !c.ept_attached && c.refcount.count==1);
qcom_glink_rx_close(&g,7);assert(releases==1 && !closes && acks==1);
''')

    def test_edge_removal_releases_retained_references(self):
        for bound in (True, False):
            self.check('bind_client();\n' + ('' if bound else 'unregister_client(&c);\n') + r'''
qcom_glink_native_remove(&g);
assert(g.abort_tx && releases==1 && !c.refcount.count);
assert(!c.ept.cb && !c.ept.flow_cb && !c.ept.priv && !c.ept.rpdev);
assert(!g.lcids.slots[5] && !g.rcids.slots[7]);
''')

    def test_open_allocation_or_transmit_failure_balances_reference(self):
        self.check(r'''
fail_send_open=true;
assert(!qcom_glink_create_ept(&p,receive,NULL,info));
assert(c.refcount.count==1 && !c.lcid && !closes && !c.ept_attached);
fail_send_open=false;bind_client();assert(c.refcount.count==2 && opens==2);
unregister_client(&c);qcom_glink_native_remove(&g);assert(releases==1);
''')

    def test_unrelated_channel_keeps_ordinary_close(self):
        self.check(r'''
board=false;c.keepalive=false;bind_client();
unregister_client(&c);assert(closes==1 && !c.ept.cb);
qcom_glink_rx_close_ack(&g,5);assert(c.refcount.count==1);
qcom_glink_rx_close(&g,7);assert(releases==1 && closes==1 && acks==1);
''')


if __name__ == '__main__':
    unittest.main(argv=['charge-logs-kernel.py', *rest], verbosity=2)
