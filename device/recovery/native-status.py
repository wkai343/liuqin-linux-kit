#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""RAM-only status console for the liuqin native Recovery prototype."""
import json
import os
from pathlib import Path
import re
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from liuqin_board_identity import identify

MARKER='etc/liuqin-native-recovery.json'
STATE='run/liuqin-native-recovery-state.json'
PHASES={
    'ready':'Waiting for the computer installer',
    'connected':'Computer connected - checking identity',
    'reading':'Reading partitions (no disk writes)',
    'complete':'Read-only inspection complete',
    'error':'Inspection stopped - see the computer log',
    'installing':'Installing - do not disconnect or reboot',
    'write-failed':'Installation interrupted - keep Recovery for diagnostics',
}


def read(path,default=''):
    try:return path.read_text().strip()
    except (OSError,UnicodeError):return default


def collect(root=Path('/')):
    manifest=json.loads((root/MARKER).read_text())
    identity=identify(root)
    words=read(root/'proc/cmdline').split()
    def unique(prefix):
        values={w[len(prefix):] for w in words if w.startswith(prefix)}
        return next(iter(values)) if len(values)==1 else ''
    serial=unique('androidboot.serialno=')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',serial):serial=''
    blocks=[]
    for entry in sorted((root/'sys/class/block').glob('sd*')):
        if not re.fullmatch('sd[a-z][0-9]*',entry.name):continue
        blocks.append(dict(name=entry.name,readonly=read(entry/'ro')=='1',dev=read(entry/'dev')))
    mounts=read(root/'proc/self/mountinfo').splitlines()
    devices={b['dev'] for b in blocks}
    mounted=any(len(f:=line.split())>4 and f[2] in devices for line in mounts)
    state={}
    try:state=json.loads((root/STATE).read_text())
    except (OSError,ValueError):pass
    phase=state.get('phase','ready')
    if phase not in PHASES:phase='error'
    battery='unavailable'
    for p in (root/'sys/class/power_supply').glob('*'):
        capacity=read(p/'capacity')
        if read(p/'type')=='Battery' and capacity.isdigit() and 0<=int(capacity)<=100:
            battery=capacity+'%';break
    from liuqin_native_install import capable,session
    installing=capable(manifest);lease=session(root) if installing else None
    if lease and lease.get('state') in ('active','failed'):phase='installing' if lease['state']=='active' else 'write-failed'
    witness={}
    try:
        w=json.loads((root/'run/liuqin-native-fastboot.json').read_text())
        if (w.get('boot_id')==read(root/'proc/sys/kernel/random/boot_id') and w.get('build_id')==manifest['build_id'] and w.get('serial')==serial and
            type(w.get('anti_version')) is int and 0<=w['anti_version']<10**8):witness['anti_version']=w['anti_version']
    except (OSError,ValueError):pass
    return dict(interface='liuqin-native-recovery-v1',build_id=manifest['build_id'],
        device=identity['device'],board_identity=identity,
        serial=serial,slot=unique('androidboot.slot_suffix=').removeprefix('_'),
        unlocked=unique('androidboot.verifiedbootstate=')=='orange' and unique('androidboot.flash.locked=') in ('','0'),
        boot_id=read(root/'proc/sys/kernel/random/boot_id'),kernel=read(root/'proc/sys/kernel/osrelease'),
        inspection_only=not installing,installer_version=1 if installing else 0,fixed_installer_version=manifest.get('fixed_installer_version',0),fixed_recovery_restore_version=manifest.get('fixed_recovery_restore_version',0),generic_ota_version=manifest.get('generic_ota_version',0),usb_transport=manifest.get('usb_transport','unknown'),phase=phase,battery=battery,blocks=blocks,**witness,
        sealed=bool(blocks) and all(b['readonly'] for b in blocks) and not mounted)


def render(value):
    return ('\033[2J\033[HLIUQIN NATIVE RECOVERY - ENGINEERING TEST\n\n'
        'Computer GUI control | No touch menu in this build\n'
        +('Fixed Recovery installer\n\n' if value.get('fixed_installer_version')==1 else 'Temporary boot only - do not flash recovery_a / recovery_b\n\n')
        +PHASES[value['phase']]+'\n\n'
        +'Storage: '+('READ ONLY' if value['sealed'] else 'NOT VERIFIED - stop inspection')+'\n'
        +'Device: '+value['device']+'   Serial: '+value['serial']+'\n'
        +'Kernel: '+value['kernel']+'\n'
        +'Battery: '+value['battery']+'\n'
        +('USB: ADB (trusted USB computer only)\n\n' if value.get('usb_transport')=='adb' else 'USB: ADB image required\n\n')
        +('Computer GUI may perform explicitly confirmed scoped installation.\n' if not value.get('inspection_only',True) else 'This prototype does not install, format or switch systems.\n')
        +'No Android system, userdata or Linux rootfs is mounted at boot.\n')


def show(phase,root=Path('/')):
    if phase not in PHASES:raise ValueError('unknown status phase')
    path=root/STATE;path.parent.mkdir(parents=True,exist_ok=True)
    pending=path.with_suffix('.tmp');pending.write_text(json.dumps(dict(phase=phase))+'\n');pending.replace(path)


def main():
    if os.geteuid()!=0 or os.uname().machine!='aarch64' or not Path('/'+MARKER).is_file():
        raise RuntimeError('not the native ARM64 Recovery')
    args=sys.argv[1:]
    if args==['probe']:print(json.dumps(collect()));return
    if len(args)==2 and args[0]=='show':show(args[1]);return
    if args!=['serve']:raise ValueError('usage: probe | show PHASE | serve')
    # All state and console changes are volatile. Never open a UFS block node.
    Path('/run/liuqin-native-status.pid').write_text(str(os.getpid())+'\n')
    try:Path('/proc/sys/kernel/printk').write_text('3 4 1 7\n')
    except OSError:pass
    previous=''
    while True:
        try:
            text=render(collect())
            if text!=previous:
                for console in ('/dev/tty1','/dev/console'):
                    try:
                        with open(console,'w') as output:output.write(text)
                        previous=text;break
                    except OSError:pass
        except (OSError,ValueError,KeyError):pass
        time.sleep(2)


if __name__=='__main__':main()
