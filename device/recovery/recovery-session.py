#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare the isolated tools root; never unmount or map arbitrary host disks."""
import fcntl,json,os,re,stat,struct,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from liuqin_block_identity import partition_fields


def nodes(reconcile=False):
    Path('/dev/disk/by-partlabel').mkdir(parents=True,exist_ok=True)
    Path('/dev/disk/by-partuuid').mkdir(parents=True,exist_ok=True)
    found={};parents=set();entries=[];gpt_cache={}
    for entry in Path('/sys/class/block').glob('sd*'):
        if not re.fullmatch(r'sd[a-z][0-9]*',entry.name):continue
        real=entry.resolve()
        if not any((p/'driver').is_symlink() and (p/'driver').resolve().name=='ufshcd-qcom' for p in real.parents):continue
        major,minor=map(int,(entry/'dev').read_text().strip().split(':'));path=Path('/dev')/entry.name
        ensure_node(path,major,minor,reconcile and bool(re.fullmatch(r'sda[0-9]+',entry.name)))
        entries.append(entry)
        if not (entry/'partition').exists():parents.add(entry.name)
    # All parent device nodes must exist before a fallback GPT information query.
    for entry in entries:
        path=Path('/dev')/entry.name
        fields=partition_fields(entry,gpt_cache)
        for field,directory in (('PARTNAME','by-partlabel'),('PARTUUID','by-partuuid')):
            if field not in fields:continue
            value=fields[field]
            if not re.fullmatch(r'[A-Za-z0-9_-]+',value):raise RuntimeError('unsafe partition identity')
            link=Path('/dev/disk')/directory/value
            key=(directory,value)
            if key in found:raise RuntimeError('duplicate partition identity')
            found[key]=path
            if link.is_symlink():link.unlink()
            elif link.exists():raise RuntimeError('partition alias is not a symlink')
            link.symlink_to(path)
    if 'sda' not in parents:raise RuntimeError('liuqin UFS data disk absent')
    for link in Path('/dev/disk').glob('*/*'):
        if link.is_symlink() and (link.parent.name,link.name) not in found:link.unlink()
    for name,major,minor in [('null',1,3),('zero',1,5),('random',1,8),('urandom',1,9)]:
        p=Path('/dev')/name
        if not p.exists():os.mknod(p,stat.S_IFCHR|0o600,os.makedev(major,minor))
    for name,n in [('stdin',0),('stdout',1),('stderr',2)]:
        p=Path('/dev')/name
        if not p.exists():p.symlink_to('/proc/self/fd/'+str(n))
    return parents


def ensure_node(path,major,minor,reconcile=False):
    wanted=os.makedev(major,minor)
    try:info=path.lstat()
    except FileNotFoundError:info=None
    if info is not None:
        if not stat.S_ISBLK(info.st_mode):raise RuntimeError('block alias is not a block device')
        if info.st_rdev==wanted:return
        if not reconcile:raise RuntimeError('block alias changed')
        # BLKRRPART can assign new dynamic minors even to unchanged GPT entries.
        # Only the explicit post-reread path may replace these private nodes.
        path.unlink()
    os.mknod(path,stat.S_IFBLK|0o600,wanted)


def release_auxiliary_users(base,physical):
    """Release the two known Recovery clients of UFS; never force-unload."""
    devices={tuple(map(int,(base/n/'dev').read_text().strip().split(':'))) for n in physical}
    owners=set()
    for fd in Path('/proc').glob('[0-9]*/fd/*'):
        try:
            info=fd.stat()
            if stat.S_ISBLK(info.st_mode) and (os.major(info.st_rdev),os.minor(info.st_rdev)) in devices:
                owners.add(fd.parent.parent)
        except (FileNotFoundError,ProcessLookupError):continue
    for process in owners:
        if process.joinpath('exe').resolve().as_posix() not in ('/vendor/bin/qseecomd','/system/vendor/bin/qseecomd'):
            raise RuntimeError('UFS is open by process '+process.name+'; refusing partition maintenance')
        service=next((name for name in ('vendor.qseecomd','qseecomd') if subprocess.run(
            ['/system/bin/getprop','init.svc_debug_pid.'+name],check=True,capture_output=True,text=True).stdout.strip()==process.name),None)
        if service is None:raise RuntimeError('cannot identify the UFS-owning qseecomd service')
        subprocess.run(['/system/bin/stop',service],check=True,capture_output=True)
        for _ in range(20):
            state=subprocess.run(['/system/bin/getprop','init.svc.'+service],check=True,capture_output=True,text=True).stdout.strip()
            if state=='stopped':break
            time.sleep(.1)
        else:raise RuntimeError('qseecomd did not stop')
    mtd=Path('/proc/mtd')
    lines=mtd.read_text().splitlines()[1:] if mtd.exists() else []
    if any('block2mtd:' in line for line in lines):
        for line in lines:
            match=re.search(r'"block2mtd: /dev/block/(sd[a-z][0-9]+)"$',line)
            if not match or match[1] not in physical:raise RuntimeError('unqualified MTD mapping; refusing to unload')
        loaded={line.split()[0] for line in Path('/proc/modules').read_text().splitlines()}
        for module in ('mtdoops','block2mtd'):
            if module in loaded:subprocess.run(['/system/bin/liuqin/busybox','rmmod',module],check=True,capture_output=True)
        if 'block2mtd:' in mtd.read_text():raise RuntimeError('block2mtd still holds UFS')


