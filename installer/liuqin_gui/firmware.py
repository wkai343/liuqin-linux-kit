# SPDX-License-Identifier: MIT
"""Read firmware through a verified native RAM session; no device writes."""
import json
import re
import shlex
import struct
from types import SimpleNamespace
import uuid
from pathlib import Path
from liuqin_firmware import ROLES,LAYOUT,fingerprint_id,validate_snapshot
from .engine import InstallerError,Cancelled

# Keep remote programs below the RAM shell line limit. Names come only from
# validated sysfs records; hashing never follows /dev/disk/by-partlabel links.
INVENTORY_CODE='''import json,pathlib
rows=[]
for p in pathlib.Path("/sys/class/block").glob("sd*"):
 if not (p/"partition").exists():continue
 f=dict(x.split("=",1) for x in (p/"uevent").read_text().splitlines() if "=" in x)
 rows.append(dict(node=p.name,name=f.get("PARTNAME",""),dev=(p/"dev").read_text().strip(),size=int((p/"size").read_text())*512,start=int((p/"start").read_text())*512,ro=(p/"ro").read_text().strip()))
print(json.dumps(rows))'''

def command(code):return '/usr/bin/python3 -I -B -c '+shlex.quote(code)

def inventory(remote):
    raw=remote(command(INVENTORY_CODE),timeout=12)
    if len(raw)>256*1024:raise InstallerError('分区识别数据过大。')
    rows=json.loads(raw);result={};nodes=set()
    if not isinstance(rows,list) or len(rows)>512:raise InstallerError('分区识别数据无效。')
    allowed={r+'_'+s for r in (*ROLES,*LAYOUT) for s in ('a','b')}
    for row in rows:
        if not isinstance(row,dict):raise InstallerError('分区身份无效。')
        if row.get('name') not in allowed:continue
        if (row['name'] in result or row.get('node') in nodes or not re.fullmatch(r'sd[a-z][1-9][0-9]*',row.get('node','')) or
            not re.fullmatch(r'[0-9]{1,5}:[0-9]{1,7}',row.get('dev','')) or row.get('ro')!='1' or
            type(row.get('size')) is not int or not 0<row['size']<=512*1024**2 or
            type(row.get('start')) is not int or not 0<=row['start']<2**44):raise InstallerError('固件分区身份重复、不可读或未处于只读状态。')
        result[row['name']]=row;nodes.add(row['node'])
    return result

def open_code(row):
    major,minor=map(int,row['dev'].split(':'))
    return ('import os,stat,hashlib\n'
      f'f=os.open("/dev/{row["node"]}",os.O_RDONLY|os.O_NOFOLLOW)\n'
      's=os.fstat(f)\n'
      f'assert stat.S_ISBLK(s.st_mode) and (os.major(s.st_rdev),os.minor(s.st_rdev))==({major},{minor})\n')

def measurement(remote,role,row):
    size=row['size']
    caps={'abl':16*1024**2,'dtbo':64*1024**2,'vbmeta':2*1024**2,'vbmeta_system':2*1024**2}
    if size>caps[role]:raise InstallerError('固件分区容量超出探测边界：'+role)
    length=size
    if role=='dtbo':
        raw=remote(command(open_code(row)+'print(os.read(f,32).hex())\nos.close(f)'),timeout=8)
        try:h=bytes.fromhex(raw.decode().strip());magic,total,hs,es,count,offset,page,version=struct.unpack('>8I',h)
        except (ValueError,struct.error):raise InstallerError('DTBO 表头不完整。') from None
        if (magic!=0xd7b7ab1e or not 32<=total<=size or hs!=32 or es!=32 or not 1<=count<=512 or
            offset<hs or offset+es*count>total or page not in (2048,4096,8192) or version not in (0,1)):
            raise InstallerError('DTBO 表边界无效。')
        length=total
    code=open_code(row)+f'n={length}\nh=hashlib.sha256()\nwhile n:\n b=os.read(f,min(n,1048576))\n assert b\n h.update(b);n-=len(b)\nos.close(f)\nprint(h.hexdigest())'
    digest=remote(command(code),timeout=30).decode().strip()
    if not re.fullmatch('[a-f0-9]{64}',digest):raise InstallerError('固件校验值格式无效。')
    return dict(partition_bytes=size,hash_bytes=length,sha256=digest)

