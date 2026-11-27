#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise M81 panel rail ownership and compare DT supplies with the retail ROM."""
import argparse
import hashlib
from pathlib import Path
import subprocess

from kernel_test import function, run_c, write_report


def sequence(source):
    panel = (source / 'drivers/gpu/drm/panel/panel-novatek-nt36532.c').read_text()
    names = panel[panel.index('#define NT36532_COMMON_SUPPLIES'):panel.index('struct nt36532 {')]
    mask = next(line for line in panel.splitlines() if line.startswith('#define LIUQIN_ALL_VOTES'))
    functions = ('nt36532_get_supplies', 'liuqin_release_supply_votes', 'nt36532_enable_supplies',
                 'nt36532_disable_supplies', 'liuqin_release_boot_supplies', 'liuqin_hold_boot_supplies')
    run_c(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <string.h>
#define BIT(n) (1U << (n))
#define GENMASK_U32(h,l) ((BIT((h)+1)-1) & ~(BIT(l)-1))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define EPROBE_DEFER 517
#define IS_ERR(p) ((intptr_t)(p)<0)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define dev_err_probe(d,e,...) (e)
#define dev_warn(...) ((void)0)
#define liuqin_log_supplies(...) ((void)0)
struct regulator { int index, refs, inherited; };
struct regulator_bulk_data { const char *supply; struct regulator *consumer; };
struct device { int requests, missing, error; };
static int liuqin_desc, other_desc;
struct nt36532 {
    int *desc, *reset_gpio;
    unsigned int supply_votes;
    struct regulator_bulk_data supplies[5];
};
static struct regulator rails[5];
static const char * const expected_names[]={"vddio","dvddbuck","dvddldo","vsp","vsn"};
static struct regulator *devm_regulator_get_optional(struct device *dev,const char *name) {
    int i=dev->requests++;
    assert(i<5 && !strcmp(name,expected_names[i]));
    return i==dev->missing ? (void *)(intptr_t)dev->error : &rails[i];
}
static int devm_regulator_bulk_get(struct device *dev,int count,struct regulator_bulk_data *s) {
    assert(count==3 && !dev->requests);
    for (int i=0;i<count;i++) assert(!strcmp(s[i].supply,expected_names[i]));
    dev->requests=count; return dev->error;
}
static int physical_reset, fail_on, fail_off, bulk, cursor;
static char trace[64];
static void event(char c) { assert(cursor < (int)sizeof(trace)-1); trace[cursor++]=c; trace[cursor]=0; }
static int regulator_enable(struct regulator *r) {
    assert(!r->refs); event('A'+r->index);
    if (r->index == fail_on) return -EIO;
    r->refs++; return 0;
}
static int regulator_disable(struct regulator *r) {
    assert(r->refs==1); event('a'+r->index);
    if (r->index == fail_off) return -EBUSY;
    r->refs--; return 0;
}
static int regulator_is_enabled(struct regulator *r) { return r->inherited; }
static int regulator_bulk_enable(unsigned int n, struct regulator_bulk_data *s) {
    assert(n==3 && s); bulk++; return 0;
}
static int regulator_bulk_disable(unsigned int n, struct regulator_bulk_data *s) {
    assert(n==3 && s); bulk--; return 0;
}
static void usleep_range(int lo, int hi) {
    if (lo==2000 && hi==2010) event('.');
    else { assert(lo==10000 && hi==11000); event('T'); }
}
static void gpiod_set_value_cansleep(int *pin, int value) {
    assert(pin==&physical_reset && value==1); *pin=0; event('R');
}
''' + names + mask + '\n' + '\n'.join(function(panel, name) for name in functions) + r'''
static struct nt36532 setup(void) {
    struct nt36532 ctx = {.desc=&liuqin_desc, .reset_gpio=&physical_reset};
    for (int i=0;i<5;i++) {
        rails[i]=(struct regulator){.index=i,.inherited=1};
        ctx.supplies[i].consumer=&rails[i];
    }
    physical_reset=1; fail_on=fail_off=-1; cursor=0; trace[0]=0;
    return ctx;
}
static void votes(struct nt36532 *ctx, unsigned int expected) {
    assert(ctx->supply_votes==expected);
    for (unsigned int i=0;i<ARRAY_SIZE(ctx->supplies);i++) assert(rails[i].refs==!!(expected & BIT(i)));
}
int main(void) {
    struct nt36532 ctx=setup();
    struct device device={.missing=-1};
    assert(!nt36532_get_supplies(&ctx,&device) && device.requests==5);
    assert(LIUQIN_ALL_VOTES==31);
    assert(!liuqin_hold_boot_supplies(&ctx) && physical_reset);
    assert(!strcmp(trace,"A.BC.D.E.")); votes(&ctx,31);
    assert(!nt36532_enable_supplies(&ctx) && !strcmp(trace,"A.BC.D.E."));
    assert(!nt36532_disable_supplies(&ctx)); votes(&ctx,0);
    assert(!strcmp(trace,"A.BC.D.E.e.d.cb.a."));
    ctx=setup(); rails[0].inherited=-EINVAL; /* Unknown RPMh state at probe. */
    assert(!liuqin_hold_boot_supplies(&ctx) && physical_reset && !strcmp(trace,"A.BC.D.E.T"));
    for (int i=0;i<5;i++) {
        ctx=setup();
        for (int e=0;e<2;e++) {
            device=(struct device){.missing=i,.error=e ? -EPROBE_DEFER : -ENODEV};
            assert(nt36532_get_supplies(&ctx,&device)==device.error && device.requests==i+1);
            assert(!ctx.supply_votes && !cursor);
        }
        ctx=setup(); rails[i].inherited=0;
        assert(!liuqin_hold_boot_supplies(&ctx) && !physical_reset);
        assert(!strcmp(trace,"RA.BC.D.E.T")); votes(&ctx,31);
        liuqin_release_boot_supplies(&ctx); votes(&ctx,0);
        ctx=setup(); fail_on=i;
        assert(nt36532_enable_supplies(&ctx)==-EIO); votes(&ctx,0);
        const char *rollback[]={"A","A.Ba.","A.BCb.a.","A.BC.Dcb.a.","A.BC.D.Ed.cb.a."};
        assert(!strcmp(trace,rollback[i]));
        fail_on=-1;
        assert(!nt36532_enable_supplies(&ctx)); fail_off=i;
        assert(nt36532_disable_supplies(&ctx)==-EBUSY); votes(&ctx,BIT(i+1)-1);
        fail_off=-1; assert(!nt36532_disable_supplies(&ctx)); votes(&ctx,0);
    }
    ctx=setup(); rails[0].refs=1; ctx.supply_votes=1; fail_on=4; fail_off=2;
    assert(nt36532_enable_supplies(&ctx)==-EIO); votes(&ctx,7);
    assert(!strcmp(trace,"BC.D.Ed.c"));
    fail_on=fail_off=-1; assert(!nt36532_enable_supplies(&ctx)); votes(&ctx,31);
    liuqin_release_boot_supplies(&ctx); votes(&ctx,0);
    ctx=setup(); ctx.desc=&other_desc; device=(struct device){0};
    assert(!nt36532_get_supplies(&ctx,&device) && device.requests==3);
    assert(!nt36532_enable_supplies(&ctx) && bulk==1);
    assert(!nt36532_disable_supplies(&ctx) && !bulk && !cursor);
}
''')


def get(dtb, option, node, *properties):
    return subprocess.check_output(['fdtget', option, dtb, node, *properties], text=True).strip()


def check_board(dtb):
    panel = '/soc@0/display-subsystem@ae00000/dsi@ae94000/panel@0'
    fb = '/chosen/framebuffer@b8000000'
    backlight = '/soc@0/geniqup@9c0000/i2c@984000/backlight@11'
    properties = get(dtb, '-p', backlight).split()
    assert 'kinetic,external-bias-control' in properties
    assert all(name not in properties for name in ('vddpos-supply', 'vddneg-supply'))
    rails = (('vddio', '/soc@0/rsc@17a00000/regulators-1/ldo2', None),
             ('dvddbuck', '/display-panel-dvdd-buck', 2), ('dvddldo', '/display-panel-dvdd-ldo', 93),
             ('vsp', '/backlight-gpio-reg-vsp', 66), ('vsn', '/backlight-gpio-reg-vsn', 53))
    for name, node, gpio in rails:
        phandle = get(dtb, '-tu', node, 'phandle')
        assert get(dtb, '-tu', panel, name + '-supply') == phandle
        assert get(dtb, '-tu', fb, ('iovdd' if name == 'vddio' else name) + '-supply') == phandle
        if gpio is not None:
            assert int(get(dtb, '-tu', node, 'gpio').split()[1]) == gpio
            properties = get(dtb, '-p', node).split()
            assert 'regulator-boot-on' in properties and 'regulator-always-on' not in properties


def check_stock(dtbo):
    assert hashlib.sha256(dtbo.read_bytes()).hexdigest() == '295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87'
    table = '/fragment@89/__overlay__/dsi_panel_pwr_supply_M81'
    phandle = get(dtbo, '-tu', table, 'phandle')
    for fragment, variant in ((87, '36_02_0a'), (88, '42_02_0b')):
        panel = f'/fragment@{fragment}/__overlay__/qcom,mdss_dsi_m81_{variant}_dualdsi_dsc_lcd_video'
        assert get(dtbo, '-tu', panel, 'qcom,panel-supply-entries') == phandle
    expected = [('iovdd', 1800000, 2, 2), ('dvddbuck', 1300000, 0, 2), ('dvddldo', 1300000, 2, 0),
                ('vsp', 5500000, 2, 2), ('vsn', 5500000, 2, 2)]
    actual = []
    for child in get(dtbo, '-l', table).split():
        node = table + '/' + child
        props = get(dtbo, '-p', node).split()
        name = get(dtbo, '-ts', node, 'qcom,supply-name')
        voltage = int(get(dtbo, '-tu', node, 'qcom,supply-min-voltage'))
        assert voltage == int(get(dtbo, '-tu', node, 'qcom,supply-max-voltage'))
        delays = [int(get(dtbo, '-tu', node, k)) if k in props else 0
                  for k in ('qcom,supply-post-on-sleep', 'qcom,supply-post-off-sleep')]
        actual.append((name, voltage, *delays))
    assert actual == expected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--dtb', type=Path, action='append', default=[])
    parser.add_argument('--stock-overlay', type=Path, help='Pinned M81 dtbo.img entry 15')
    args = parser.parse_args()
    sequence(args.source)
    checks = ['five-rail sequence and waits', 'boot handover and cold reset', 'failure rollback and retry',
              'all M81 providers required', 'three generic supplies on Pipa and Yudi']
    for dtb in args.dtb:
        check_board(dtb)
        checks.append(str(dtb))
    if args.stock_overlay:
        check_stock(args.stock_overlay)
        checks.append('both retail M81 panel supply tables')
    write_report(checks)
