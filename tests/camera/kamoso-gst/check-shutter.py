#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One single still, then a two-frame burst in an isolated official Kamoso."""
import argparse
import json
from pathlib import Path
import re
import time
import gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf
import pyatspi


def wait(test, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if result := test():
            return result
        time.sleep(.012)
    raise TimeoutError(f'condition not met within {seconds}s')


def nodes(n):
    yield n
    for child in n:
        if child:
            yield from nodes(child)


def buttons(pid):
    for app in pyatspi.Registry.getDesktop(0):
        if app and app.get_process_id() == pid:
            return {n.name: n for n in nodes(app) if n.getRoleName() == 'button'
                    and n.getState().contains(pyatspi.STATE_SHOWING)}
    return {}


def action(pid, names):
    for name, btn in buttons(pid).items():
        if name in names and btn.getState().contains(pyatspi.STATE_ENABLED):
            return btn


def press(btn):
    if not btn.queryAction().doAction(0):
        raise RuntimeError('UI action failed')


def marker(path):
    info, _, _ = GdkPixbuf.Pixbuf.get_file_info(str(path))
    assert info and info.get_name() == 'jpeg', path
    pix = GdkPixbuf.Pixbuf.new_from_file(str(path))
    w, h = pix.get_width(), pix.get_height()
    values = pix.get_pixels()
    codes = []
    for dx, dy in ((1,0), (-1,0), (0,1), (0,-1)):
        code = 0
        for bit in range(8):
            x, y = int(w*(.5+dx*(bit+.5)/16)), int(h*(.5+dy*(bit+.5)/16))
            offset = y*pix.get_rowstride()+x*pix.get_n_channels()
            rgb = values[offset:offset+3]
            assert max(rgb)-min(rgb)<12 and (min(rgb)>170 or max(rgb)<80), (path, bit, rgb)
            code |= int(min(rgb)>170) << bit
        codes.append(code)
    assert codes.count(codes[0]) == 4, (path, codes)
    return dict(file=path.name, width=w, height=h, code=codes[0])


def main(pid, directory, log):
    start = ('快速连拍','Capture a Burst')
    stop = ('结束连拍','停止连拍','End Burst')
    single = ('拍摄照片','Take a Picture')
    def history(kind):
        return [(int(n), int(t)) for n,t in re.findall(rf'^SHUTTERPROOF {kind} (\d+) (\d+)', log.read_text(), re.M)]
    def files():
        return sorted(directory.glob('*.jpg'))
    def finished(count):
        found = files()
        if len(found) != count:
            return False
        try:
            for f in found:
                with f.open('rb') as stream:
                    stream.seek(-2, 2)
                    if stream.read() != b'\xff\xd9':
                        return False
            return True
        except (OSError, ValueError):
            return False
    wait(lambda: log.exists() and 'SHUTTERPROOF preview ' in log.read_text(), 18)
    assert not files() and not history('prepare')
    press(wait(lambda: action(pid, single), 6))
    wait(lambda: finished(1), 6)
    assert marker(files()[0])['code'] == 0xb1
    press(wait(lambda: action(pid, start), 3))
    try:
        wait(lambda: finished(2), 5)
        wait(lambda: action(pid, stop), 1)
        wait(lambda: len(history('prepare')) >= 3, 4)
        before = len(files())
    finally:
        btn = action(pid, stop)
        if btn is not None:
            press(btn)
    stopped = time.monotonic_ns()//1000
    assert before == 2
    wait(lambda: finished(3), 4)
    time.sleep(2.7)
    req, ret = history('prepare'), history('return')
    assert [n for n,t in req] == [1,2,3] == [n for n,t in ret]
    assert req[-1][1] < stopped < ret[-1][1]
    result = [marker(f) for f in files()]
    assert [f['code'] for f in result] == [0xb1,0xb2,0xb3]
    print(json.dumps(dict(requests=req, returns=ret, stop_completed_us=stopped, saved=result),indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('pid',type=int)
    p.add_argument('photos',type=Path)
    p.add_argument('log',type=Path)
    a = p.parse_args()
    main(a.pid, a.photos, a.log)
