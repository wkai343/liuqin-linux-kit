# SPDX-License-Identifier: MIT
"""Local ROM analysis plus fresh, strictly read-only native device capture."""
import copy
import json
import os
from pathlib import Path
import uuid
from liuqin_rom_analysis import analyze,reference
from liuqin_rom_preview import build,validate_layout
from .engine import InstallerError,Cancelled

def capture(engine,serial,path):
    from . import firmware,native_profiles
    from .native_recovery_transport import probe_native
    probe=probe_native(serial,timeout=8)
    if probe.get('serial')!=serial:raise InstallerError('当前原生 Recovery 与所选平板不一致。')
    running=native_profiles.find_running(probe,path);reader=firmware.verified_reader(engine,running,probe)
    def layout():
        raw=reader.remote(firmware.command(firmware.INVENTORY_CODE),timeout=12)
        if len(raw)>256*1024:raise InstallerError('分区数据过大。')
        return validate_layout(json.loads(raw))
    rows=layout();snapshot=firmware.collect(reader,probe,reader.probe_current)
    if rows!=layout():raise InstallerError('分析期间分区布局变化，请重新读取。')
    return snapshot,rows

def demo_capture(serial):
    """Explicit simulation only; never used as fallback for a disconnected device."""
    from liuqin_firmware import load_catalog,fingerprint_id
    from liuqin_rom_analysis import profiles
    match=copy.deepcopy(load_catalog()[0]['match']);p=next(p for p in profiles() if p['id']=='miui14')
    slot=dict(status='complete',match=match,measured=copy.deepcopy(match),fingerprint_id=fingerprint_id(match),
        avb=p['projected']['vbmeta']['avb'],avb_system=p['projected']['vbmeta_system']['avb'])
    snapshot=dict(schema=1,device='liuqin',serial=serial,boot_id=str(uuid.uuid4()),runtime_build_id='a'*64,unlocked=True,sealed=True,active_slot='a',slots={'a':slot,'b':copy.deepcopy(slot)})
    names=['boot_a','vendor_boot_a','dtbo_a','vbmeta_a','vbmeta_system_a','super','userdata','metadata','recovery_a','recovery_b','linux_boot','lqr_123456789012']
    rows=[dict(name=n,node='sda'+str(i+1),dev='8:'+str(i+1),size=16*1024**3,start=i*4096,ro='1') for i,n in enumerate(names)]
    return snapshot,rows

def preview(engine,payload):
    engine.guard_operation();serial=engine.require_serial(payload);old_state=engine.state
    mode=payload.get('source_mode','file');policy=payload.get('firmware_policy','preserve')
    if mode not in ('file','reference','ota') or policy not in ('preserve','rom'):raise InstallerError('ROM 预览选项无效。')
    engine.rom_preview=None;engine.cancel_event.clear();engine.set_state('rom_analyzing')
    try:
        engine.emit('notice',message='只读分析 ROM；不会备份后自动安装，也不会重启平板。')
        if mode=='ota':
            from .native_ota import preview as ota_preview
            result=ota_preview(engine,payload)
        else:
            analysis=reference(payload.get('reference_id','')) if mode=='reference' else analyze(payload.get('rom_path',''),engine.check_cancel)
            engine.check_cancel()
            if engine.simulation:snapshot,rows=demo_capture(serial)
            else:
                engine.lock_device(serial)
                try:snapshot,rows=capture(engine,serial,payload.get('runtime_path',''))
                finally:engine.release_device()
            engine.check_cancel();result=build(analysis,snapshot,rows,policy)
        directory=engine.state_dir/'rom-previews'
        if directory.is_symlink():raise InstallerError('ROM 预览记录目录不能是链接。')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700);path=directory/(result['preview_id']+'-'+uuid.uuid4().hex[:8]+'.json')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
        result['record_path']=str(path);engine.rom_preview=result
        engine.emit('rom_preview',preview=result)
    except Cancelled:
        old_state='cancelled'
        raise
    finally:
        engine.cancel_event.clear();engine.set_state(old_state)
