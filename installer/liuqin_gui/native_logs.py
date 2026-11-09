# SPDX-License-Identifier: MIT
"""Export the native UI's bounded RAM-only log bundle to a chosen PC folder."""
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import uuid
from .engine import InstallerError
from .native_recovery_transport import NativeRecoveryTransport


def validate_metadata(value):
    if (not isinstance(value,dict) or not re.fullmatch(r'/run/liuqin-ui-exports/recovery-log-[a-f0-9]{16}\.tar\.gz',value.get('file','')) or
        type(value.get('bytes'))!=int or not 0<value['bytes']<=2*1024**2 or
        not re.fullmatch('[a-f0-9]{64}',value.get('sha256',''))):
        raise InstallerError('Recovery 日志包描述无效。')
    return value


def decode_export(metadata,raw):
    validate_metadata(metadata)
    data=base64.b64decode(b''.join(raw.split()),validate=True)
    if len(data)!=metadata['bytes'] or hashlib.sha256(data).hexdigest()!=metadata['sha256']:
        raise InstallerError('Recovery 日志传输校验失败。')
    return data


def export(engine,payload):
    engine.guard_operation();serial=engine.require_serial(payload)
    destination=Path(payload['destination']).expanduser().resolve()
    if not destination.is_dir():raise InstallerError('请选择电脑上的日志保存目录。')
    if engine.simulation:
        engine.emit('notice',message='演示模式：未从平板导出日志。');return
    from liuqin_artifacts import boot_interface
    info=boot_interface(Path(payload['runtime_path']),installer=True)
    native=info.get('native_recovery_manifest',{})
    if native.get('ui_version')!=1:raise InstallerError('请使用带 LVGL 维护服务的原生 Recovery。')
    manifest=dict(native_recovery_manifest=native,installer_interface=info['interface'],
        installer_kernel_release=info['kernel_release'],installer_multiboot_version=info['multiboot_version'])
    proxy=SimpleNamespace(manifest=manifest,check_cancel=engine.check_cancel)
    engine.lock_device(serial)
    try:
        transport=NativeRecoveryTransport(proxy,serial);value=transport.probe_current(timeout=10)
        transport.validate_probe(value);transport.boot_id=value['boot_id'];transport.verify_ram_interface()
        metadata=validate_metadata(json.loads(transport.remote('/usr/bin/python3 -I -B /usr/lib/liuqin/native-ui-service.py request export_logs',timeout=20)))
        data=decode_export(metadata,transport.remote('/bin/busybox base64 '+metadata['file'],timeout=30))
        output=destination/('liuqin-recovery-'+serial+'-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.tar.gz')
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data)
        engine.emit('native_logs_exported',path=str(output),sha256=metadata['sha256'],bytes=len(data))
    finally:engine.release_device()
