#!/usr/bin/env python3
"""Compare camera tables to the pinned stock sensor modules, without a device."""
import argparse
import hashlib
from pathlib import Path
import re
import struct
import subprocess


def decode(path, digest):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Unexpected stock sensor module: ' + str(path))
    if not data.startswith(b'QTI Chromatix Header\0'):
        raise ValueError('Not a QTI sensor module')
    start, length = struct.unpack_from('<2I', data, 172)
    payload, payload_length = struct.unpack_from('<2I', data, 184)
    assert start + length == payload and length % 56 == 0
    rows = {}
    for offset in range(start, start + length, 56):
        ident, name, major, minor, patch, pos, size = struct.unpack_from('<I32s5I', data, offset)
        assert ident not in rows and pos + size <= payload_length
        rows[ident] = (name.rstrip(b'\0').decode(), data[payload + pos:payload + pos + size])
    return rows


def registers(rows, ident):
    name, raw = rows[ident]
    assert name == 'regSetting' and len(raw) % 40 == 0
    result = []
    for offset in range(0, len(raw), 40):
        item = struct.unpack_from('<10I', raw, offset)
        assert item[3] == 1 and item[7] == 0
        value = struct.unpack('<I', rows[item[4]][1])[0]
        delay = struct.unpack('<I', rows[item[9]][1])[0]
        result.append((item[2], value, item[5], item[6], delay))
    return result


def check_rear_modes(kernel, rows):
    source = (kernel/'drivers/media/i2c/s5kjn1.c').read_text()
    header = (kernel/'drivers/media/i2c/s5kjn1-liuqin-modes.h').read_text()
    aliases = {name: int(address, 16) for name, address in re.findall(
        r'#define (S5KJN1_\w+)\s+CCI_REG16\((0x\w+)\)', source)}

    def table(text, name):
        return text.split(name + '[] = {', 1)[1].split('\n};', 1)[0]

    def sequence(text):
        return [(int(addr, 16) if addr else aliases[name], int(value, 16))
                for addr, name, value in re.findall(
                    r'\{\s*(?:CCI_REG16\((0x\w+)\)|(S5KJN1_\w+)),\s*(0x\w+)\s*\}', text)]

    base = sequence(table(source, 's5kjn1_4080x3060_30fps_mode_19p2'))
    for ident, mode in [(40, 'full'), (903, 'normal'), (1766, 'wide'), (2629, 'uhd')]:
        resolved = sequence(table(header, 's5kjn1_full_19p2')) if mode == 'full' else base.copy()
        if mode in ('wide', 'uhd'):
            overrides = re.findall(r'\{ (\d+), (0x\w+) \}', table(header, 's5kjn1_' + mode + '_overrides'))
            indices = [int(index) for index, _ in overrides]
            assert indices == sorted(set(indices))
            for index, value in overrides:
                i = int(index)
                resolved[i] = resolved[i][0], int(value, 16)
        expected = registers(rows, ident)
        assert [(a, v, 2, 2, 0) for a, v in resolved] == expected
        reg = dict(resolved)
        entry = next(e for e in table(source, 's5kjn1_liuqin_modes').split('\n\t},')
                     if f'.width = {reg[0x34c]},' in e and f'.height = {reg[0x34e]},' in e)
        assert f'.hts = {reg[0x342]},' in entry and f'.vts = {reg[0x340]},' in entry
        scale = reg[0x0380]
        assert reg[0x344] + scale * reg[0x350] - 16 == (8160 - scale * reg[0x34c]) // 2
        assert reg[0x346] + scale * reg[0x352] - 28 == (6120 - scale * reg[0x34e]) // 2
    assert struct.unpack('<4I', rows[3682][1]) == (0, 0, 8160, 6120)
    assert struct.unpack_from('<d', rows[1][1], 532)[0] == .64
    assert all(struct.unpack('<I', rows[i][1])[0] == 2 for i in (3686, 3687, 3688))


