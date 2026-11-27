#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check compiled DT graphs and OEM provenance without accessing a device."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct

parser = argparse.ArgumentParser(description="Compare compiled Liuqin CPU/power DT data against pinned OEM inputs.")
for name in ('before', 'after', 'stock-base', 'stock-overlay', 'oem-source', 'report'):
    parser.add_argument('--' + name, type=Path, required=True)
args = parser.parse_args()
inputs = {}
for name in ('before', 'after', 'stock_base', 'stock_overlay', 'oem_source'):
    path = getattr(args, name)
    inputs[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
assert inputs['stock_base']['sha256'] == '527344cd52497d0b8b9311f1ab82ac3418b86b95c25e7786890f38f40054e467'
assert inputs['stock_overlay']['sha256'] == '295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87'
assert inputs['oem_source']['sha256'] == '7721c0830818db9c0a2bae42d8aac197dfde057bbb0a09f3657280bba4c097f9'


def read_dtb(path):
    data = path.read_bytes()
    magic, total, pos, strings = struct.unpack_from('>4I', data)
    assert magic == 0xd00dfeed and total <= len(data)
    nodes = {}
    stack = []
    while True:
        token = struct.unpack_from('>I', data, pos)[0]
        pos += 4
        if token == 1:
            end = data.index(0, pos)
            stack.append(data[pos:end].decode())
            pos = (end + 4) & ~3
            nodes['/'.join(stack) or '/'] = {}
        elif token == 2:
            stack.pop()
        elif token == 3:
            size, offset = struct.unpack_from('>2I', data, pos)
            pos += 8
            begin = strings + offset
            name = data[begin:data.index(0, begin)].decode()
            nodes['/'.join(stack) or '/'][name] = data[pos:pos + size]
            pos = (pos + size + 3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            return nodes
        else:
            raise AssertionError(token)


def cells(data):
    assert len(data) % 4 == 0
    return list(struct.unpack('>' + 'I' * (len(data) // 4), data))


def strings(data):
    return data.rstrip(b'\0').decode().split('\0')


def phandles(nodes):
    return {cells(props['phandle'])[0]: path for path, props in nodes.items()
            if 'phandle' in props}


def enabled(nodes, path):
    while path:
        if strings(nodes[path].get('status', b'okay'))[0] not in ('ok', 'okay'):
            return False
        if path == '/':
            break
        path = path.rsplit('/', 1)[0] or '/'
    return True


before = read_dtb(args.before)
after = read_dtb(args.after)
stock = read_dtb(args.stock_base)
overlay = read_dtb(args.stock_overlay)
bp, ap, sp = [phandles(tree) for tree in (before, after, stock)]

# Inspect base-node scalar overrides without pretending to reproduce ABL's
# vendor overlay application (libfdt rejects this stock overlay with BADVALUE).
effective = {path: dict(props) for path, props in stock.items()}
targets = {}
for symbol, fixups in overlay['/__fixups__'].items():
    for fixup in strings(fixups):
        path, prop, offset = fixup.rsplit(':', 2)
        if prop == 'target':
            assert int(offset) == 0
            targets[path] = strings(stock['/__symbols__'][symbol])[0]
scalar_overrides = []
assert all(path in targets for path in overlay
           if path.count('/') == 1 and path.startswith('/fragment@'))
for fragment, target in targets.items():
    prefix = fragment + '/__overlay__'
    for path, props in overlay.items():
        if path == prefix or path.startswith(prefix + '/'):
            destination = target.rstrip('/') + path[len(prefix):] or '/'
            if destination in effective:
                effective[destination].update(props)
                scalar_overrides.append(destination)

cpu_rows = []
source = args.oem_source.read_text()
for cpu in range(8):
    path = f'/cpus/cpu@{cpu:x}00' if cpu else '/cpus/cpu@0'
    old, new, oem = before[path], after[path], effective[path]
    assert 'dynamic-power-coefficient' not in old
    assert new['capacity-dmips-mhz'] == oem['capacity-dmips-mhz']
    assert new['dynamic-power-coefficient'] == oem['dynamic-power-coefficient']
    public = re.search(rf'CPU{cpu}: cpu@[^{{]+\{{(.*?)\n\t\t\}};', source, re.S)[1]
    for name in ('capacity-dmips-mhz', 'dynamic-power-coefficient'):
        assert int(re.search(name + r' = <(\d+)>', public)[1]) == cells(new[name])[0]
    # CPU policy remains generated from the hardware LUT, preserving fast switch.
    assert not {'operating-points-v2', 'interconnects'} & new.keys()
    domain = cells(new['qcom,freq-domain'])
    assert len(domain) == 2
    assert domain[1] == cells(oem['qcom,freq-domain'])[1]
    cpu_rows.append(dict(cpu=cpu, domain=domain[1], capacity=cells(new['capacity-dmips-mhz'])[0],
                         coefficient=cells(new['dynamic-power-coefficient'])[0]))

idle_keys = ('arm,psci-suspend-param', 'entry-latency-us', 'exit-latency-us', 'min-residency-us')
idle_rows = []
for mainline, oem in [('/cpus/idle-states/cpu-sleep-0-0', '/idle-states/silver-c4'),
                       ('/cpus/idle-states/cpu-sleep-1-0', '/idle-states/gold-c4'),
                       ('/cpus/domain-idle-states/cluster-sleep-0', '/idle-states/cluster-d4'),
                       ('/cpus/domain-idle-states/cluster-sleep-1', '/idle-states/cluster-e3')]:
    for key in idle_keys:
        assert after[mainline][key] == effective[oem][key]
    assert ('local-timer-stop' in after[mainline]) == ('local-timer-stop' in effective[oem])
    idle_rows.append(dict(mainline=mainline, oem=oem,
                          values={key: cells(after[mainline][key])[0] for key in idle_keys}))
for row in cpu_rows:
    cpu = row['cpu']; path = f'/cpus/cpu@{cpu:x}00' if cpu else '/cpus/cpu@0'
    pd = ap[cells(after[path]['power-domains'])[0]]
    idle = ap[cells(after[pd]['domain-idle-states'])[0]]
    assert idle == idle_rows[0 if cpu < 4 else 1]['mainline']
    cluster = ap[cells(after[pd]['power-domains'])[0]]
    assert [ap[x] for x in cells(after[cluster]['domain-idle-states'])] == [x['mainline'] for x in idle_rows[2:]]

# Normalize only properties whose binding actually contains phandles.
list_cells = {'thermal-sensors': '#thermal-sensor-cells', 'sound-dai': '#sound-dai-cells',
              'cooling-device': '#cooling-cells'}
single_refs = {'remote-endpoint', 'trip', 'qcom,rx-device', 'qcom,tx-device'}
def normalized(nodes, handles, key, value):
    if key == 'phandle':
        return handles[cells(value)[0]]
    if key in single_refs or key.endswith('-supply'):
        assert len(value) == 4
        return handles[cells(value)[0]]
    if key in list_cells or re.fullmatch(r'pinctrl-\d+', key) or key == 'nvmem-cells':
        raw = cells(value); result = []; pos = 0
        while pos < len(raw):
            target = handles[raw[pos]]
            n = cells(nodes[target][list_cells[key]])[0] if key in list_cells else 0
            result.append((target, raw[pos + 1:pos + 1 + n])); pos += n + 1
        assert pos == len(raw)
        return result
    return value

for path, props in before.items():
    assert path in after
    for key, value in props.items():
        candidate = after[path].get(key)
        if candidate != value:
            assert normalized(before, bp, key, value) == normalized(after, ap, key, candidate), (path, key)

bandwidth_rows = []
expected_new_nodes = set()
soc_path = strings(after['/__symbols__']['soc'])[0]
for name, address, compatible in [('cpu', 0x190b6400, 'qcom,bwmon4'), ('llcc', 0x19091000, 'qcom,bwmon5')]:
    path = f'{soc_path}/pmu@{address:x}'
    oem_path = next(p for p, v in stock.items() if v.get('compatible') == compatible.encode() + b'\0')
    oem = effective[oem_path]; new = after[path]
    assert oem['interrupts'] == new['interrupts']
    assert cells(oem['qcom,hw-timer-hz']) == [19200000]
    assert cells(oem['qcom,count-unit']) == [65536]
    region = cells(new['reg']); assert region[:3] == [0, address, 0]
    assert cells(oem['reg'])[:2] == [address, region[3]]
    if name == 'cpu':
        assert cells(oem['reg'])[2:] == [address - 0x100, 0x200]
    dcvs = sp[cells(oem['qcom,target-dev'])[0]]
    width = cells(effective[dcvs]['qcom,bus-width'])[0]
    freq_path = sp[cells(effective[dcvs]['qcom,freq-tbl'])[0]]
    assert freq_path not in scalar_overrides and dcvs not in scalar_overrides and oem_path not in scalar_overrides
    frequencies = cells(effective[freq_path]['qcom,freq-tbl'])
    bus = 'llcc' if name == 'cpu' else 'ddr'
    public_table = re.search(rf'{bus}_freq_table:.*?qcom,freq-tbl =(.*?);', source, re.S)[1]
    assert frequencies == [int(v) for v in re.findall(r'<\s*(\d+)\s*>', public_table)]
    assert width == int(re.search(rf'qcom_{bus}_dcvs_hw:.*?qcom,bus-width = <(\d+)>', source, re.S)[1])
    opp = ap[cells(new['operating-points-v2'])[0]]
    expected_new_nodes.update([path, opp, *(f'{opp}/opp-{i}' for i in range(len(frequencies)))])
    votes = [cells(after[f'{opp}/opp-{i}']['opp-peak-kBps'])[0] for i in range(len(frequencies))]
    assert votes == [frequency * width for frequency in frequencies]
    assert votes == sorted(set(votes))
    # All register ranges already described directly under /soc remain disjoint.
    for old_path, props in before.items():
        if old_path.rsplit('/', 1)[0] == soc_path and 'reg' in props:
            regs = cells(props['reg'])
            if len(regs) % 4 == 0:
                for i in range(0, len(regs), 4):
                    start = (regs[i] << 32) | regs[i + 1]
                    size = (regs[i + 2] << 32) | regs[i + 3]
                    assert not (address < start + size and start < address + region[3]), old_path
    icc = cells(new['interconnects']); assert len(icc) == 6
    assert icc[2] == icc[5] == 3  # ACTIVE + WAKE, no SLEEP vote.
    assert enabled(after, ap[icc[0]]) and enabled(after, ap[icc[3]])
    bandwidth_rows.append(dict(node=path, oem_node=oem_path, bus_width=width,
                               frequencies_khz=frequencies, peak_kBps=votes,
                               source_provider=ap[icc[0]], destination_provider=ap[icc[3]],
                               source_id=icc[1], destination_id=icc[4]))

assert after.keys() - before.keys() == expected_new_nodes
for path, props in before.items():
    added = after[path].keys() - props.keys()
    if path == '/__symbols__':
        assert added == {'cpu_bwmon', 'llcc_bwmon', 'cpu_bwmon_opp_table', 'llcc_bwmon_opp_table'}
    elif re.fullmatch(r'/cpus/cpu@[0-9a-f]+', path):
        assert added == {'dynamic-power-coefficient'}
    else:
        assert not added, (path, added)

# Build a review inventory; differences in established voltage constraints are
# reported, not automatically widened or narrowed.
stock_regulators = {strings(props['regulator-name'])[0]: (path, props)
                    for path, props in effective.items() if 'regulator-name' in props}
regulator_rows = []
keys = ('regulator-min-microvolt', 'regulator-max-microvolt', 'regulator-boot-on', 'regulator-always-on')
for path, props in after.items():
    if 'regulator-name' not in props:
        continue
    name = strings(props['regulator-name'])[0]
    if not name.startswith(('pm8350', 'pmr735')):
        continue
    oem_path, oem = stock_regulators[name]
    def values(p):
        return {key: (True if p[key] == b'' else cells(p[key])[0]) for key in keys if key in p}
    regulator_rows.append(dict(name=name, node=path, oem_node=oem_path,
                               mainline=values(props), oem=values(oem)))

supplies = []; icc_consumers = []
for path, props in after.items():
    if path.startswith('/__') or not enabled(after, path):
        continue
    for name, value in props.items():
        if name.endswith('-supply'):
            target = ap[cells(value)[0]]
            assert enabled(after, target), (path, name, target)
            supplies.append(dict(consumer=path, supply=name, provider=target))
    if 'interconnects' in props:
        raw = cells(props['interconnects']); entries = []; pos = 0
        while pos < len(raw):
            target = ap[raw[pos]]; n = cells(after[target]['#interconnect-cells'])[0]
            assert enabled(after, target), (path, target)
            entries.append(dict(provider=target, args=raw[pos + 1:pos + 1 + n])); pos += n + 1
        assert pos == len(raw) and len(entries) % 2 == 0
        icc_consumers.append(dict(consumer=path, paths=[entries[i:i+2] for i in range(0, len(entries), 2)]))

report = dict(inputs=inputs, cpu=cpu_rows, idle=idle_rows,
              bandwidth=bandwidth_rows, regulators=regulator_rows, supplies=supplies,
              interconnect_consumers=icc_consumers,
              existing_properties='unchanged after resolving phandle renumbering',
              stock_view='base DTB plus scalar overrides of existing nodes; not an ABL runtime FDT',
              hardware_validation='NOT_DEVICE_TESTED')
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(dict(cpu_count=len(cpu_rows), idle_states=len(idle_rows), bwmon_count=len(bandwidth_rows),
                      regulators=len(regulator_rows), supply_links=len(supplies),
                      interconnect_consumers=len(icc_consumers), existing_properties=report['existing_properties']), indent=2))
