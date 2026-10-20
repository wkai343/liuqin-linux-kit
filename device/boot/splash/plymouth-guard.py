#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Bound root-stage splash IPC; record and release an unresponsive daemon."""
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

STATE = Path('/run/liuqin-plymouth-guard')
PID_FILE = Path('/run/plymouth/pid')
CLIENT = '/usr/bin/plymouth'
DAEMON = '/usr/bin/plymouthd'


def event(name, **fields):
    item = dict(event=name, monotonic=time.monotonic(), **fields)
    line = json.dumps(item, ensure_ascii=True) + '\n'
    print(line, end='', flush=True)
    try:
        STATE.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(STATE / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(line)
    except OSError:
        pass


def call(argv, seconds):
    try:
        # A daemon may retain standard descriptors after forking. Do not wait
        # for pipe EOF or read keyboard/terminal contents when recording IPC.
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=seconds,
                              start_new_session=True).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError as error:
        event('exec-error', program=argv[0], errno=error.errno)
        return 127


def identity(pid):
    root = Path('/proc') / str(pid)
    if root.stat().st_uid != 0 or (root / 'comm').read_text().strip() != 'plymouthd':
        raise ValueError('not a root Plymouth daemon')
    executable = os.readlink(root / 'exe').removesuffix(' (deleted)')
    if Path(executable).name != 'plymouthd':
        raise ValueError('unexpected executable')
    # Field22=starttime; comm may contain spaces or parentheses.
    started = (root / 'stat').read_text().rsplit(') ', 1)[1].split()[19]
    return executable, started


def release_stalled_daemon(action):
    pidfd = None
    try:
        fd = os.open(PID_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('unsafe PID file')
            text = stream.read(32).strip()
        if not text.isdecimal() or int(text) <= 1:
            raise ValueError('invalid PID')
        pid = int(text)
        before = identity(pid)
        pidfd = os.pidfd_open(pid)
        if identity(pid) != before:
            raise ValueError('daemon identity changed')
        root = Path('/proc') / str(pid)
        evidence = {'pid': pid, 'executable': before[0], 'start_ticks': before[1],
                    'wchan': (root / 'wchan').read_text().strip()}
        # Record only descriptor names and flags, never terminal/input data.
        evidence['fds'] = {}
        for entry in sorted((root / 'fd').iterdir())[:24]:
            try:
                target = os.readlink(entry)
                flags = next((s for s in (root / 'fdinfo' / entry.name).read_text().splitlines()
                              if s.startswith('flags:')), '')
                evidence['fds'][entry.name] = {'target': target, 'flags': flags}
            except OSError:
                pass
        event('stalled-daemon', action=action, **evidence)
        # pidfd pins the checked process: a recycled numeric PID is never used.
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if not select.select([pidfd], [], [], 0.5)[0]:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        stopped = bool(select.select([pidfd], [], [], 1)[0])
        event('daemon-released', action=action, pid=pid, stopped=stopped)
        return stopped
    except (OSError, ValueError, AttributeError) as error:
        event('daemon-not-signalled', action=action, reason=str(error))
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)


def request(action, argv, seconds):
    result = call(argv, seconds)
    event('client-result', action=action, result=result)
    if result == 124:
        release_stalled_daemon(action)
    return result


def main(argv):
    if len(argv) != 1 or argv[0] not in ('start', 'show', 'read-write', 'quit', 'wait'):
        return 2
    action = argv[0]
    if action == 'start':
        result = call([CLIENT, '--ping'], 1)
        if result == 0:
            event('reuse-daemon')
            return 0
        if result == 124 and not release_stalled_daemon('ping'):
            return 0
        request('start', [DAEMON, '--mode=boot', '--pid-file=/run/plymouth/pid',
                          '--attach-to-session'], 5)
    elif action == 'show':
        request(action, [CLIENT, 'show-splash'], 3)
    elif action == 'read-write':
        request(action, [CLIENT, 'update-root-fs', '--read-write'], 3)
    elif action == 'quit':
        request(action, [CLIENT, 'quit', '--retain-splash'], 3)
    else:
        # This waiter runs alongside quit. Its own expiry is not evidence
        # that the daemon is unresponsive, and must never kill a healthy one.
        event('client-result', action=action, result=call([CLIENT, '--wait'], 5))
    # Cosmetic IPC failure must not hold sysinit/network/login indefinitely.
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
