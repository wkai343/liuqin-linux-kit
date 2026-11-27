#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Audit stock HDR401 tuning without enabling it or submitting hardware work.

Parses both module trees with the shipping parser and exercises the stock
settings calculation for synthetic two/three-exposure metadata. This is not
an HDR image generator: no sensor exposure sequence or input topology is proven.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct


def audit(vendor, sensor, module):
    _, sha = module.SENSORS[sensor]
    version = 4 | (1 << 32)
    h, entries = module.tuning_file(vendor, sensor, version)
    obj = module.parse_tuning(h, entries, 70, 'hdr40_ipe_v2', version, 0x51bf48)
    # Execute reads pChromatix+8 for enable; Calculate reads enable_section+4.
    enabled = h.r32(obj + 0x128)
    assert enabled == 0 and h.r32(obj + 0x1b0) == 1
    node, triggers = h.r64(obj + 0x1b8), []
    for depth in range(3):
        index, trigger, low, high, count, ident, child, regions, rid, leaf = \
            struct.unpack('<IIffIIQIIQ', h.u.mem_read(node, 48))
        assert index == depth and trigger == (5, 5, 0)[depth]
        triggers.append(dict(trigger=trigger, low=low, high=high))
        if depth < 2:
            assert count == 1 and child
            node = child
        else:
            assert count == 0 and regions == 1 and leaf
    inp, unpacked = h.malloc(256), h.malloc(648)
    h.w64(inp + 8, obj + 0x120)
    # IQSetupTriggerData supplies long/mid, mid/short, long/short in each group.
    topology = module.module('hdr_topology', module.HERE / 'hdr-topology.py')
    ratios = topology.hdr401_ratios(h, [(1000, 1.), (3000, 2.), (17000, 4.)])
    h.u.mem_write(inp + 0x20, ratios)
    calculated = []
    for exposures in (2, 3):
        h.u.mem_write(unpacked, bytes(648))
        h.w32(inp + 0x60, exposures)
        assert h.call(0xd59998, inp, leaf, obj + 0x174, obj + 0x124, unpacked) == 1
        data = bytes(h.u.mem_read(unpacked, 648))
        assert data[0] == enabled
        calculated.append(dict(exposures=exposures, enable=data[0],
                               unpacked_sha256=hashlib.sha256(data).hexdigest()))
    return dict(sensor=sensor, tuning_sha256=sha, hdr401_enable=enabled,
                triggers=triggers, calculated=calculated)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location('tf_fixture',
        Path(__file__).parent / 'camera-icp/make-temporal-fixture.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    result = dict(scope='offline HDR401 tuning audit only; no hardware submission',
                  sensors=[audit(args.vendor, s, m) for s in ('front', 'rear')])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: front/rear HDR401 trees and two/three-exposure calculation; stock enable remains zero')


if __name__ == '__main__':
    main()
