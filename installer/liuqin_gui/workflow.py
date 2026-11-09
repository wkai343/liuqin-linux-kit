# SPDX-License-Identifier: MIT
"""Device-first workflow; UI selections are translated to verified operations."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import uuid
from liuqin_partition_names import is_root_name
from liuqin_boot_target import bind
from .engine import InstallerError,canonical,digest_file
from . import runtime,partitioning,recovery
from .managed_linux import boot_snapshot_matches
from .workflow_capacity import check_capacity,plan_capacity
from .multiboot import inspect_system
from .android import import_rom,select_profile
from liuqin_android import image_plan
from liuqin_partitions import snapshot_id,discover
from liuqin_partition_editor import capabilities,plan_edit,validate_edit


def runtime_path(engine,payload):
    found=str(engine.demo_bundle/'installer.img') if engine.simulation else runtime.locate(payload.get('runtime_path',''),payload.get('bundle',''),engine.state_dir)
    if not found:raise InstallerError('未找到配套 Recovery。请使用内置镜像的安装包，或用 --recovery-image 指定镜像。')
    return found


def connection_payload(engine,payload):
    path=runtime_path(engine,payload)
    if not engine.simulation and runtime.backend_hint(path)=='adb':
        from . import compatibility
        # Re-read the device instead of trusting the GUI's earlier report.
        environment=compatibility.inspect_device(payload['serial'])
        path,_status=compatibility.select_image(environment,path)
    if not engine.simulation and runtime.backend_hint(path) in ('network','adb-native'):
        from liuqin_artifacts import boot_interface
        interface=boot_interface(Path(path),installer=True)
        if 'native_recovery_manifest' in interface:
            from .native_recovery_transport import probe_native
            from .native_profiles import inspect_running
            if interface['native_recovery_manifest'].get('usb_transport')!='adb':raise InstallerError('旧 USB 网络 Rec 已停用，请选择新版 ADB Rec。')
            try:probe=probe_native(payload['serial'])
            except (OSError,RuntimeError,ValueError,KeyError):probe=None
            if probe:
                if probe['serial']!=payload['serial']:raise InstallerError('ADB 设备与所选平板不一致。')
                acquired=engine.device_lock is None
                if acquired:engine.lock_device(payload['serial'])
                try:
                    label='固定 Recovery（安装环境）' if probe.get('fixed_installer_version')==1 else '原生 Recovery（安装候选）' if probe.get('installer_version')==1 else '原生 Recovery（只读维护）'
                    env=dict(serial=probe['serial'],kind='native-recovery',label=label,transport='adb-native',properties=probe)
                    environment,path,status=inspect_running(engine,env,path)
                    engine.emit('environment',environment=environment,compatibility=status,runtime_path=path)
                    if not status['ready']:raise InstallerError(status['reason'])
                finally:
                    if acquired:engine.release_device()
    return dict(serial=payload['serial'],bundle=str(Path(path).parent),layout='partition-only',installer_path=path)


def scan(engine,payload):
    # A fresh read is also the diagnostic entry after a failed write. Its
    # receipt already remains on disk; this never resumes the old operation.
    if engine.state=='failed' and not getattr(engine,'workflow_committing',False):engine.state='idle'
    engine.guard_operation();request=connection_payload(engine,payload)
    state=partitioning.connect(engine,request,'scan-partitions')
    if not state.get('inventory') and state.get('error'):
        raise InstallerError('无法核对实际分区身份：'+state['error'])
    summary=discover(state,engine.device['serial']);engine.partition_discovery=state
    native=engine.manifest.get('native_recovery_manifest',{})
    inspection_only=bool(native and (native.get('inspection_only') or not engine.device.get('native_install_allowed',False)))
    rows=capabilities(state,engine.device['serial'])
    if inspection_only:
        summary=dict(summary,can_create=False,reason='原生 Recovery 测试版仅开放读取。')
        rows=[dict(row,can_install=False,can_delete=False,can_format=False,reason='此测试版仅开放读取。') for row in rows]
    if engine.device.get('native_ota_only'):
        summary=dict(summary,can_create=False,reason='当前固件仅允许保留分区的通用 OTA。')
        rows=[dict(row,can_install=row['name']=='userdata',can_delete=False,can_format=False,reason='仅支持保留原分区的 Android 通用 OTA。') for row in rows]
    from .boot_switch import available as can_switch
    registry = state.get('registry') or {}
    switch_systems = []
    if summary['can_create'] and can_switch(engine):
        for system in registry.get('systems', []):
            partition = 'userdata' if system['type'] == 'android' else system['partition']['name']
            switch_systems.append(dict(id=system['id'], name=system['name'], type=system['type'],
                partition=partition, is_default=system['id']==registry.get('default_id')))
    engine.emit('wizard_inventory',summary=summary,partitions=rows,inspection_only=inspection_only,
        switch_systems=switch_systems,
        slots=state.get('registry',{}).get('rootfs_slots',[]) if state.get('registry') else [],runtime_path=request['installer_path'],
        runtime_backend=engine.manifest.get('installer_usb_network','network'))
    engine.transport.cancel_cleanup();engine.clean_scratch();engine.release_device();engine.set_state('wizard_scanned')


def check_preserved_default(edit, targets, default, previous_boot, previous_sha256):
    if default or targets==['__recovery__']:
        return
    state = edit['source']
    registry = state.get('registry') or {}
    systems = registry.get('systems', [])
    affected = {p['name'] for p in edit['deletes']}
    affected.update(row['partition']['name'] for row in edit['formats'])
    affected.update(targets)
    current = next((system for system in systems if system['id']==registry.get('default_id')), None)
    if current:
        current_partition = 'userdata' if current['type']=='android' else current['partition']['name']
        if current_partition in affected or 'linux_boot' in affected:
            if not targets:
                raise InstallerError('本次会删除或格式化当前默认系统。请先切换到另一个系统作为默认，再删除；重装时可选择新的默认启动目标。')
            raise InstallerError('本次会重装或移除当前默认系统，请在安装材料页选择本次安装的一个系统作为默认启动。')

    # Even a newly created userdata can accompany an overwrite of the shared Android ROM.
    replaces_android = 'userdata' in targets
    edits_existing_data = ('userdata' in affected and 'userdata' in state['inventory']
                           and not state.get('userdata_blank'))
    edits_installed_linux = any(system['type']=='linux' and system['partition']['name'] in affected
                                for system in systems)
    if replaces_android or edits_existing_data or edits_installed_linux:
        if not current or not boot_snapshot_matches(current, state['files'], previous_boot, previous_sha256):
            raise InstallerError('无法确认现有 boot_a 仍适用，请在安装材料页选择本次安装的一个系统作为默认启动。')


def previous_rom_snapshots(backup_parent, serial):
    parent = Path(backup_parent)
    snapshots = [p for p in (parent/'rom-snapshots'/serial).glob('*') if p.is_dir() and not p.is_symlink()]
    legacy = [p for p in parent.glob(serial+'-*/rom-overwrites') if p.is_dir() and not p.is_symlink()]
    return sorted(snapshots, key=lambda p:p.stat().st_mtime, reverse=True)[:32] + sorted(legacy, key=lambda p:p.stat().st_mtime, reverse=True)[:16]


def prepare(engine,payload):
    if payload.get('recovery_mode')=='install':raise InstallerError(recovery.FIXED_INSTALL_UNAVAILABLE)
    fixed_policy=payload.get('fixed_recovery_policy','auto')
    if fixed_policy not in ('auto','rom'):raise InstallerError('固定 Recovery 处理方式无效。')
    engine.guard_operation();request=connection_payload(engine,payload)
    # The backup is a read-only step owned by this workflow, not another UI mode.
    engine.create_backup(dict(request,backup_parent=payload['backup_parent'],reuse_current=True))
    backup=engine.receipt['backup_dir']
    backup_reused=engine.receipt.get('backup_reused',False)
    state=partitioning.connect(engine,request,'wizard')
    if snapshot_id(state)!=payload.get('snapshot_id'):raise InstallerError('平板状态已改变，请回到第一步重新读取。')
    old=copy.deepcopy(payload);actions=copy.deepcopy(payload.get('actions',[]));config=copy.deepcopy(payload.get('partition_config',{}))
    targets=payload.get('install_targets',[payload['target']] if payload.get('target') else [])
    if (not isinstance(targets,list) or len(targets)>17 or any(not isinstance(n,str) for n in targets) or
        len(set(targets))!=len(targets) or any(n not in ('userdata','__recovery__') and not is_root_name(n) for n in targets) or
        ('__recovery__' in targets and len(targets)>1)):
        raise InstallerError('请选择一个 Android 和/或不同的 Linux rootfs 安装目标。')
    target_name=targets[0] if len(targets)==1 else '';android_selected='userdata' in targets
    roots=[n for n in targets if is_root_name(n)]
    default=payload.get('default_target',target_name if payload.get('set_default') else '')
    if default and default not in targets:raise InstallerError('默认启动系统必须是本次选中的安装目标。')
    if default=='__recovery__':raise InstallerError('临时 Recovery 不能设为系统默认引导。')
    old.update(set_default=bool(default),default_target=default)
    if len(targets)>1 and getattr(engine.transport,'installer_enabled',False) and engine.manifest['native_recovery_manifest'].get('combined_install_version')!=1:
        raise InstallerError('请临时启动支持 Android + Linux 联合计划的新版 Rec；旧版 Rec 可继续单独安装。')
    rows=capabilities(state,payload['serial'])
    for name in roots:
        selected=next((p for p in rows if p['name']==name),None)
        if selected and selected['status']=='installed' and not any(a['name']==name for a in actions):
            actions.append(dict(name=name,partuuid=selected['partuuid'],action='format',filesystem=payload.get('filesystem',selected['filesystem'])))
    if android_selected and state.get('userdata_linux_signature') and not any(a['name']=='userdata' for a in actions):
        actions.append(dict(name='userdata',partuuid=state['inventory']['userdata']['partuuid'],action='format'))
    config['ensure_store']=bool(roots)
    edit=plan_edit(state,actions,config,payload['serial'],str(uuid.uuid4()))
    projected=edit['projected'];record=engine.load_backup(backup,payload['serial'])
    compatibility=engine.verify_current_backup(record)
    directory=Path(engine.receipt['run_dir'])
    previous=engine.transport.snapshot_boot(directory)
    previous_boot=directory/'boot_a.img'
    if previous_boot.stat().st_size!=engine.device['boot_bytes']:
        raise InstallerError('当前 boot_a 快照大小不符。')
    check_preserved_default(edit,targets,default,previous_boot,previous)
    if roots and hasattr(engine.transport,'validate_linux_install'):
        engine.transport.validate_linux_install()
    materials={};kind='combined' if len(targets)>1 else 'partitions';source=None;direct_images=[];installations=[];profile=None;fixed_records=[]
    if android_selected:
        if 'userdata' not in projected['inventory']:raise InstallerError('所选 userdata 已删除，请选择新建的安装目标。')
        if not payload.get('rom_path'):raise InstallerError('请在材料页选择完整 Android ROM。')
        if len(targets)==1:kind='android'
        if fixed_policy=='auto' and not engine.simulation:
            from .fixed_recovery import candidates
            fixed_records=candidates(engine)
            running_fixed=engine.manifest.get('native_recovery_manifest',{}).get('fixed_installer_version')==1
            if (fixed_records or running_fixed) and engine.manifest.get('native_recovery_manifest',{}).get('fixed_recovery_restore_version')!=1:
                raise InstallerError('当前 Rec 不支持自动保护固定 Rec。请从 Fastboot 临时启动新版配套安装环境后继续；现有固定 Rec 保留。也可明确选择按 ROM 原样处理 Recovery。')
        profile=select_profile(engine,engine.android_rom_path(payload['rom_path']))
        if fixed_policy=='auto' and not engine.simulation:
            if (fixed_records or running_fixed) and profile.get('generic_ota_version')!=2:
                raise InstallerError('固定 Rec 自动保护已接入完整 OTA 流程；此旧 ROM 后端尚未支持。请使用完整 OTA 或明确选择按 ROM 原样处理 Recovery。')
        if hasattr(engine.transport,'validate_android_install'):engine.transport.validate_android_install()
        anti=engine.device.get('anti_version')
        if profile.get('generic_ota_version')!=2 and (not str(anti).isdigit() or (not profile.get('generic_ota_version') and int(anti)>profile['anti_version'])):raise InstallerError('Android 防回滚等级不兼容。')
        engine.phase('android_plan',detail='导入 '+profile['rom'])
        engine.emit('notice',message='正在校验并导入 '+profile['rom']+'；首次导入 OTA 会解包系统镜像并组装 super。')
        source=import_rom(engine.android_rom_path(payload.get('rom_path','')),profile,engine.state_dir/'rom-cache',engine.check_cancel)
        installations.append(dict(kind='android',target='userdata',source=source,set_default=default=='userdata'))
        if engine.manifest.get('native_recovery_manifest', {}).get('android_sparse_store_version') == 1:
            from liuqin_android import sparse_boot_file_bytes
            installations[-1]['store_boot_bytes'] = sparse_boot_file_bytes(source['boot.img']) + sparse_boot_file_bytes(previous_boot)
        engine.transport.android_rpc('android-check',{})
        for name in profile['install_images']:
            entry=profile['images'][name]
            direct_images.append(image_plan(source[name],projected['inventory'][entry['partition']],entry,engine.check_cancel))
        materials={path:profile['images'][name]['sha256'] for name,path in source.items()}
        if profile.get('generic_ota_version'):
            if profile['generic_ota_version']==1 and edit['changes']:raise InstallerError('通用 OTA v1 不与 GPT/格式化编辑混在同一次计划中；请先单独完成分区配置，再安装 ROM。')
            from .native_ota import PROFILE,ARCHIVE
            descriptor=Path(next(iter(source.values()))).parent/PROFILE
            materials[str(descriptor)]=digest_file(descriptor)
            materials[str(descriptor.parent/ARCHIVE)]=profile['archive']['sha256']
    for name in roots:
        slot=next((s for s in (projected.get('registry') or {}).get('rootfs_slots',[]) if s['partition']['name']==name),None)
        if not slot or slot['status']!='reserved':raise InstallerError('请选择 rootfs，不能把固件或引导存储作为 Linux 安装目标。')
        bundle=payload.get('linux_bundles',{}).get(name,payload.get('linux_bundle',''))
        linux_source=inspect_system(dict(id=slot['id'],name=slot['name'],bundle=bundle,boot_image='',
            filesystem=slot['target']['root_fstype'],size_gib=slot['partition']['bytes']//1024**3,
            size_bytes=slot['partition']['bytes']),bundle,engine.check_cancel)
        if len(targets)==1:kind='linux';source=linux_source
        materials.update({linux_source['boot_image']:linux_source['boot_sha256'],linux_source['archive']:linux_source['archive_sha256']})
        if linux_source['archive_bytes']+512*1024**2>=engine.device['available_memory_bytes']:raise InstallerError('可用 RAM 不足以暂存 Linux 根归档。')
        if slot['target']['root_fstype'] not in engine.manifest['filesystems']:raise InstallerError('RAM 安装环境不支持目标文件系统。')
        generation=str(uuid.uuid4())
        with tempfile.TemporaryDirectory(prefix='check-boot-',dir=directory) as scratch:
            bound=Path(scratch)/'boot.img'
            try:
                bound_sha=bind(Path(linux_source['boot_image']),bound,dict(slot['target'],boot_generation=generation),engine.device['boot_bytes'])
            except (OSError,ValueError) as error:
                raise InstallerError(f'{slot["name"]} 引导镜像无法绑定到 {name}：{error}') from error
            bound_size=bound.stat().st_size
        installations.append(dict(kind='linux',target=name,name=slot['name'],rootfs_id=slot['id'],source=linux_source,
            set_default=default==name,boot_generation=generation,bound_boot_bytes=bound_size,bound_boot_sha256=bound_sha))
        if engine.manifest.get('native_recovery_manifest',{}).get('sparse_boot_store_version')==1:
            from liuqin_android import sparse_boot_file_bytes
            installations[-1]['store_boot_bytes']=(bound_size+4095)//4096*4096+sparse_boot_file_bytes(previous_boot)
    if engine.device.get('native_ota_only') and (kind!='android' or not profile.get('generic_ota_version') or edit['changes']):raise InstallerError('当前底层固件仅允许保留原分区的通用 OTA 安装，其他写入尚未适配。')
    rec_mode=payload.get('recovery_mode','off');rec=None
    if rec_mode not in ('off','boot','install'):raise InstallerError('Recovery 操作无效。')
    if rec_mode!='off':
        rec=recovery.inspect_image(payload.get('recovery_path',''))
        recovery.validate_usage(rec,rec_mode)
        if rec_mode=='boot':recovery.validate_temporary_target(engine,rec)
        if rec_mode=='install':recovery.targets(state)
        materials[rec['path']]=rec['sha256']
    if target_name=='__recovery__' and rec is None:raise InstallerError('请选择 Recovery 镜像和使用方式。')
    if kind=='partitions' and not edit['changes'] and rec is None:raise InstallerError('请选择安装目标或分区操作。')
    materials[request['installer_path']]=engine.manifest['files'][engine.manifest.get('runtime_file','installer.img')]
    materials[str(previous_boot)]=previous
    snapshot=dict(request=old,connection=request,edit=edit,materials=materials,kind=kind,source=source,
        target=target_name,recovery=rec,recovery_mode=rec_mode,backup_dir=backup,backup_id=record['backup_id'],
        persist_sha256=compatibility['current_persist_sha256'],boot_id=engine.transport.boot_id,previous_boot_sha256=previous,
        direct_android=android_selected and 'linux_boot' not in projected['inventory'],direct_images=direct_images,
        installations=installations,default_target=default,backup_policy='reuse-layout-v1',backup_reused=backup_reused)
    if getattr(engine.transport,'installer_enabled',False):
        if rec_mode!='off':raise InstallerError('原生安装保持当前 Rec；请将配套 Recovery 操作设为不处理。')
        snapshot['native_firmware_sha256']=hashlib.sha256(canonical(engine.receipt['firmware_fingerprint'])).hexdigest()
    if snapshot['direct_android'] and default!='userdata':
        raise InstallerError('没有引导存储的 Android 重装必须同时写入对应 boot.img。')
    if android_selected:
        from liuqin_rom_admission import require_install
        snapshot['rom_admission']=require_install(profile)
        snapshot['android_boot_sha256']=profile['images']['boot.img']['sha256']
        snapshot['ota_device_check']=getattr(engine,'ota_device_check',None)
    if android_selected:snapshot.update(android_profile_sha256=engine.android_profile_sha,android_profile_id=profile.get('id','stock'),rom=profile['rom'])
    if android_selected and profile.get('generic_ota_version')==2:
        from .native_ota import backup_overwrites
        from . import fixed_recovery
        if fixed_policy=='auto' and not engine.simulation:
            if fixed_records and not getattr(engine.transport,'installer_enabled',False):raise InstallerError('自动保护固定 Rec 需要原生安装环境。')
        backup_parent=Path(backup).parent
        previous=previous_rom_snapshots(backup_parent,payload['serial'])
        rom_directory=backup_parent/'rom-snapshots'/payload['serial']/engine.session_id
        backup_options=dict(previous=previous)
        if fixed_records:backup_options['extra_targets']=['recovery_a']
        backups=backup_overwrites(engine,profile,state['inventory'],rom_directory,**backup_options)
        for b in backups.values():materials[b['path']]=b['sha256']
        backup_manifest=rom_directory/'manifest.json'
        materials[str(backup_manifest)]=digest_file(backup_manifest)
        from liuqin_ota import write_names
        rom_targets=write_names(profile)|({'boot_a'} if default=='userdata' else set())
        snapshot.update(full_rom=True,rom_backup_dir=str(rom_directory),rom_targets=sorted(rom_targets),
            rom_backups={n:{k:v for k,v in b.items() if k not in ('path','reused')} for n,b in backups.items() if n in rom_targets},
            rom_backups_reused=sum(bool(b.get('reused')) for b in backups.values()))
        if fixed_policy=='auto' and not engine.simulation:
            android_source=next(job['source'] for job in installations if job['kind']=='android')
            fixed=fixed_recovery.prepare(engine,profile,android_source,state,direct_images,backups,records=fixed_records)
            if fixed:
                snapshot['fixed_recovery']=fixed
                materials[fixed['path']]=fixed['plan']['image_sha256']
                materials[fixed['qualification_path']]=fixed['qualification_sha256']
                materials[fixed['compatibility_path']]=fixed['compatibility_sha256']
    snapshot['fixed_recovery_policy']=fixed_policy
    snapshot['capacity']=plan_capacity(engine,snapshot,record)
    engine.workflow_snapshot=copy.deepcopy(snapshot);engine.plan_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(operation='wizard',plan_id=engine.plan_id,backup_dir=backup,workflow=snapshot)
    engine.save_receipt();engine.set_state('prepared')
    destructive=[dict(name=p['name'],action='删除',bytes=p['bytes']) for p in edit['deletes']]
    destructive += [dict(name=p['partition']['name'],action='格式化 '+p['filesystem'],bytes=p['partition']['bytes']) for p in edit['formats']]
    for job in installations:destructive.append(dict(name=job['target'],action='安装 '+('Android' if job['kind']=='android' else 'Linux'),bytes=projected['inventory'][job['target']]['bytes']))
    if android_selected or any(p['role']=='android' for p in edit['formats']):destructive.append(dict(name='metadata',action='初始化 Android 加密元数据',bytes=state['inventory']['metadata']['bytes']))
    engine.emit('wizard_plan',plan=dict(plan_id=engine.plan_id,serial=payload['serial'],kind=kind,target=target_name,
        additions=edit['partitions'],destructive=destructive,recovery=rec,recovery_mode=rec_mode,
        set_default=bool(default),default_target=default,installations=[{k:v for k,v in j.items() if k!='source'} for j in installations],
        preserved_roots=[n for n in projected['inventory'] if is_root_name(n) and n not in roots and n not in {p['partition']['name'] for p in edit['formats']}],
        backup_dir=backup,backup_reused=backup_reused,rom_backup_dir=snapshot.get('rom_backup_dir',''),
        rom_backups_reused=snapshot.get('rom_backups_reused',0),experimental=True,rom=snapshot.get('rom',''),
        full_rom=snapshot.get('full_rom',False),rom_admission=snapshot.get('rom_admission'),ota_device_check=snapshot.get('ota_device_check'),
        fixed_recovery=({k:v for k,v in snapshot['fixed_recovery'].items() if k not in ('path','qualification_path','qualification_sha256','compatibility_path','compatibility_sha256')} if snapshot.get('fixed_recovery') else None),
        fixed_recovery_policy=fixed_policy,
        android_writes=([p['partition']['name'] for p in direct_images if not (snapshot.get('fixed_recovery') and p['partition']['name']=='recovery_a')]+(['boot_a'] if default=='userdata' else [])) if android_selected else [],
        native_install=bool(getattr(engine.transport,'installer_enabled',False)),
        boot_store_retention=engine.manifest.get('native_recovery_manifest',{}).get('boot_store_retention_version')==1,
        default_removed=bool((state.get('registry') or {}).get('default_id')) and (state.get('registry') or {}).get('default_id')!=(projected.get('registry') or {}).get('default_id')))


def prepare_resume(engine,payload):
    """Review initialization of an exact fresh layout; never replay GPT writes."""
    path=Path(payload.get('resume_receipt','')).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size>16*1024**2:
        raise InstallerError('请选择原失败运行的 receipt.json。')
    previous=json.loads(path.read_text());original=previous.get('workflow',{})
    if (previous.get('serial')!=payload['serial'] or previous.get('simulation')!=engine.simulation or
        previous.get('operation')!='wizard' or previous.get('failed') is not True or
        previous.get('write_dispatched') is not True or previous.get('partitions_edited') or
        previous.get('system_installed') or previous.get('boot_flash_started') or
        hashlib.sha256(canonical(original)).hexdigest()!=previous.get('plan_id')):
        raise InstallerError('该记录不属于可续接的分区初始化中断；不会重复安装或修改 GPT。')
    edit=original['edit'];validate_edit(edit,payload['serial'])
    if (edit['source']['inventory'].get('linux_boot') or edit['formats'] or not edit['create_store'] or
        not edit['partitions'] or not edit['candidate_registry'] or edit['candidate_registry']['systems'] or
        any(p['role'] not in ('store','root') for p in edit['partitions'])):
        raise InstallerError('仅支持尚未初始化的全新 Linux 引导存储/rootfs 布局。')
    if engine.state=='failed':
        engine.clean_scratch();engine.release_device();engine.state='idle'
    engine.guard_operation();engine.committing=False
    connection=connection_payload(engine,payload)
    engine.create_backup(dict(connection,backup_parent=payload['backup_parent'],reuse_current=True))
    backup=engine.receipt['backup_dir'];state=partitioning.connect(engine,connection,'wizard')
    if engine.transport.current_boot_digest()!=original['previous_boot_sha256']:
        raise InstallerError('默认 boot 已改变，停止续接。')
    result=engine.transport.android_rpc('partition-resume-check',dict(plan=edit))
    if result.get('resumable') is not True:raise InstallerError('设备未确认可安全续接。')
    record=engine.load_backup(backup,payload['serial']);compatibility=engine.verify_current_backup(record)
    snapshot=dict(request=dict(payload,set_default=False),connection=connection,edit=edit,
        materials={connection['installer_path']:engine.manifest['files'][engine.manifest.get('runtime_file','installer.img')]},
        kind='partitions',source=None,target='',recovery=None,recovery_mode='off',backup_dir=backup,
        backup_id=record['backup_id'],persist_sha256=compatibility['current_persist_sha256'],boot_id=engine.transport.boot_id,
        previous_boot_sha256=original['previous_boot_sha256'],direct_android=False,direct_images=[],
        installations=[],resume_state=state,resume_origin=dict(path=str(path.resolve()),plan_id=previous['plan_id']))
    if getattr(engine.transport,'installer_enabled',False):snapshot['native_firmware_sha256']=hashlib.sha256(canonical(engine.receipt['firmware_fingerprint'])).hexdigest()
    snapshot['capacity']=plan_capacity(engine,snapshot,record)
    engine.workflow_snapshot=copy.deepcopy(snapshot);engine.plan_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    engine.receipt.update(operation='wizard',plan_id=engine.plan_id,backup_dir=backup,workflow=snapshot)
    engine.save_receipt();engine.set_state('prepared')
    engine.emit('wizard_plan',plan=dict(plan_id=engine.plan_id,serial=payload['serial'],kind='partitions',target='',additions=[],
        destructive=[dict(name='linux_boot',action='续接初始化已创建的引导存储（不重写 GPT）',bytes=edit['store']['bytes'])],
        recovery=None,recovery_mode='off',set_default=False,backup_dir=backup,experimental=True,resume_initialization=True,
        resume_message='只初始化已匹配原方案且未格式化的 linux_boot，建立预留 rootfs 登记。完成后重新选择 rootfs 安装 Linux。'))


def install(engine,payload):
    if engine.state!='prepared' or not getattr(engine,'workflow_snapshot',None):
        raise InstallerError('没有可执行的已确认方案；ROM 分析预览不能用于刷写。')
    consent=payload.get('confirmation',{});snapshot=engine.workflow_snapshot
    if (engine.state!='prepared' or payload.get('plan_id')!=engine.plan_id or payload.get('session_id')!=engine.session_id or
        consent.get('serial')!=engine.device['serial'] or consent.get('accept_operations') is not True or consent.get('allow_experimental') is not True):
        raise InstallerError('请核对设备并确认完整操作列表。')
    if hashlib.sha256(canonical(snapshot)).hexdigest()!=engine.plan_id:raise InstallerError('确认后的完整方案发生变化。')
    if snapshot['kind'] == 'switch':
        from .boot_switch import install as install_switch
        return install_switch(engine,snapshot)
    if snapshot['kind'] == 'maintenance':
        from .boot_store import install as install_maintenance
        return install_maintenance(engine,snapshot)
    if snapshot.get('android_profile_sha256') and (engine.android_profile_sha!=snapshot['android_profile_sha256'] or
        hashlib.sha256(canonical(engine.android_profile)).hexdigest()!=snapshot['android_profile_sha256']):
        raise InstallerError('确认后的 Android ROM 适配发生变化。')
    if snapshot.get('android_profile_sha256'):
        from liuqin_rom_admission import require_install
        if require_install(engine.android_profile)!=snapshot['rom_admission']:raise InstallerError('ROM 准入状态在确认后改变，未写入。')
    for path,digest in snapshot['materials'].items():
        if digest_file(Path(path))!=digest:raise InstallerError('确认后的安装材料发生变化：'+str(path))
    if engine.transport.boot_id!=snapshot['boot_id'] or engine.transport.current_boot_digest()!=snapshot['previous_boot_sha256']:
        raise InstallerError('RAM 会话或默认引导改变。')
    live_state=engine.transport.android_rpc('partition-inspect',{})
    if snapshot_id(live_state)!=snapshot_id(snapshot.get('resume_state',snapshot['edit']['source'])):
        raise InstallerError('分区或系统登记在确认后改变。')
    record=engine.load_backup(snapshot['backup_dir'],engine.device['serial'])
    compatibility=engine.verify_current_backup(record)
    if record['backup_id']!=snapshot['backup_id'] or compatibility['current_persist_sha256']!=snapshot['persist_sha256']:
        raise InstallerError('确认后的设备备份或校准信息发生变化。')
    check_capacity(engine,snapshot,live_state)
    if any(job['kind']=='linux' for job in snapshot['installations']) and hasattr(engine.transport,'validate_linux_install'):
        engine.transport.validate_linux_install()
    engine.committing=True;engine.workflow_committing=True;engine.defer_reboot=True;engine.set_state('installing')
    overall=copy.deepcopy(engine.receipt);overall['write_dispatched']=False;children=[]
    emit=engine.emit_callback
    def filtered(event):
        if event['type'] in ('phase','progress','notice','command'):emit(event)
    engine.emit_callback=filtered
    native_session=None
    try:
        if getattr(engine.transport,'installer_enabled',False):
            from .native_installer import Session
            native_session=Session(engine,snapshot);native_session.start()
            overall.update({k:v for k,v in engine.receipt.items() if k.startswith(('native_','recovery_'))})
        if snapshot['edit']['changes']:
            engine.phase('write_layout',detail='应用已确认的分区方案')
            overall['write_dispatched']=True;engine.receipt['write_dispatched']=True;engine.save_receipt()
            action='partition-resume' if 'resume_state' in snapshot else 'partition-edit'
            result=engine.transport.android_rpc(action,dict(plan=snapshot['edit'],confirm_edits=True))
            if result.get('edited') is not True:raise InstallerError('分区编辑未完成。')
            overall['partitions_edited']=True
            engine.receipt['partitions_edited']=True;engine.save_receipt()
        after=engine.transport.android_rpc('partition-inspect',{})
        expected=snapshot['edit']['projected']['inventory']
        if set(after['inventory'])!=set(expected) or any(any(after['inventory'][name].get(k)!=part.get(k) for k in ('parent','number','start','bytes','partuuid')) for name,part in expected.items()):
            raise InstallerError('编辑后的目标身份与确认方案不一致。')
        if snapshot['recovery_mode']=='install':
            overall['write_dispatched']=True;engine.receipt['write_dispatched']=True;engine.save_receipt()
            recovery.install(engine,snapshot['recovery'],after,Path(overall['run_dir'])/'recovery')
            overall.update({k:v for k,v in engine.receipt.items() if k.startswith('recovery_')})
        if snapshot['direct_android']:
            overall['write_dispatched']=True;engine.receipt['write_dispatched']=True;engine.save_receipt()
            images=copy.deepcopy(snapshot['direct_images'])
            for image in images:
                actual=after['inventory'][image['partition']['name']]
                if any(actual[k]!=image['partition'][k] for k in ('name','parent','number','partuuid','start','bytes')):
                    raise InstallerError('Android 写入目标在分区刷新后改变。')
                image['partition']=copy.deepcopy(actual)
            engine.android_plan=dict(images=images)
            engine.android_image_paths={engine.android_profile['images'][name]['partition']:path for name,path in snapshot['source'].items()}
            result=engine.transport.android_rpc('android-direct',dict(plan=dict(inventory=after['inventory'],images=images,
                previous_boot_sha256=snapshot['previous_boot_sha256']),erase_android_data=True))
            if result.get('rom_written') is not True or result.get('android_data_initialized') is not True:raise InstallerError('Android 写入或初始化未确认完成。')
            # Persist the completed destructive stage before attempting any
            # reboot or boot-image verification. A later failure is not a ROM
            # write failure and must never erase this information.
            progress=dict(android_rom_written=True,android_data_initialized=True,system_installed=True,
                **{k:v for k,v in result.items() if k.startswith('recovery_')})
            overall.update(progress);engine.receipt.update(progress);engine.save_receipt()
            if snapshot['request'].get('set_default'):direct_android_boot(engine,snapshot,Path(overall['run_dir'])/'boot_a.img')
            overall.update({k:v for k,v in engine.receipt.items() if k.startswith('boot_') or k=='active_slot_set'})
            overall['system_installed']=True
        engine.transport.cancel_cleanup();engine.clean_scratch();engine.release_device()
        jobs=snapshot.get('installations',[])
        # Keep boot_a unchanged until the last selected system has completed.
        # This also gives the native session one previous-boot identity.
        for job in sorted(jobs,key=lambda j:j['set_default']):
            if snapshot['direct_android']:continue
            engine.state='idle';engine.committing=False
            connection=snapshot['connection']
            engine.create_backup(dict(connection,backup_parent=snapshot['request']['backup_parent'],reuse_current=True))
            new_backup=engine.receipt['backup_dir'];engine.state='idle'
            child=dict(connection,backup_dir=new_backup)
            if job['kind']=='linux':
                child.update(layout='linux-managed',bundle=job['source']['bundle'],rootfs_id=job['rootfs_id'],
                    set_default=job['set_default'],boot_generation=job['boot_generation'])
                confirmation=dict(serial=connection['serial'],personal_data_backed_up=True,erase_rootfs=True,preserve_other_systems=True,allow_experimental=True)
            else:
                child.update(layout='android-manage',android_config=dict(schema_version=1,mode='reinstall',
                    rom_path=str(Path(next(iter(job['source'].values()))).parent),set_default=job['set_default']))
                confirmation=dict(serial=connection['serial'],personal_data_backed_up=True,erase_android_data=True,preserve_linux=True,allow_experimental=True)
            engine.prepare(child)
            # The child revalidates sources and the exact target again after GPT changes.
            actual=engine.linux_plan['system']['partition'] if job['kind']=='linux' else engine.android_plan['inventory']['userdata']
            if any(actual[k]!=expected[job['target']][k] for k in ('name','partuuid','start','bytes')):raise InstallerError('后续安装目标不匹配已确认的分区。')
            if job['kind']=='linux' and (engine.linux_system['source']!=job['source'] or
                engine.linux_system['boot_sha256']!=job['bound_boot_sha256']):
                raise InstallerError('Linux 子计划的安装材料或绑定引导不匹配确认方案。')
            overall['write_dispatched']=True
            engine.install(dict(session_id=engine.session_id,plan_id=engine.plan_id,confirmation=confirmation))
            children.append(copy.deepcopy(engine.receipt));overall['system_installed']=True
            overall['children']=copy.deepcopy(children)
            overall.setdefault('installed_targets',[]).append(job['target'])
            for k,v in engine.receipt.items():
                if k.startswith(('boot_','android_','recovery_')):overall[k]=v
            engine.receipt=overall;engine.save_receipt()
        if snapshot['recovery_mode']=='boot':
            transport=engine.transport
            if transport is None:raise InstallerError('缺少 Fastboot 会话。')
            if digest_file(Path(snapshot['recovery']['path']))!=snapshot['recovery']['sha256']:raise InstallerError('Recovery 镜像变化。')
            recovery.validate_temporary_target(engine,snapshot['recovery'])
            if not engine.simulation:
                transport.require_boot_compatibility();transport.clear_boot_evidence()
            transport.fastboot('boot',snapshot['recovery']['path']);overall['recovery_boot_requested']=True
            if not engine.simulation:
                transport.wait_recovery();overall['recovery_boot_connected']=True
        elif jobs and snapshot['request'].get('set_default') and not native_session:
            engine.defer_reboot=False;engine.transport.fastboot('reboot')
        if native_session:
            native_session.finish(True)
            # With no installation children, receipt is still the original
            # object rather than overall. Preserve the completion result too.
            if 'boot_store_retention' in engine.receipt:
                overall['boot_store_retention']=copy.deepcopy(engine.receipt['boot_store_retention'])
        overall.update(children=children,completed=True)
        if native_session:overall.update(recovery_retained=True,native_install_state='complete',first_boot_verified=False)
        engine.receipt=overall;engine.save_receipt()
        engine.emit_callback=emit;engine.set_state('wizard_complete');engine.emit('wizard_complete',receipt=overall)
    except Exception:
        if engine.receipt is not overall:children.append(copy.deepcopy(engine.receipt))
        overall.update(children=children,failed=True)
        for k,v in engine.receipt.items():
            if k.startswith(('recovery_','boot_','android_','gpt_','native_')) or k in ('kernel_refreshed','partitions_edited','active_slot_set','system_installed','phase'):overall[k]=v
        if overall.get('write_dispatched'):
            overall['recovery_required']=True
            overall['recovery_guidance']='写入可能已开始。保留当前安装环境与执行记录，不要直接重启、重复分区或安装；先只读核对 GPT、系统写入和默认引导的完成状态。'
        engine.receipt=overall;engine.state='failed';engine.save_receipt();raise
    finally:
        if native_session and native_session.active:
            try:native_session.finish(False)
            except Exception as cleanup_error:
                engine.receipt['native_session_cleanup_error']=str(cleanup_error);engine.receipt['recovery_retained']=True;engine.save_receipt()
        engine.emit_callback=emit;engine.workflow_committing=False;engine.committing=False;engine.defer_reboot=False
        engine.clean_scratch();engine.release_device()


def direct_android_boot(engine,snapshot,previous):
    transport=engine.transport;image=Path(snapshot['source']['boot.img'])
    expected=engine.android_profile['images']['boot.img']['sha256']
    engine.receipt.update(boot_finalization_pending=True,boot_readback_verified=False,boot_expected_sha256=expected);engine.save_receipt()
    if digest_file(image)!=expected or image.stat().st_size!=engine.device['boot_bytes']:
        raise InstallerError('Android 引导材料在 ROM 写入期间改变，未写入 boot_a。')
    if digest_file(previous)!=snapshot['previous_boot_sha256'] or transport.current_boot_digest()!=snapshot['previous_boot_sha256']:
        raise InstallerError('当前引导或恢复快照改变。')
    if getattr(transport,'installer_enabled',False):
        transport.write_native_boot(image,expected,snapshot['previous_boot_sha256']);return
    try:
        transport.return_fastboot();transport.check_device()
        engine.phase('flash_boot');engine.receipt.update(boot_flash_started=True,boot_expected_sha256=expected);engine.save_receipt()
        transport.fastboot('flash','boot_a',str(image));engine.receipt['boot_written']=True;engine.save_receipt()
        transport.fastboot('--set-active=a');engine.receipt['active_slot_set']=True;engine.save_receipt()
        engine.phase('verify_boot');transport.boot_ram()
        if transport.current_boot_digest()!=expected:raise InstallerError('Android 引导回读失败。')
        engine.receipt.update(boot_readback_verified=True,boot_finalization_pending=False);engine.save_receipt()
    except Exception as error:
        # Android ROM/userdata have already been replaced. Restoring an old
        # Android or userdata-based Linux boot cannot restore that system.
        engine.receipt.update(boot_finalization_pending=True,boot_error=str(error),
            boot_rollback_skipped='Android ROM/data already replaced; old boot is not a complete rollback')
        try:
            if transport.ram_active and getattr(transport,'boot_id',None):transport.return_fastboot()
        except Exception as cleanup_error:engine.receipt['boot_cleanup_error']=str(cleanup_error)
        engine.save_receipt()
        raise
