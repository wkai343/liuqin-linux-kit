#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compile actual UCSI supply readers against the kernel's PD definitions."""
import argparse
import importlib.util
from pathlib import Path
import re
import sys
import unittest

parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path)
args,rest=parser.parse_known_args()
spec=importlib.util.spec_from_file_location('ucsi_fixture',Path(__file__).with_name('kernel-common-fixes.py'))
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)

PRELUDE=common.COMMON+r'''
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint16_t __le16;
typedef uint32_t __le32;
#define __packed __attribute__((packed))
#define BIT(n) (1UL << (n))
#define GENMASK(h,l) ((~0UL << (l)) & (~0UL >> (63-(h))))
#define FIELD_GET(m,v) (((v)&(m)) >> __builtin_ctzl(m))
#define FIELD_PREP(m,v) (((v) << __builtin_ctzl(m)) & (m))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define READ_ONCE(v) (v)
#define IS_ENABLED(v) 0
#define le16_to_cpu(v) (v)
#define cpu_to_le16(v) (v)
enum { TYPEC_SINK, TYPEC_SOURCE, TYPEC_HOST };
'''
STRUCTS=r'''
#define UCSI_CONSTAT(c,field) ((c)->field)
enum { UCSI_CONSTAT_PWR_OPMODE_NONE, UCSI_CONSTAT_PWR_OPMODE_DEFAULT,
       UCSI_CONSTAT_PWR_OPMODE_BC, UCSI_CONSTAT_PWR_OPMODE_PD,
       UCSI_CONSTAT_PWR_OPMODE_TYPEC1_5, UCSI_CONSTAT_PWR_OPMODE_TYPEC3_0 };
enum { UCSI_PSY_OFFLINE, UCSI_PSY_FIXED_ONLINE, UCSI_PSY_PROG_ONLINE };
enum { POWER_SUPPLY_USB_TYPE_C, POWER_SUPPLY_USB_TYPE_PD, POWER_SUPPLY_USB_TYPE_PD_PPS };
#define UCSI_TYPEC_VSAFE5V 5000
#define UCSI_TYPEC_DEFAULT_CURRENT 500
#define UCSI_TYPEC_1_5_CURRENT 1500
#define UCSI_TYPEC_3_0_CURRENT 3000
struct ucsi_connector { int CONNECTED,PWR_DIR,PWR_OPMODE,num_pdos;
 u32 rdo,src_pdos[PDO_MAX_OBJECTS]; };
union power_supply_propval { int intval; };
typedef int (*reader)(struct ucsi_connector *,union power_supply_propval *);
static void value(reader fn,struct ucsi_connector *c,int expected) {
 union power_supply_propval v={.intval=-123};assert(!fn(c,&v));assert(v.intval==expected);
}
static void unavailable(reader fn,struct ucsi_connector *c) {
 union power_supply_propval v={.intval=-123};assert(fn(c,&v)==-ENODATA);assert(v.intval==-123);
}
'''

