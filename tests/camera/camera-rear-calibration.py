#!/usr/bin/env python3
"""Verify rear calibration against the pinned shipping module and tuning data.

Uses synthetic EEPROM contents, including checksum/geometry failures. No camera
access and no per-device data is needed or included in the repository.
"""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import struct


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vendor', type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[2]
    decode = runpy.run_path(str(root / 'tests/camera/camera-oem.py'))['decode']
    module = decode(a.vendor / 'lib64/camera/com.qti.sensormodule.liuqin_qtech_s5kjn1_wide_i.bin',
                    '17f17716c7dffcf80aae767f46bc7a86b6be6f7c087d005fd5c7ffb415999c3a')
    sha = 'ab4c7b74b4f4d88c65b2c545994e4c90314be3fee20b2b05042ca7e9114147ab'
    tuned = decode(a.vendor / 'lib64/camera/com.qti.tuned.liuqin_qtech_s5kjn1_wide.bin', sha)
    overlay = root / 'build/rootfs/plasma-overlay'
    reference = json.loads((overlay / 'usr/share/libcamera/liuqin/s5kjn1-reference.json').read_text())
    assert reference['source_sha256'] == sha
    assert reference['golden_region'] == 1767 and reference['baseline_region'] == 1745
    assert reference['golden'] == list(struct.unpack('<884f', tuned[1767][1]))
    assert reference['baseline'] == list(struct.unpack_from('<884f', tuned[1745][1]))
    # Actual stock read sequence: three blocks, 8-bit slave address 0xa2.
    rows = [struct.unpack_from('<10I', module[3828][1], i * 40) for i in range(3)]
    assert all(r[3] == 1 and r[5:9] == (2, 1, 3, 1) for r in rows)
    assert [(r[2], struct.unpack('<I', module[r[4]][1])[0]) for r in rows] == [(0, 8192), (8192, 8192), (16384, 2048)]
    for ident in (3829, 3832, 3835):
        assert struct.unpack('<H', module[ident][1]) == (0xa2,)
    # Selected baseline is the day-light branch, not a front-camera grid.
    assert struct.unpack_from('<2f', tuned[1739][1], 48) == (4700, 9000)
    assert struct.unpack_from('<I', tuned[1739][1], 68) == (1745,)
    calibrator = runpy.run_path(str(overlay / 'usr/libexec/liuqin-front-calibrate'))
    decode_cal = calibrator['decode']
    ranges = ((0, 1, 0x24), (0x27, 0x28, 0x43), (0x47, 0x48, 0x51), (0x60, 0x61, 0x74d))

    def checksums(data):
        for flag, start, end in ranges:
            data[flag] = 1
            data[end] = sum(data[start:end]) % 255 + 1
        return data

    data = bytearray(18432)
    struct.pack_into('>H', data, 0x2d, 800)
    struct.pack_into('>H', data, 0x31, 400)
    struct.pack_into('>3H', data, 0x4b, 640, 704, 1056)
    for c in range(4):
        for point in range(221):
            struct.pack_into('>H', data, 0x65 + point * 8 + c * 2, int(reference['golden'][c * 221 + point]))
    checksums(data)
    result = decode_cal(data, reference, 's5kjn1')
    assert (result['rg'], result['bg'], result['grgb']) == (.625, .6875, 1.03125)
    assert (result['af_min'], result['af_max']) == (320, 880)
    assert result['lsc'] == reference['baseline']
    chart = [{'distance_cm': 200, 'dac': 430}, {'distance_cm': 20, 'dac': 670}]
    profile = dict(schema=1, model='s5kjn1', eeprom_sha256=hashlib.sha256(data).hexdigest(), points=chart)
    assert calibrator['focus_points'](profile, data, result) == [
        {'dioptres': 0.5, 'dac': 430}, {'dioptres': 5.0, 'dac': 670}]
    for invalid in (dict(profile, eeprom_sha256='0' * 64), dict(profile, model='imx596'),
                    dict(profile, points=chart[:1]), dict(profile, points=chart[::-1]),
                    dict(profile, points=[chart[0], dict(chart[1], dac=430)]),
                    dict(profile, points=[chart[0], dict(chart[1], distance_cm=float('nan'))]),
                    dict(profile, points=[chart[0], dict(chart[1], dac=result['af_max'] + 1)])):
        try:
            calibrator['focus_points'](invalid, data, result)
        except ValueError:
            continue
        raise AssertionError('Invalid physical calibration accepted')
    assert 'focus_points' not in result
    # A physically mirrored calibration must map the mesh coordinates and Gr/Gb.
    for mirror, flip in ((1, 0), (0, 1), (1, 1)):
        swapped = bytearray(data)
        swapped[0x49], swapped[0x4a] = mirror, flip
        for c in range(4):
            for point in range(221):
                x, y = point % 17, point // 17
                source = (12 - y if flip else y) * 17 + (16 - x if mirror else x)
                swapped[0x65 + point * 8 + c * 2:0x67 + point * 8 + c * 2] = data[0x65 + source * 8 + c * 2:0x67 + source * 8 + c * 2]
        value = decode_cal(checksums(swapped), reference, 's5kjn1')
        assert value['lsc'] == reference['baseline']
        assert value['grgb'] == (1 / 1.03125 if mirror else 1.03125)

    def rejects(value, ref=reference):
        try:
            decode_cal(value, ref, 's5kjn1')
        except ValueError:
            return
        raise AssertionError('Invalid calibration accepted')

    for flag, start, end in ranges:
        bad = bytearray(data)
        bad[start] ^= 1
        rejects(bad)
        bad = bytearray(data)
        bad[flag] = 0
        rejects(bad)
    bad = bytearray(data)
    struct.pack_into('>H', bad, 0x2d, 200)  # macro below infinity, but valid checksum
    rejects(checksums(bad))
    bad = bytearray(data)
    struct.pack_into('>H', bad, 0x65, 0)  # invalid shading, even with valid checksum
    rejects(checksums(bad))
    rejects(data[:-1])
    rejects(data, dict(reference, model='liuqin_aac_imx596_front'))
    print('PASS: stock rear read map, golden/baseline, WB orientation, AF margins, four checksums and corrupt-data rejection')


if __name__ == '__main__':
    main()
