#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Press two Kamoso rear shutters in the same second and check the saved JPEGs."""
import argparse
import json
from pathlib import Path
import threading
import time

import gi
import pyatspi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf


def buttons(node):
    if node.getRole() == pyatspi.ROLE_PUSH_BUTTON and node.getState().contains(pyatspi.STATE_SHOWING):
        yield node
    for child in node:
        if child:
            yield from buttons(child)


def shutter(app):
    visible = {button.name: button for button in buttons(app)}
    if not {'闪光灯：打开', 'Flash: Forced'}.intersection(visible):
        raise ValueError(f'Choose the rear camera and forced flash in Kamoso PID {app.get_process_id()}')
    return next(visible[name] for name in ('拍摄照片', 'Take a Picture') if name in visible)


def watch_led(path, edges, stop):
    while not stop.is_set():
        now = time.monotonic()
        level = int(path.read_text())
        if not edges or level != edges[-1][1]:
            edges.append((now, level))
        stop.wait(0.005)


def check_pair(shutters, directory, led):
    before = set(directory.glob('picture_*.jpg'))
    edges = []
    stop = threading.Event()
    watcher = threading.Thread(target=watch_led, args=(led, edges, stop))
    watcher.start()
    try:
        # Leave enough room in the second for both Qt event loops to dispatch.
        time.sleep(1.02 - time.time() % 1)
        start = time.time()
        for button in shutters:
            if not button.getState().contains(pyatspi.STATE_ENABLED) or not button.queryAction().doAction(0):
                raise RuntimeError('Shutter was unavailable')
        dispatched = time.time()
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            photos = sorted(set(directory.glob('picture_*.jpg')) - before)
            if len(photos) >= 2:
                break
            time.sleep(0.025)
        time.sleep(0.25)
    finally:
        stop.set()
        watcher.join()
    photos = sorted(set(directory.glob('picture_*.jpg')) - before)
    pulses = [round(1000 * (off[0] - on[0]), 1) for on, off in zip(edges, edges[1:]) if on[1]]
    result = dict(dispatch_ms=round((dispatched - start) * 1000), files=[p.name for p in photos],
                  pulses_ms=pulses, same_second=int(start) == int(dispatched))
    print(json.dumps(result), flush=True)
    if (not result['same_second'] or len(photos) != 2 or len(pulses) != 2 or
            not all(100 <= duration <= 250 for duration in pulses) or not edges or edges[-1][1]):
        raise RuntimeError('Expected two separate 200 ms strobes and two new photos')
    for photo in photos:
        kind, width, height = GdkPixbuf.Pixbuf.get_file_info(str(photo))
        if kind is None or kind.get_name() != 'jpeg' or (width, height) != (4080, 3060):
            raise ValueError(f'Unexpected rear photo format: {photo}')
        GdkPixbuf.Pixbuf.new_from_file(str(photo))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pids', type=int, nargs=2, required=True, metavar=('A', 'B'))
    parser.add_argument('--directory', type=Path, required=True, help='both windows must save to this directory')
    parser.add_argument('--pairs', type=int, default=3)
    args = parser.parse_args()
    if len(set(args.pids)) != 2 or args.pairs < 1 or not args.directory.is_dir():
        parser.error('supply two distinct PIDs, a positive pair count and an existing photo directory')
    apps = {app.get_process_id(): app for app in pyatspi.Registry.getDesktop(0)
            if app and app.name.lower() == 'kamoso'}
    shutters = [shutter(apps[pid]) for pid in args.pids]
    led = Path('/sys/class/leds/white:flash/flash_strobe')
    for _ in range(args.pairs):
        check_pair(shutters, args.directory, led)


if __name__ == '__main__':
    main()
