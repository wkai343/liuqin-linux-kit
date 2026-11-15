#!/usr/bin/env python3
"""Execute the actual M81 control callbacks with adversarial GLINK responses."""
import argparse
import importlib.util
from pathlib import Path
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
args, rest = parser.parse_known_args()
spec = importlib.util.spec_from_file_location('common', Path(__file__).with_name('kernel-common-fixes.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
/* Match the kernel's -Wno-sign-compare for signed sysfs to u32 wire values. */
#pragma GCC diagnostic ignored "-Wsign-compare"
typedef uint32_t u32;
#define PMIC_GLINK_OWNER_BATTMGR 32778
#define PMIC_GLINK_REQ_RESP 1
#define BATTMGR_BAT_PROPERTY_SET 0x31
#define BATTMGR_USB_PROPERTY_GET 0x32
#define BATTMGR_USB_PROPERTY_SET 0x33
#define BATT_CHG_CTRL_LIM 10
#define USB_INPUT_CURR_LIMIT 5
#define USB_ADAP_TYPE 7
#define U32_MAX UINT32_MAX
#define QCOM_BATTMGR_SM8475 1
enum power_supply_property { POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT };
enum { POWER_SUPPLY_USB_TYPE_SDP=1, POWER_SUPPLY_USB_TYPE_PD=6 };
union power_supply_propval {int intval;};
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,y) ((x)=(y))
#define smp_store_release(x,y) (*(x)=(y))
#define le32_to_cpu(x) (x)
#define spin_lock_irqsave(x,f) ((f)=0)
#define spin_unlock_irqrestore(x,f) ((void)(f))
#define mutex_lock(x) ((void)0)
#define mutex_unlock(x) ((void)0)
struct pmic_glink_hdr {u32 owner,type,opcode;};
struct qcom_battmgr_message {
 struct pmic_glink_hdr hdr;
 struct {u32 property,value,result;} intval;
};
struct qcom_battmgr {
 bool service_up, stopping; u32 service_generation;
 struct {bool active; u32 opcode,property,value;} control;
 u32 thermal_level,thermal_generation,icl_ua,icl_generation,replay_generation;
 bool thermal_valid,icl_requested,icl_valid;
 unsigned int replay_attempts;
 int state_lock,lock,error,ack,variant;
};
struct power_supply {struct qcom_battmgr *b;int changed;};
static void *power_supply_get_drvdata(struct power_supply *p) {return p->b;}
static void power_supply_changed(struct power_supply *p) {p->changed++;}
static int requests,result,bounce;
static u32 last_value,last_property,last_opcode,usb_type=6;
static int usb_set_result;
static void complete(int *ack) {(*ack)++;}
static int qcom_battmgr_request_property(struct qcom_battmgr *b,u32 op,u32 prop,u32 value) {
 assert(b->control.active && b->control.opcode==op && b->control.property==prop);
 requests++; last_value=value;last_property=prop;last_opcode=op;
 if(bounce) {b->service_generation++;bounce=0;}
 b->control.value=op==BATTMGR_USB_PROPERTY_GET ? usb_type : 123;
 return result ? result : (op==BATTMGR_USB_PROPERTY_SET ? usb_set_result : 0);
}
'''


class Tests(unittest.TestCase):
    def code(self):
        source = (args.source / 'drivers/power/supply/qcom_battmgr.c').read_text()
        names = ['qcom_battmgr_control_locked', 'qcom_battmgr_thermal_set_locked',
                 'qcom_battmgr_thermal_get', 'qcom_battmgr_controls_restore',
                 'qcom_battmgr_control_response', 'qcom_battmgr_usb_set_property']
        return FIXTURE + '\n'.join(common.function(source, name) for name in names)

    def test_level_limits_failures_and_old_service_ack(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b={.service_up=true,.service_generation=1};
 unsigned long level=99;
 assert(qcom_battmgr_thermal_get(&b,&level)==-EAGAIN);
 assert(qcom_battmgr_thermal_set_locked(&b,16)==-EINVAL && requests==0);
 assert(qcom_battmgr_thermal_set_locked(&b,(unsigned long)-1)==-EINVAL && requests==0);
 assert(!qcom_battmgr_thermal_set_locked(&b,15));
 assert(last_opcode==0x31 && last_property==10 && last_value==15);
 assert(!qcom_battmgr_thermal_get(&b,&level) && level==15);
 result=-ETIMEDOUT;
 assert(qcom_battmgr_thermal_set_locked(&b,0)==-ETIMEDOUT);
 assert(b.thermal_level==15 && !b.thermal_valid);
 result=0;bounce=1;
 assert(qcom_battmgr_thermal_set_locked(&b,0)==-ENOTCONN);
 assert(b.thermal_level==15 && !b.thermal_valid);
 b.service_up=false;int n=requests;
 assert(qcom_battmgr_thermal_set_locked(&b,0)==-EAGAIN && requests==n);
 return 0;
}
''')

    def test_response_identity_length_status_and_signed_error(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b={.control={.opcode=0x31,.property=10}};
 struct qcom_battmgr_message r={{32778,1,0x31},{10,7,0}};
 qcom_battmgr_control_response(&b,&r,12); assert(!b.ack);
 qcom_battmgr_control_response(&b,&r,23); assert(!b.ack);
 qcom_battmgr_control_response(&b,&r,25); assert(!b.ack);
 r.hdr.type=2;qcom_battmgr_control_response(&b,&r,24);assert(!b.ack);r.hdr.type=1;
 r.hdr.owner=3;qcom_battmgr_control_response(&b,&r,24);assert(!b.ack);r.hdr.owner=32778;
 r.hdr.opcode=0x30;qcom_battmgr_control_response(&b,&r,24);assert(!b.ack);r.hdr.opcode=0x31;
 r.intval.property=11;qcom_battmgr_control_response(&b,&r,24);assert(!b.ack);r.intval.property=10;
 r.intval.result=1;qcom_battmgr_control_response(&b,&r,24);assert(b.ack==1 && b.error==-EREMOTEIO);
 r.intval.result=0;qcom_battmgr_control_response(&b,&r,24);assert(b.ack==2 && !b.error && b.control.value==7);
 return 0;
}
''')

    def test_replay_preserves_restriction_and_has_bounded_retry(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b={.service_up=true,.service_generation=1,.thermal_level=12,
 .icl_requested=true,.icl_ua=500000};
 qcom_battmgr_controls_restore(&b);
 assert(requests==2 && b.thermal_valid && b.icl_valid && last_value==500000);
 qcom_battmgr_controls_restore(&b);assert(requests==2);
 b.service_generation++;result=-ETIMEDOUT;
 for(int i=0;i<30;i++)qcom_battmgr_controls_restore(&b);
 assert(requests==12 && b.thermal_level==12 && !b.thermal_valid);
 b.service_generation++;result=0;
 qcom_battmgr_controls_restore(&b);assert(requests==14 && b.thermal_valid && b.icl_valid);
 b.service_up=false;qcom_battmgr_controls_restore(&b);assert(requests==14);
 return 0;
}
''')

    def test_usb_0x204_is_not_success_or_a_new_cached_vote(self):
        common.execute(self.code() + r'''
int main(void) {
 struct qcom_battmgr b={.service_up=true,.service_generation=1,
                       .variant=QCOM_BATTMGR_SM8475};
 struct power_supply psy={.b=&b};
 union power_supply_propval val={.intval=500000};
 struct qcom_battmgr_message r={{32778,1,0x33},{5,6,0x204}};
 b.control.opcode=0x33;b.control.property=5;b.control.value=999;
 qcom_battmgr_control_response(&b,&r,24);
 assert(b.ack==1 && b.error==-EREMOTEIO && b.control.value==999);
 usb_set_result=b.error;
 assert(qcom_battmgr_usb_set_property(&psy,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&val)==-EREMOTEIO);
 assert(last_opcode==0x33 && last_property==5 && last_value==500000);
 assert(!b.icl_requested && !b.icl_valid && !psy.changed);
 /* A prior successful vote survives a failed replacement; the failed value
    must never become the value replayed after a service restart. */
 usb_type=1;usb_set_result=0;
 assert(!qcom_battmgr_usb_set_property(&psy,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&val));
 assert(b.icl_requested && b.icl_valid && b.icl_ua==500000 && psy.changed==1);
 usb_type=6;usb_set_result=-EREMOTEIO;val.intval=750000;
 assert(qcom_battmgr_usb_set_property(&psy,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&val)==-EREMOTEIO);
 assert(b.icl_ua==500000 && !b.icl_valid && psy.changed==1);
 val.intval=-1;
 assert(qcom_battmgr_usb_set_property(&psy,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&val)==-EREMOTEIO);
 assert(last_value==UINT32_MAX && b.icl_ua==500000 && psy.changed==1);
 val.intval=-2;int n=requests;
 assert(qcom_battmgr_usb_set_property(&psy,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&val)==-EINVAL);
 assert(requests==n);
 usb_type=8;val.intval=500000;
 assert(qcom_battmgr_usb_set_property(&psy,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&val)==-EINVAL);
 assert(requests==n+1 && last_opcode==0x32);
 return 0;
}
''')


if __name__ == '__main__':
    unittest.main(argv=['charge-thermal-kernel.py', *rest], verbosity=2)
