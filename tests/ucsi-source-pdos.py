#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute UCSI PDO reads, capability refresh and failure cleanup from kernel source."""
import argparse
import importlib.util
from pathlib import Path
import re
import sys
import unittest

parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path)
args,rest=parser.parse_known_args()
# Reuse the C harness helpers without passing this suite's unittest selection.
path=Path(__file__).with_name('kernel-common-fixes.py')
spec=importlib.util.spec_from_file_location('common_fixtures',path)
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)



CAPABILITIES=r"""
#define BIT(n) (1UL << (n))
#define UCSI_READ_PARTNER_SOURCE_PDOS BIT(3)
#define PDO_MAX_OBJECTS 7
#define ERR_PTR(n) ((void *)(intptr_t)(n))
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define IS_ERR(p) ((intptr_t)(p)<0)
enum typec_role { TYPEC_SINK, TYPEC_SOURCE };
struct usb_power_delivery { int unused; };
struct usb_power_delivery_capabilities { bool live;u32 pdo[7]; };
struct usb_power_delivery_capabilities_desc { enum typec_role role;u32 pdo[7]; };
struct usb_power_delivery_desc { int revision; };
struct ucsi { struct { int pd_version; } cap; unsigned long quirks; };
struct ucsi_connector {
 struct ucsi *ucsi;void *partner;struct usb_power_delivery *partner_pd;
 struct usb_power_delivery_capabilities *partner_source_caps,*partner_sink_caps;
 u32 src_pdos[7],partner_src_pdos[7];int num_pdos;
};
static struct usb_power_delivery pd_object;
static struct usb_power_delivery_capabilities cap_objects[64];
static bool pd_live,sink_present;
static int pd_registers,pd_unregisters,caps_registers,caps_unregisters,links;
static int pd_error,cap_error,read_error,sink_error,link_error,supplied_count=7;
static u32 stale_tail=0xdeadbeef;
static u32 supplied[7]={0x2e01912c,0x0002d12c,0x0003c12c,0x0004b12c,0x000641f4,0xc0dc323c,0xc190323c};
static struct usb_power_delivery *typec_partner_usb_power_delivery_register(void *p,struct usb_power_delivery_desc *d) {
 pd_registers++;if(pd_error)return ERR_PTR(pd_error);assert(!pd_live);pd_live=true;return &pd_object;
}
static void usb_power_delivery_unregister(struct usb_power_delivery *pd) {
 if(!pd)return;
 assert(pd==&pd_object && pd_live);pd_live=false;pd_unregisters++;
}
static struct usb_power_delivery_capabilities *usb_power_delivery_register_capabilities(struct usb_power_delivery *pd,struct usb_power_delivery_capabilities_desc *d) {
 assert(pd==&pd_object && pd_live && d->role==TYPEC_SOURCE);
 caps_registers++;if(cap_error)return ERR_PTR(cap_error);assert(caps_registers<64);
 struct usb_power_delivery_capabilities *cap=&cap_objects[caps_registers];
 assert(!cap->live);cap->live=true;memcpy(cap->pdo,d->pdo,sizeof(cap->pdo));return cap;
}
static void usb_power_delivery_unregister_capabilities(struct usb_power_delivery_capabilities *cap) {
 if(!cap)return;
 assert(cap->live);cap->live=false;
 if(cap!=&cap_objects[63])caps_unregisters++;
}
static int ucsi_get_pdos(struct ucsi_connector *c,enum typec_role role,bool partner,u32 *pdos) {
 assert(role==TYPEC_SOURCE && partner);
 /* A failed second read may already have overwritten the temporary buffer. */
 if(read_error){pdos[0]=0xdeadbeef;return read_error;}
 for(int i=0;i<7;i++)pdos[i]=stale_tail;
 memcpy(pdos,supplied,supplied_count*sizeof(u32));return supplied_count;
}
static void ucsi_port_psy_changed(struct ucsi_connector *c) {}
static struct usb_power_delivery_capabilities *ucsi_get_pd_caps(struct ucsi_connector *c,enum typec_role role,bool partner) {
 assert(role==TYPEC_SINK && partner);
 if(sink_error)return ERR_PTR(sink_error);
 if(!sink_present)return NULL;
 struct usb_power_delivery_capabilities *cap=&cap_objects[63];
 assert(!cap->live);cap->live=true;return cap;
}
static int typec_partner_set_usb_power_delivery(void *p,struct usb_power_delivery *pd) {
 assert(pd==&pd_object && pd_live);links++;return link_error;
}
"""

