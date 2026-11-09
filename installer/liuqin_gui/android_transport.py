# SPDX-License-Identifier: MIT
"""Hash-checked RPC and bounded ROM chunk transport to the RAM environment."""
import base64
import hashlib
import http.server
import io
import json
from pathlib import Path
import shlex
import socket
import threading
import uuid

from . import host
from .engine import DEVICE_SCRIPTS, InstallerError, cli, digest_file
from liuqin_android import canonical
from liuqin_android_images import Image


def verify_helper(transport):
    if getattr(transport,'android_trusted_boot_id',None)==transport.boot_id:return
    shared=Path(cli.layout.__file__).parent
    files={'android-device.py':DEVICE_SCRIPTS/'android-device.py'}
    files.update({name:shared/name for name in ('liuqin_android.py','liuqin_android_images.py','liuqin_layout.py',
        'liuqin_multiboot.py','liuqin_managed.py','liuqin_boot_target.py','liuqin_partitions.py','liuqin_partition_names.py','liuqin_partition_editor.py','liuqin_block_identity.py','liuqin-android-profile.json','liuqin-miku-profile.json','liuqin-rom-images.json')})
    if 'native_recovery_manifest' in transport.engine.manifest:
        files['liuqin_board_identity.py']=shared/'liuqin_board_identity.py'
    native=transport.engine.manifest.get('native_recovery_manifest',{})
    if native.get('inspection_only') is True or native.get('fixed_installer_version')==1:
        # Bind an installed/inspection runtime to its inspected local image.
        # New protocols still require their explicit runtime capability version.
        import struct
        from liuqin_artifacts import bounded_initramfs,cpio_files
        path=Path(transport.engine.runtime_path or transport.engine.installer_snapshot);b=path.read_bytes()
        expected_image=transport.engine.manifest['files'][transport.engine.manifest.get('runtime_file','native-recovery.img')]
        if hashlib.sha256(b).hexdigest()!=expected_image:raise InstallerError('用于核对只读助手的原生镜像发生变化。')
        ks,rs=struct.unpack_from('<I',b,8)[0],struct.unpack_from('<I',b,16)[0];start=4096+(ks+4095)//4096*4096
        packed=cpio_files(bounded_initramfs(b[start:start+rs],256*1024**2))
        expected={name:hashlib.sha256(packed['usr/lib/liuqin/'+name][1]).hexdigest() for name in files}
    else:
        files['liuqin_native_install.py']=shared/'liuqin_native_install.py'
        expected={name:digest_file(path) for name,path in files.items()}
    names=list(files);rows=[]
    for start in range(0,len(names),6):rows+=transport.remote('/bin/busybox sha256sum '+' '.join('/usr/lib/liuqin/'+name for name in names[start:start+6])).decode().splitlines()
    actual={Path(fields[1]).name:fields[0] for line in rows if len(fields:=line.split())==2}
    if actual!=expected:
        raise InstallerError('RAM 安装器的 Android 代码或适配清单与电脑端不同，请重新构建配套 Recovery 或安装环境。')
    transport.android_trusted_boot_id=transport.boot_id


def rpc(transport, action, request):
    verify_helper(transport)
    engine=transport.engine;token=uuid.uuid4().hex
    script=(DEVICE_SCRIPTS/'android-operation.sh').read_bytes()
    payload={}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*_args):pass
        def do_GET(self):
            if self.client_address[0]!=transport.http_peer:self.send_error(404);return
            try:
                if self.path==f'/{token}/script':data=script
                elif self.path==f'/{token}/request':data=payload['request']
                elif self.path==f'/{token}/boot':data=engine.android_boot.read_bytes()
                elif self.path.startswith(f'/{token}/images/') and action in ('install','android-direct'):
                    pieces=self.path.split('/')
                    name,index=pieces[-2],int(pieces[-1])
                    plan=next(p for p in engine.android_plan['images'] if p['partition']['name']==name)
                    chunk=plan['chunks'][index]
                    if index<0:raise ValueError('negative chunk')
                    source=Image(engine.android_image_paths[name])
                    try:
                        available=min(chunk['bytes'],max(0,source.size-chunk['offset']))
                        data=source.read(chunk['offset'],available) if available else b''
                        data+=bytes(chunk['bytes']-available)
                    finally:source.close()
                    if hashlib.sha256(data).hexdigest()!=chunk['sha256']:raise ValueError('source changed')
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
            except (ValueError,OSError,IndexError,StopIteration):self.send_error(409)
    directory='/run/liuqin-android-host/'+token
    with transport.http_server(Handler) as endpoint:
        base=endpoint+'/'+token
        extra={}
        if engine.android_profile.get('generic_ota_version') in (1,2):extra['generated_profile']=engine.android_profile
        payload['request']=canonical(dict(request,**extra,base=base,serial=engine.device['serial'],profile_sha256=engine.android_profile_sha,
            profile_id=engine.android_profile.get('id','stock')))
        transport.remote('mkdir -p '+directory+' && /bin/busybox wget -O '+directory+'/script '+base+'/script',120)
        transport.remote('printf "%s  %s\\n" '+hashlib.sha256(script).hexdigest()+' '+directory+'/script | /bin/busybox sha256sum -c -')
        transport.remote('/bin/busybox wget -O '+directory+'/request '+base+'/request',120)
        pending=bytearray()
        def observe(data):
            pending.extend(data)
            while b'\n' in pending:
                line,_,rest=pending.partition(b'\n');pending[:]=rest
                if line.startswith(b'liuqin-android-progress '):
                    value=json.loads(line[len(b'liuqin-android-progress '):])
                    engine.phase_if_android_partition(value['partition'])
                    engine.progress('android_write',value['offset'],value['bytes'])
                elif line.startswith(b'liuqin-partition-progress '):
                    value=json.loads(line[len(b'liuqin-partition-progress '):]);stage=value.get('stage')
                    if stage in ('gpt_written','kernel_refreshed'):
                        engine.receipt[stage]=True;engine.save_receipt()
                        engine.phase('write_layout',detail='GPT 已写入，正在刷新内核分区视图' if stage=='gpt_written' else '内核分区已刷新，正在核验分区身份')
        result=transport.remote(shlex.join(['sh',directory+'/script',transport.boot_id,action,directory+'/request',
            hashlib.sha256(payload['request']).hexdigest()]),7200,observe=observe if action in ('install','android-direct','partition-edit','partition-apply') else None)
        prefix=b'liuqin-android-result '
        records=[line[len(prefix):] for line in result.splitlines() if line.startswith(prefix)]
        if len(records)!=1:raise InstallerError('Android 操作没有返回完整结果。')
        return json.loads(records[0])


