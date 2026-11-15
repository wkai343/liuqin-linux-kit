#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute DWC3 initial-role policy and the actual Qualcomm probe role block."""
import argparse
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
enum usb_role { USB_ROLE_NONE, USB_ROLE_HOST, USB_ROLE_DEVICE };
enum { USB_DR_MODE_UNKNOWN, USB_DR_MODE_HOST, USB_DR_MODE_PERIPHERAL, USB_DR_MODE_OTG };
#define DWC3_GCTL_PRTCAP_HOST 1
#define DWC3_GCTL_PRTCAP_DEVICE 2
#define IS_ERR(p) false
#define PTR_ERR(p) (-EIO)
#define dev_err(...) ((void)0)
struct device { int default_mode; bool role_switch; void *of_node; };
struct usb_role_switch { void *data; };
struct usb_role_switch_desc {
 void *fwnode, *driver_data; bool allow_userspace_control;
 int (*set)(struct usb_role_switch *, enum usb_role);
 enum usb_role (*get)(struct usb_role_switch *);
};
struct dwc3 { struct device *dev; int role_switch_default_mode; struct usb_role_switch *role_sw; };
struct dwc3_qcom { int mode; enum usb_role current_role; };
static bool board;
static int modes[8], count, vbus;
static enum usb_role notified;
static struct usb_role_switch sw;
static bool liuqin_board_quirks(void) { return board; }
static int usb_get_role_switch_default_mode(struct device *dev) { return dev->default_mode; }
static bool device_property_read_bool(struct device *dev, const char *name) {
 assert(!strcmp(name,"usb-role-switch")); return dev->role_switch;
}
static void *usb_role_switch_get_drvdata(struct usb_role_switch *s) { return s->data; }
static void *dev_fwnode(struct device *dev) { return dev; }
static void dwc3_set_mode(struct dwc3 *dwc, u32 mode) { assert(count<8); modes[count++]=mode; }
static void dwc3_pre_set_role(struct dwc3 *dwc, enum usb_role role) { notified=role; }
static enum usb_role dwc3_usb_role_switch_get(struct usb_role_switch *s) { return USB_ROLE_NONE; }
static struct usb_role_switch *usb_role_switch_register(struct device *dev, struct usb_role_switch_desc *desc) {
 assert(desc->set && desc->get && desc->allow_userspace_control && desc->fwnode==dev);
 sw.data=desc->driver_data; return &sw;
}
static void usb_role_switch_unregister(struct usb_role_switch *s) { }
static int devm_of_platform_populate(struct device *dev) { return 0; }
static void dwc3_qcom_vbus_override_enable(struct dwc3_qcom *qcom, bool enable) { vbus=enable; }
'''


@unittest.skipUnless(args.source, '--source enables driver initial-role execution')
class Tests(unittest.TestCase):
    def code(self):
        drd = (args.source / 'drivers/usb/dwc3/drd.c').read_text()
        qcom = (args.source / 'drivers/usb/dwc3/dwc3-qcom.c').read_text()
        # Execute the production probe's role selection, without probing hardware.
        start = qcom.index('\tif (qcom->mode == USB_DR_MODE_HOST) {')
        end = qcom.index('\tqcom->dwc.glue_ops =', start)
        return (common.COMMON + FIXTURE + common.function(drd, 'dwc3_usb_role_switch_set') +
                common.function(drd, 'dwc3_setup_role_switch') +
                '\nstatic void glue_initial_role(struct device *dev, struct dwc3_qcom *qcom) {\n' +
                qcom[start:end] + '}\n')

    def test_liuqin_waits_in_device_mode_but_preserves_later_host_fallback(self):
        common.execute(self.code() + r'''
int main(void) {
 board=true; struct device dev={.default_mode=USB_DR_MODE_HOST,.role_switch=true};
 struct dwc3 dwc={.dev=&dev}; struct dwc3_qcom q={.mode=USB_DR_MODE_OTG};
 glue_initial_role(&dev,&q); assert(q.current_role==USB_ROLE_DEVICE && !vbus);
 assert(!dwc3_setup_role_switch(&dwc));
 assert(count==1 && modes[0]==DWC3_GCTL_PRTCAP_DEVICE && dwc.role_switch_default_mode==USB_DR_MODE_HOST);
 /* UCSI's first role follows its orientation hook; missing partner flags use NONE. */
 assert(!dwc3_usb_role_switch_set(&sw,USB_ROLE_NONE));
 assert(modes[1]==DWC3_GCTL_PRTCAP_HOST && notified==USB_ROLE_NONE);
 assert(!dwc3_usb_role_switch_set(&sw,USB_ROLE_DEVICE));
 assert(modes[2]==DWC3_GCTL_PRTCAP_DEVICE && notified==USB_ROLE_DEVICE);
 assert(!dwc3_usb_role_switch_set(&sw,USB_ROLE_HOST));
 assert(modes[3]==DWC3_GCTL_PRTCAP_HOST && notified==USB_ROLE_HOST); return 0;
}
''')

    def test_other_boards_and_peripheral_defaults_keep_their_initial_role(self):
        common.execute(self.code() + r'''
int main(void) {
 struct device dev={.default_mode=USB_DR_MODE_HOST,.role_switch=true};
 struct dwc3 dwc={.dev=&dev}; struct dwc3_qcom q={.mode=USB_DR_MODE_OTG};
 glue_initial_role(&dev,&q); assert(q.current_role==USB_ROLE_HOST);
 assert(!dwc3_setup_role_switch(&dwc)); assert(modes[0]==DWC3_GCTL_PRTCAP_HOST);
 for (int b=0;b<2;b++) {
  board=b; dev.default_mode=USB_DR_MODE_PERIPHERAL;
  glue_initial_role(&dev,&q); assert(q.current_role==USB_ROLE_DEVICE);
  assert(!dwc3_setup_role_switch(&dwc)); assert(modes[1+b]==DWC3_GCTL_PRTCAP_DEVICE);
 }
 assert(!dwc3_usb_role_switch_set(&sw,USB_ROLE_NONE));
 assert(modes[3]==DWC3_GCTL_PRTCAP_DEVICE); return 0;
}
''')

    def test_fixed_host_and_peripheral_glue_are_unchanged(self):
        common.execute(self.code() + r'''
int main(void) {
 struct device dev={.default_mode=USB_DR_MODE_HOST,.role_switch=true};
 struct dwc3_qcom q;
 for (int b=0;b<2;b++) {
  board=b; q.mode=USB_DR_MODE_HOST; vbus=0; glue_initial_role(&dev,&q);
  assert(q.current_role==USB_ROLE_HOST && !vbus);
  q.mode=USB_DR_MODE_PERIPHERAL; glue_initial_role(&dev,&q);
  assert(q.current_role==USB_ROLE_DEVICE && vbus);
 }
 return 0;
}
''')


if __name__ == '__main__':
    unittest.main(argv=[__file__, *rest])
