#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Binary-safe command decoding with a mandatory current Recovery boot ID."""
import base64,json,os,signal,subprocess,sys,uuid
from pathlib import Path
def command(boot_id,encoded,root=Path('/')):
    uuid.UUID(boot_id)
    m=json.loads((root/'etc/liuqin-native-recovery.json').read_text())
    if m.get('interface')!='liuqin-native-recovery-v1' or m.get('native_adb_version')!=1 or m.get('usb_transport')!='adb':raise RuntimeError('not a native ADB Recovery')
    if (root/'proc/sys/kernel/random/boot_id').read_text().strip()!=boot_id:raise RuntimeError('Recovery boot session changed')
    if len(encoded)>90000:raise ValueError('command too large')
    data=base64.b64decode(encoded,validate=True)
    if not data or len(data)>65536 or b'\0' in data:raise ValueError('invalid command')
    return data.decode('utf-8')
def main():
    if len(sys.argv)!=3 or os.geteuid()!=0 or os.uname().machine!='aarch64':raise RuntimeError('native ARM64 Recovery only')
    text=command(sys.argv[1],sys.argv[2]);sys.exit(run(text))
def run(text):
    # adbd reports disconnect by SIGHUP to this PID. Forward it to the whole
    # operation group and wait, allowing writer finally/trap cleanup to run.
    child=subprocess.Popen(['/bin/sh','-c',text],start_new_session=True)
    def stop(number,frame):
        try:os.killpg(child.pid,signal.SIGTERM)
        except ProcessLookupError:pass
    old={s:signal.signal(s,stop) for s in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT)}
    try:
        code=child.wait();return code if code>=0 else 128-code
    finally:
        for s,handler in old.items():signal.signal(s,handler)
if __name__=='__main__':main()
