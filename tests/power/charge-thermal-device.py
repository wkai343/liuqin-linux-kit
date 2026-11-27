#!/usr/bin/env python3
"""Bounded M81 current-limit and real-temperature recovery checks (on device)."""
import argparse
import errno
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

POWER = Path('/sys/class/power_supply')
BATTERY = POWER / 'qcom-battmgr-bat'
USB = POWER / 'qcom-battmgr-usb'


def read(path):
    value = path.read_text().strip()
    try:
        return int(value)
    except ValueError:
        return value


def write(path, value):
    data = (str(value) + '\n').encode('ascii')
    with path.open('wb', buffering=0) as stream:
        assert stream.write(data) == len(data)


def emit(event, **fields):
    record = dict(event=event, time=time.time(), **fields)
    print(json.dumps(record, sort_keys=True), flush=True)
    return record


def telemetry():
    values = {}
    for name in ('capacity', 'temp', 'current_now', 'voltage_now', 'status', 'health', 'charge_control_limit'):
        values['battery_' + name] = read(BATTERY / name)
    for name in ('online', 'usb_type', 'current_now', 'voltage_now', 'input_current_limit'):
        values['usb_' + name] = read(USB / name)
    if values['battery_temp'] >= 380 or values['battery_capacity'] < 10:
        raise RuntimeError('battery test stop condition')
    if not values['usb_online']:
        raise RuntimeError('external source disconnected')
    hottest = max(read(p / 'temp') for p in Path('/sys/class/thermal').glob('thermal_zone*')
                  if (p / 'type').read_text().startswith(('cpu', 'gpu', 'soc')))
    if hottest >= 70000:
        raise RuntimeError(f'SoC test stop temperature reached: {hottest} mC')
    values['soc_max_mc'] = hottest
    raw = next(Path('/sys/bus/auxiliary/devices').glob('pmic_glink.power-supply.*/sm8475_raw'))
    for name in ('raw_bat_charge_limit', 'raw_bat_charge_limit_max', 'raw_xm_thermal_temp'):
        values[name] = read(raw / name)
    return values


def collect(label, count=8):
    rows = []
    for _ in range(count):
        rows.append(emit('sample', stage=label, **telemetry()))
        time.sleep(2)
    return rows


def median(rows, name):
    return statistics.median(row[name] for row in rows[-5:])


def limits(state):
    initial = read(state)
    control = BATTERY / 'charge_control_limit'
    icl = USB / 'input_current_limit'
    icl_written = False
    try:
        for bad in (-1, 16, 1000000):
            try:
                write(control, bad)
            except OSError as error:
                if error.errno != errno.EINVAL:
                    raise
            else:
                raise AssertionError(f'accepted invalid thermal level {bad}')
            assert read(state) == initial
        write(state, 0)
        baseline = collect('baseline')
        for level in (7, 12, 15):
            write(control, level)
            assert read(state) == level
            rows = collect(f'level-{level}')
            if level == 15:
                limited = rows
        write(state, 0)
        recovered = collect('level-restored', 12)
        before = median(baseline, 'battery_current_now')
        hot = median(limited, 'battery_current_now')
        after = median(recovered, 'battery_current_now')
        # Positive M81 current denotes charging. Compare idle windows at the same
        # source; workload-induced battery drain alone is not accepted as evidence.
        assert before > 200000 and hot < before * 0.6, (before, hot)
        assert after > before * 0.6, (before, after)
        original_icl = read(icl)
        try:
            write(icl, 500000)
        except OSError as error:
            if error.errno != errno.EREMOTEIO:
                raise
            assert read(icl) == original_icl
            # Some M81 firmware rejects the independent USB vote despite the
            # OEM host interface. Record rejection; never call it a passed cap.
            emit('usb-control-rejected', errno=error.errno, unchanged_limit_ua=original_icl)
            input_result = {'usb_control': 'firmware-rejected'}
        else:
            icl_written = True
            input_limited = collect('usb-500mA')
            assert median(input_limited, 'usb_input_current_limit') <= 500000
            write(icl, -1)
            input_restored = collect('usb-vote-released', 12)
            assert median(input_restored, 'usb_input_current_limit') > 500000
            input_result = {'usb_control': 'verified',
                            'limited_usb_ua': median(input_limited, 'usb_current_now'),
                            'restored_usb_ua': median(input_restored, 'usb_current_now')}
        emit('limits-pass', baseline_battery_ua=before, limited_battery_ua=hot,
             restored_battery_ua=after, **input_result)
    finally:
        try:
            if icl_written:
                write(icl, -1)
        finally:
            write(state, initial)
            assert read(state) == initial
            emit('restored', initial_level=initial)