def check_flash(dtb, stock, vendor):
    def get(image, node, prop, kind='u'):
        value = subprocess.check_output(['fdtget', '-t', kind, image, node, prop], text=True).strip()
        return value if kind == 's' else int(value)

    def symbol(name):
        return get(stock, '/__symbols__', name, 's')

    assert hashlib.sha256(stock.read_bytes()).hexdigest() == '295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87'
    camera = symbol('led_flash_wide')
    for mode in ('flash', 'torch'):
        source = symbol('pm8350c_' + mode + '0')
        assert get(stock, camera, mode + '-source') == get(stock, source, 'phandle')
        assert get(stock, source, 'qcom,id') == 0
    switch = symbol('pm8350c_switch0')
    assert get(stock, camera, 'switch-source') == get(stock, switch, 'phandle')
    assert get(stock, switch, 'qcom,led-mask') == 1

    pmic = '/soc@0/spmi@c400000/pmic@2/led-controller@ee00'
    led = pmic + '/led-0'
    rear = '/soc@0/cci@ac15000/i2c-bus@1/camera@10'
    assert get(dtb, pmic, 'status', 's') == 'okay'
    assert get(dtb, rear, 'flash-leds') == get(dtb, led, 'phandle')
    front = rear.replace('bus@1', 'bus@0')
    assert 'flash-leds' not in subprocess.check_output(['fdtget', '-p', dtb, front], text=True).splitlines()
    assert get(dtb, led, 'led-sources') == get(stock, symbol('pm8350c_flash0'), 'qcom,id') + 1
    settings = (vendor / 'etc/camera/camxoverridesettings.txt').read_text()
    currents = dict(re.findall(r'^overrideFlash(\w+)Current=(\d+)\s*$', settings, re.M))
    assert get(dtb, led, 'led-max-microamp') == 1000 * int(currents['Torch'])
    step = get(stock, symbol('pm8350c_flash0'), 'qcom,ires-ua')
    assert get(dtb, led, 'flash-max-microamp') == int(currents['SnapshotLight']) * 1000 // step * step
    assert get(dtb, led, 'flash-max-timeout-us') == 200000
    print('PASS: OEM flash, torch and switch routes, current steps, timeout and rear-only DT link')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--vendor', type=Path, required=True, help='extracted stock vendor partition')
    parser.add_argument('--libcamera', type=Path, help='also check the IMX596 static properties')
    parser.add_argument('--dtb', type=Path, help='also check the compiled rear flash connection')
    parser.add_argument('--stock-overlay', type=Path, help='pinned stock DTBO entry.15')
    args = parser.parse_args()
    if bool(args.dtb) != bool(args.stock_overlay):
        parser.error('--dtb and --stock-overlay must be used together')
    if args.dtb:
        check_flash(args.dtb, args.stock_overlay, args.vendor)
    front = decode(args.vendor/'lib64/camera/com.qti.sensormodule.liuqin_aac_imx596_front_i.bin',
                   '802e81a1a1045e5b591c2824fb4042881977e52c7ae9620722a80dc84c52e41c')
    rear = decode(args.vendor/'lib64/camera/com.qti.sensormodule.liuqin_qtech_s5kjn1_wide_i.bin',
                  '17f17716c7dffcf80aae767f46bc7a86b6be6f7c087d005fd5c7ffb415999c3a')
    sensor = (args.kernel/'drivers/media/i2c/imx596.c').read_text()
    for name, ident, count in [('imx596_init', 1078, 364), ('imx596_2592x1952', 290, 75),
                               ('imx596_5184x3904', 37, 75), ('imx596_2592x1472', 543, 75)]:
        body = sensor.split(name+'[] = {', 1)[1].split('};', 1)[0]
        actual = [(int(a, 16), int(v, 16), 2, 1, 0) for a, v in re.findall(
            r'\{ CCI_REG8\((0x[\da-f]+)\), (0x[\da-f]+) \}', body)]
        expected = registers(front, ident)
        assert len(actual) == count and actual == expected, name
    defines = {name: int(value) for name, value in re.findall(r'#define IMX596_(\w+) (\d+)', sensor)}
    # QTI RemosaicType: 0 = software, 1 = hardware. The full mode supplies
    # ordinary Bayer after the sensor's own remosaic, not unprocessed Quad Bayer.
    assert front[269] == ('RemosaicTypeInfo', struct.pack('<I', 1))
    # All four shipping modes have empty HDR descriptors. This does not rule
    # out stock multi-frame software HDR, but is not a sensor HDR mode table.
    for base in (266, 519, 772, 1025):
        for offset, name in [(0, 'HDRExposureType'), (1, 'ZZHDRInfo'),
                             (2, 'HDR3ExposureInfo'), (8, 'QHDRInfo')]:
            assert front[base + offset] == (name, b'')
    modes = sensor.split('imx596_modes[] = {', 1)[1].split('\n};', 1)[0].split('}, {')
    assert len(modes) == 3
    for body, ident, fps in zip(modes, [290, 543, 37], [30, 30, 10]):
        desc = {k: int(v) for k, v in re.findall(r'\.(\w+) = (\d+)', body.split('.crop =')[0])}
        mode = dict((a, v) for a, v, *_ in registers(front, ident))
        word = lambda a: mode[a] * 256 + mode[a+1]
        assert (desc['width'], desc['height'], desc['hts'], desc['vts_def']) == (
            word(0x034c), word(0x034e), word(0x0342), word(0x0340))
        crop = {k: int(v) for k, v in re.findall(r'\.(\w+) = (\d+)', body.split('.crop =')[1])}
        assert (crop.get('left', 0), crop.get('top', 0), crop['width'], crop['height']) == (
            word(0x0344), word(0x0346), word(0x0348)-word(0x0344)+1,
            word(0x034a)-word(0x0346)+1)
        assert defines['MARGIN'] == word(0x0340) - word(0x0202) == 24
        assert abs(desc['pixel_rate'] / desc['hts'] / desc['vts_def'] - fps) < .01
        assert desc['pixel_rate'] == defines['XCLK'] * word(0x0306) * 4 / (
            mode[0x0305] * mode[0x0303] * mode[0x0301])
        assert defines['XCLK'] / mode[0x030d] * word(0x030e) / mode[0x030b] / 2 == 678400000
        if ident == 543:
            # 60 fps uses the same crop/PLL/tuning; VBLANK selects its VTS.
            mode[0x0340], mode[0x0341] = divmod(desc['vts_min'], 256)
            mode[0x0202], mode[0x0203] = divmod(desc['vts_min'] - defines['MARGIN'], 256)
            assert mode == dict((a, v) for a, v, *_ in registers(front, 796))
            assert abs(desc['pixel_rate'] / desc['hts'] / desc['vts_min'] - 60) < .01
        else:
            assert desc['vts_min'] == desc['vts_def']
    lens = (args.kernel/'drivers/media/i2c/gt9764.c').read_text()
    check_rear_modes(args.kernel, rear)
    expected = registers(rear, 3894)
    assert expected == [(2, 2, 1, 1, 100), (6, 0x40, 1, 1, 100), (7, 0x6a, 1, 1, 100)]
    begin = lens.split('static int gt9764_resume', 1)[1].split('static int gt9764_suspend', 1)[0]
    writes = [(int(a, 16), int(v, 16)) for a, v in re.findall(
        r'cci_write\(lens->regmap, CCI_REG8\((0x[\da-f]+)\), (0x[\da-f]+), &ret\)', begin)]
    assert writes == [(a, v) for a, v, *_ in expected]
    assert begin.count('usleep_range(100, 200)') == 3
    assert struct.unpack('<3I', rear[3891][1]) == (4, 1, 3)
    assert 'usleep_range(3000, 3500)' in begin
    dts = (args.kernel/'arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts').read_text()
    node = dts.split('pm8350c_l7: ldo7 {', 1)[1].split('};', 1)[0]
    lo = int(re.search(r'regulator-min-microvolt = <(\d+)>', node)[1])
    hi = int(re.search(r'regulator-max-microvolt = <(\d+)>', node)[1])
    regulator = (args.kernel/'drivers/regulator/qcom-rpmh-regulator.c').read_text()
    assert lo == hi == 3100000, 'VAF must preserve the exact stock RPMh request'
    assert 'liuqin_board_quirks()' in regulator
    assert '!strcmp(rpmh_resource_name, "ldoc7")' in regulator
    assert 'REGULATOR_LINEAR_RANGE(3100000, 0, 0, 0)' in regulator
    if args.libcamera:
        props = (args.libcamera/'src/libcamera/sensor/camera_sensor_properties.cpp').read_text()
        props = props.split('{ "imx596", {', 1)[1].split('\n\t\t} },', 1)[0]
        # sensorDriverData: delayInfo count/ref immediately precede pixelSize.
        assert struct.unpack_from('<II', front[1][1], 524) == (1, 2216)
        cell_nm = round(struct.unpack_from('<d', front[1][1], 532)[0] * 1000)
        assert list(map(int, re.search(r'unitCellSize = \{ (\d+), (\d+) \}', props).groups())) == [cell_nm]*2
        for field, ident in [('exposureDelay', 2218), ('gainDelay', 2219)]:
            assert int(re.search(field + r' = (\d+)', props)[1]) == struct.unpack('<I', front[ident][1])[0]
        helper = (args.libcamera/'src/ipa/libipa/camera_sensor_helper.cpp').read_text()
        helper = helper.split('class CameraSensorHelperImx596 ', 1)[1].split('REGISTER_CAMERA_SENSOR_HELPER', 1)[0]
        black = struct.unpack_from('<4I', front[1][1], 468)
        assert black == (64, 64, 64, 64)
        assert int(re.search(r'blackLevel_ = (\d+)', helper)[1]) == black[0] << 6
        tuned = decode(args.vendor/'lib64/camera/com.qti.tuned.liuqin_aac_imx596_front.bin',
                       'd7f013bef1c515e57f8c8d6d12488e0dc8018794792223d8ace2bf6e83b5bd08')
        # A conservative baseline, not an implementation of custom trigger 1001.
        assert tuned[60][0] == 'cc13_ipe_v2'
        assert struct.unpack_from('<I', tuned[60][1], 48)[0] == 1526
        assert struct.unpack('<5I', tuned[1525][1]) == (2, 1001, 100, 0, 6)
        # Follow the selected DRC / custom / LED / lux branch to CCT data.
        for ident, offset, expected in [(1526, 0, 1527), (1527, 24, 1564),
                                        (1564, 0, 1565), (1565, 24, 1574)]:
            assert struct.unpack_from('<I', tuned[ident][1], offset + 12)[0] == expected
        text = (args.libcamera/'src/ipa/simple/data/imx596.yaml').read_text()
        anchors = re.findall(r'ct: (\d+)\s+ccm: \[ ([^\]]+) \]', text)
        assert len(anchors) == 10
        for j in range(5):
            lo, hi, count, child, region_count, region = struct.unpack_from('<ff4I', tuned[1574][1], j*24)
            assert count == 0 and region_count == 1 and tuned[child][1] == b''
            expected = struct.unpack('<9f3i', tuned[region][1])
            assert expected[9:] == (0, 0, 0)
            for anchor, ct in zip(anchors[j*2:j*2+2], (lo, hi)):
                assert int(anchor[0]) == ct
                actual = [float(x) for x in anchor[1].split(',')]
                assert len(actual) == 9 and all(abs(a-b) < 1e-6 for a,b in zip(actual, expected))
    print('PASS: S5KJN1 four OEM register sequences and crops / IMX596 common init and all modes / crop / timing and GT9764 stock init / delays')


if __name__ == '__main__':
    main()