class FirmwareChanged(InstallerError):pass

def avb_metadata(remote,row,expected_sha):
    from liuqin_universal_recovery import avb_measurement
    if row['size']>2*1024**2:raise InstallerError('AVB 分区超出读取边界。')
    code=open_code(row)+f'n={row["size"]}\nh=hashlib.sha256();d=b""\nwhile n:\n b=os.read(f,min(n,1048576))\n assert b\n h.update(b);d=(d+b)[:65536];n-=len(b)\nos.close(f)\nprint(h.hexdigest()+":"+d.hex())'
    raw=remote(command(code),timeout=15)
    if len(raw)>140000:raise InstallerError('AVB 元数据返回过大。')
    digest,sep,body=raw.decode().strip().partition(':')
    if not sep:raise InstallerError('AVB 观测数据不完整。')
    if digest!=expected_sha:raise FirmwareChanged('AVB 在读取期间发生变化，指纹已丢弃。')
    return avb_measurement(bytes.fromhex(body),digest)

def collect(transport,probe,probe_again):
    if probe.get('device')!='liuqin' or probe.get('serial')!=transport.serial or probe.get('sealed') is not True or probe.get('unlocked') is not True or probe.get('slot') not in ('a','b'):raise InstallerError('不能在未核验的设备会话中读取固件。')
    uuid.UUID(probe['boot_id'])
    before=inventory(transport.remote);slots={}
    for slot in ('a','b'):
        match={};errors=[];avb_values={}
        for role in ROLES:
            name=role+'_'+slot;row=before.get(name)
            if row is None:errors.append('缺少 '+name);continue
            try:
                transport.engine.check_cancel()
                match[role]=measurement(transport.remote,role,row)
            except Cancelled:raise
            except (OSError,RuntimeError,ValueError) as error:errors.append(name+'：'+str(error)[:300])
            if role in ('vbmeta','vbmeta_system') and role in match:
                field='avb' if role=='vbmeta' else 'avb_system'
                try:avb_values[field]=avb_metadata(transport.remote,row,match[role]['sha256'])
                except (Cancelled,FirmwareChanged):raise
                except (OSError,RuntimeError,ValueError) as error:avb_values[field]=dict(status='unavailable',error=str(error)[:300])

        layout={}
        for role in LAYOUT:
            row=before.get(role+'_'+slot)
            if row is None:errors.append('缺少 '+role+'_'+slot)
            else:layout[role]=row['size']
        match['layout']=layout
        slots[slot]=dict(status='incomplete',errors=errors) if errors else dict(status='complete',match=match,fingerprint_id=fingerprint_id(match))
        slots[slot].update(measured=match,**avb_values)
    after=inventory(transport.remote);current=probe_again()
    if (after!=before or any(current.get(k)!=probe.get(k) for k in ('serial','boot_id','build_id','slot','sealed','unlocked','device','kernel'))):
        raise InstallerError('读取期间设备、分区或启动会话发生变化，固件指纹已丢弃。')
    value=dict(schema=1,device='liuqin',serial=probe['serial'],boot_id=probe['boot_id'],runtime_build_id=probe['build_id'],
        active_slot=probe['slot'],unlocked=True,sealed=True,slots=slots,partitions=before)
    return validate_snapshot(value,transport.serial,probe['boot_id'],probe['build_id'])

def verified_reader(engine,path,probe):
    from liuqin_artifacts import boot_interface
    from .native_recovery_transport import NativeRecoveryTransport
    info=boot_interface(Path(path),installer=True);m=info.get('native_recovery_manifest')
    if not m or m['build_id']!=probe.get('build_id'):raise InstallerError('本地镜像与正在运行的原生 Recovery 不一致。')
    manifest=dict(native_recovery_manifest=m,installer_interface=info['interface'],installer_kernel_release=info['kernel_release'],installer_multiboot_version=info['multiboot_version'])
    proxy=SimpleNamespace(manifest=manifest,check_cancel=engine.check_cancel)
    reader=NativeRecoveryTransport(proxy,probe['serial']);reader.validate_probe(probe);reader.boot_id=probe['boot_id'];reader.ram_active=True;reader.verify_ram_interface()
    return reader
