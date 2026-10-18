#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the isolated Liuqin reset port against OEM timing and board scope."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--baseline-source', type=Path, required=True)
parser.add_argument('--oem-panel', type=Path, action='append', required=True)
parser.add_argument('--report', type=Path)
args = parser.parse_args()
relative = Path('drivers/gpu/drm/panel/panel-novatek-nt36532.c')
new = (args.source / relative).read_text()
old = (args.baseline_source / relative).read_text()

def function(text, name):
    match = re.search(r'^static [^\n]+\b' + re.escape(name) + r'\(', text, re.M)
    assert match, name
    start = text.index('{', match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[match.start():end]

unchanged = ['pipa_reset', 'yudi_reset', 'pipa_init_sequence', 'yudi_init_sequence',
             'nt36532_prepare', 'nt36532_enable', 'nt36532_disable',
             'nt36532_unprepare', 'nt36532_probe']
for name in unchanged:
    assert function(old, name) == function(new, name), name
assert 'liuqin-panel-readback' not in new
assert new.count('.reset_sequence = liuqin_reset,') == 1
assert '.reset_sequence = pipa_reset\n' in new
assert '.reset_sequence = yudi_reset\n' in new
assert new.index('.reset_sequence = liuqin_reset,') > new.index('static const struct panel_desc liuqin_desc')
dt = Path('arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts')
assert (args.source / dt).read_text().replace('The Liuqin reset helper', 'The shared reset helper') == (args.baseline_source / dt).read_text()

tables = []
for path in args.oem_panel:
    text = path.read_text()
    cells = [int(x, 0) for x in re.search(r'qcom,mdss-dsi-reset-sequence = <([^>]+)>;', text)[1].split()]
    assert len(cells) == 8
    tables.append([(cells[i], cells[i + 1] * 1000) for i in range(0, len(cells), 2)])
assert all(table == tables[0] for table in tables)
expected = []
for level, minimum in tables[0]:
    expected += [f'gpio {level}', f'wait {minimum} {minimum + 100}']
stubs = r'''
#include <stdio.h>
struct nt36532 { void *reset_gpio; };
static void gpiod_set_value_cansleep(void *gpio, int value) { printf("gpio %d\n", value); }
static void usleep_range(unsigned int min, unsigned int max) { printf("wait %u %u\n", min, max); }
'''
events = {}
with tempfile.TemporaryDirectory(prefix='liuqin-panel-sequence-') as directory:
    work = Path(directory)
    for label, text, name in [('liuqin', new, 'liuqin_reset'),
                              ('pipa-before', old, 'pipa_reset'), ('pipa-after', new, 'pipa_reset'),
                              ('yudi-before', old, 'yudi_reset'), ('yudi-after', new, 'yudi_reset')]:
        code = stubs + function(text, name) + '\nint main(void) { struct nt36532 ctx = {0}; ' + name + '(&ctx); return 0; }\n'
        src = work / (label + '.c'); src.write_text(code)
        binary = work / label
        subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', '-Wno-unused-parameter', str(src), '-o', str(binary)], check=True)
        events[label] = subprocess.check_output([str(binary)], text=True).splitlines()
assert events['liuqin'] == expected
assert events['pipa-before'] == events['pipa-after']
assert events['yudi-before'] == events['yudi-after']
result = dict(actual_reset_helpers_executed=5, oem_tables_compared=len(tables),
              liuqin_events=events['liuqin'], oem_waveform_matches=True,
              unchanged_functions=unchanged, other_boards_unchanged=True,
              extra_waits_unchanged=True, gpio_polarity_unchanged=True,
              production_patch_activation_required=True, hardware_validation_pending=True)
if args.report:
    args.report.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
