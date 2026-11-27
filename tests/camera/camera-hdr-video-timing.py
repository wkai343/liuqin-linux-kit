#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Match 4K HDR capture and processing events to the timestamps in the saved movie."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from fractions import Fraction
import json
from math import ceil
from pathlib import Path
from statistics import mean, median
import subprocess


def startup_report(pts, by_index, captures, claimed):
    intervals = [i for i in range(len(pts) - 1) if pts[i] < 10**9]
    if not intervals or any(i not in by_index for i in range(intervals[-1] + 2)):
        raise ValueError('missing first-second stage events; use LIUQIN_HDR_TRACE_STARTUP=1')
    groups = [by_index[i] for i in range(intervals[-1] + 2)]
    sent = {(seq, slot): wall for event, seq, slot, _, wall in captures if event == 'sent'}
    if any((g['seq'] + 2, g['slot']) not in sent for g in groups):
        raise ValueError('missing first-second interleaved capture events')
    longest = max(intervals, key=lambda i: (pts[i+1] - pts[i],
                                            groups[i+1]['sensor_ns'] - groups[i]['sensor_ns']))
    left, right = groups[longest:longest+2]
    origin = groups[0]['sensor_ns']
    stages = {'registration': ('register_begin', 'register_end'),
              'ipe_write': ('ipe_begin', 'ipe_written'), 'ipe_read': ('ipe_written', 'ipe_end'),
              'hlg': ('hlg_begin', 'hlg_end'), 'encoder_queue': ('push_begin', 'push_end'),
              'wait_for_encoder': ('encoder_wait_begin', 'encoder_wait_end')}
    def ms(ns):
        return round(ns / 1e6, 3)

    missed = []
    for event, seq, _, sensor, wall in captures:
        if not left['seq'] + 2 < seq < right['seq']:
            continue
        active, owners = [], []
        for g in groups:
            start = claimed.get((g['seq'], g['slot']))
            if start is not None and start <= wall <= g['release']:
                state = ('capturing_or_sending' if wall < sent[g['seq']+2, g['slot']] and wall < g['packet'] else
                         'queued_for_registration' if wall < g['register_begin'] else
                         'registering' if wall < g['register_end'] else 'waiting_for_release')
                owners.append(dict(slot=g['slot'], group=g['index'], state=state))
            active.extend((g['index'], name) for name, (begin, end) in stages.items()
                          if begin in g and g[begin] <= wall < g[end])
        missed.append(dict(event=event, sequence=seq, sensor_ms=ms(sensor-origin),
                           slot_owners=owners, active_stages=active))
    return dict(first_second_max_ms=ms(pts[longest+1]-pts[longest]),
                pts_ms=[ms(pts[longest]), ms(pts[longest+1])],
                source_gap_ms=ms(right['sensor_ns']-left['sensor_ns']),
                short_sequences=[left['seq'], right['seq']], capture_events_in_gap=missed,
                groups=[dict(index=g['index'], short_sequence=g['seq'], sensor_ms=ms(g['sensor_ns']-origin),
                             stage_ms={name: ms(g[end]-g[begin]) for name, (begin, end) in stages.items()
                                       if begin in g})
                        for g in groups])


