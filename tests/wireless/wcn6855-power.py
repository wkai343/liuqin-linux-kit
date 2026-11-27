#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check Liuqin WCN6855 bootstrap votes, independent retention and stock wiring."""
import argparse
from pathlib import Path
import sys
import re
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_test import function, run_c, write_report


# Initial, minimum and maximum requests (uV) from the M81 production DT.
RAILS = {
    'pm8350_s11': (952000, 382000, 1170000),
    'pm8350_s12': (1256000, 1224000, 2040000),
    'pm8350c_s1': (1880000, 1800000, 2024000),
    'pmr735a_s2': (500000, 500000, 1040000),
}
# Each independent consumer, its physical rail and OEM bootstrap range (uV).
SUPPLIES = {
    'bt-io': ('pm8350_s10', 1800000, 1800000),
    'bt-dig': ('pm8350_s11', 966000, 1170000),
    'bt-rfa1': ('pm8350c_s1', 1900000, 2040000),
    'bt-rfa2': ('pm8350_s12', 1350000, 2024000),
    'bt-asd': ('pmr735a_l7', 2800000, 2800000),
    'wlan-aon': ('pmr735a_s2', 1012000, 1040000),
    'wlan-dig': ('pm8350_s11', 966000, 1170000),
    'wlan-io': ('pm8350_s10', 1800000, 1800000),
    'wlan-rfa1': ('pm8350c_s1', 1900000, 2024000),
    'wlan-rfa2': ('pm8350_s12', 1350000, 2040000),
    'wlan-ant-switch': ('pmr735a_l7', 2800000, 2800000),
}
VOLTAGES = ('qcom,init-voltage', 'regulator-min-microvolt', 'regulator-max-microvolt')


def check_stock(dtb):
    def get(node, prop, strings=False):
        data = subprocess.check_output(['fdtget', '-t', 's' if strings else 'u',
                                        dtb, node, prop], text=True).strip()
        return data if strings else tuple(map(int, data.split()))

    paths = {label: get('/__symbols__', label, True) for label, _, _ in SUPPLIES.values()}
    for label, values in RAILS.items():
        assert tuple(get(paths[label], p)[0] for p in VOLTAGES) == values, label
        parent = str(Path(paths[label]).parent)
        properties = subprocess.check_output(['fdtget', '-p', dtb, parent], text=True).split()
        assert 'qcom,send-defaults' not in properties
    for name, (rail, low, high) in SUPPLIES.items():
        if name.startswith('bt-'):
            node = '/soc/bt_qca6490'
            names = ['bt-vdd-' + name[3:]]
            if name == 'bt-dig':
                names += ['bt-vdd-aon', 'bt-vdd-rfacmn', 'bt-vdd-rfa-0p8']
            prefix, flags = 'qcom,', (0, 1)
        else:
            node = '/soc/qcom,cnss-qca6490@b0000000'
            names = [name if name == 'wlan-ant-switch' else 'vdd-' + name]
            prefix, flags = '', (0, 0, 1)
        for prop in names:
            assert get(node, prefix + prop + '-supply') == get(paths[rail], 'phandle'), prop
            assert get(node, 'qcom,' + prop + '-config') == (low, high, *flags), prop


def check_board(source, wcn):
    dts = (source/'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts').read_text()
    for rail, values in RAILS.items():
        node = re.search(rf'\b{rail}: \w+ \{{([^}}]*)', dts)[1]
        assert tuple(int(re.search(rf'{key} = <(\d+)>;', node)[1])
                     for key in VOLTAGES) == values, rail
    pmu = dts.split('wcn6855-pmu {', 1)[1].split('regulators {', 1)[0]
    assert '"xiaomi,liuqin-wcn6855-pmu", "qcom,wcn6855-pmu"' in pmu
    for name, (rail, low, high) in SUPPLIES.items():
        assert f'{name}-supply = <&{rail}>;' in pmu, name
        assert f'{{ "{name}", {low}, {high} }}' in wcn, name
    rpmh = (source/'drivers/regulator/qcom-rpmh-regulator.c').read_text()
    init = function(rpmh, 'rpmh_regulator_init_vreg')
    assert 'liuqin_board_quirks() && of_property_present(node, "qcom,init-voltage")' in init
    assert 'range->step = 1000;' in init and 'range->min = c->min_uV;' in init
    assert 'range->max_sel = (c->max_uV - c->min_uV) / range->step;' in init
    assert init.index('vreg->voltage_selector = (uv - range->min) / range->step;') < init.index('devm_regulator_register(')
    assert 'uv < c->min_uV || uv > c->max_uV' in init
    handoff = (source/'drivers/power/sequencing/core.c').read_text()
    state = function(handoff, 'pwrseq_set_retention')
    assert state.index('guard(rwsem_read)') < state.index('guard(mutex)') < state.index('unit->enable_count')
    assert 'return retain ? 0 : -EHOSTDOWN;' in state
    bt = function((source/'drivers/bluetooth/hci_qca.c').read_text(), 'qca_setup')
    assert (bt.index('qca_power_on(hdev)') < bt.index('pwrseq_set_retention(fw_pwrseq, false)')
            < bt.index('qca_uart_setup(') < bt.index('if (!ret)', bt.index('qca_uart_setup('))
            < bt.index('pwrseq_set_retention(fw_pwrseq, true)') < bt.index('clear_bit(QCA_IBS_DISABLED'))
    ath = source/'drivers/net/wireless/ath/ath11k'
    up = function((ath/'pci.c').read_text(), 'ath11k_pci_power_up')
    assert up.index('ath11k_wcn_set_retention(ab, false)') < up.index('ath11k_pci_sw_reset(') < up.index('ath11k_mhi_start(')
    ready = function((ath/'core.c').read_text(), 'ath11k_core_qmi_firmware_ready')
    assert ready.index('ath11k_core_pdev_create(') < ready.index('ath11k_wcn_set_retention(ab, true)')
    qmi = function((ath/'qmi.c').read_text(), 'ath11k_qmi_driver_event_work')
    assert re.search(r'ret = ath11k_qmi_process_coldboot_calibration\(ab\);\s*'
                     r'if \(!ret && ath11k_wcn_set_retention\(ab, true\)\)', qmi)


