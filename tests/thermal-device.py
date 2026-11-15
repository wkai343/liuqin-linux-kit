#!/usr/bin/env python3
"""Bounded Liuqin thermal test using a temporarily lowered passive trip.

Run on the tablet as root. JSON lines go to stdout. CPU tests load the selected
frequency domain; GPU tests take an offscreen command after --workload.
Only the selected passive trip changes; SIGINT/SIGTERM and ordinary failures
stop the workload and restore that trip. Do not use SIGKILL during this test.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import time


def read(path):
    text = path.read_text().strip()
    try:
        return int(text)
    except ValueError:
        return text


def emit(event, **values):
    print(json.dumps(dict(event=event, time=time.time(), **values)), flush=True)


def worker(cpu):
    os.sched_setaffinity(0, {cpu})
    os.nice(10)
    end = time.monotonic() + 95
    while time.monotonic() < end:
        hashlib.pbkdf2_hmac('sha256', b'liuqin-thermal', b'bounded-load', 30000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zone', required=True)
    parser.add_argument('--cpu', type=int, choices=range(8))
    parser.add_argument('--workload', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('root is required to write the passive trip')
    if (args.cpu is None) == (not args.workload):
        parser.error('provide exactly one of --cpu or --workload')
    zones = {read(p / 'type'): p for p in Path('/sys/class/thermal').glob('thermal_zone*')}
    zone = zones[args.zone]
    trip = zone / 'trip_point_0_temp'
    assert read(zone / 'trip_point_0_type') == 'passive'
    original = read(trip)
    hysteresis = read(zone / 'trip_point_0_hyst')
    devices = list({p.resolve() for p in zone.glob('cdev*') if p.is_symlink()})
    assert devices, f'{args.zone} has no thermal cooling binding'
    assert all(read(p / 'cur_state') == 0 for p in devices), 'Cooling already active'
    all_trips = {p: read(p) for z in zones.values() for p in z.glob('trip_point_*_temp')}
    if args.cpu is not None:
        assert args.zone.startswith(f'cpu{args.cpu}'), 'CPU and sensor must agree'
        policy = next(p for p in Path('/sys/devices/system/cpu/cpufreq').glob('policy*')
                      if str(args.cpu) in str(read(p / 'affected_cpus')).split())
        cap = policy / 'scaling_max_freq'
        actual = policy / 'cpuinfo_cur_freq'
    else:
        assert args.zone in ('gpu-top-thermal', 'gpu-bottom-thermal')
        policy = Path('/sys/class/devfreq/3d00000.gpu')
        cap, actual = policy / 'max_freq', policy / 'cur_freq'
    original_cap = read(cap)
    children = []

    def sample(stage):
        temps = {n: read(p / 'temp') for n, p in zones.items()}
        states = {read(p / 'type'): read(p / 'cur_state') for p in devices}
        point = dict(stage=stage, temperatures=temps, cooling=states,
                     frequency=read(actual), maximum=read(cap))
        battery = Path('/sys/class/power_supply/qcom-battmgr-bat')
        usb = Path('/sys/class/power_supply/qcom-battmgr-usb')
        point['battery'] = {n: read(battery / n) for n in ('temp', 'current_now', 'status')}
        point['usb'] = {n: read(usb / n) for n in ('current_now', 'input_current_limit')}
        emit('sample', **point)
        for name, value in temps.items():
            limit = 40000 if name == 'qcom-battmgr-bat' else 75000
            assert isinstance(value, int) and -10000 < value < limit, (name, value)
        return point

    def stop():
        for child in children:
            if isinstance(child, multiprocessing.Process):
                child.terminate()
            else:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in children:
            if isinstance(child, multiprocessing.Process):
                child.join(5)
                if child.is_alive():
                    child.kill()
                    child.join(5)
            else:
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(5)
        children.clear()

    def interrupted(signum, _frame):
        raise SystemExit(f'interrupted by signal {signum}')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    # Let SSH/sudo startup activity settle before choosing a real heat-up target.
    for _ in range(5):
        sample('settling')
        time.sleep(1)
    target = max(35000, read(zone / 'temp') + 4000)
    assert target <= 55000 and target < original, 'Let the tablet cool before testing'
    engaged = recovered = limited = False
    try:
        emit('begin', kernel=os.uname().release, zone=args.zone,
             original_trip=original, test_trip=target, hysteresis=hysteresis,
             original_maximum=original_cap)
        sample('before')
        trip.write_text(str(target))
        if args.cpu is not None:
            for cpu in str(read(policy / 'affected_cpus')).split():
                child = multiprocessing.Process(target=worker, args=(int(cpu),))
                child.start()
                children.append(child)
        else:
            child = subprocess.Popen(args.workload, start_new_session=True,
                                     stdout=subprocess.DEVNULL)
            children.append(child)
        end = time.monotonic() + 90
        hit = None
        while time.monotonic() < end:
            point = sample('load')
            if any(point['cooling'].values()):
                engaged = True
                if hit is None:
                    hit = time.monotonic()
                if point['maximum'] < original_cap and point['frequency'] <= point['maximum']:
                    limited = True
                if time.monotonic() - hit >= 8:
                    break
            if args.workload and children[0].poll() is not None:
                raise RuntimeError('GPU workload exited before the load phase completed')
            time.sleep(0.25)
        stop()
        assert engaged and limited, 'No verified frequency limit under load'
        end = time.monotonic() + 120
        while time.monotonic() < end:
            point = sample('cooldown')
            if (point['temperatures'][args.zone] < target - hysteresis
                    and not any(point['cooling'].values())
                    and point['maximum'] == original_cap):
                recovered = True
                break
            time.sleep(0.5)
        assert recovered, 'Cooling did not release after natural cooldown'
    finally:
        try:
            stop()
        finally:
            trip.write_text(str(original))
            changed = {str(p): [value, read(p)] for p, value in all_trips.items()
                       if read(p) != value}
            assert not changed, f'Thermal thresholds not restored: {changed}'
            emit('restored', original_trip=read(trip), all_trip_temperatures_match=True)
    emit('result', passed=engaged and limited and recovered)


if __name__ == '__main__':
    main()