def tail_report(log, clip, startup=False):
    groups = {}
    captures = []
    for line in log.read_text().splitlines():
        fields = line.split()
        if fields[:2] == ['HDR', 'stage_event']:
            event = fields[2]
            timestamp, wall, *detail = map(int, fields[3:])
            group = groups.setdefault(timestamp, {'sensor_ns': timestamp})
            if event == 'file_input':
                # A Matroska block header and its payload can carry the same PTS.
                group.setdefault(event, wall)
            else:
                group[event] = wall
            if event == 'packet':
                group['index'], group['seq'], group['slot'] = detail
            elif event == 'vp9_end':
                group['keyframe'], *queued = detail
                if queued:
                    group['queued_frames'], = queued
        elif fields[:2] == ['HDR', 'capture_event']:
            captures.append((fields[2], *map(int, fields[3:])))
    probe = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=width,height,color_transfer,time_base:packet=pts:format=duration', '-of', 'json', clip]))
    stream = probe['streams'][0]
    if (stream['width'], stream['height'], stream.get('color_transfer')) != (3840, 2160, 'arib-std-b67'):
        raise ValueError('expected 3840x2160 HLG video')
    tick = Fraction(stream['time_base']) * 10**9
    pts = [round(int(p['pts']) * tick) for p in probe['packets']]
    end = round(Decimal(probe['format']['duration']) * 10**9)
    lo = max(0, end - 60 * 10**9)
    first = bisect_left(pts, lo)
    if not pts or pts[0] != 0 or any(a >= b for a, b in zip(pts, pts[1:])):
        raise ValueError('expected strictly increasing sensor-timed video packets starting at zero')

    by_index = {}
    offsets = []
    for ts, g in sorted(groups.items()):
        i = g.get('index')
        if i is None:  # A window boundary can split a group across logging processes.
            continue
        if not 0 <= i < len(pts) or i in by_index:
            raise ValueError('invalid group index for this movie')
        offsets.append(ts - pts[i])
        by_index[i] = g
    if not offsets or any(i not in by_index for i in range(max(0, first-1), len(pts))):
        raise ValueError('missing end-of-recording trace; use LIUQIN_HDR_TRACE_TAIL=1')
    if max(offsets) - min(offsets) > tick:
        raise ValueError('encoded timestamps do not match the traced middle exposures')
    origin = round(median(offsets))
    if not any(e == 'sent' and ts - origin >= lo for e, _, _, ts, _ in captures):
        raise ValueError('missing capture-side tail trace; use a matching camera-hdr-live')
    times = defaultdict(list)
    for i in range(first, len(pts)):
        g = by_index[i]
        times['registration'].append(g['register_end'] - g['register_begin'])
        times['release_after_registration'].append(g['release'] - g['register_end'])
        times['hlg'].append(g['hlg_end'] - g['hlg_begin'])
        times['encoder_queue'].append(g['push_end'] - g['push_begin'])
        if not i:
            continue
        prev = by_index[i-1]
        write = prev['ipe_written'] - prev['ipe_begin']
        read = prev['ipe_end'] - prev['ipe_written']
        # The submitting thread waits for its previous encoder job before
        # consuming the next registered group. Charge simultaneous waits once.
        wait_end = prev['encoder_wait_end'] if i > 1 else prev['ipe_end']
        encoder = wait_end - prev['encoder_wait_begin'] if i > 1 else 0
        registration = max(0, g['register_end'] - wait_end)
        period = g['ipe_begin'] - prev['ipe_begin']
        rest = period - write - read - encoder - registration
        if min(write, read, encoder, registration, rest) < 0:
            raise ValueError('inconsistent event order')
        for key, value in [('submit_period', period), ('ipe_write', write), ('ipe_read', read),
                           ('wait_for_encoder', encoder), ('wait_for_registration', registration),
                           ('dispatch_overhead', rest)]:
            times[key].append(value)

    # A rejected bracket may retain its slot for a subsequent attempt. Only
    # attribute ownership when the accepted short-exposure sequence matches.
    claimed = {(seq, slot): wall for e, seq, slot, _, wall in captures if e == 'acquire'}
    slots = defaultdict(list)
    for g in by_index.values():
        start = claimed.get((g['seq'], g['slot']))
        if start is not None:
            slots[g['slot']].append((start, g))
    starts = {slot: [t for t, _ in spans] for slot, spans in slots.items()}
    drops = complete = 0
    held = Counter()
    for event, _, _, ts, wall in captures:
        if event != 'no_slot' or not lo <= ts - origin < end:
            continue
        drops += 1
        states = []
        for slot, spans in slots.items():
            j = bisect_right(starts[slot], wall) - 1
            if j < 0:
                continue
            _, g = spans[j]
            if wall <= g['release']:
                states.append('capture_or_queued' if wall < g['register_begin'] else
                              'registering' if wall < g['register_end'] else 'waiting_for_release')
        if len(states) == 3:
            held.update(states)
            complete += 1
    windows = [bisect_left(pts, min(t + 10**10, end)) - bisect_left(pts, t)
               for t in range(lo, end, 10**10)]
    report = dict(frames=len(pts), first_second_max_ms=max(
                    ((b-a)/1e6 for a, b in zip(pts, pts[1:]) if a < 10**9), default=None),
                tail_frames_per_10s=windows, no_slot=drops, fully_matched_drops=complete,
                slots_at_matched_drops=dict(held), tail_ms={
                    k: dict(mean=round(mean(v)/1e6, 3), median=round(median(v)/1e6, 3))
                    for k, v in times.items()})
    if startup:
        report['startup'] = startup_report(pts, by_index, captures, claimed)
    if any('vp9_begin' in g for g in by_index.values()):
        stages = defaultdict(list)
        queue_depths = Counter()
        for i in range(first, len(pts)):
            g = by_index[i]
            events = ('dequeue', 'vp9_begin', 'vp9_end', 'mux_input', 'file_input')
            if any(e not in g for e in events):
                raise ValueError(f'incomplete encoder trace for frame {i}')
            spans = [g[b] - g[a] for a, b in zip(events, events[1:])]
            if min(spans) < 0:
                raise ValueError(f'out-of-order encoder trace for frame {i}')
            for stage, ns in zip(('dequeue_to_vp9', 'vp9', 'vp9_to_mux', 'mux_to_filesink'), spans):
                stages[stage].append(ns)
            stages['vp9_keyframe' if g['keyframe'] else 'vp9_interframe'].append(spans[1])
            if 'queued_frames' in g:
                queue_depths[g['queued_frames']] += 1
        report['encoder_tail_ms'] = {
            name: dict(frames=len(v), mean=round(mean(v)/1e6, 3),
                       p95=round(sorted(v)[ceil(.95*len(v))-1]/1e6, 3))
            for name, v in stages.items()}
        if queue_depths:
            report['appsrc_queued_frames_at_vp9_output'] = dict(sorted(queue_depths.items()))
    return report


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('log', type=Path)
    cli.add_argument('movie', type=Path)
    cli.add_argument('--startup', action='store_true', help='also attribute the longest first-second gap')
    args = cli.parse_args()
    print(json.dumps(tail_report(args.log, args.movie, args.startup), indent=2))