@unittest.skipUnless(args.source,'--source enables compiled UCSI function checks')
class Tests(unittest.TestCase):
    def test_board_scope_survives_abl_root_replacement(self):
        glink=(args.source/'drivers/usb/typec/ucsi/ucsi_glink.c').read_text()
        probe=common.function(glink,'pmic_glink_ucsi_probe')
        self.assertIn('if (liuqin_board_quirks())',probe)
        self.assertIn('#include <linux/platform_data/liuqin.h>',glink)
        board=common.function((args.source/'include/linux/platform_data/liuqin.h').read_text(),
                              'liuqin_board_quirks')
        common.execute(common.COMMON+common.BOARD+board+r'''
int main(void) {
 assert(liuqin_board_quirks());
 compatible=false;assert(liuqin_board_quirks());
 model=false;assert(!liuqin_board_quirks());model=true;
 ids_present=false;assert(!liuqin_board_quirks());ids_present=true;
 board_ids[0]=17;assert(!liuqin_board_quirks());board_ids[0]=16;
 board_ids[1]=1;assert(!liuqin_board_quirks());board_ids[1]=0;
 assert(liuqin_board_quirks());return 0;
}
''')

    def test_read_scope_command_encoding_and_response_lengths(self):
        source=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        code=common.COMMON+r'''
typedef uint8_t u8;
#define BIT(n) (1UL << (n))
#define UCSI_CAP_PDO_DETAILS BIT(4)
#define UCSI_NO_PARTNER_PDOS BIT(0)
#define UCSI_READ_PARTNER_SOURCE_PDOS BIT(3)
#define UCSI_VERSION_1_0 0x100
#define UCSI_MAX_PDOS 4
#define PDO_MAX_OBJECTS 7
#define UCSI_GET_PDOS 0x10
#define UCSI_COMMAND(v) ((v) & 0xff)
#define UCSI_CONNECTOR_NUMBER(n) ((u64)(n) << 16)
#define UCSI_GET_PDOS_PARTNER_PDO(n) ((u64)(n) << 23)
#define UCSI_GET_PDOS_PDO_OFFSET(n) ((u64)(n) << 24)
#define UCSI_GET_PDOS_NUM_PDOS(n) ((u64)(n) << 32)
#define UCSI_GET_PDOS_SRC_PDOS ((u64)1 << 34)
#define UCSI_CONSTAT(c,field) ((c)->field)
#define UCSI_CONSTAT_PWR_OPMODE_PD 3
#define dev_err(...) ((void)0)
enum typec_role { TYPEC_SINK, TYPEC_SOURCE };
static bool is_source(enum typec_role role) { return role==TYPEC_SOURCE; }
struct ucsi { struct { unsigned int features; } cap; unsigned long quirks; unsigned short version; };
struct ucsi_connector { struct ucsi *ucsi; int num,CONNECTED,PWR_DIR,PWR_OPMODE; };
static int calls, responses[2];
static u64 commands[2];
static int ucsi_send_command(struct ucsi *u,u64 cmd,void *p,size_t size) {
 assert(calls<2); commands[calls]=cmd; memset(p,0,size);return responses[calls++];
}
'''+common.function(source,'ucsi_read_pdos')+'\n'+common.function(source,'ucsi_get_pdos')+r'''
int main(void) {
 struct ucsi u={.version=0x100};
 struct ucsi_connector c={.ucsi=&u,.num=1,.CONNECTED=1,.PWR_OPMODE=3};u32 pdos[7];
 assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==0 && !calls);
 u.quirks=UCSI_READ_PARTNER_SOURCE_PDOS;
 assert(ucsi_get_pdos(&c,TYPEC_SOURCE,0,pdos)==0 && !calls);
 assert(ucsi_get_pdos(&c,TYPEC_SINK,1,pdos)==0 && !calls);
 u.version=0x200;assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==0 && !calls);u.version=0x100;
 c.CONNECTED=0;assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==-ENODATA && !calls);c.CONNECTED=1;
 c.PWR_DIR=1;assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==-ENODATA && !calls);c.PWR_DIR=0;
 c.PWR_OPMODE=1;assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==-ENODATA && !calls);c.PWR_OPMODE=3;
 responses[0]=4;assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==1 && calls==1);
 assert(commands[0]==0x700810010ULL);
 calls=0;responses[0]=16;responses[1]=12;
 assert(ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos)==7 && calls==2);
 assert(commands[1]==0x604810010ULL);
 for(int ret=-ETIMEDOUT;ret<=20;ret++) {
  calls=0;responses[0]=ret;responses[1]=0;
  int got=ucsi_get_pdos(&c,TYPEC_SOURCE,1,pdos);
  if(ret<0) assert(got==ret);
  else if(ret>16 || ret%4) assert(got==-EPROTO);
  else assert(got==ret/4);
 }
 calls=0;u.quirks=0;u.cap.features=UCSI_CAP_PDO_DETAILS;responses[0]=4;
 assert(ucsi_get_pdos(&c,TYPEC_SOURCE,0,pdos)==1 && calls==1);
 return 0;
}
'''
        common.execute(code)

    def capability_fixture(self, main):
        source=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        code=common.COMMON+CAPABILITIES
        for name in ('ucsi_get_src_pdos', 'ucsi_unregister_partner_pdos',
                     'ucsi_update_partner_source_pdos', 'ucsi_register_partner_pdos'):
            code+='\n'+common.function(source,name)
        common.execute(code+main)

    def test_authentication_refreshes_complete_set_without_replacing_partner_pd(self):
        self.capability_fixture(r'''
int main(void) {
 struct ucsi u={.quirks=UCSI_READ_PARTNER_SOURCE_PDOS};
 struct ucsi_connector c={.ucsi=&u,.partner=(void *)42};
 assert(!ucsi_register_partner_pdos(&c));
 struct usb_power_delivery *pd=c.partner_pd;
 struct usb_power_delivery_capabilities *initial=c.partner_source_caps;
 assert(pd_live && initial->live && pd_registers==1 && links==1);
 assert(!memcmp(initial->pdo,supplied,sizeof(supplied)));
 supplied[5]=0xc0dc3278; supplied[6]=0xc1903278; /* Authentication: 3 A -> 6 A */
 assert(!ucsi_register_partner_pdos(&c));
 assert(!initial->live && c.partner_source_caps->live && caps_registers==2);
 assert(c.partner_pd==pd && pd_registers==1 && links==1);
 assert(!memcmp(c.partner_source_caps->pdo,supplied,sizeof(supplied)));
 for(int i=0;i<8;i++) assert(!ucsi_register_partner_pdos(&c));
 assert(caps_registers==2 && caps_unregisters==1 && links==1);
 /* A shorter list and a changed PDO type must remove obsolete children. */
 supplied_count=2;supplied[1]=0xc0dc323c;
 assert(!ucsi_register_partner_pdos(&c) && caps_registers==3);
 assert(c.partner_source_caps->pdo[1]==supplied[1]);
 for(int i=2;i<7;i++) assert(!c.partner_source_caps->pdo[i] && !c.partner_src_pdos[i]);
 assert(c.partner_pd==pd && links==1);
 /* Firmware may leave changing status bytes after a short PDO response. */
 stale_tail=0x130280a0;assert(!ucsi_register_partner_pdos(&c));
 assert(caps_registers==3 && c.partner_source_caps->pdo[2]==0);
 /* A computer may advertise only its mandatory 5 V fixed PDO. */
 supplied_count=1;assert(!ucsi_register_partner_pdos(&c));
 for(int i=1;i<7;i++)assert(!c.partner_source_caps->pdo[i]);
 stale_tail=0x01300300;assert(!ucsi_register_partner_pdos(&c));
 assert(caps_registers==4 && c.partner_pd==pd && links==1);
 ucsi_unregister_partner_pdos(&c);
 assert(!pd_live && !c.partner_pd && !c.partner_source_caps && !c.partner_sink_caps);
 for(int i=0;i<7;i++) assert(!c.partner_src_pdos[i]);
 assert(caps_unregisters==caps_registers);return 0;
}
''')

    def test_initial_registration_failures_cleanup_and_remain_retryable(self):
        self.capability_fixture(r'''
int main(void) {
 struct ucsi u={.quirks=UCSI_READ_PARTNER_SOURCE_PDOS};struct ucsi_connector c={.ucsi=&u};
 pd_error=-ENOMEM;assert(ucsi_register_partner_pdos(&c)==-EBUSY && !c.partner_pd);
 pd_error=0;read_error=-ETIMEDOUT;
 assert(ucsi_register_partner_pdos(&c)==-ETIMEDOUT && !pd_live && !c.partner_pd);
 read_error=0;supplied_count=0;
 assert(ucsi_register_partner_pdos(&c)==-EBUSY && !pd_live && !c.partner_pd);
 supplied_count=7;cap_error=-ENOMEM;
 assert(ucsi_register_partner_pdos(&c)==-EBUSY && !pd_live && !c.partner_source_caps);
 cap_error=0;sink_error=-EAGAIN;
 assert(ucsi_register_partner_pdos(&c)==-EBUSY && !pd_live && !c.partner_source_caps);
 sink_error=0;sink_present=true;link_error=-ENOMEM;
 assert(ucsi_register_partner_pdos(&c)==-EBUSY && !pd_live && !c.partner_source_caps);
 assert(!cap_objects[63].live && !c.partner_sink_caps);
 link_error=0;assert(!ucsi_register_partner_pdos(&c) && pd_live && c.partner_source_caps && c.partner_sink_caps);
 ucsi_unregister_partner_pdos(&c);assert(!pd_live && !cap_objects[63].live);
 assert(pd_unregisters==pd_registers-1 && caps_unregisters==caps_registers-1);return 0;
}
''')

    def test_refresh_read_and_allocation_failures_preserve_live_pd_and_retry(self):
        self.capability_fixture(r'''
int main(void) {
 struct ucsi u={.quirks=UCSI_READ_PARTNER_SOURCE_PDOS};struct ucsi_connector c={.ucsi=&u};
 assert(!ucsi_register_partner_pdos(&c));
 struct usb_power_delivery *pd=c.partner_pd;
 struct usb_power_delivery_capabilities *initial=c.partner_source_caps;
 u32 published[7];memcpy(published,c.partner_src_pdos,sizeof(published));
 read_error=-ETIMEDOUT;
 assert(ucsi_register_partner_pdos(&c)==-ETIMEDOUT);
 assert(c.partner_pd==pd && c.partner_source_caps==initial && initial->live);
 assert(!memcmp(c.src_pdos,published,sizeof(published)) && !caps_unregisters);
 read_error=0;supplied_count=0;
 assert(ucsi_register_partner_pdos(&c)==-EBUSY && c.partner_source_caps==initial);
 supplied_count=7;supplied[6]=0xc1903278;cap_error=-ENOMEM;
 assert(ucsi_register_partner_pdos(&c)==-EBUSY);
 assert(!initial->live && !c.partner_source_caps && c.partner_pd==pd && pd_live);
 assert(!memcmp(c.partner_src_pdos,published,sizeof(published)));
 cap_error=0;assert(!ucsi_register_partner_pdos(&c));
 assert(c.partner_source_caps && c.partner_source_caps->pdo[6]==supplied[6]);
 assert(pd_registers==1 && links==1 && !pd_unregisters);
 ucsi_unregister_partner_pdos(&c);assert(caps_unregisters==caps_registers-1);return 0;
}
''')

    def test_optional_capabilities_can_be_published_later_on_same_pd(self):
        self.capability_fixture(r'''
int main(void) {
 struct ucsi u={0};struct ucsi_connector c={.ucsi=&u};supplied_count=0;
 assert(!ucsi_register_partner_pdos(&c) && pd_live && !c.partner_source_caps && links==1);
 supplied_count=7;assert(!ucsi_register_partner_pdos(&c));
 assert(c.partner_source_caps && pd_registers==1 && links==1);
 ucsi_unregister_partner_pdos(&c);return 0;
}
''')

    def test_explicit_pdo_changes_schedule_bounded_refresh(self):
        source=(args.source/'drivers/usb/typec/ucsi/ucsi.c').read_text()
        event=common.function(source,'ucsi_handle_connector_change')
        condition=re.search(r'if \((change & \(UCSI_CONSTAT_POWER_OPMODE_CHANGE[^;]+?)\)\s*\{?\s*ucsi_pwr_opmode_change',event)
        self.assertIsNotNone(condition)
        common.execute(common.COMMON+r'''
#define UCSI_CONSTAT_POWER_OPMODE_CHANGE BIT_ULL(0)
#define UCSI_CONSTAT_POWER_LEVEL_CHANGE BIT_ULL(1)
#define UCSI_CONSTAT_PDOS_CHANGE BIT_ULL(5)
static bool refresh(u32 change) { return '''+condition[1]+r'''; }
int main(void) {
 assert(refresh(UCSI_CONSTAT_PDOS_CHANGE));assert(refresh(UCSI_CONSTAT_POWER_LEVEL_CHANGE));
 assert(refresh(UCSI_CONSTAT_POWER_OPMODE_CHANGE));assert(!refresh(0));
 assert(!refresh(BIT_ULL(2)));return 0;
}
''')
        mode=common.function(source,'ucsi_pwr_opmode_change')
        self.assertRegex(mode,r'ucsi_partner_task\(con,\s*ucsi_register_partner_pdos,\s*30,\s*HZ\)')

    def test_late_capability_link_notifies_once_and_unlink_notifies_again(self):
        source=(args.source/'drivers/usb/typec/class.c').read_text()
        code=common.COMMON+r'''
#define IS_ERR_OR_NULL(p) (!(p) || (intptr_t)(p)<0)
#define KOBJ_CHANGE 2
struct usb_power_delivery {};
struct typec_partner { struct usb_power_delivery *pd; struct { int kobj; } dev; };
static int notifications,links,unlinks,link_error;
static int usb_power_delivery_link_device(void *pd,void *dev) {links++;return link_error;}
static void usb_power_delivery_unlink_device(void *pd,void *dev) {unlinks++;}
static void kobject_uevent(void *k,int action) {assert(action==KOBJ_CHANGE);notifications++;}
'''+common.function(source,'typec_partner_set_usb_power_delivery')+r'''
int main(void) {
 struct typec_partner p={0};struct usb_power_delivery pd;
 link_error=-ENOMEM;assert(typec_partner_set_usb_power_delivery(&p,&pd)==-ENOMEM);
 assert(!p.pd && !notifications);
 link_error=0;assert(typec_partner_set_usb_power_delivery(&p,&pd)==0 && notifications==1);
 assert(typec_partner_set_usb_power_delivery(&p,&pd)==0 && notifications==1);
 assert(typec_partner_set_usb_power_delivery(&p,NULL)==0 && notifications==2 && unlinks==1);
 return 0;
}
'''
        common.execute(code)

if __name__=='__main__':unittest.main(argv=[sys.argv[0],*rest],verbosity=2)
