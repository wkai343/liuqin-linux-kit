#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the actual Liuqin CC query and UCSI orientation callbacks."""
import argparse
import errno
import importlib.util
from pathlib import Path
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path)
args, rest = parser.parse_known_args()
spec = importlib.util.spec_from_file_location('common', Path(__file__).with_name('kernel-common-fixes.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

FIXTURE = r'''
#define PMIC_GLINK_MAX_PORTS 2
#define TYPEC_ORIENTATION_NONE 0
#define TYPEC_ORIENTATION_NORMAL 1
#define TYPEC_ORIENTATION_REVERSE 2
#define UCSI_CONSTAT(c,k) ((c)->status_##k)
#define UCSI_CONSTAT_PARTNER_TYPE_AUDIO 5
#define UCSI_CONSTAT_PARTNER_TYPE_DEBUG 6
#define TYPEC_STATE_USB 1
#define QCOM_BATTMGR_SM8475 8475
#define BATTMGR_XM_PROPERTY_GET 0x50
#define XM_CC_ORIENTATION 36
#define dev_warn_ratelimited(...) do { warnings++; } while (0)
#define dev_err(...) do { errors++; } while (0)
#define dev_dbg(...) ((void)0)
struct device { struct device *parent; const char *name; void *driver, *data; };
struct qcom_battmgr { int variant; };
static struct { int driver; } qcom_battmgr_driver;
static struct device supplier;
static bool available=true, board=true;
static int lock_depth, refs, queries, sleeps, applied, lane=-1, switch_error, warnings, errors;
static int results[16], values[16];
static int mux_calls, mux_error;
static void *dev_get_drvdata(struct device *d) { return d->data; }
static const char *dev_name(struct device *d) { return d->name; }
static bool str_has_prefix(const char *s, const char *prefix) { return !strncmp(s,prefix,strlen(prefix)); }
static struct device *device_find_child(struct device *p, const void *data,
                                        int (*match)(struct device *, const void *)) {
 assert(p && !refs && !lock_depth);
 if (!available || !match(&supplier,data)) return NULL;
 refs++; return &supplier;
}
static void device_lock(struct device *d) { assert(d==&supplier && refs==1 && !lock_depth); lock_depth++; }
static void device_unlock(struct device *d) { assert(d==&supplier && lock_depth==1); lock_depth--; }
static void put_device(struct device *d) { assert(d==&supplier && !lock_depth && refs==1); refs--; }
static int qcom_battmgr_raw_property(struct qcom_battmgr *b, int opcode, int property, unsigned int *out) {
 assert(b==supplier.data && refs==1 && lock_depth==1);
 assert(opcode==BATTMGR_XM_PROPERTY_GET && property==XM_CC_ORIENTATION && queries<16);
 int i=queries++; *out=values[i]; return results[i];
}
struct pmic_glink_ucsi { struct device *dev; int *port_orientation[2]; };
struct ucsi_connector {
 struct pmic_glink_ucsi *ucsi; unsigned int num; bool status_CONNECTED;
 int status_PARTNER_FLAG_USB, status_PARTNER_FLAG_ALT_MODE, status_PARTNER_TYPE;
 struct { bool orientation_aware; } typec_cap; void *port, *partner;
};
static struct pmic_glink_ucsi *ucsi_get_drvdata(struct pmic_glink_ucsi *u) { return u; }
static bool liuqin_board_quirks(void) { return board; }
static int typec_set_orientation(void *port, int orientation) {
 assert(!refs && !lock_depth); applied++;
 if (switch_error) return switch_error;
 lane=orientation; return 0;
}
static int gpiod_get_value(int *gpio) { return *gpio; }
static int typec_set_mode(void *port, int mode) {
 assert(!refs && !lock_depth && applied && !switch_error);
 assert(mode==TYPEC_STATE_USB && lane>0); mux_calls++; return mux_error;
}
static void msleep(int ms) { assert(ms==50 && !refs && !lock_depth); sleeps++; }
'''


@unittest.skipUnless(args.source, '--source enables actual CC function execution')
class Tests(unittest.TestCase):
    def code(self):
        battery = (args.source / 'drivers/power/supply/qcom_battmgr.c').read_text()
        ucsi = (args.source / 'drivers/usb/typec/ucsi/ucsi_glink.c').read_text()
        functions = [common.function(battery, name) for name in
                     ('qcom_battmgr_match_child', 'qcom_battmgr_read_cc_orientation')]
        functions += [common.function(ucsi, name) for name in
                      ('pmic_glink_ucsi_update_connector', 'pmic_glink_ucsi_liuqin_orientation',
                       'pmic_glink_ucsi_prepare_connection', 'pmic_glink_ucsi_connector_status')]
        return common.COMMON + FIXTURE + '\n'.join(functions) + r'''
static int apply_connection(struct ucsi_connector *con) {
 int ret=pmic_glink_ucsi_prepare_connection(con);
 if(!ret)pmic_glink_ucsi_connector_status(con);
 return ret;
}
static struct qcom_battmgr battery={.variant=QCOM_BATTMGR_SM8475};
static struct device parent, client={.parent=&parent};
static struct pmic_glink_ucsi glink={.dev=&client};
static struct ucsi_connector con={.ucsi=&glink,.num=1,.status_CONNECTED=true};
static void setup(void) {
 (void)con;
 supplier=(struct device){.parent=&parent,.name="pmic_glink.power-supply.0",
                          .driver=&qcom_battmgr_driver.driver,.data=&battery};
}
'''

    def test_both_orientations_and_disconnect_without_gpio(self):
        common.execute(self.code() + r'''
int main(void) {
 setup(); pmic_glink_ucsi_update_connector(&con); assert(con.typec_cap.orientation_aware);
 values[0]=1; values[1]=2;
 apply_connection(&con); assert(lane==1 && queries==1);
 con.status_CONNECTED=false; apply_connection(&con); assert(lane==0 && queries==1);
 con.status_CONNECTED=true; apply_connection(&con); assert(lane==2 && queries==2);
 assert(applied==3 && !sleeps && !refs && !lock_depth); return 0;
}
''')

    def test_stale_detached_and_transient_transport_retry_before_switch(self):
        common.execute(self.code() + r'''
int main(void) {
 setup(); values[0]=0; results[1]=-EAGAIN; values[2]=2;
 apply_connection(&con);
 assert(queries==3 && sleeps==2 && applied==1 && lane==2 && !warnings);
 return 0;
}
''')

    def test_bounded_retry_and_no_guessed_lane_on_invalid_or_failed_query(self):
        for result, value, expected_queries, expected_sleeps in (
                (0, 0, 10, 9), (-errno.EAGAIN, 0, 10, 9),
                (-errno.ETIMEDOUT, 2, 1, 0), (0, 3, 1, 0)):
            with self.subTest(result=result, value=value):
                common.execute(self.code() + f'''
int main(void) {{
 setup(); for (int i=0;i<16;i++) {{ results[i]={result}; values[i]={value}; }}
 apply_connection(&con);
 assert(queries=={expected_queries} && sleeps=={expected_sleeps});
 assert(!applied && lane==-1 && warnings==1 && !refs && !lock_depth); return 0;
}}
''')

    def test_supplier_lifetime_variant_and_output_guards(self):
        common.execute(self.code() + r'''
int main(void) {
 setup(); unsigned int cc=99;
 available=false; assert(qcom_battmgr_read_cc_orientation(&parent,&cc)==-EAGAIN);
 available=true; supplier.name="pmic_glink.ucsi.0";
 assert(qcom_battmgr_read_cc_orientation(&parent,&cc)==-EAGAIN);
 supplier.name="pmic_glink.power-supply.0"; supplier.driver=NULL;
 assert(qcom_battmgr_read_cc_orientation(&parent,&cc)==-EAGAIN);
 supplier.driver=&qcom_battmgr_driver.driver; battery.variant=1;
 assert(qcom_battmgr_read_cc_orientation(&parent,&cc)==-EOPNOTSUPP);
 battery.variant=QCOM_BATTMGR_SM8475; values[0]=3;
 assert(qcom_battmgr_read_cc_orientation(&parent,&cc)==-EPROTO);
 assert(cc==99 && queries==1 && !refs && !lock_depth); return 0;
}
''')

    def test_other_boards_keep_gpio_and_switch_errors_are_reported(self):
        common.execute(self.code() + r'''
int main(void) {
 setup(); board=false; int gpio=1; glink.port_orientation[0]=&gpio;
 pmic_glink_ucsi_update_connector(&con); assert(con.typec_cap.orientation_aware);
 apply_connection(&con); assert(lane==2 && !queries);
 gpio=0; apply_connection(&con); assert(lane==1 && !queries);
 gpio=-EIO; apply_connection(&con); assert(lane==1 && applied==2);
 board=true; values[0]=2; switch_error=-EIO;
 apply_connection(&con); assert(lane==1 && errors==1 && queries==1);
 return 0;
}
''')

    def test_initial_usb_mux_follows_lanes_and_preserves_existing_partner(self):
        common.execute(self.code() + r'''
int main(void) {
 setup(); con.status_PARTNER_FLAG_USB=1;
 for (int i=0;i<16;i++) values[i]=2;
 apply_connection(&con); assert(mux_calls==1 && lane==2);
 con.partner=&con;
 apply_connection(&con); assert(mux_calls==1);
 con.partner=NULL; con.status_CONNECTED=false;
 apply_connection(&con); assert(mux_calls==1 && lane==0);
 con.status_CONNECTED=true; mux_error=-EIO;
 apply_connection(&con); assert(mux_calls==2 && errors==1);
 mux_error=0; switch_error=-EIO;
 apply_connection(&con); assert(mux_calls==2 && errors==2);
 return 0;
}
''')

    def test_initial_mux_does_not_override_altmode_accessory_or_other_board(self):
        common.execute(self.code() + r'''
int main(void) {
 setup(); for (int i=0;i<16;i++) values[i]=2;
 apply_connection(&con); assert(!mux_calls);
 con.status_PARTNER_FLAG_USB=1; con.status_PARTNER_FLAG_ALT_MODE=1;
 apply_connection(&con); assert(!mux_calls);
 con.status_PARTNER_FLAG_ALT_MODE=0; con.status_PARTNER_TYPE=UCSI_CONSTAT_PARTNER_TYPE_AUDIO;
 apply_connection(&con); assert(!mux_calls);
 con.status_PARTNER_TYPE=UCSI_CONSTAT_PARTNER_TYPE_DEBUG;
 apply_connection(&con); assert(!mux_calls);
 con.status_PARTNER_TYPE=0; board=false;
 apply_connection(&con); assert(!mux_calls);
 return 0;
}
''')


if __name__ == '__main__':
    unittest.main(argv=[__file__, *rest])
