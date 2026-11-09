# SPDX-License-Identifier: MIT
"""Full-OTA import and generic native-Recovery preparation; never runs ROM code."""
import copy,hashlib,json,os,re,shutil,stat,struct,tempfile
from pathlib import Path
from liuqin_payload import metadata,extract
from liuqin_super import build
from liuqin_android import sha,format_settings,verify_rom
from liuqin_ota import CORE,LOGICAL,FIRMWARE,INSTALL,SUPER_BYTES,validate,generic,assess,VERSION,full,install_order
from liuqin_rom_analysis import stamp
from .engine import InstallerError
PROFILE='.liuqin-ota-profile.json'
ARCHIVE='source.zip'

def contract(directory,boot_bytes=None):
    from liuqin_recovery_image import ramdisk_decode,records
    result={};root=Path(directory)
    b=(root/'boot.img').read_bytes();u=lambda at:struct.unpack_from('<I',b,at)[0]
    if len(b)<4096 or len(b)>192*1024**2 or b[:8]!=b'ANDROID!' or u(40) not in (3,4) or not u(8) or 4096+((u(8)+4095)//4096*4096)+u(12)>(boot_bytes or len(b)):raise ValueError('OTA boot.img 不是受支持的 liuqin Android v3/v4 镜像。')
    result['boot_header']=u(40)
    b=(root/'vendor_boot.img').read_bytes();u=lambda at:struct.unpack_from('<I',b,at)[0]
    if len(b)<4096 or len(b)>96*1024**2 or b[:8]!=b'VNDRBOOT' or u(8) not in (3,4) or u(12) not in (2048,4096,8192):raise ValueError('OTA vendor_boot 表头无效。')
    result['vendor_header']=u(8);page=u(12);align=lambda n:(n+page-1)//page*page
    start=align(u(2096));size=u(24)
    if not 2112<=u(2096)<=page or not 0<size<=64*1024**2 or start+size>len(b):raise ValueError('vendor ramdisk 边界无效。')
    chunks=[b[start:start+size]]
    if u(8)==4 and u(2116):
        count,entry_size=u(2116),u(2120);table=start+align(size)+align(u(2100))
        if count>128 or not 108<=entry_size<=1024 or table+count*entry_size>len(b):raise ValueError('vendor ramdisk table 无效。')
        chunks=[]
        for i in range(count):
            length,offset=struct.unpack_from('<II',b,table+i*entry_size)
            if not length or offset+length>size:raise ValueError('vendor ramdisk 分片越界。')
            chunks.append(b[start+offset:start+offset+length])
    found={'/data':set(),'/metadata':set()}
    for chunk in chunks:
        for name,(fields,body) in records(ramdisk_decode(chunk)).items():
            if 'fstab' not in name or fields[1]&0o170000!=0o100000:continue
            if len(body)>256*1024:raise ValueError('fstab 超出分析上限。')
            for line in body.decode().splitlines():
                if not line.strip() or line.lstrip().startswith('#'):continue
                f=line.split()
                if len(f)>=3 and f[1] in found:found[f[1]].add(f[2])
    if len(found['/data'])!=1 or not found['/data']<={'f2fs','ext4'} or found['/metadata']!={'ext4'}:raise ValueError('此 OTA 的 data/metadata 文件系统不符合当前格式化执行器。')
    result['data_filesystems']={n:next(iter(v)) for n,v in found.items()}
    b=(root/'dtbo.img').read_bytes()
    if len(b)<32:raise ValueError('DTBO 被截断。')
    magic,total,hs,es,count,offset,page,version=struct.unpack_from('>8I',b)
    if magic!=0xd7b7ab1e or not 32<=total<=len(b) or hs!=32 or es!=32 or not 1<=count<=512 or offset<hs or offset+count*es>total or page not in (2048,4096,8192) or version not in (0,1):raise ValueError('DTBO 表边界无效。')
    from liuqin_universal_recovery import avb_measurement
    for name in ('vbmeta','vbmeta_system'):
        b=(root/(name+'.img')).read_bytes();a=avb_measurement(b);a['rollback_index']=struct.unpack_from('>Q',b,112)[0];result[name]=a
    recovery=root/'recovery.img'
    if recovery.exists():
        from .recovery import inspect_image
        rec=inspect_image(recovery)
        if rec['kernel_bytes']!=0:raise ValueError('ROM Recovery 含内核，不能用于 liuqin 独立 Recovery 分区。')
        result['recovery']={k:rec[k] for k in ('device','kernel_bytes','header_version','sha256')}
    return result

def verify_metadata(p,info):
    expected={n:{k:r[k] for k in ('bytes','sha256')} for n,r in info['partitions'].items()}
    ota=p['ota'];groups=info['groups'];spec=p['super']['layout']
    if (ota['partitions']!=expected or ota['payload_sha256']!=info['payload_sha256'] or ota['metadata_sha256']!=info['metadata_sha256'] or
        ota['fingerprint']!=info['ota']['post-build'] or ota['sdk']!=int(info['ota']['post-sdk-level']) or
        sorted(ota['optional_postinstall'])!=sorted(n for n,r in info['partitions'].items() if r['optional_postinstall']) or
        info['snapshot']!=1 or len(groups)!=1 or groups[0]['name']!=spec['group'] or groups[0]['bytes']!=spec['group_bytes'] or
        sorted(groups[0]['partitions'])!=[p['name'] for p in spec['partitions']]):raise ValueError('OTA 缓存描述与原始 payload 元数据不一致。')

def read_profile(directory,check=lambda:None):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('OTA 材料目录不能是链接。')
    path=directory/PROFILE
    if path.is_symlink() or not path.is_file() or path.stat().st_size>1024**2:raise ValueError('缺少经过校验的完整 OTA 导入描述。')
    p=validate(json.loads(path.read_text()));archive=directory/ARCHIVE
    if directory.name!=p['id'] or stamp(archive)[2]!=p['archive']['bytes'] or sha(archive,check)!=p['archive']['sha256']:raise ValueError('OTA 缓存来源完整性不匹配。')
    verify_metadata(p,metadata(archive));return p

def verify_directory(directory,p,check=lambda:None):
    from liuqin_super import verify
    selected=dict(p,material_images=[n for n in p['material_images'] if n!='super.img'])
    files=verify_rom(directory,selected,check)
    super_path=Path(directory)/'super.img'
    if verify(super_path,p['super']['layout'],check)!=p['images']['super.img']['sha256']:raise ValueError('OTA super 与完整镜像摘要不符。')
    files['super.img']=str(super_path)
    if full(p):
        with (Path(directory)/'boot.img').open('rb') as f:
            n=p['ota']['partitions']['boot']['bytes'];h=hashlib.sha256()
            while n:
                check();b=f.read(min(n,4*1024**2))
                if not b:raise ValueError('boot 原始内容被截断。')
                h.update(b);n-=len(b)
            if h.hexdigest()!=p['ota']['partitions']['boot']['sha256']:raise ValueError('boot 原始内容不属于 payload。')
            while b:=f.read(4*1024**2):
                if b.strip(b'\0'):raise ValueError('boot 填充区域不是零。')
    if contract(directory,p['ota']['partitions']['boot']['bytes'])!=p['boot_contract']:raise ValueError('OTA 缓存启动镜像或 data 格式观测被修改。')
    return files

def import_profile(path,cache,check=lambda:None):
    source=Path(path).expanduser();before=stamp(source);archive_sha=sha(source,check);cache=Path(cache)
    if cache.is_symlink():raise ValueError('OTA 缓存目录不能是链接。')
    ident='ota2-'+archive_sha[:32];destination=cache/ident
    if destination.exists():
        if destination.is_symlink():raise ValueError('OTA 缓存不能是链接。')
        # Older local caches can acquire their exact original ZIP once.
        if not (destination/ARCHIVE).exists():
            data=json.loads((destination/PROFILE).read_text())
            if data.get('archive',{}).get('sha256')!=archive_sha:raise ValueError('旧缓存与 OTA 来源不同。')
            with source.open('rb') as inp,(destination/ARCHIVE).open('xb') as output:shutil.copyfileobj(inp,output,4*1024**2)
        p=read_profile(destination,check)
        if p['archive']['sha256']!=archive_sha or p['archive']['bytes']!=before[2]:raise ValueError('OTA 缓存与来源不一致。')
        verify_directory(destination,p,check)
        if before!=stamp(source):raise ValueError('分析期间 ROM 发生变化。')
        return p,destination
    info=metadata(source);parts={n:{k:r[k] for k in ('bytes','sha256')} for n,r in info['partitions'].items()};names=set(parts)
    if not CORE<=names or not {'system','vendor','product'}<=names or names-(CORE|LOGICAL|FIRMWARE|{'recovery'}):raise ValueError('完整 OTA 缺少必要镜像或含未支持的设备分区。')
    logical=sorted(names&LOGICAL)
    if info['snapshot']!=1 or len(info['groups'])!=1:raise ValueError('未实现此动态分区组格式。')
    group=info['groups'][0]
    if sorted(group['partitions'])!=logical:raise ValueError('只支持 liuqin 单 super 的完整 Virtual A/B OTA，不支持改变分区组布局。')
    spec=dict(bytes=SUPER_BYTES,group=group['name'],group_bytes=group['bytes'],partitions=[dict(name=n,**parts[n]) for n in logical])
    from liuqin_super import layout
    layout(spec)
    cache.mkdir(mode=0o700,parents=True,exist_ok=True)
    if shutil.disk_usage(cache).free<sum(r['bytes'] for r in parts.values())+SUPER_BYTES+before[2]+512*1024**2:raise ValueError('电脑可用空间不足以导入完整 OTA。')
    with tempfile.TemporaryDirectory(prefix='ota-',dir=cache) as temp:
        temp=Path(temp);extract(source,temp,info,parts,sorted(names),check)
        if parts['boot']['bytes']>192*1024**2:raise ValueError('boot 超过目标分区容量。')
        with (temp/'boot.img').open('r+b') as f:f.truncate(192*1024**2)
        boot=contract(temp,parts['boot']['bytes']);ranges=build(temp,temp/'super.img',spec,check);super_sha=sha(temp/'super.img',check)
        images={n+'.img':dict(partition=n+'_a',**parts[n]) for n in names-LOGICAL};images['boot.img']=dict(partition='boot_a',bytes=192*1024**2,sha256=sha(temp/'boot.img',check));images['super.img']=dict(partition='super',bytes=SUPER_BYTES,sha256=super_sha)
        p=dict(schema_version=3,generic_ota_version=VERSION,id=ident,device='liuqin',slot='a',rom='OTA-'+archive_sha[:16],anti_version=None,firmware_policy='rom-package',shared_firmware=[],
            install_images=install_order(parts),material_images=sorted(images),images=images,boot_contract=boot,
            archive=dict(format='payload-zip-v2',bytes=before[2],sha256=archive_sha),
            ota=dict(device='liuqin',fingerprint=info['ota']['post-build'],sdk=int(info['ota']['post-sdk-level']),partitions=parts,
                payload_sha256=info['payload_sha256'],metadata_sha256=info['metadata_sha256'],ignored_partitions=[],optional_postinstall=sorted(n for n,r in info['partitions'].items() if r['optional_postinstall'])),
            super=dict(source_sha256=super_sha,expanded_bytes=SUPER_BYTES,verification_ranges=ranges,layout=spec),userdata=format_settings('userdata',boot['data_filesystems']['/data']),metadata=format_settings('metadata'))
        validate(p);shutil.copyfile(source,temp/ARCHIVE)
        if sha(temp/ARCHIVE,check)!=archive_sha:raise ValueError('OTA 来源复制时发生变化。')
        (temp/PROFILE).write_text(json.dumps(p,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
        if before!=stamp(source):raise ValueError('导入期间 ROM 文件发生变化。')
        for f in temp.iterdir():
            if f.name not in set(p['material_images'])|{PROFILE,ARCHIVE}:f.unlink()
        temp.rename(destination)
    return p,destination

def assess_reader(profile,reader):
    """Read firmware via an already verified native session, including old read-only Rec."""
    from . import firmware
    from liuqin_rom_preview import validate_layout
    def inventory():
        raw=reader.remote(firmware.command(firmware.INVENTORY_CODE),timeout=15)
        if len(raw)>256*1024:raise ValueError('分区信息超过读取上限。')
        return {r['name']:r for r in validate_layout(json.loads(raw))}
    before=inventory();rows={n:dict(r,bytes=r['size']) for n,r in before.items()}
    def measure(row,length):
        code=firmware.open_code(row)+f'n={length}; h=hashlib.sha256()\nwhile n:\n b=os.read(f,min(n,1048576)); assert b; h.update(b); n-=len(b)\nos.close(f); print(h.hexdigest())'
        value=reader.remote(firmware.command(code),timeout=60).decode().strip()
        if not __import__('re').fullmatch('[a-f0-9]{64}',value):raise ValueError('固件读取摘要无效。')
        return value
    def read(row,length):
        raw=reader.remote(firmware.command(firmware.open_code(row)+f'print(os.read(f,{length}).hex()); os.close(f)'),timeout=15)
        return bytes.fromhex(raw.decode().strip())
    boot=reader.probe_current();result=assess(profile,rows,measure,read)
    current=reader.probe_current()
    if before!=inventory() or any(current.get(k)!=boot.get(k) for k in ('boot_id','build_id','sealed','slot','serial')):raise ValueError('OTA 检查期间设备会话或分区改变。')
    result['layout']=list(before.values())
    return result


def project_firmware(profile, directory, snapshot):
    """Derive the exact A-slot measurements emitted by the full-ROM writer."""
    from liuqin_firmware import LAYOUT, ROLES, fingerprint_id, validate_snapshot
    from liuqin_universal_recovery import avb_measurement

    directory = Path(directory)
    after = copy.deepcopy(validate_snapshot(snapshot))
    after['active_slot'] = 'a'
    slot = after['slots']['a']
    measured = slot.setdefault('measured', copy.deepcopy(slot.get('match', {})))
    changes = {}
    for role in ROLES:
        filename = role+'.img'
        record = profile['images'].get(filename)
        if record is None:
            continue
        avb_field = {'vbmeta': 'avb', 'vbmeta_system': 'avb_system'}.get(role)
        row = after['partitions'].get(role+'_a')
        if row is None or record['bytes'] > row['size']:
            measured.pop(role, None)
            if avb_field:
                slot.pop(avb_field, None)
            continue
        with (directory/filename).open('rb') as stream:
            data = stream.read(record['bytes']+1)
        if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('Recovery 条件预览的 ROM 镜像已变化：'+filename)
        # The full-ROM writer zero-fills the remainder of each partition.
        length = struct.unpack_from('>I', data, 4)[0] if role == 'dtbo' else row['size']
        if not 0 < length <= row['size'] or (role == 'dtbo' and length > len(data)):
            raise ValueError('Recovery 条件预览的 DTBO 边界无效。')
        hashed = data[:length].ljust(length, b'\0')
        digest = hashlib.sha256(hashed).hexdigest()
        changes[role] = measured.get(role, {}).get('sha256') != digest
        measured[role] = dict(partition_bytes=row['size'], hash_bytes=length, sha256=digest)
        if avb_field:
            slot[avb_field] = avb_measurement(data, digest)
    if set(measured) == set(ROLES)|{'layout'} and set(measured['layout']) == set(LAYOUT):
        slot.update(status='complete', match=measured, fingerprint_id=fingerprint_id(measured))
        slot.pop('errors', None)
    else:
        slot.update(status='incomplete', errors=['预览缺少完整的 A 槽启动链信息。'])
        slot.pop('match', None)
        slot.pop('fingerprint_id', None)
    return after, changes


def project_recovery(profile, directory, snapshot):
    """Assess the projected firmware, separately from ROM write admission."""
    from liuqin_universal_recovery import assess, load_catalog
    after, changes = project_firmware(profile, directory, snapshot)
    slot = after['slots']['a']
    measured = slot.get('measured', {})
    result = assess(after, load_catalog())
    rom_recovery = 'recovery.img' in profile['images']
    result.update(basis='按 ROM 写入清单推算 A 槽固件。'+
        ('将采用 ROM 自带的 Recovery' if rom_recovery else '保留当前 Recovery 分区内容'),
        recovery_from_rom=rom_recovery, firmware_changes=changes,
        projected_fingerprint_id=slot.get('fingerprint_id'),
        projected_abl_sha256=measured.get('abl', {}).get('sha256'))
    if slot.get('avb', {}).get('status') == 'complete':
        result['root_avb_flags'] = slot['avb']['flags']
    return result


def preview(engine,payload):
    if engine.simulation:raise InstallerError('演示模式不读取真实 OTA 或平板；可使用已收录基线预览。')
    profile,directory=import_profile(payload['rom_path'],engine.state_dir/'rom-cache',engine.check_cancel)
    from . import firmware,native_profiles
    from .native_adb import probe_adb
    from liuqin_rom_preview import unadmitted_preview
    from liuqin_rom_admission import assessment
    serial=payload['serial'];engine.lock_device(serial)
    try:
        probe=probe_adb(serial);path=native_profiles.find_running(probe,payload.get('runtime_path',''))
        reader=firmware.verified_reader(engine,path,probe);snapshot=firmware.collect(reader,probe,reader.probe_current)
        check=assess_reader(profile,reader)
    finally:engine.release_device()
    analysis=dict(profile=dict(id=profile['id'],label='通用完整 OTA · SDK '+str(profile['ota']['sdk'])),
        source=dict(kind='payload-zip',path=str(Path(payload['rom_path']).resolve()),sha256=profile['archive']['sha256'],bytes=profile['archive']['bytes'],
            verification='完整 payload、解包镜像、启动容器与 data 格式已核验；未验证发行者身份，未执行 ROM 程序。'),
        images=[dict(name=n,**r,matched=True) for n,r in profile['ota']['partitions'].items()],issues=[],missing=[],reference_only=False)
    rows=check.pop('layout');result=unadmitted_preview(analysis,snapshot,rows,'rom')
    rec_after=project_recovery(profile,directory,snapshot)
    admission=assessment(profile);result.update(admission=admission,ota_check=check,generic_ota_version=2,firmware_policy='rom-package',abl_source='按 ROM 完整包更新声明的 A 槽启动和固件资源',
        blockers=list(check['blockers']),warnings=check['limitations']+['optional postinstall 不执行。','备份并确认后才执行实际刷写。'],
        material_status='完整 OTA 已校验，设备条件满足（仍需安装确认）' if check['compatible'] else '完整 OTA 已校验，设备条件不满足（'+str(len(check['blockers']))+' 项）',
        firmware=[dict(partition=r['partition'],action=r.get('action','写入 ROM 版本'),material='与当前一致' if r['matched'] else '版本不同，将更新',matched=True) for r in check['firmware_checks']],
        rec_after=rec_after,
        writes=[dict(partition=r['partition'],action='待确认写入',material='完整镜像已校验') for name,r in profile['images'].items()],
        imported_profile_path=str((directory/PROFILE).resolve()))
    result['preserved']=['Linux rootfs','引导存储中的已有系统文件','B 槽分区','分区边界（除已明确选择的分区操作）']
    result['runtime_required']=probe.get('inspection_only') or probe.get('generic_ota_version',0)<2
    if result['runtime_required']:result['warnings'].append('当前 Rec 可查看计划；实际刷写请临时启动新版完整 ROM 安装环境。')
    result.pop('preview_id',None);result['preview_id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result

def _previous_backup_manifests(directories, serial):
    manifests = []
    for directory in directories:
        directory = Path(directory)
        path = directory/'manifest.json'
        try:
            metadata = path.lstat()
            if (directory.is_symlink() or not stat.S_ISREG(metadata.st_mode) or
                    metadata.st_size > 1024**2):
                continue
            manifest = json.loads(path.read_text())
            if (isinstance(manifest, dict) and manifest.get('schema') == 1 and
                    manifest.get('complete') is True and manifest.get('serial') == serial and
                    isinstance(manifest.get('files'), dict)):
                manifests.append((directory, manifest['files']))
        except (OSError, ValueError):
            continue
    return manifests


def _backup_file_matches(path, size, expected, check_cancel):
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            return False
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                return False
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                check_cancel()
                chunk = stream.read(min(remaining, 4*1024**2))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
            return not stream.read(1) and digest.hexdigest() == expected
    except OSError:
        return False


def _partition_digest(engine, part):
    output = engine.transport.remote('/bin/busybox sha256sum /dev/'+part['node'], 300)
    fields = output.decode().split()
    if not fields or not re.fullmatch('[a-f0-9]{64}', fields[0]):
        raise InstallerError('无法校验 ROM 分区：'+part['name'])
    return fields[0]


def backup_overwrites(engine, profile, inventory, directory, previous=(), extra_targets=()):
    """Save current ROM contents, reusing independently verified local images."""
    from liuqin_native_install import identity
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if set(extra_targets)-{'recovery_a'}:
        raise ValueError('额外 ROM 备份仅支持固定 Recovery。')
    targets = sorted({record['partition'] for record in profile['images'].values()} | set(extra_targets))
    total = sum(inventory[name]['bytes'] for name in targets)
    manifest = dict(schema=1, complete=False, serial=engine.device['serial'],
                    boot_id=engine.transport.boot_id,
                    target_rom_sha256=profile['archive']['sha256'], files={})

    def save_manifest():
        temporary = directory/'manifest.tmp'
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        temporary.chmod(0o600)
        temporary.replace(directory/'manifest.json')

    save_manifest()
    candidates = _previous_backup_manifests(previous, engine.device['serial'])
    planned = []
    required_bytes = 0
    for name in targets:
        engine.check_cancel()
        part = inventory[name]
        engine.phase('backup', detail=name)
        if not re.fullmatch('sd[a-z][1-9][0-9]*', part['node']):
            raise InstallerError('备份块设备身份无效。')
        expected = _partition_digest(engine, part)
        source = None
        for candidate_directory, files in candidates:
            record = files.get(name)
            if (not isinstance(record, dict) or record.get('identity') != identity(part) or
                    record.get('sha256') != expected or record.get('file') != name+'.img'):
                continue
            candidate = candidate_directory/record['file']
            if _backup_file_matches(candidate, part['bytes'], expected, engine.check_cancel):
                source = candidate
                break
        path = directory/(name+'.img')
        linked = False
        if source is not None:
            try:
                os.link(source, path, follow_symlinks=False)
                linked = _backup_file_matches(path, part['bytes'], expected, engine.check_cancel)
                if not linked:
                    path.unlink()
                    source = None
            except OSError:
                # Cross-volume destinations can reuse the existing local
                # image through a verified copy instead of another USB read.
                pass
        if not linked:
            required_bytes += part['bytes']
        planned.append((part, path, expected, source, linked))
    if shutil.disk_usage(directory).free < required_bytes+512*1024**2:
        raise InstallerError('电脑空间不足以备份所有 ROM 写入目标。')

    result = {}
    completed = 0
    for part, path, expected, source, linked in planned:
        name = part['name']
        engine.phase('backup', detail=name)
        if not linked:
            with path.open('xb') as stream:
                path.chmod(0o600)
                if source is not None:
                    with source.open('rb') as original:
                        for offset in range(0, part['bytes'], 4*1024**2):
                            engine.check_cancel()
                            size = min(4*1024**2, part['bytes']-offset)
                            data = original.read(size)
                            if len(data) != size:
                                raise InstallerError('已保存的 ROM 分区备份被截断：'+name)
                            stream.write(data)
                            engine.progress('backup', completed+offset+len(data), total)
                else:
                    for offset in range(0, part['bytes'], 4*1024**2):
                        engine.check_cancel()
                        size = min(4*1024**2, part['bytes']-offset)
                        command = ('/bin/busybox dd if=/dev/'+part['node']+' bs=4096 skip='+
                                   str(offset//4096)+' count='+str(size//4096)+' 2>/dev/null')
                        data = engine.transport.remote(command, 120)
                        if len(data) != size:
                            raise InstallerError('ROM 分区备份被截断：'+name)
                        stream.write(data)
                        engine.progress('backup', completed+offset+size, total)
                stream.flush()
                os.fsync(stream.fileno())
        if (not _backup_file_matches(path, part['bytes'], expected, engine.check_cancel) or
                _partition_digest(engine, part) != expected):
            raise InstallerError('ROM 分区备份回读不一致：'+name)
        reused = source is not None
        result[name] = dict(identity=identity(part), sha256=expected,
                            path=str(path.resolve()), reused=reused)
        completed += part['bytes']
        engine.progress('backup', completed, total)
        manifest['files'][name] = dict(identity=identity(part), sha256=expected,
                                       file=path.name, reused=reused)
        save_manifest()
    manifest['complete'] = True
    save_manifest()
    return result