def cycle(mod, config, device, state):
    initial = read(state)
    loads = []
    try:
        write(state, 0)
        baseline = collect('cycle-baseline', 6)
        probe = mod.Policy(config)
        temperature, _ = probe.update(device.sample(config['weights']), device.screen_on())
        trigger, clear = temperature + 800, temperature + 200
        assert trigger < 35000, 'start from a cool tablet'
        adapted = dict(config, trigger_mc=[trigger], clear_mc=[clear],
                       screen_on_levels=[15], screen_off_levels=[15])
        controller = mod.Controller(mod.Policy(adapted), device)
        emit('test-thresholds', trigger_mc=trigger, clear_mc=clear, baseline_virtual_mc=temperature)
        for cpu in (0, 1, 2, 3):
            loads.append(subprocess.Popen(['taskset', '-c', str(cpu), sys.executable,
                                           '-c', 'import time; end=time.monotonic()+240\nwhile time.monotonic()<end: pass']))
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = controller.tick()
            assert not result.get('error'), result
            emit('heating', policy=result, **telemetry())
            if result['applied_level'] == 15:
                assert result['virtual_mc'] >= trigger
                break
            time.sleep(2)
        else:
            raise AssertionError('real temperature did not cross the lowered trip')
        for process in loads:
            process.terminate()
            process.wait(timeout=5)
        loads.clear()
        deadline = time.monotonic() + 600
        limited_rows = []
        while time.monotonic() < deadline:
            result = controller.tick()
            assert not result.get('error'), result
            row = emit('cooling', policy=result, **telemetry())
            if result['applied_level'] == 0:
                assert result['virtual_mc'] < clear
                break
            limited_rows.append(row)
            time.sleep(2)
        else:
            raise AssertionError('real temperature did not cool below the clear threshold')
        restored = collect('cycle-restored', 10)
        assert limited_rows, 'no limited state observed during natural cooling'
        assert median(restored, 'battery_current_now') > 200000
        emit('cycle-pass', limited_idle_samples=len(limited_rows),
             baseline_battery_ua=median(baseline, 'battery_current_now'),
             limited_battery_ua=median(limited_rows, 'battery_current_now'),
             restored_battery_ua=median(restored, 'battery_current_now'))
    finally:
        for process in loads:
            process.terminate()
            process.wait(timeout=5)
        write(state, initial)
        assert read(state) == initial
        emit('restored', initial_level=initial)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['limits', 'cycle'], required=True)
    parser.add_argument('--controller', default='/usr/local/libexec/liuqin-charge-thermal')
    parser.add_argument('--config', default='/usr/share/liuqin/charge-thermal.json')
    args = parser.parse_args()
    assert os.geteuid() == 0, 'run on the tablet with sudo'
    assert subprocess.run(['systemctl', 'is-active', '--quiet', 'liuqin-charge-thermal.service']).returncode != 0, 'stop the production thermal service for this bounded test'

    def interrupted(signum, frame):
        raise InterruptedError(f'signal {signum}')

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    loader = importlib.machinery.SourceFileLoader('charge_thermal', args.controller)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    config = json.loads(Path(args.config).read_text())
    device = mod.Device('/sys')
    emit('start', mode=args.mode, release=os.uname().release,
         temperatures_mc=device.sample(config['weights']), **telemetry())
    if args.mode == 'limits':
        limits(device.cooling())
    else:
        cycle(mod, config, device, device.cooling())


if __name__ == '__main__':
    main()