def check_rpmh(source):
    rpmh = (source/'drivers/regulator/qcom-rpmh-regulator.c').read_text()
    functions = '\n'.join(function(rpmh, name) for name in (
        '_rpmh_regulator_vrm_set_voltage_sel', 'rpmh_regulator_vrm_set_voltage_sel',
        'rpmh_regulator_set_enable_state'))
    run_c(r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#define RPMH_REGULATOR_REG_VRM_VOLTAGE 0
#define RPMH_REGULATOR_REG_ENABLE 4
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
struct rpmh_vreg { int addr, min_uv, voltage_selector, enabled; };
struct regulator_dev { struct rpmh_vreg *vreg; };
struct tcs_cmd { int addr, data; };
static struct tcs_cmd messages[3];
static unsigned int count;
static bool fail;
#define rdev_get_drvdata(rdev) ((rdev)->vreg)
static int regulator_list_voltage_linear_range(struct regulator_dev *rdev, unsigned int sel)
{
    return rdev->vreg->min_uv + sel * 1000;
}
static int rpmh_regulator_send_request(struct rpmh_vreg *vreg, struct tcs_cmd *cmd, bool wait)
{
    assert(cmd->addr == vreg->addr || cmd->addr == vreg->addr + RPMH_REGULATOR_REG_ENABLE);
    assert(wait && count < 3);
    if (fail)
        return -EIO;
    messages[count++] = *cmd;
    return 0;
}
""" + functions + r"""
int main(void)
{
    const int rails[][3] = {{382, 952, 966}, {1224, 1256, 1350},
                            {1800, 1880, 1900}, {500, 500, 1012}};
    for (unsigned int i = 0; i < sizeof(rails)/sizeof(rails[0]); i++) {
        const int *r = rails[i];
        for (int vote_before_enable = 0; vote_before_enable < 2; vote_before_enable++) {
            struct rpmh_vreg v = {.addr = 0x40000, .min_uv = r[0] * 1000,
                                  .voltage_selector = r[1] - r[0], .enabled = -EINVAL};
            struct regulator_dev dev = {&v};
            count = 0;
            if (vote_before_enable)
                assert(!rpmh_regulator_vrm_set_voltage_sel(&dev, r[2] - r[0]));
            assert(!count);
            fail = true;
            assert(rpmh_regulator_set_enable_state(&dev, true) == -EIO);
            assert(!count && v.enabled == -EINVAL);
            fail = false;
            assert(!rpmh_regulator_set_enable_state(&dev, true));
            assert(count == 2 && messages[0].addr == v.addr);
            assert(messages[0].data == (vote_before_enable ? r[2] : r[1]));
            assert(messages[1].data == 1 && messages[1].addr == v.addr + 4);
        }
    }
    return 0;
}
""", flags=('-Wno-sign-compare',))



def check_votes(wcn):
    structs = re.search(r'struct liuqin_wcn_supply \{.*?struct liuqin_wcn_radio \{.*?\n};', wcn, re.S)[0]
    levels = re.search(r'static const struct liuqin_wcn_supply liuqin_bt_supplies\[\].*?'
                       r'liuqin_wlan_supplies\[\].*?\n};', wcn, re.S)[0]
    functions = '\n'.join(function(wcn, name) for name in (
        'liuqin_wcn_vote_one', 'liuqin_wcn_vote', 'liuqin_wcn_power_on', 'liuqin_wcn_power_off'))
    run_c(r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#define pr_err(...) fprintf(stderr, __VA_ARGS__)
#define COUNT(a) (sizeof(a) / sizeof((a)[0]))
struct regulator { int min, max, enables; };
struct regulator_bulk_data { struct regulator *consumer; };
static struct regulator *fail_vote;
static bool fail_enable, fail_disable;
static int regulator_set_voltage(struct regulator *reg, int min, int max)
{
    if (reg == fail_vote) {
        fail_vote = NULL;
        return -EIO;
    }
    reg->min = min;
    reg->max = max;
    return 0;
}
static int regulator_bulk_enable(unsigned int count, struct regulator_bulk_data *regs)
{
    if (fail_enable)
        return -EIO;
    for (unsigned int i = 0; i < count; i++) {
        assert(regs[i].consumer->enables++ == 0);
        assert(regs[i].consumer->min > 0);
    }
    return 0;
}
static int regulator_bulk_disable(unsigned int count, struct regulator_bulk_data *regs)
{
    if (fail_disable)
        return -EIO;
    for (unsigned int i = 0; i < count; i++)
        assert(regs[i].consumer->enables-- == 1);
    return 0;
}
""" + structs + levels + functions + r"""
static void expect(struct liuqin_wcn_radio *radio, bool boot, int enables)
{
    assert(radio->boot_vote == boot);
    for (unsigned int i = 0; i < radio->count; i++) {
        struct regulator *reg = radio->regs[i].consumer;
        assert(reg->min == (boot ? radio->levels[i].min_uv : 0));
        assert(reg->max == radio->levels[i].max_uv && reg->enables == enables);
    }
}
static void check_failures(struct liuqin_wcn_radio *radio)
{
    assert(!liuqin_wcn_vote(radio, false));
    for (unsigned int i = 0; i < radio->count; i++) {
        fail_vote = radio->regs[i].consumer;
        assert(liuqin_wcn_power_on(radio) == -EIO);
        expect(radio, false, 0);
        fail_enable = true;
        assert(liuqin_wcn_power_on(radio) == -EIO);
        fail_enable = false;
        expect(radio, false, 0);
        assert(!liuqin_wcn_power_on(radio));
        fail_vote = radio->regs[i].consumer;
        assert(liuqin_wcn_vote(radio, false) == -EIO);
        expect(radio, true, 1);
        fail_disable = true;
        assert(liuqin_wcn_power_off(radio) == -EIO);
        fail_disable = false;
        expect(radio, true, 1);
        fail_vote = radio->regs[i].consumer;
        assert(liuqin_wcn_power_off(radio) == -EIO);
        expect(radio, true, 1);
        assert(!liuqin_wcn_power_off(radio));
        expect(radio, false, 0);
    }
}
int main(void)
{
    struct regulator storage[COUNT(liuqin_bt_supplies) + COUNT(liuqin_wlan_supplies)] = {};
    struct regulator_bulk_data regs[COUNT(storage)];
    struct liuqin_wcn_radio bt = {liuqin_bt_supplies, regs, COUNT(liuqin_bt_supplies), false};
    struct liuqin_wcn_radio wlan = {liuqin_wlan_supplies, regs + bt.count, COUNT(liuqin_wlan_supplies), false};
    struct liuqin_wcn_radio other_board = {};
    for (size_t i = 0; i < COUNT(storage); i++)
        regs[i].consumer = &storage[i];
    check_failures(&bt);
    check_failures(&wlan);
    assert(!liuqin_wcn_power_on(&other_board) && !liuqin_wcn_power_off(&other_board));
    assert(!liuqin_wcn_power_on(&bt) && !liuqin_wcn_power_on(&wlan));
    assert(!liuqin_wcn_vote(&bt, false));
    expect(&wlan, true, 1);
    expect(&bt, false, 1);
    assert(!liuqin_wcn_vote(&bt, true) && !liuqin_wcn_vote(&wlan, false));
    expect(&bt, true, 1);
    expect(&wlan, false, 1);
    assert(!liuqin_wcn_power_off(&bt) && !liuqin_wcn_power_off(&wlan));
    return 0;
}
""")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--stock-dtb', type=Path, help='M81 production base DTB for direct comparison')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    sequencer = (args.source/'drivers/power/sequencing/pwrseq-qcom-wcn.c').read_text()
    check_board(args.source, sequencer)
    check_votes(sequencer)
    check_rpmh(args.source)
    checks = ['M81 supply wiring, initial selector and firmware handoff ordering',
              'first RPMh enable sends cached OEM millivolts or the newer consumer vote',
              'independent BT/WLAN bootstrap votes, retained enables and warm restarts',
              'each consumer failure, bulk enable/disable failure, rollback and retry']
    if args.stock_dtb:
        check_stock(args.stock_dtb)
        checks.append('production DTB voltage ranges, physical phandles and retention flags')
    write_report(checks, args.report)
