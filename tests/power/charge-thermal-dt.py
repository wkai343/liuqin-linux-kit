#!/usr/bin/env python3
"""Compare compiled Liuqin ADC7 wiring with the pinned shipping DTBO."""
import argparse
import hashlib
from pathlib import Path
import struct


def nodes(path):
    data = path.read_bytes()
    magic, total, position, strings = struct.unpack_from('>4I', data)
    assert magic == 0xd00dfeed and total <= len(data)
    stack, result = [], {}
    while True:
        token = struct.unpack_from('>I', data, position)[0]
        position += 4
        if token == 1:
            end = data.index(0, position)
            stack.append(data[position:end].decode())
            position = (end + 4) & ~3
            result['/'.join(stack)] = {}
        elif token == 2:
            stack.pop()
        elif token == 3:
            size, offset = struct.unpack_from('>2I', data, position)
            position += 8
            name = data[strings + offset:data.index(0, strings + offset)].decode()
            result['/'.join(stack)][name] = data[position:position + size]
            position = (position + size + 3) & ~3
        elif token == 9:
            return result
        else:
            assert token == 4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dtb', required=True, type=Path)
    parser.add_argument('--stock-overlay', required=True, type=Path)
    args = parser.parse_args()
    assert hashlib.sha256(args.stock_overlay.read_bytes()).hexdigest() == '295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87'
    current, stock = nodes(args.dtb), nodes(args.stock_overlay)
    mappings = [('cpu_therm', 'pm8350_skin_therm', 0x144),
                ('flash_therm', 'pm8350_flash_therm', 0x145),
                ('quiet_therm', 'pm8350_quiet_therm', 0x146),
                ('backlight_therm', 'pm8350_lcd_therm', 0x147),
                ('conn_therm', 'pm8350b_conn_therm', 0x347),
                ('charger_therm0', 'pm8350b_sc8551A_therm', 0x34d),
                ('wifi_therm', 'wifi_therm', 0x44c),
                ('xo_therm', 'xo_therm', 0x44)]
    for name, old_name, channel in mappings:
        new_matches = [v for v in current.values() if v.get('label') == name.encode() + b'\0']
        old_matches = [v for v in stock.values() if v.get('label') == old_name.encode() + b'\0']
        assert len(new_matches) == len(old_matches) == 1, (name, new_matches, old_matches)
        new, old = new_matches[0], old_matches[0]
        assert new['reg'] == old['reg'] == struct.pack('>I', channel), name
        for prop in ('qcom,ratiometric', 'qcom,hw-settle-time', 'qcom,pre-scaling'):
            assert new[prop] == old[prop], (name, prop)
    print('PASS: all eight ADC7 thermistor channels and calibration settings match shipping DTBO')


if __name__ == '__main__':
    main()
