#!/usr/bin/env python3
"""Check the compiled Liuqin thermal policy, including cpufreq membership."""
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dtb', required=True)
    args = parser.parse_args()

    def get(node, prop, kind='x'):
        output = subprocess.check_output(
            ['fdtget', '-t', kind, args.dtb, node, prop], text=True).strip()
        return output if kind == 's' else [int(x, 16) for x in output.split()]

    def children(node):
        return subprocess.check_output(['fdtget', '-l', args.dtb, node], text=True).split()

    cpus = {}
    for node in children('/cpus'):
        if node.startswith('cpu@'):
            path = '/cpus/' + node
            reg = get(path, 'reg')
            cpus[reg[-1] >> 8] = get(path, 'phandle')[0]
    assert set(cpus) == set(range(8)), cpus
    zones = 0
    for cpu in range(8):
        parts = [''] if cpu < 4 else ['-top', '-bottom'] if cpu < 7 else ['-top', '-middle', '-bottom']
        cluster = range(4) if cpu < 4 else range(4, 7) if cpu < 7 else [7]
        expected = {cpus[c] for c in cluster}
        for part in parts:
            path = f'/thermal-zones/cpu{cpu}{part}-thermal'
            assert get(path, 'polling-delay-passive') == [250], path
            trips = path + '/trips'
            for index, temp in enumerate([90000, 95000]):
                trip = f'{trips}/trip-point{index}'
                assert get(trip, 'temperature') == [temp], trip
                assert get(trip, 'hysteresis') == [2000], trip
                assert get(trip, 'type', 's') == 'passive', trip
                matches = []
                for name in children(path + '/cooling-maps'):
                    node = path + '/cooling-maps/' + name
                    if get(node, 'trip') == get(trip, 'phandle'):
                        cells = get(node, 'cooling-device')
                        assert len(cells) % 3 == 0, node
                        assert set(cells[::3]) == expected, (node, cells, expected)
                        assert all(x == 0xffffffff for i, x in enumerate(cells) if i % 3), node
                        matches.append(node)
                assert len(matches) == 1, (trip, matches)
            assert get(trips + '/cpu-crit', 'temperature') == [110000], path
            assert get(trips + '/cpu-crit', 'type', 's') == 'critical', path
            zones += 1
    for part in ['top', 'bottom']:
        path = f'/thermal-zones/gpu-{part}-thermal'
        trips = {get(path + '/trips/' + n, 'type', 's'):
                 get(path + '/trips/' + n, 'temperature')[0]
                 for n in children(path + '/trips')}
        assert trips == {'passive': 85000, 'hot': 90000, 'critical': 110000}, trips
        assert children(path + '/cooling-maps'), path
    print(f'PASS: {zones} CPU zones, both passive trips, policy membership, critical trips and GPU policy')


if __name__ == '__main__':
    main()
