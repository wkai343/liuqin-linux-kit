# SPDX-License-Identifier: GPL-2.0-only
"""Temporarily use a verified local HDR bundle while keeping the boot service on its desktop bundle."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import pwd
import runpy
import signal
import subprocess

BASE = Path('/usr/local/lib/liuqin-ipe')
HDR = Path('/usr/local/lib/liuqin-ipe-hdr')
SYS = Path('/sys/bus/platform/devices/ac01000.icp')


def running(path):
    # The kernel exposes the ELF note including its header and padded GNU name.
    note = Path('/sys/module/qcom_icp_test/notes/.note.gnu.build-id').read_bytes()
    build = note[16:].hex()
    notes = subprocess.check_output(['readelf', '-n', str(path)], text=True, env={**os.environ, 'LC_ALL': 'C'})
    return bool(build) and 'Build ID: ' + build in notes


def pause_sessions(stack):
    for bus in sorted(Path('/run/user').glob('*/bus')):
        user = pwd.getpwuid(int(bus.parent.name)).pw_name
        ctl = ['runuser', '-u', user, '--', 'env', 'XDG_RUNTIME_DIR=' + str(bus.parent), 'systemctl', '--user']
        if subprocess.run([*ctl, 'is-active', '--quiet', 'wireplumber.service']).returncode == 0:
            subprocess.run([*ctl, 'stop', 'wireplumber.service'], check=True)
            stack.callback(subprocess.run, [*ctl, 'start', 'wireplumber.service'], check=True)


@contextmanager
def snapshot_module():
    if (SYS / 'snapshot_p010').is_file():
        yield
        return
    verify = runpy.run_path('/usr/local/libexec/liuqin-ipe-load')['verify']
    verify(BASE)
    verify(HDR)
    desktop, still = (directory / 'qcom-icp-test.ko' for directory in (BASE, HDR))
    if not running(desktop) or 'unsafe=0 ' not in (SYS / 'status').read_text():
        raise RuntimeError('the running IPE driver does not match the desktop bundle or reports an unsafe state')
    with ExitStack() as stack:
        # Finish the short module transaction, including recovery, before
        # honoring a terminal disconnect or an interruption from the caller.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP, signal.SIGINT, signal.SIGTERM})
        stack.callback(signal.pthread_sigmask, signal.SIG_SETMASK, previous)
        pause_sessions(stack)
        # rmmod refuses while a capture client still owns the video node.
        subprocess.run(['rmmod', 'qcom_icp_test'], check=True)
        def restore():
            subprocess.run(['insmod', str(desktop)], check=True)
            subprocess.run(['udevadm', 'settle', '--timeout=10'], check=True)
            if not running(desktop):
                raise RuntimeError('desktop IPE module was not restored')
        stack.callback(restore)
        subprocess.run(['insmod', str(still)], check=True)
        stack.callback(subprocess.run, ['rmmod', 'qcom_icp_test'], check=True)
        if not (SYS / 'snapshot_p010').is_file() or not running(still):
            raise RuntimeError('HDR module did not probe')
        yield
