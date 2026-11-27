#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pass M81 firmware adapter values through the power supply sysfs and uevent code."""
import argparse
from pathlib import Path
import sys
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_test import function, run_c, write_report


def declaration(source, start):
    return source[source.index(start):].split('\n};', 1)[0] + '\n};\n'


def check(source):
    base = source / 'drivers/power/supply'
    driver = (base / 'qcom_battmgr.c').read_text()
    sysfs = (base / 'power_supply_sysfs.c').read_text()
    header = (source / 'include/linux/power_supply.h').read_text()
    descriptor = declaration(driver, 'static const struct power_supply_desc sm8350_usb_psy_desc')
    assert '.get_property = qcom_battmgr_usb_get_property,' in descriptor
    supported = re.search(r'\.usb_types = ([^,]+),', descriptor)[1]
    wire = declaration(driver, 'static const u8 sm8350_usb_prop_map[]')
    constants = '\n'.join(re.findall(r'^#define (?:USB_(?:ONLINE|VOLT_NOW|VOLT_MAX|CURR_NOW|CURR_MAX|INPUT_CURR_LIMIT|ADAP_TYPE|TYPE)|BATTMGR_USB_PROPERTY_GET)\s+\S+', driver, re.M))
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
typedef uint32_t u32;
typedef uint8_t u8;
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
''' + declaration(header, 'enum power_supply_property {') + declaration(header, 'enum power_supply_usb_type {')
    code += constants + '\n' + wire + declaration(sysfs, 'static const char * const POWER_SUPPLY_USB_TYPE_TEXT')
    code += r'''
enum { QCOM_BATTMGR_SM8475, QCOM_BATTMGR_SM8350, QCOM_BATTMGR_SC8280XP, QCOM_BATTMGR_X1E80100 };
struct qcom_battmgr {
    int variant; bool service_up;
    struct { u32 usb_type; int online, voltage_now, voltage_max, current_now, current_max, current_limit; } usb;
};
struct power_supply_desc { unsigned int usb_types; int type; };
struct power_supply { struct power_supply_desc *desc; struct qcom_battmgr *battmgr; };
struct device { struct power_supply *psy; };
union power_supply_propval { int intval; const char *strval; };
struct device_attribute { enum power_supply_property psp; };
struct power_supply_attr {
    struct device_attribute dev_attr; const char *prop_name;
    int text_values_len; const char * const *text_values;
};
struct kobj_uevent_env { char entry[512]; };
static struct power_supply_attr power_supply_attrs[] = {
    [POWER_SUPPLY_PROP_USB_TYPE] = {.dev_attr.psp = POWER_SUPPLY_PROP_USB_TYPE, .prop_name = "USB_TYPE"}
};
static u32 adapter;
static int service_error;
#define power_supply_get_drvdata(p) ((p)->battmgr)
#define mutex_lock(l) ((void)0)
#define mutex_unlock(l) ((void)0)
static int qcom_battmgr_request_property(struct qcom_battmgr *b, int opcode, int prop, int value) {
    assert(opcode == BATTMGR_USB_PROPERTY_GET && !value);
    assert(prop == (b->variant == QCOM_BATTMGR_SM8475 ? USB_ADAP_TYPE : USB_TYPE));
    if (!service_error) b->usb.usb_type = adapter;
    return service_error;
}
#define qcom_battmgr_bat_sc8280xp_update(...) (-EINVAL)
#define dev_to_psy(d) ((d)->psy)
#define dev_attr_psp(a) ((a)->psp)
#define to_ps_attr(a) (&power_supply_attrs[(a)->psp])
#define power_supply_get_property qcom_battmgr_usb_get_property
#define power_supply_show_charge_behaviour(...) (-EINVAL)
#define power_supply_show_charge_types(...) (-EINVAL)
#define dev_warn(d, ...) ((void)(d))
#define dev_dbg_ratelimited(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define sysfs_emit(buf, ...) snprintf(buf, 512, __VA_ARGS__)
#define sysfs_emit_at(buf, n, ...) snprintf((buf) + (n), 512 - (n), __VA_ARGS__)
#define add_uevent_var(env, ...) (snprintf((env)->entry, sizeof((env)->entry), __VA_ARGS__) < 0 ? -EIO : 0)
#define strscpy(buf, src, size) snprintf(buf, size, "%s", src)
static void strreplace(char *text, char from, char to) { for (; *text; text++) if (*text == from) *text = to; }
'''
    for name in ('qcom_battmgr_usb_sm8350_update', 'qcom_battmgr_sm8475_usb_type', 'qcom_battmgr_usb_get_property'):
        code += function(driver, name) + '\n'
    for name in ('power_supply_escape_spaces', 'power_supply_show_enum_with_available',
                 'power_supply_format_property', 'power_supply_show_property', 'add_prop_uevent'):
        code += function(sysfs, name) + '\n'
    code += 'static struct power_supply_desc desc = {.usb_types = ' + supported + '};\n'
    code += r'''
int main(void) {
    const struct { u32 raw; const char *label; } cases[] = {
        {0,"Unknown"}, {1,"SDP"}, {2,"DCP"}, {3,"CDP"}, {4,"ACA"}, {5,"C"}, {6,"PD"},
        {7,"PD_DRP"}, {8,"PD_PPS"}, {9,"BrickID"}, {10,"Unknown"}, {11,"Unknown"},
        {0x7f,"Unknown"}, {0x80,"DCP"}, {0x81,"DCP"}, {0x82,"DCP"},
        {0x83,"Unknown"}, {0x84,"Unknown"}, {0x85,"Unknown"}, {UINT32_MAX,"Unknown"}
    };
    struct qcom_battmgr b = {.variant = QCOM_BATTMGR_SM8475, .service_up = true};
    struct power_supply psy = {.desc = &desc, .battmgr = &b};
    struct device dev = {.psy = &psy};
    struct device_attribute *attr = &power_supply_attrs[POWER_SUPPLY_PROP_USB_TYPE].dev_attr;
    char buf[512];
    for (size_t i = 0; i < ARRAY_SIZE(cases); i++) {
        struct kobj_uevent_env env = {0};
        char active[32];
        snprintf(active, sizeof(active), "[%s]", cases[i].label);
        adapter = cases[i].raw;
        assert(power_supply_show_property(&dev, attr, buf) > 0);
        assert(strstr(buf, active) && b.usb.usb_type == adapter);
        assert(!add_prop_uevent(&dev, &env, POWER_SUPPLY_PROP_USB_TYPE, buf));
        assert(!strncmp(env.entry, "POWER_SUPPLY_USB_TYPE=", strlen("POWER_SUPPLY_USB_TYPE=")));
        assert(strstr(env.entry, active) && b.usb.usb_type == adapter);
    }
    b.variant = QCOM_BATTMGR_SM8350; adapter = 0x84;
    union power_supply_propval val;
    assert(!qcom_battmgr_usb_get_property(&psy, POWER_SUPPLY_PROP_USB_TYPE, &val) && val.intval == 0x84);
    b.variant = QCOM_BATTMGR_SM8475; service_error = -ETIMEDOUT;
    assert(power_supply_show_property(&dev, attr, buf) == -ETIMEDOUT);
    b.service_up = false;
    assert(power_supply_show_property(&dev, attr, buf) == -EAGAIN);
}
'''
    run_c(code)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    check(args.source)
    write_report(['20 wire values through the class attribute and its uevent field',
                  'raw cache, SM8350 passthrough and service failures'], args.report)
