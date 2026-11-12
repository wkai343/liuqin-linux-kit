#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile the actual notification worker against a deterministic GLINK scheduler."""
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
#define SERVREG_SERVICE_STATE_UP 1
#define QCOM_BATTMGR_SM8475 1
#define PMIC_GLINK_OWNER_BATTMGR 32778
#define PMIC_GLINK_NOTIFY 2
#define BATTMGR_REQUEST_NOTIFICATION 4
#define cpu_to_le32(x) (x)
#define WRITE_ONCE(p, v) ((p) = (v))
#define system_wq NULL
struct work_struct { bool disabled; };
struct delayed_work { struct work_struct work; bool pending; int delay; };
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define to_delayed_work(w) container_of(w, struct delayed_work, work)
struct pmic_glink_hdr { u32 owner, type, opcode; };
struct qcom_battmgr_enable_request { struct pmic_glink_hdr hdr; u32 unused[4]; };
struct qcom_battmgr {
 int lock, state_lock, variant; void *dev, *bat_psy, *usb_psy, *ac_psy, *wls_psy;
 bool service_up, stopping, notifications_enabled; u32 service_generation;
 unsigned int enable_attempts; struct delayed_work enable_work, refresh_work;
 struct work_struct xm_notify_work;
};
static int spin_depth, mutex_depth, requests, publications, errors, result;
static int queued, disabled;
static void (*request_hook)(struct qcom_battmgr *);
static void (*mutex_hook)(void);
#define spin_lock_irqsave(l, f) do { assert(!spin_depth); spin_depth++; (f) = 0; } while (0)
#define spin_unlock_irqrestore(l, f) do { assert(spin_depth == 1); spin_depth--; (void)(f); } while (0)
#define dev_err(...) do { assert(!spin_depth); errors++; } while (0)
#define dev_dbg(...) ((void)0)
static void mutex_lock(int *l) {
 assert(!spin_depth && !mutex_depth); mutex_depth++;
 if (mutex_hook) { void (*hook)(void) = mutex_hook; mutex_hook = NULL; hook(); }
}
static void mutex_unlock(int *l) { assert(mutex_depth == 1 && !spin_depth); mutex_depth--; }
static bool mod_delayed_work(void *q, struct delayed_work *w, int delay) {
 assert(spin_depth == 1); if (w->work.disabled) return false;
 w->pending = true; w->delay = delay; queued++; return true;
}
static void cancel_delayed_work(struct delayed_work *w) { w->pending = false; }
static void disable_delayed_work_sync(struct delayed_work *w) {
 assert(!spin_depth && !mutex_depth); w->pending = false; w->work.disabled = true; disabled++;
}
static void disable_work_sync(struct work_struct *w) {
 assert(!spin_depth && !mutex_depth); w->disabled = true; disabled++;
}
static int qcom_battmgr_request(struct qcom_battmgr *b, void *data, size_t size) {
 struct qcom_battmgr_enable_request *r = data;
 assert(mutex_depth == 1 && !spin_depth && size == sizeof(*r));
 assert(r->hdr.owner == 32778 && r->hdr.type == 2 && r->hdr.opcode == 4);
 requests++; if (request_hook) request_hook(b); return result;
}
static void power_supply_changed(void *p) { assert(p && !spin_depth && !mutex_depth); publications++; }
'''


@unittest.skipUnless(args.source, '--source enables compiled battery recovery checks')
class Tests(unittest.TestCase):
    def code(self):
        source = (args.source / 'drivers/power/supply/qcom_battmgr.c').read_text()
        constants = '\n'.join(re.findall(r'^#define BATTMGR_ENABLE_.*$', source, re.M))
        functions = ['qcom_battmgr_enable_retry', 'qcom_battmgr_enable_worker',
                     'qcom_battmgr_pdr_notify', 'qcom_battmgr_stop']
        return common.COMMON + FIXTURE + constants + '\n' + '\n'.join(common.function(source, f) for f in functions) + r'''
