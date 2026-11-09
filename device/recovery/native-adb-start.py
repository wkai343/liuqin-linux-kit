#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Set up a USB-only FunctionFS ADB gadget after storage has been sealed."""
import importlib.util,json,os,re,stat,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('native_status',HERE/'native-status.py');status=importlib.util.module_from_spec(spec);spec.loader.exec_module(status)

def aliases(root):
    found={}
    for p in (root/'sys/class/block').glob('sd*'):
        if not re.fullmatch('sd[a-z][1-9][0-9]*',p.name):continue
        values=dict(line.split('=',1) for line in (p/'uevent').read_text().splitlines() if '=' in line);name=values.get('PARTNAME')
        if name not in ('boot_a','boot_b','persist','userdata'):continue
        if name in found or (p/'ro').read_text().strip()!='1':raise RuntimeError('installer partition missing/duplicated/writable')
        dev=root/'dev'/p.name
        if not stat.S_ISBLK(dev.stat().st_mode):raise RuntimeError('installer node is not a block device')
        found[name]=dev
    if not {'boot_a','boot_b','persist'}<=set(found):raise RuntimeError('required installer partitions are missing')
    directory=root/'dev/disk/by-partlabel';directory.mkdir(parents=True,exist_ok=True)
    for name,dev in found.items():
        path=directory/name
        if path.is_symlink() or path.exists():
            if not path.is_symlink() or path.resolve()!=dev.resolve():raise RuntimeError('conflicting partition alias')
        else:path.symlink_to(dev)

def start(root=Path('/'),runner=subprocess.run,spawn=subprocess.Popen,sleep=time.sleep):
    m=json.loads((root/'etc/liuqin-native-recovery.json').read_text());v=status.collect(root)
    if (m.get('native_adb_version')!=1 or m.get('usb_transport')!='adb' or v['device']!='liuqin' or
        not v['sealed'] or not v['unlocked'] or v['slot']!='a' or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',v['serial'])):raise RuntimeError('ADB startup requires a sealed, identified native Recovery')
    aliases(root)
    for attempt in range(21):
        controllers=[p.name for p in (root/'sys/class/udc').glob('*')]
        if controllers:break
        for p in (root/'sys/class/usb_role').glob('*/role'):
            try:p.write_text('device\n')
            except OSError:pass
        sleep(.25)
    if len(controllers)!=1 or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',controllers[0]):raise RuntimeError('expected exactly one device-mode USB controller')
    gadget=root/'sys/kernel/config/usb_gadget/liuqin-adb'
    if gadget.exists():raise RuntimeError('ADB gadget already exists; not reconfiguring a live USB session')
    gadget.mkdir()
    def put(name,value):(gadget/name).write_text(value+'\n')
    for name,value in [('idVendor','0x18d1'),('idProduct','0x4ee7'),('bcdDevice','0x0100'),('bcdUSB','0x0200')]:put(name,value)
    (gadget/'strings/0x409').mkdir(parents=True)
    for name,value in [('serialnumber',v['serial']),('manufacturer','Liuqin project'),('product','Liuqin Native Recovery ADB')]:put('strings/0x409/'+name,value)
    (gadget/'configs/c.1/strings/0x409').mkdir(parents=True);put('configs/c.1/strings/0x409/configuration','ADB maintenance');put('configs/c.1/MaxPower','500')
    (gadget/'functions/ffs.adb').mkdir(parents=True);(gadget/'configs/c.1/ffs.adb').symlink_to(gadget/'functions/ffs.adb')
    if (gadget/'os_desc').is_dir():
        put('os_desc/use','1');put('os_desc/b_vendor_code','0x1');put('os_desc/qw_sign','MSFT100');(gadget/'os_desc/c.1').symlink_to(gadget/'configs/c.1')
    endpoint=root/'dev/usb-ffs/adb';endpoint.mkdir(parents=True,exist_ok=True)
    runner(['/bin/busybox','mount','-t','functionfs','-o','uid=0,gid=0','adb',str(endpoint)],check=True)
    env=dict(PATH='/usr/bin:/usr/sbin:/bin:/sbin',HOME='/root',TMPDIR='/run',LANG='C',ADBD_USB='1',ADBD_SHELL='/bin/sh',
        ADBD_GADGET_UDC_PATH=str(gadget/'UDC'),ADBD_GADGET_UDC=controllers[0])
    with (root/'run/liuqin-adbd.log').open('ab') as output:process=spawn([str(root/'usr/bin/adbd')],env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    (root/'run/liuqin-adbd.pid').write_text(str(process.pid)+'\n')
    for attempt in range(100):
        if process.poll() is not None:raise RuntimeError('adbd exited before FunctionFS became ready')
        if (gadget/'functions/ffs.adb/ready').read_text().strip()=='1' and (gadget/'UDC').read_text().strip()==controllers[0]:return dict(transport='adb',serial=v['serial'],pid=process.pid)
        sleep(.1)
    raise RuntimeError('ADB FunctionFS did not bind within the startup deadline')
if __name__=='__main__':
    if os.geteuid()!=0 or os.uname().machine!='aarch64':raise SystemExit('native ARM64 Recovery only')
    print(json.dumps(start()),flush=True)
