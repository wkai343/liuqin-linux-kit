#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate stopped-stream HDR inputs. Does not claim HDR fusion or live AE sync."""
import argparse
import copy
import hashlib
import json
import math
from statistics import median
from pathlib import Path
import tempfile
import unittest
import uuid


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate(directory, images=True):
    directory = Path(directory)
    require((directory / 'COMPLETE').is_file(), 'capture did not complete')
    info = json.loads((directory / 'capture.json').read_text())
    require(info['schema'] in (1, 2) and info['exposure_basis'] == 'fixed-before-stream-on', 'unknown capture contract')
    require(info['hdr_merged'] is False and info['output_sequence_kind'] in ('converter', 'ife'), 'mislabelled output')
    uuid.UUID(info['boot_id'])
    width, height, stride = (info[k] for k in ('width', 'height', 'stride'))
    require(info['sensor'] in ('front', 'rear') and min(width, height) > 0 and
            not (width % 2 or height % 2) and stride >= width and
            info['frame_size'] == stride * height * 3 // 2, 'invalid linear NV12 geometry')
    seen_files = set()
    rows = [json.loads(line) for line in (directory / 'frames.jsonl').read_text().splitlines()]
    require(len(rows) == 198, 'incomplete warmup or bracket')
    last_timestamp = 0
    invariant = None
    selected = []
    ae_tail = []
    for generation in range(4):
        segment = rows[:150] if generation == 0 else rows[150 + (generation - 1) * 16:150 + generation * 16]
        last_sequence = -1
        settings = None
        for ordinal, row in enumerate(segment, 1):
            require(row['generation'] == generation and row['ordinal'] == ordinal, 'mixed generations or missing frame')
            require(row['fixed'] == (generation != 0), 'unlocked exposure leg')
            timestamp = row['sensor_timestamp_ns']
            require(timestamp > last_timestamp and row['sequence'] > last_sequence, 'duplicate/reordered frame')
            require(timestamp // 1000 == row['timestamp_ns'] // 1000, 'sensor/converter timestamp mismatch')
            last_timestamp, last_sequence = timestamp, row['sequence']
            parameters = [row['exposure_us'], row['analogue_gain'], *row['colour_gains'], *row['ccm']]
            require(all(math.isfinite(x) for x in parameters), 'non-finite imaging parameters')
            require(row['exposure_us'] > 0 and row['analogue_gain'] >= 1, 'invalid exposure')
            if not generation:
                # Use the settled end of warmup; a single late AE update must
                # not move the still's output exposure by several stops.
                if ordinal > len(segment) - 8:
                    ae_tail.append(row['exposure_us'] * row['analogue_gain'])
                continue
            require(settings is None or parameters == settings, 'imaging parameters changed within exposure leg')
            settings = parameters
            if invariant is None:
                invariant = parameters[1:]
            require(parameters[1:] == invariant, 'gain/WB/CCM changed across bracket')
            require(abs(row['exposure_us'] - info['requested_us'][generation - 1]) <= 30, 'wrong exposure assigned to leg')
            if ordinal in (1, 16):
                name = row['image']
                require(name == f"leg-{generation}-frame-{row['sequence']}.nv12",
                        'image filename does not match stream generation and sequence')
                path = directory / name
                checksum = row.get('image_sha256', '')
                if info['schema'] == 2:
                    require(len(checksum) == 64 and all(c in '0123456789abcdef' for c in checksum),
                            'missing/invalid captured digest')
                if images:
                    require(not path.is_symlink() and path.is_file(), 'missing or linked image')
                    st = path.stat()
                    require((st.st_dev, st.st_ino) not in seen_files, 'aliased frame file')
                    seen_files.add((st.st_dev, st.st_ino))
                    require(st.st_size == info['frame_size'], 'truncated image')
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    if info['schema'] == 2:
                        require(digest == checksum, 'image differs from completed request')
                if ordinal == 16:
                    item = dict(generation=generation, sensor_timestamp_ns=timestamp,
                                exposure_us=row['exposure_us'],
                                analogue_gain=row['analogue_gain'], image=name)
                    item[info['output_sequence_kind'] + '_sequence'] = row['sequence']
                    if images:
                        item['sha256'] = digest
                        sample = data[:info['stride'] * info['height']:17]
                        item['mean_y'] = sum(sample) / len(sample)
                    selected.append(item)
            else:
                require(row['image'] == '' and not row.get('image_sha256'), 'unexpected image selection')
    products = [x['exposure_us'] * x['analogue_gain'] for x in selected]
    require(products[0] < products[1] < products[2], 'exposures are not strictly ordered')
    return dict(schema=info['schema'], captured_digests_verified=bool(images and info['schema'] == 2),
                sensor=info['sensor'], boot_id=info['boot_id'], width=width, height=height, stride=stride,
                exposure_basis=info['exposure_basis'], hdr_merged=False,
                ae_to_middle=median(ae_tail) / products[1],
                selected_frames=selected, medium_to_short=products[1] / products[0],
                long_to_medium=products[2] / products[1], long_to_short=products[2] / products[0])


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        info = dict(schema=1, exposure_basis='fixed-before-stream-on', hdr_merged=False,
                    output_sequence_kind='converter', boot_id=str(uuid.uuid4()), sensor='front',
                    width=4, height=4, stride=4, frame_size=24, requested_us=[2000, 8000, 32000])
        (self.path / 'capture.json').write_text(json.dumps(info))
        (self.path / 'COMPLETE').touch()
        self.rows = []
        for g, n in enumerate((150, 16, 16, 16)):
            for i in range(n):
                t = (len(self.rows) + 1) * 33333000 + 789
                self.rows.append(dict(generation=g, ordinal=i + 1, sequence=i, fixed=bool(g),
                    timestamp_ns=t // 1000 * 1000, sensor_timestamp_ns=t,
                    exposure_us=(10000, 2000, 8000, 32000)[g], analogue_gain=1,
                    colour_gains=[1, 1], ccm=[1, 0, 0, 0, 1, 0, 0, 0, 1],
                    image=f'leg-{g}-frame-{i}.nv12' if g and i in (0, 15) else ''))

    def tearDown(self):
        self.temp.cleanup()

    def run_rows(self, rows):
        (self.path / 'frames.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return validate(self.path, images=False)

    def test_valid(self):
        self.assertEqual(self.run_rows(self.rows)['long_to_short'], 16)

    def test_settled_ae_exposure(self):
        for row in self.rows[:142]:
            row['exposure_us'] = 1000
        for row in self.rows[142:150]:
            row.update(exposure_us=15000, analogue_gain=2)
        # Last-frame outlier must not override the recent exposure product.
        self.rows[149]['exposure_us'] = 500
        self.assertEqual(self.run_rows(self.rows)['ae_to_middle'], 30000 / 8000)

    def test_schema_two_pixel_swap(self):
        info = json.loads((self.path / 'capture.json').read_text())
        info['schema'] = 2
        (self.path / 'capture.json').write_text(json.dumps(info))
        for index, row in enumerate(self.rows):
            if row['image']:
                data = bytes([index]) * 24
                (self.path / row['image']).write_bytes(data)
                row['image_sha256'] = hashlib.sha256(data).hexdigest()
        self.run_rows(self.rows)
        self.assertTrue(validate(self.path)['captured_digests_verified'])
        (self.path / self.rows[165]['image']).write_bytes((self.path / self.rows[181]['image']).read_bytes())
        with self.assertRaisesRegex(ValueError, 'differs from completed request'):
            validate(self.path)

    def test_corruptions(self):
        mutations = [
            lambda r: r.pop(155),
            lambda r: r[170].update(sensor_timestamp_ns=r[169]['sensor_timestamp_ns']),
            lambda r: r[170].update(timestamp_ns=r[169]['timestamp_ns']),
            lambda r: r[170].update(sequence=r[169]['sequence']),
            lambda r: r[170].update(exposure_us=2000),
            lambda r: r[170].update(analogue_gain=2),
            lambda r: r[170].update(colour_gains=[2, 1]),
            lambda r: r[170].update(fixed=False),
            lambda r: r[165].update(image='../image.nv12'),
            lambda r: r[165].update(image=''),
            lambda r: r[165].update(image=r[181]['image']),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                rows = copy.deepcopy(self.rows)
                mutate(rows)
                with self.assertRaises(ValueError):
                    self.run_rows(rows)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture', type=Path, nargs='?')
    p.add_argument('--output', type=Path)
    p.add_argument('--self-test', action='store_true')
    args = p.parse_args()
    if args.self_test:
        unittest.main(argv=['camera-hdr-validate'])
    else:
        if args.capture is None:
            p.error('a capture directory is required')
        result = validate(args.capture)
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
