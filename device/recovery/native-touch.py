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


def preflight(root=Path('/')):
    m=json.loads((root/'etc/liuqin-native-recovery.json').read_text())
    v=status.collect(root)
    from liuqin_native_install import capable
    if (m.get('touch_version')!=1 or not v['sealed'] or v['device']!='liuqin' or
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


def start(root=Path('/'),runner=subprocess.run):
    device,v=preflight(root)
    if not (root/'sys/module/liuqin_bootloader_panel').exists():
        runner(['/bin/busybox','insmod',str(root/MODULE),'bootloader_handoff=1'],check=True,timeout=10)
    if not (device/'driver').exists():
        (root/'sys/bus/spi/drivers_probe').write_text(device.name)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        try:
            state=(device/'touch_state').read_text()
            fields=dict(x.split('=',1) for x in state.split() if '=' in x)
            if (fields.get('eng_reset_count')=='0' and fields.get('reset_ret')=='0' and
                fields.get('fw_ret')=='0' and fields.get('restore_ret')=='0' and
                int(fields.get('reset','00')[:2],16)>=0xa1):
                return dict(status='ready',boot_id=v['boot_id'],device=device.name,touch_state=state.strip())
        except (OSError,ValueError):pass
        time.sleep(.1)
    raise RuntimeError('touch controller did not become ready without engineering reset')


if __name__=='__main__':
    if len(sys.argv)!=1 or os.geteuid()!=0 or os.uname().machine!='aarch64':raise SystemExit('native ARM64 Recovery only')
    try:
        result=start();code=0
    except Exception as error:
        result=dict(status='unavailable',error=str(error));code=1
    Path('/run/liuqin-native-touch.json').write_text(json.dumps(result)+'\n')
    print(json.dumps(result),flush=True)
    raise SystemExit(code)
