#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the Rec lane worker, injecting failures at each session transition."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('common', ROOT/'tests/kernel-common-fixes.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

FIXTURE = r'''
#define TYPEC_ORIENTATION_NONE 0
#define TYPEC_ORIENTATION_REVERSE 2
#define TYPEC_STATE_USB 1
#define USB_ROLE_NONE 0
#define USB_ROLE_DEVICE 2
#define BATTMGR_XM_PROPERTY_GET 0x50
#define XM_CC_ORIENTATION 36
#define dev_info(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define spin_lock_irqsave(lock,flags) do {(void)(lock); (flags)=0;} while(0)
#define spin_unlock_irqrestore(lock,flags) do {(void)(lock); (void)(flags);} while(0)
#define to_delayed_work(work) (work)
#define container_of(ptr,type,member) ((type *)((char *)(ptr)-offsetof(type,member)))
struct work_struct {int unused;};
struct typec_mux_state {int mode;};
struct qcom_battmgr {
 struct work_struct rec_orientation_work;
 unsigned rec_orientation, service_generation;
 int state_lock;
 bool service_up, stopping, rec_session_dirty;
 void *rec_role,*rec_switch,*rec_mux,*dev;
};
static void *system_dfl_wq;
static unsigned orientation, calls, queued;
static int role=USB_ROLE_DEVICE, lane=1, fail_step, read_error;
static int transition(void) {calls++; return fail_step==(int)calls?-EIO:0;}
static int usb_role_switch_set_role(void *sw,int value) {
 int ret=transition(); if(!ret)role=value; return ret;
}
static int typec_switch_set(void *sw,unsigned value) {
 assert(role==USB_ROLE_NONE); int ret=transition(); if(!ret)lane=value; return ret;
}
static int typec_mux_set(void *sw,struct typec_mux_state *state) {
 assert(role==USB_ROLE_NONE && state->mode==TYPEC_STATE_USB); return transition();
}
static int qcom_battmgr_raw_property(struct qcom_battmgr *d,int op,int prop,unsigned *value) {
 assert(op==BATTMGR_XM_PROPERTY_GET && prop==XM_CC_ORIENTATION);
 *value=orientation; return read_error;
}
static unsigned msecs_to_jiffies(unsigned ms) {return ms;}
static void queue_delayed_work(void *wq,struct work_struct *w,unsigned delay) {
 assert(delay==500); queued++;
}
'''


class Tests(unittest.TestCase):
    def code(self):
        patch=(ROOT/'build/recovery/kernel/patches/linux-7.3/0005-recovery-typec-orientation.patch').read_text()
        added='\n'.join(line[1:] for line in patch.splitlines()
                        if line.startswith('+') and not line.startswith('+++'))
        return common.COMMON+FIXTURE+common.function(added,'qcom_battmgr_rec_orientation_worker')

    def test_each_partial_failure_recovers_even_when_cc_returns_to_cached_value(self):
        for failure in range(1,5):
            with self.subTest(step=failure):
                common.execute(self.code()+f'''
int main(void) {{
 struct qcom_battmgr d={{.service_up=true,.rec_orientation=1}};
 orientation=2; fail_step={failure};
 qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(d.rec_session_dirty && d.rec_orientation==1 && queued==1);
 orientation=1; fail_step=0; calls=0;
 qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(!d.rec_session_dirty && d.rec_orientation==1 && calls==4);
 assert(role==USB_ROLE_DEVICE && lane==1);
 calls=0; qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(!calls); return 0;
}}
''')

    def test_dirty_session_does_not_act_on_unknown_or_transport_failure(self):
        common.execute(self.code()+r'''
int main(void) {
 struct qcom_battmgr d={.service_up=true,.rec_orientation=1,.rec_session_dirty=true};
 orientation=99; qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 orientation=1; read_error=-ETIMEDOUT;
 qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(!calls && d.rec_session_dirty && queued==2);
 read_error=0; orientation=0;
 qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(!d.rec_session_dirty && !d.rec_orientation && role==USB_ROLE_NONE && lane==0);
 return 0;
}
''')

    def test_service_loss_invalidates_session_and_resume_reapplies_it(self):
        common.execute(self.code()+r'''
int main(void) {
 struct qcom_battmgr d={.rec_orientation=1};
 qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(d.rec_session_dirty && !d.rec_orientation && role==USB_ROLE_NONE && !queued);
 d.service_up=true; orientation=1; calls=0;
 qcom_battmgr_rec_orientation_worker(&d.rec_orientation_work);
 assert(!d.rec_session_dirty && role==USB_ROLE_DEVICE && lane==1 && calls==4);
 return 0;
}
''')


if __name__=='__main__':
    unittest.main()