def quiesce():
    if os.geteuid()!=0 or os.uname().machine!='aarch64':raise RuntimeError('not ARM64 Recovery root')
    base=Path('/sys/class/block');physical=set()
    for entry in base.glob('sd*'):
        if any((p/'driver').is_symlink() and (p/'driver').resolve().name=='ufshcd-qcom' for p in entry.resolve().parents):physical.add(entry.name)
    if 'sda' not in physical:raise RuntimeError('no internal UFS')
    def backed(name,seen=()):
        if name in physical:return True
        if name in seen:return False
        slaves=list((base/name/'slaves').glob('*'))
        return bool(slaves) and all(backed(p.name,(*seen,name)) for p in slaves)
    maps={p.name for p in base.glob('dm-*') if backed(p.name)}
    devices={(base/n/'dev').read_text().strip() for n in physical|maps}
    mounts=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields=line.split()
        if fields[2] in devices:
            path=fields[4].replace('\\040',' ').replace('\\134','\\')
            if path in ('/','/system','/vendor'):
                # A populated RAM /system or /vendor is not in this list. A
                # block-backed one may host live Recovery dependencies.
                raise RuntimeError('Recovery depends on a mounted UFS '+path)
            mounts.append(path)
    for path in sorted(set(mounts),key=lambda p:p.count('/'),reverse=True):
        subprocess.run(['/system/bin/umount',path],check=True,capture_output=True)
    release_auxiliary_users(base,physical)
    if maps:
        ctl=next((p for p in (Path('/dev/device-mapper'),Path('/dev/mapper/control')) if p.exists()),None)
        if ctl is None:raise RuntimeError('device mapper control is missing')
        while maps:
            leaf=next((n for n in maps if not list((base/n/'holders').glob('*'))),None)
            if leaf is None:raise RuntimeError('unresolved device mapper dependency')
            name=(base/leaf/'dm/name').read_text().strip()
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,127}',name):raise RuntimeError('invalid mapper name')
            data=bytearray(312);struct.pack_into('<3I2I',data,0,4,0,0,312,312);data[48:48+len(name)]=name.encode()
            with ctl.open('rb',buffering=0) as stream:fcntl.ioctl(stream.fileno(),0xc138fd04,data)
            maps.remove(leaf)
    # The chroot has its own mountinfo view; retain the global view for a
    # second validation before any block device write-protection change.
    Path('/tmp/liuqin-runtime/run/global-mountinfo').write_text(Path('/proc/self/mountinfo').read_text())


def enter():
    session=json.loads(Path('/etc/liuqin-recovery-session.json').read_text())
    if os.geteuid()!=0 or os.uname().machine!='aarch64' or session['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():raise RuntimeError('invalid Recovery identity')
    parents=nodes()
    # The global mounts are inspected before setting any RO flags. Chroot
    # cannot safely unmount paths outside its root; the outer gateway handles
    # known mountpoints, and any remainder blocks entry rather than forcing it.
    devices={p.read_text().strip() for name in parents for p in Path('/sys/class/block').glob(name+'*/dev')}
    for line in (Path('/proc/self/mountinfo').read_text()+Path('/run/global-mountinfo').read_text()).splitlines():
        if line.split()[2] in devices:raise RuntimeError('UFS partition remains mounted; unmount it in Recovery first')
    for parent in parents:
        for p in Path('/sys/class/block').glob(parent+'*/holders/*'):
            raise RuntimeError('UFS has an active device-mapper holder: '+p.name)
    for parent in parents:
        for p in Path('/sys/class/block').glob(parent+'*/dev'):
            with open('/dev/'+p.parent.name,'rb',buffering=0) as f:fcntl.ioctl(f.fileno(),0x125d,struct.pack('i',1))
    filesystems=Path('/proc/filesystems').read_text().split()
    support=[fs for fs in ('ext4','btrfs') if fs in filesystems]
    if 'ext4' not in support:raise RuntimeError('Recovery kernel lacks ext4')
    Path('/etc/liuqin-filesystems').write_text('\n'.join(support)+'\n')
    Path('/run/maintenance-ready').write_text(session['boot_id']+'\n')
    print(json.dumps(dict(ready=True,serial=session['serial'],boot_id=session['boot_id'],filesystems=support)))

if __name__=='__main__':
    if sys.argv[1:] == ['enter']:enter()
    elif sys.argv[1:] == ['nodes']:nodes()
    elif sys.argv[1:] == ['nodes-after-gpt']:nodes(reconcile=True)
    elif sys.argv[1:] == ['quiesce']:quiesce()
    else:raise SystemExit('unknown recovery-session operation')