@unittest.skipUnless(args.source,'--source enables compiled UCSI checks')
class Tests(unittest.TestCase):
    def test_notification_quirk_keeps_only_negotiated_power_change(self):
        header=(args.source/'drivers/usb/typec/ucsi/ucsi.h').read_text()
        macros='\n'.join(line for line in header.splitlines() if re.match(
            r'#define UCSI_(ENABLE_NTFY_|CAP_|VERSION_|READ_PARTNER_SOURCE_PDOS)',line))
        source=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        code=PRELUDE+macros+r'''
struct ucsi { struct { u16 features; } cap; u16 version; unsigned long quirks; };
'''+common.function(source,'ucsi_get_supported_notifications')+r'''
int main(void) {
 struct ucsi u={.version=UCSI_VERSION_1_0,.cap.features=1};
 u64 baseline=ucsi_get_supported_notifications(&u);
 assert(!(baseline & (UCSI_ENABLE_NTFY_PWR_LEVEL_CHANGE | UCSI_ENABLE_NTFY_CAP_CHANGE)));
 u.quirks=UCSI_READ_PARTNER_SOURCE_PDOS;
 assert(ucsi_get_supported_notifications(&u)==(baseline | UCSI_ENABLE_NTFY_PWR_LEVEL_CHANGE));
 u.version=UCSI_VERSION_1_1;assert(ucsi_get_supported_notifications(&u)==baseline);
 u.version=UCSI_VERSION_2_1;
 assert(ucsi_get_supported_notifications(&u)==(baseline | UCSI_ENABLE_NTFY_SINK_PATH_STS_CHANGE));
 u.cap.features=UCSI_CAP_PDO_DETAILS;
 assert(ucsi_get_supported_notifications(&u)&UCSI_ENABLE_NTFY_PWR_LEVEL_CHANGE);
 assert(ucsi_get_supported_notifications(&u)&UCSI_ENABLE_NTFY_CAP_CHANGE);
 return 0;
}
'''
        common.execute(code)

    def test_source_cache_is_published_only_after_complete_read(self):
        source=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        code=common.COMMON+r'''
#define PDO_MAX_OBJECTS 7
#define TYPEC_SOURCE 1
struct ucsi_connector {u32 src_pdos[7];int num_pdos;};
static int result,events;
static int ucsi_get_pdos(struct ucsi_connector *c,int role,int partner,u32 *pdos) {
 assert(role==TYPEC_SOURCE && partner==1);pdos[0]=0x2f01912c;return result;
}
static void ucsi_port_psy_changed(struct ucsi_connector *c) {events++;}
'''+common.function(source,'ucsi_get_src_pdos')+r'''
int main(void) {
 struct ucsi_connector c={.num_pdos=2,.src_pdos={1,2,3,4,5,6,7}},original=c;
 result=-ETIMEDOUT;assert(ucsi_get_src_pdos(&c)==-ETIMEDOUT);assert(!memcmp(&c,&original,sizeof(c)) && !events);
 result=1;assert(ucsi_get_src_pdos(&c)==1);assert(c.num_pdos==1 && c.src_pdos[0]==0x2f01912c && events==1);
 for(int i=1;i<7;i++)assert(!c.src_pdos[i]);
 return 0;
}
'''
        common.execute(code)

    def test_detach_clears_cached_contract_before_next_attach(self):
        source=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        code=common.COMMON+r'''
#define TYPEC_STATE_SAFE 0
#define UCSI_RECIPIENT_SOP 0
struct ucsi_connector;
struct ops { void (*remove_partner_altmodes)(struct ucsi_connector *); };
struct ucsi { struct ops *ops; };
struct ucsi_connector {u32 rdo,src_pdos[7];int num_pdos;void *partner,*port;u32 partner_identity;struct ucsi *ucsi;};
static void typec_set_mode(void *p,int mode) {}
static void typec_partner_set_usb_power_delivery(void *p,void *pd) {}
static void ucsi_unregister_partner_pdos(struct ucsi_connector *c) {}
static void ucsi_unregister_altmodes(struct ucsi_connector *c,int recipient) {}
static void ucsi_unregister_cable(struct ucsi_connector *c) {}
static void typec_unregister_partner(void *p) {}
'''+common.function(source,'ucsi_unregister_partner')+r'''
int main(void) {
 struct ops ops={0};struct ucsi u={.ops=&ops};
 for(int present=0;present<=1;present++) {
  struct ucsi_connector c={.rdo=0x7303843c,.src_pdos={1,2,3,4,5,6,7},.num_pdos=7,.ucsi=&u};
  c.partner=present?&c:NULL;ucsi_unregister_partner(&c);
  assert(!c.partner && !c.rdo && !c.num_pdos);
  for(int i=0;i<7;i++)assert(!c.src_pdos[i]);
 }
 return 0;
}
'''
        common.execute(code)

    def run_readers(self,body):
        # Use the actual pd.h masks, constructors and decoders, not copied encodings.
        pd=re.sub(r'^#include .*\n','',(args.source/'include/linux/usb/pd.h').read_text(),flags=re.M)
        psy=(args.source/'drivers/usb/typec/ucsi/psy.c').read_text()
        names=['ucsi_psy_is_sink','ucsi_psy_is_pps','ucsi_psy_get_pd_request','ucsi_psy_pps_request']
        names+=['ucsi_psy_get_'+x for x in ['online','voltage_min','voltage_max','voltage_now',
                                          'current_max','current_now','usb_type']]
        common.execute(PRELUDE+pd+STRUCTS+'\n'.join(common.function(psy,n) for n in names)+body)

    def test_pps_request_units_selected_limits_and_protocol(self):
        self.run_readers(r'''
int main(void) {
 struct ucsi_connector c={.CONNECTED=1,.PWR_OPMODE=UCSI_CONSTAT_PWR_OPMODE_PD,.num_pdos=7};
 c.src_pdos[0]=PDO_FIXED(5000,3000,0);c.src_pdos[4]=PDO_FIXED(20000,5000,0);
 c.src_pdos[5]=PDO_PPS_APDO(5000,11000,5000);c.src_pdos[6]=PDO_PPS_APDO(5000,20000,5000);
 /* Literal request independently observed from M81: PDO 7, 9 V, 3 A. */
 c.rdo=0x7303843c;
 value(ucsi_psy_get_voltage_now,&c,9000000);value(ucsi_psy_get_current_now,&c,3000000);
 value(ucsi_psy_get_online,&c,UCSI_PSY_PROG_ONLINE);value(ucsi_psy_get_usb_type,&c,POWER_SUPPLY_USB_TYPE_PD_PPS);
 c.rdo=RDO_PROG(6,9020,1250,0);
 value(ucsi_psy_get_voltage_now,&c,9020000);value(ucsi_psy_get_current_now,&c,1250000);
 value(ucsi_psy_get_voltage_min,&c,5000000);value(ucsi_psy_get_voltage_max,&c,11000000);
 value(ucsi_psy_get_current_max,&c,5000000);
 for(int mv=5000;mv<=11000;mv+=20) for(int ma=50;ma<=5000;ma+=50) {
  c.rdo=RDO_PROG(6,mv,ma,0);value(ucsi_psy_get_voltage_now,&c,mv*1000);value(ucsi_psy_get_current_now,&c,ma*1000);
 }
 /* Each property follows the next firmware contract without changing PDOs. */
 c.rdo=RDO_PROG(7,5000,3000,0);value(ucsi_psy_get_voltage_now,&c,5000000);
 c.rdo=RDO_PROG(7,9000,3000,0);value(ucsi_psy_get_voltage_now,&c,9000000);
 return 0;
}
''')

    def test_fixed_pd_and_typec_current_remain_supported(self):
        self.run_readers(r'''
int main(void) {
 struct ucsi_connector c={.CONNECTED=1,.PWR_OPMODE=UCSI_CONSTAT_PWR_OPMODE_PD,.num_pdos=3};
 c.src_pdos[0]=PDO_FIXED(5000,3000,0);c.src_pdos[1]=PDO_FIXED(9000,3000,0);c.src_pdos[2]=PDO_FIXED(20000,5000,0);
 c.rdo=RDO_FIXED(2,1600,3000,0);
 value(ucsi_psy_get_voltage_now,&c,9000000);value(ucsi_psy_get_current_now,&c,1600000);
 value(ucsi_psy_get_voltage_min,&c,5000000);value(ucsi_psy_get_voltage_max,&c,20000000);
 value(ucsi_psy_get_current_max,&c,5000000);value(ucsi_psy_get_online,&c,UCSI_PSY_FIXED_ONLINE);
 value(ucsi_psy_get_usb_type,&c,POWER_SUPPLY_USB_TYPE_PD);
 for(int mode=1;mode<=5;mode++) {
  if(mode==3)continue;
  c.PWR_OPMODE=mode;
  value(ucsi_psy_get_voltage_now,&c,5000000);
  value(ucsi_psy_get_current_now,&c,mode==4?1500000:mode==5?3000000:500000);
  value(ucsi_psy_get_online,&c,UCSI_PSY_FIXED_ONLINE);value(ucsi_psy_get_usb_type,&c,POWER_SUPPLY_USB_TYPE_C);
 }
 return 0;
}
''')

    def test_missing_malformed_and_unsupported_contracts(self):
        self.run_readers(r'''
int main(void) {
 struct ucsi_connector c={.CONNECTED=1,.PWR_OPMODE=UCSI_CONSTAT_PWR_OPMODE_PD,.num_pdos=1};
 c.src_pdos[0]=PDO_PPS_APDO(5000,11000,3000);
 const u32 bad[]={RDO_PROG(1,4980,1000,0),RDO_PROG(1,11020,1000,0),RDO_PROG(1,9000,3050,0),RDO_PROG(1,9000,0,0)};
 for(unsigned int i=0;i<ARRAY_SIZE(bad);i++) {
  c.rdo=bad[i];unavailable(ucsi_psy_get_voltage_now,&c);unavailable(ucsi_psy_get_current_now,&c);
 }
 for(int count=-1;count<=8;count++) for(int index=0;index<=7;index++) {
  c.num_pdos=count;c.rdo=RDO_PROG(index,9000,1000,0);
  if(count<=0 || count>7 || index==0 || index>count || index>1) {
   unavailable(ucsi_psy_get_voltage_now,&c);unavailable(ucsi_psy_get_current_now,&c);
  }
 }
 c.num_pdos=1;c.rdo=RDO_PROG(1,9000,1000,0);
 c.src_pdos[0]=PDO_TYPE(PDO_TYPE_APDO) | (1U<<28); /* unsupported EPR AVS subtype */
 unavailable(ucsi_psy_get_voltage_now,&c);unavailable(ucsi_psy_get_current_now,&c);
 unavailable(ucsi_psy_get_voltage_min,&c);unavailable(ucsi_psy_get_voltage_max,&c);unavailable(ucsi_psy_get_current_max,&c);
 value(ucsi_psy_get_online,&c,UCSI_PSY_FIXED_ONLINE);value(ucsi_psy_get_usb_type,&c,POWER_SUPPLY_USB_TYPE_PD);
 c.src_pdos[0]=PDO_BATT(5000,12000,15000);c.rdo=RDO_BATT(1,10000,15000,0);
 unavailable(ucsi_psy_get_voltage_now,&c);unavailable(ucsi_psy_get_current_now,&c);
 c.src_pdos[0]=PDO_VAR(5000,12000,3000);c.rdo=RDO_FIXED(1,1500,2000,0);
 unavailable(ucsi_psy_get_voltage_now,&c);value(ucsi_psy_get_current_now,&c,1500000);
 return 0;
}
''')

    def test_detached_and_source_roles_do_not_report_sink_power(self):
        self.run_readers(r'''
int main(void) {
 struct ucsi_connector c={.CONNECTED=1,.PWR_OPMODE=UCSI_CONSTAT_PWR_OPMODE_PD,.num_pdos=1};
 c.src_pdos[0]=PDO_PPS_APDO(5000,11000,3000);c.rdo=RDO_PROG(1,9000,2000,0);
 reader readers[]={ucsi_psy_get_voltage_now,ucsi_psy_get_current_now,ucsi_psy_get_voltage_min,
                   ucsi_psy_get_voltage_max,ucsi_psy_get_current_max,ucsi_psy_get_online};
 for(int source=0;source<2;source++) {
  c.CONNECTED=source;c.PWR_DIR=source?TYPEC_SOURCE:TYPEC_SINK;
  for(unsigned int i=0;i<ARRAY_SIZE(readers);i++)value(readers[i],&c,0);
 }
 return 0;
}
''')

if __name__=='__main__':unittest.main(argv=[sys.argv[0],*rest],verbosity=2)