def previous_boot_can_run(plan):
    if plan.get('mode')=='preserve':return True
    previous=next((s for s in plan['registry']['systems'] if s['id']==plan['registry'].get('default_id')),None)
    if not previous or previous.get('type')!='linux' or previous.get('boot_sha256')!=plan['previous_boot_sha256']:return False
    part=previous.get('partition',{});protected=next((p for p in plan['protected'] if p.get('name')==part.get('name')),None)
    return (bool(protected) and protected.get('parent')=='sda' and part.get('parent','sda')=='sda' and
        all(protected.get(k)==part.get(k) for k in ('name','number','partuuid','start','bytes')) and
        (not part.get('fsuuid') or protected.get('fsuuid')==part['fsuuid']))


def set_default(transport, engine, registry):
    previous=engine.android_previous_boot
    expected=engine.android_profile['images']['boot.img']['sha256']
    plan=engine.android_plan
    if digest_file(engine.android_boot)!=expected or digest_file(previous)!=plan['previous_boot_sha256']:
        raise InstallerError('Android 引导或本次回退快照发生变化。')
    if transport.current_boot_digest()!=plan['previous_boot_sha256']:
        raise InstallerError('当前默认引导已变化，未写入 boot_a。')
    engine.receipt.update(boot_finalization_pending=True,boot_readback_verified=False,boot_expected_sha256=expected);engine.save_receipt()
    if getattr(transport,'installer_enabled',False):
        transport.write_native_boot(engine.android_boot,expected,plan['previous_boot_sha256'])
        transport.android_rpc('commit',dict(plan=plan,registry=registry,expected_boot=expected))
        engine.receipt.update(android_registered=True,android_commit_pending=False,default_id=plan['android_id'],recovery_retained=True);engine.save_receipt();return
    try:
        transport.return_fastboot();transport.check_device()
        engine.phase('flash_boot');engine.receipt['boot_flash_started']=True;engine.save_receipt()
        transport.fastboot('flash','boot_a',str(engine.android_boot))
        transport.fastboot('--set-active=a')
        engine.receipt.update(boot_written=True,active_slot_set=True);engine.save_receipt()
        engine.phase('verify_boot');transport.boot_ram()
        if transport.current_boot_digest()!=expected:raise InstallerError('Android boot_a 回读失败。')
        engine.receipt.update(boot_readback_verified=True,boot_finalization_pending=False,android_commit_pending=True);engine.save_receipt()
        rpc(transport,'commit',dict(plan=plan,registry=registry,expected_boot=expected))
        engine.receipt.update(boot_readback_verified=True,boot_finalization_pending=False,android_commit_pending=False,android_registered=True,default_id=plan['android_id'])
        engine.save_receipt()
    except Exception as error:
        if engine.receipt.get('boot_flash_started'):
            if not previous_boot_can_run(plan):
                engine.receipt.update(boot_finalization_pending=not engine.receipt.get('boot_readback_verified',False),
                    android_commit_pending=not engine.receipt.get('android_registered',False),boot_error=str(error),
                    boot_rollback_skipped='Previous system is not verified intact after Android ROM replacement')
                try:
                    if transport.ram_active and getattr(transport,'boot_id',None):transport.return_fastboot()
                except Exception as cleanup_error:engine.receipt['boot_cleanup_error']=str(cleanup_error)
                engine.save_receipt();raise
            engine.receipt['boot_rollback_attempted']=True;engine.save_receipt()
            try:
                if transport.ram_active:transport.return_fastboot()
                transport.fastboot('flash','boot_a',str(previous));transport.fastboot('--set-active=a');transport.boot_ram()
                if transport.current_boot_digest()!=plan['previous_boot_sha256']:
                    raise InstallerError('旧默认引导恢复回读失败。')
                engine.receipt.update(boot_rollback_verified=True,boot_written=False,boot_readback_verified=False,active_slot_set=False)
                engine.save_receipt()
                from liuqin_android import next_registry
                android=next(s for s in registry['systems'] if s['type']=='android')
                fallback=next_registry(plan['registry'],android,plan['transaction'],plan['mode'],False)
                fallback['resolved_android_transactions']=registry['resolved_android_transactions']
                rpc(transport,'commit',dict(plan=plan,registry=fallback,expected_boot=plan['previous_boot_sha256'],restore_default=True))
                engine.receipt.update(boot_rollback_verified=True,boot_written=False,boot_readback_verified=False,
                                      boot_finalization_pending=False,android_commit_pending=False,
                                      active_slot_set=False,android_registered=True)
                transport.return_fastboot()
            except Exception as error:engine.receipt['boot_rollback_error']=str(error)
            engine.save_receipt()
        raise
    transport.return_fastboot();transport.fastboot('reboot')
