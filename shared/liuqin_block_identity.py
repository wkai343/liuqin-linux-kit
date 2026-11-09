# SPDX-License-Identifier: MIT
"""Read partition GUIDs from GPT when the kernel omits PARTUUID in uevent.

Only fixed sgdisk information queries are used, through a verified disk fd.
Every returned name and byte range must match the live kernel partition view.
The cache is scoped to one inventory read and is never persisted across edits.
"""
import os,re,stat,subprocess,uuid
from pathlib import Path


def parse_information(text,expected,sector):
    chunks=re.split(r'^Partition GUID code:',text,flags=re.M)[1:]
    if len(chunks)!=len(expected):
        raise RuntimeError('GPT 分区身份信息不完整：GPT 与内核视图不一致（读取 '+str(len(chunks))+'，内核 '+str(len(expected))+'），需要重新读取分区表。')
    result={}
    for item,chunk in zip(expected,chunks):
        def field(pattern):
            match=re.search(pattern,chunk,re.M)
            if match is None:raise RuntimeError('GPT 分区身份字段缺失。')
            return match[1]
        guid=field(r'^Partition unique GUID: ([0-9A-Fa-f-]+)\s*$').lower()
        if str(uuid.UUID(guid))!=guid or uuid.UUID(guid).int==0:raise RuntimeError('GPT 分区 GUID 无效。')
        first=int(field(r'^First sector: (\d+)\b'))
        last=int(field(r'^Last sector: (\d+)\b'))
        name=field(r"^Partition name: '(.*)'\s*$")
        if (name,first*sector,(last-first+1)*sector)!=(item['name'],item['start'],item['bytes']):
            raise RuntimeError('GPT 与内核分区名称或边界不一致：'+item['node'])
        result[item['node']]=guid
    return result


def disk_identities(entry):
    root=entry.parent;real=entry.resolve();parent=real.parent.name
    if not re.fullmatch(r'sd[a-z]',parent) or not any((p/'driver').is_symlink() and (p/'driver').resolve().name=='ufshcd-qcom' for p in real.parents):
        raise RuntimeError('分区不属于受支持的 Qualcomm UFS。')
    expected=[]
    for child in root.glob(parent+'[0-9]*'):
        if child.resolve().parent.name!=parent or not (child/'partition').is_file():continue
        fields=dict(line.split('=',1) for line in (child/'uevent').read_text().splitlines() if '=' in line)
        number=int((child/'partition').read_text())
        if not 1<=number<=4096:raise RuntimeError('分区号超出允许范围。')
        expected.append(dict(node=child.name,number=number,name=fields.get('PARTNAME',''),
            start=int((child/'start').read_text())*512,bytes=int((child/'size').read_text())*512))
    expected.sort(key=lambda p:p['number'])
    if len({p['number'] for p in expected})!=len(expected):raise RuntimeError('重复的内核分区号。')
    sector=int((root/parent/'queue/logical_block_size').read_text())
    if sector!=4096:raise RuntimeError('不支持的 UFS 扇区大小。')
    major,minor=map(int,(root/parent/'dev').read_text().strip().split(':'))
    fd=os.open('/dev/'+parent,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev!=os.makedev(major,minor):raise RuntimeError('UFS 父磁盘设备号不匹配。')
        result=subprocess.run(['/usr/sbin/sgdisk',*['--info='+str(p['number']) for p in expected],'/proc/self/fd/'+str(fd)],
            pass_fds=(fd,),capture_output=True,text=True,check=True,timeout=30)
        if result.stderr.strip():raise RuntimeError('读取 GPT 身份时收到警告：'+result.stderr[-500:])
        return parse_information(result.stdout,expected,sector)
    finally:os.close(fd)


def partition_fields(entry,cache):
    fields=dict(line.split('=',1) for line in (entry/'uevent').read_text().splitlines() if '=' in line)
    if (entry/'partition').is_file() and not fields.get('PARTUUID'):
        parent=entry.resolve().parent.name
        if parent not in cache:cache[parent]=disk_identities(entry)
        if entry.name not in cache[parent]:raise RuntimeError('GPT 缺少当前内核分区的身份。')
        fields['PARTUUID']=cache[parent][entry.name]
    return fields
