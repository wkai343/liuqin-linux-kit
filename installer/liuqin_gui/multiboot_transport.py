# SPDX-License-Identifier: MIT
"""RAM command transport for typed layout and boot-store operations."""
import hashlib
import http.server
import io
from pathlib import Path
import shlex
import socket
import threading
import uuid
from . import host
from .engine import DEVICE_SCRIPTS, InstallerError, digest_file


def invoke(transport, name, arguments, payload=None, digest=None):
    script=(DEVICE_SCRIPTS/name).read_bytes()
    if payload is not None:
        observed=hashlib.sha256(payload).hexdigest() if isinstance(payload,bytes) else digest_file(payload)
        if observed!=digest: raise InstallerError('待传输的多系统材料发生变化。')
    token=uuid.uuid4().hex
    files={f'/{token}/script':script}
    if payload is not None: files[f'/{token}/payload']=payload
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*_args): pass
        def do_GET(self):
            item=files.get(self.path)
            if self.client_address[0]!=transport.http_peer or item is None:
                self.send_error(404);return
            size=len(item) if isinstance(item,bytes) else item.stat().st_size
            self.send_response(200);self.send_header('Content-Length',str(size));self.end_headers()
            stream=io.BytesIO(item) if isinstance(item,bytes) else item.open('rb')
            try:
                with stream:
                    while chunk:=stream.read(256*1024): self.wfile.write(chunk)
            except (BrokenPipeError,ConnectionResetError): pass
    directory='/run/liuqin-multiboot/'+token
    with transport.http_server(Handler) as endpoint:
        base=endpoint+'/'+token
        transport.remote('mkdir -p '+directory+' && /bin/busybox wget -O '+directory+'/script '+base+'/script',120)
        transport.remote('printf "%s  %s\\n" '+hashlib.sha256(script).hexdigest()+' '+directory+'/script | /bin/busybox sha256sum -c -')
        fields=arguments(base,directory) if callable(arguments) else arguments
        return transport.remote(shlex.join(['sh',directory+'/script',transport.boot_id,*fields]),3600)


def apply_layout(transport, planned, payload, report, info):
    digest=hashlib.sha256(payload).hexdigest()
    def arguments(base,directory):
        path=directory+'/plan'
        transport.remote('/bin/busybox wget -O '+path+' '+base+'/payload')
        return [path,digest,hashlib.sha256(report.encode()).hexdigest(),
                hashlib.sha256(info.encode()).hexdigest(),'ERASE-LIUQIN-USERDATA']
    result=invoke(transport,'multiboot-layout.sh',arguments,payload,digest)
    if b'liuqin-multiboot: LAYOUT_APPLIED' not in result:
        raise InstallerError('平板未确认多系统分区写入完成。')


def store_payload(transport, action, store, payload=None, digest=None, expected_boot='-'):
    def arguments(base,_directory):
        result=[action,store['partuuid'],store['fsuuid'],str(store['bytes'])]
        if payload is not None:
            size=len(payload) if isinstance(payload,bytes) else Path(payload).stat().st_size
            result += [base+'/payload',digest,str(size),expected_boot]
        return result
    result=invoke(transport,'multiboot-store.sh',arguments,payload,digest)
    if b'liuqin-store: COMPLETE' not in result: raise InstallerError('引导存储操作未完成。')


def set_default(transport, system, store, registry, backup):
    engine=transport.engine
    image=Path(system['padded_image']);digest=system['padded_sha256']
    if image.stat().st_size!=engine.device['boot_bytes'] or digest_file(image)!=digest:
        raise InstallerError('默认引导镜像被修改。')
    previous=engine.multi_previous_boot
    previous_hash=engine.multi_registry['boot_transaction']['previous_boot_sha256']
    if digest_file(previous)!=previous_hash: raise InstallerError('旧引导备份被修改。')
    if transport.current_boot_digest()!=previous_hash: raise InstallerError('当前 boot_a 不再匹配事务快照。')
    engine.receipt.update(boot_finalization_pending=True,boot_readback_verified=False);engine.save_receipt()
    try:
        engine.phase('return_fastboot');transport.return_fastboot();transport.check_device()
        engine.phase('flash_boot')
        engine.receipt['boot_flash_started']=True;engine.save_receipt()
        transport.fastboot('flash','boot_a',str(image))
        engine.receipt['boot_written']=True;engine.save_receipt()
        transport.fastboot('--set-active=a')
        engine.receipt['active_slot_set']=True;engine.save_receipt()
        engine.phase('verify_boot');transport.boot_ram()
        actual=transport.remote('/bin/busybox sha256sum /dev/disk/by-partlabel/boot_a',180).decode().split()[0]
        if actual!=digest: raise InstallerError('默认 boot_a 回读校验失败。')
        engine.receipt['boot_readback_verified']=True;engine.save_receipt()
        store_payload(transport,'publish',store,registry,hashlib.sha256(registry).hexdigest(),digest)
        engine.receipt.update(default_id=system['id'],default_registry_committed=True,boot_finalization_pending=False)
        engine.save_receipt()
    except Exception as error:
        if engine.receipt.get('boot_flash_started'):
            # This path has replaced the original userdata layout. A snapshot
            # of its old boot is evidence, not a surviving rollback system.
            engine.receipt.update(boot_error=str(error),boot_finalization_pending=True,
                boot_rollback_skipped='Original userdata layout was replaced; old boot is not a verified surviving system')
            try:
                if transport.ram_active: transport.return_fastboot()
            except Exception as cleanup_error:
                engine.receipt['boot_cleanup_error']=str(cleanup_error)
            engine.save_receipt()
        raise
    engine.phase('return_fastboot');transport.return_fastboot()
    engine.phase('reboot');transport.fastboot('reboot')
