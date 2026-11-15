#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Enable the liuqin NVT follower against an already-running simplefb panel."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('native_status',HERE/'native-status.py')
status=importlib.util.module_from_spec(spec);spec.loader.exec_module(status)
MODULE='usr/lib/liuqin/liuqin_bootloader_panel.ko'
FILES=(MODULE,'usr/lib/liuqin/native-touch.py',
 'lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_csot.bin',
 'lib/firmware/novatek/liuqin/novatek_nt36532_m81_fw_tm.bin')


def preflight(root=Path('/'), prepare=False):
    m=json.loads((root/'etc/liuqin-native-recovery.json').read_text())
    v=status.collect(root,include_battery=not prepare)
    from liuqin_native_install import capable
    if prepare and (m.get('recovery_fast_start_version') != 1 or
                    status.read(root/'etc/liuqin-storage-mode') != 'readonly'):
        raise RuntimeError('early touch requires the readonly native Recovery startup')
    if prepare and status.startup_result(root) != 'pending':
        raise RuntimeError('touch preparation cannot replace inputs after startup admission')
    if (m.get('touch_version')!=1 or (not prepare and not v['sealed']) or v['device']!='liuqin' or
        v['slot']!='a' or not v['unlocked'] or v['kernel']!=m['kernel_release'] or
        m.get('display_profile')!='bootloader-simplefb' or (m.get('inspection_only') is not True and not capable(m))):
        raise RuntimeError('not a verified read-only liuqin simplefb Recovery')
    mounts=(root/'proc/mounts').read_text().splitlines()
    if not any(line.split()[1:3]==['/','rootfs'] for line in mounts):raise RuntimeError('root is not the RAM rootfs')
    if (root/'sys/class/graphics/fb0/name').read_text().strip()!='simple':raise RuntimeError('simplefb is not active')
    for name in FILES:
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=m['payload_sha256'].get(name):
            raise RuntimeError('touch payload checksum mismatch: '+name)
    devices=[]
    for device in (root/'sys/bus/spi/devices').glob('spi*.*'):
        try:compatible=(device/'of_node/compatible').read_bytes().split(b'\0')
        except OSError:continue
        if b'novatek,NVT-ts-spi' in compatible:devices.append(device)
    if len(devices)!=1:raise RuntimeError('expected exactly one NVT touchscreen')
    return devices[0],v


def observe(device, deadline):
    """A successful firmware read can precede the first FD/FE watchdog frame."""
    since=None;watchdog=False
    while time.monotonic()<deadline:
        try:
            state=(device/'touch_state').read_text()
            fields=dict(x.split('=',1) for x in state.split() if '=' in x)
            valid=(fields.get('eng_reset_count')=='0' and fields.get('reset_ret')=='0' and
                   fields.get('fw_ret')=='0' and fields.get('restore_ret')=='0' and
                   0xa1<=int(fields.get('reset','00')[:2],16)<=0xaf)
            counters=[int(fields[name]) for name in ('wdt_frames','wdt_events','wdt_streak')]
            if valid and all(n>=0 for n in counters):
                if since is None:since=time.monotonic()
                watchdog=watchdog or any(counters)
                if time.monotonic()-since>=1.0:return state.strip(),watchdog
            else:since=None
        except (OSError,ValueError,KeyError):since=None
        time.sleep(.1)
    raise RuntimeError('touch controller did not reach an observable no-reset firmware state')


def start(root=Path('/'),runner=subprocess.run,prepare=False):
    # Preparing the RAM-backed panel/touch hardware grants no storage access.
    device,v=preflight(root,prepare)
    # The retained bootloader panel has no DRM reset/recovery callbacks.
    # Preserve the Rec ReK gate; the desktop A3 calibration/recovery path
    # belongs to the real panel driver. Set this before publishing the shim,
    # which can immediately release the NVT deferred probe.
    recovery=root/'sys/module/nt36523_ts/parameters/liuqin_tddi_recovery'
    if recovery.exists():
        recovery.write_text('0\n')
    if not (root/'sys/module/liuqin_bootloader_panel').exists():
        runner(['/bin/busybox','insmod',str(root/MODULE),'bootloader_handoff=1'],check=True,timeout=10)
    if not (device/'driver').exists():
        (root/'sys/bus/spi/drivers_probe').write_text(device.name)
    deadline=time.monotonic()+10
    state,watchdog=observe(device,deadline)
    # Startup recovery belongs to the driver, before it enables IRQs. Never
    # replace input devices from userspace, including during preparation.
    if watchdog:raise RuntimeError('touch firmware watchdog persists; touch is unavailable')
    return dict(status='ready',boot_id=v['boot_id'],device=device.name,touch_state=state,
                health_observation_seconds=1.0,monotonic=time.monotonic())


if __name__=='__main__':
    if sys.argv[1:] not in ([],['--prepare']) or os.geteuid()!=0 or os.uname().machine!='aarch64':raise SystemExit('native ARM64 Recovery only')
    try:
        result=start(prepare=sys.argv[1:]==['--prepare']);code=0
    except Exception as error:
        result=dict(status='unavailable',error=str(error));code=1
    Path('/run/liuqin-native-touch.json').write_text(json.dumps(result)+'\n')
    print(json.dumps(result),flush=True)
    raise SystemExit(code)