static void tick(struct qcom_battmgr *b) {
 assert(b->enable_work.pending && !b->enable_work.work.disabled);
 b->enable_work.pending = false;
 qcom_battmgr_enable_worker(&b->enable_work.work);
}
'''

    def test_ack_publishes_once_and_timeout_recovers_without_another_up(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b = {.bat_psy=(void *)1, .usb_psy=(void *)2, .wls_psy=(void *)3};
 qcom_battmgr_pdr_notify(&b, 1); result=-ETIMEDOUT; tick(&b);
 assert(requests == 1 && !publications && !b.notifications_enabled && b.enable_work.delay == HZ);
 result=0; tick(&b);
 assert(requests == 2 && publications == 3 && b.notifications_enabled && !b.enable_work.pending);
 qcom_battmgr_pdr_notify(&b, 1);
 qcom_battmgr_enable_worker(&b.enable_work.work);
 assert(requests == 2 && publications == 3 && !errors);
 return 0;
}
''')

    def test_retry_budget_is_bounded_and_only_a_new_service_resets_it(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b = {0}; result=-ECONNRESET;
 qcom_battmgr_pdr_notify(&b, 1);
 for (int i=0; i<BATTMGR_ENABLE_ATTEMPTS; i++) tick(&b);
 assert(requests == BATTMGR_ENABLE_ATTEMPTS && !b.enable_work.pending && errors == 1);
 qcom_battmgr_pdr_notify(&b, 1); qcom_battmgr_enable_worker(&b.enable_work.work);
 assert(requests == BATTMGR_ENABLE_ATTEMPTS && !b.enable_work.pending);
 qcom_battmgr_pdr_notify(&b, 0); qcom_battmgr_pdr_notify(&b, 1); result=0; tick(&b);
 assert(requests == BATTMGR_ENABLE_ATTEMPTS+1 && b.enable_attempts == 1 && b.notifications_enabled);
 return 0;
}
''')

    def test_down_cancels_pending_retry_and_offline_worker_sends_nothing(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b = {0}; result=-EAGAIN;
 qcom_battmgr_pdr_notify(&b, 1); tick(&b); assert(b.enable_work.pending);
 qcom_battmgr_pdr_notify(&b, 0); assert(!b.enable_work.pending);
 qcom_battmgr_enable_worker(&b.enable_work.work);
 assert(requests == 1 && !publications && !b.notifications_enabled);
 return 0;
}
''')

    def test_down_up_during_request_never_accepts_old_ack_or_delays_new_up(self):
        common.execute(self.code() + r'''
static void bounce(struct qcom_battmgr *b) {
 request_hook=NULL; qcom_battmgr_pdr_notify(b, 0); qcom_battmgr_pdr_notify(b, 1);
}
int main(void) {
 for (int fail=0; fail<2; fail++) {
  struct qcom_battmgr b = {.bat_psy=(void *)1};
  qcom_battmgr_pdr_notify(&b, 1); request_hook=bounce; result=fail ? -ETIMEDOUT : 0;
  tick(&b); assert(!b.notifications_enabled && !publications && b.enable_work.pending);
  assert(b.enable_work.delay == 0 && b.enable_attempts == 0);
  result=0; tick(&b); assert(b.notifications_enabled && publications == 1);
  publications=0;
 }
 return 0;
}
''')

    def test_service_change_while_waiting_for_request_lock_skips_old_request(self):
        common.execute(self.code() + r'''
static struct qcom_battmgr b;
static void bounce(void) { qcom_battmgr_pdr_notify(&b, 0); qcom_battmgr_pdr_notify(&b, 1); }
int main(void) {
 qcom_battmgr_pdr_notify(&b, 1); mutex_hook=bounce; tick(&b);
 assert(!requests && b.enable_work.pending && b.enable_work.delay == 0);
 tick(&b); assert(requests == 1 && b.notifications_enabled);
 return 0;
}
''')

    def test_permanent_errors_do_not_retry_and_stop_blocks_all_queues(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b = {.variant=QCOM_BATTMGR_SM8475};
 qcom_battmgr_pdr_notify(&b, 1); result=-EINVAL; tick(&b);
 assert(!b.enable_work.pending && requests == 1 && errors == 1);
 qcom_battmgr_pdr_notify(&b, 0); qcom_battmgr_pdr_notify(&b, 1); result=-EIO; tick(&b);
 assert(b.enable_work.pending);
 qcom_battmgr_stop(&b); qcom_battmgr_pdr_notify(&b, 1);
 assert(disabled == 3 && b.stopping && !b.service_up && !b.enable_work.pending);
 qcom_battmgr_enable_worker(&b.enable_work.work); assert(requests == 2);
 return 0;
}
''')

    def test_devres_drains_requests_before_client_release_including_probe_failure(self):
        source = (args.source / 'drivers/power/supply/qcom_battmgr.c').read_text()
        probe = common.function(source, 'qcom_battmgr_probe')
        self.assertLess(probe.index('devm_pmic_glink_client_alloc'),
                        probe.index('devm_add_action_or_reset(dev, qcom_battmgr_stop'))
        self.assertLess(probe.index('devm_delayed_work_autocancel(dev, &battmgr->refresh_work'),
                        probe.index('devm_add_action_or_reset(dev, qcom_battmgr_stop'))
        self.assertLess(probe.index('devm_add_action_or_reset(dev, qcom_battmgr_stop'),
                        probe.index('pmic_glink_client_register'))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
