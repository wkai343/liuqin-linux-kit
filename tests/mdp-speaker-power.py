#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the Cape MDP and speaker I2C performance dependencies in a built DTB."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def check(dtb):
    def cells(node, prop):
        result = subprocess.check_output(['fdtget', '-t', 'u', dtb, node, prop], text=True)
        return tuple(map(int, result.split()))

    def children(node):
        return subprocess.check_output(['fdtget', '-l', dtb, node], text=True).split()

    rpmh = '/soc@0/rsc@17a00000/power-controller'
    provider, = cells(rpmh, 'phandle')
    table = rpmh + '/opp-table'
    # Resolve phandles by OPP level since DTC can renumber them freely.
    corners = {}
    for name in children(table):
        node = f'{table}/{name}'
        if 'phandle' in subprocess.check_output(['fdtget', '-p', dtb, node], text=True).split():
            level, = cells(node, 'opp-level')
            corners[level], = cells(node, 'phandle')

    mdp = '/soc@0/display-subsystem@ae00000/display-controller@ae01000'
    assert cells(mdp, 'power-domains') == (provider, 6, provider, 8), 'MDP needs MMCX and MX'
    table = mdp + '/opp-table'
    assert cells(mdp, 'operating-points-v2') == cells(table, 'phandle')
    # DISP_CC_MDSS_MDP_CLK_SRC rate_max in the liuqin-t-oss dispcc-waipio.c.
    expected = {172000000: 56, 200000000: 64, 325000000: 128, 375000000: 192, 500000000: 256}
    seen = set()
    for name in children(table):
        node = f'{table}/{name}'
        high, hz = cells(node, 'opp-hz')
        assert high == 0 and hz in expected and hz not in seen, node
        assert cells(node, 'required-opps') == (corners[expected[hz]],) * 2, node
        seen.add(hz)
    assert seen == expected.keys(), 'Incomplete MDP rate table'

    speaker_buses = {'880000': {0x43: 'TL', 0x40: 'TR'}, '888000': {0x41: 'BL', 0x40: 'BR'}}
    for address, speakers in speaker_buses.items():
        bus = f'/soc@0/geniqup@8c0000/i2c@{address}'
        assert cells(bus, 'clock-frequency') == (400000,), bus
        assert cells(bus, 'power-domains') == (provider, 0, provider, 8), bus
        assert cells(bus, 'required-opps') == (corners[64], corners[64]), bus
        for reg, position in speakers.items():
            node = f'{bus}/speaker-amp@{reg:x}'
            assert cells(node, 'reg') == (reg,), node
            for prop, value in [('compatible', 'cirrus,cs35l41'), ('sound-name-prefix', position)]:
                assert subprocess.check_output(['fdtget', dtb, node, prop], text=True).strip() == value, node
    print('PASS: five MDP rates on MMCX/MX and both speaker I2C engines on CX/MX')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--dtb', type=Path, help='compiled Liuqin device tree')
    inputs.add_argument('--source', type=Path, help='assembled kernel source tree')
    args = parser.parse_args()
    if args.dtb:
        check(args.dtb)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            dts = args.source / 'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts'
            preprocessed = subprocess.check_output(['cpp', '-nostdinc', '-undef', '-D__DTS__',
                                                   '-x', 'assembler-with-cpp', '-I',
                                                   args.source / 'scripts/dtc/include-prefixes', dts])
            dtb = Path(tmp) / 'liuqin.dtb'
            subprocess.run(['dtc', '-q', '-I', 'dts', '-O', 'dtb', '-o', dtb],
                           input=preprocessed, check=True)
            check(dtb)
