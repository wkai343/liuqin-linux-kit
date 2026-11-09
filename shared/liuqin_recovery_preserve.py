# SPDX-License-Identifier: MIT
"""Confirmed fixed-Recovery preservation, shared by host and native executor.

This does not change AVB, invent support for a bootloader, or rebuild a kernel.
An independently inspected, cold-booted image is reused when its dependencies
remain compatible. Only recovery_a can be restored by this protocol.
"""
import hashlib
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

CAPACITY = 100 * 1024**2
HEX = re.compile(r'[a-f0-9]{64}')
CHAIN = {'abl_a', 'dtbo_a', 'vbmeta_a', 'vbmeta_system_a'}


def validate(plan):
    from liuqin_native_install import identity
    from liuqin_ota import FIRMWARE
    if (not isinstance(plan, dict) or set(plan) != {
            'schema', 'image_sha256', 'image_bytes', 'build_id', 'partition',
            'previous_sha256', 'backup', 'before', 'after', 'adaptation'} or
            plan['schema'] != 1 or plan['image_bytes'] != CAPACITY or
            plan['adaptation'] not in ('reuse', 'rom-metadata') or
            any(not isinstance(plan[k], str) or not HEX.fullmatch(plan[k])
                for k in ('image_sha256', 'build_id', 'previous_sha256'))):
        raise ValueError('固定 Recovery 保护方案无效。')
    part = plan['partition']
    if (identity(part) != part or part['name'] != 'recovery_a' or
            part['bytes'] != CAPACITY or
            plan['backup'] != dict(identity=part, sha256=plan['previous_sha256'])):
        raise ValueError('固定 Recovery 缺少精确的 A 槽目标及回退备份。')
    checks = []
    for phase in ('before', 'after'):
        rows = plan[phase]
        if not isinstance(rows, list) or not 4 <= len(rows) <= len(FIRMWARE)+4:
            raise ValueError('固定 Recovery 启动链检查不完整。')
        values = {}
        for row in rows:
            p = row.get('partition', {})
            if (set(row) != {'partition', 'bytes', 'sha256'} or identity(p) != p or
                    p['name'] not in CHAIN | {n+'_a' for n in FIRMWARE} or
                    p['name'] in values or type(row['bytes']) is not int or
                    not 0 < row['bytes'] <= p['bytes'] or
                    not isinstance(row['sha256'], str) or not HEX.fullmatch(row['sha256'])):
                raise ValueError('固定 Recovery 启动链范围或校验值无效。')
            values[p['name']] = row
        if not CHAIN <= values.keys():
            raise ValueError('固定 Recovery 缺少 ABL、DTBO 或 AVB 检查。')
        checks.append(values)
    before, after = checks
    if before.keys() != after.keys() or any(
            before[n]['partition'] != after[n]['partition'] or
            (n not in {'dtbo_a', 'vbmeta_a', 'vbmeta_system_a'} and before[n] != after[n])
            for n in before):
        raise ValueError('此固定 Recovery 不能自动跨越底层固件变更。')
    return plan


def check_chain(hardware, plan, phase):
    from liuqin_native_install import identity
    validate(plan)
    parts = hardware.inventory()
    hardware.sealed(list(parts.values()))
    if identity(parts['recovery_a']) != plan['partition']:
        raise RuntimeError('Recovery 分区身份改变。')
    for row in plan[phase]:
        name = row['partition']['name']
        part = parts.get(name)
        if (part is None or identity(part) != row['partition'] or
                hardware.digest(part, 0, row['bytes']) != row['sha256']):
            raise RuntimeError('固定 Recovery 的启动链与确认方案不同：'+name)
    return parts


def effective_hashes(images, session):
    result = {image['partition']['name']: image['whole_sha256'] for image in images}
    plan = session.get('scope', {}).get('fixed_recovery') if session else None
    if plan and 'recovery_a' in result:
        result['recovery_a'] = validate(plan)['image_sha256']
    return result


def active(optional=False):
    from liuqin_native_install import require,TOKEN
    if optional and not os.environ.get(TOKEN):
        return None,None
    lease = require()
    plan = lease.get('scope', {}).get('fixed_recovery') if lease else None
    return (lease, validate(plan)) if plan else (None, None)


def directory(lease, root=Path('/')):
    return root/'run/liuqin-fixed-recovery'/lease['token']


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while data := source.read(4*1024**2):
            digest.update(data)
    return digest.hexdigest()


def stage(request, hardware, fetch=urllib.request.urlopen, root=Path('/')):
    from liuqin_native_install import require, store
    lease = require(request.get('token', ''), root)
    plan = validate(lease['scope']['fixed_recovery'])
    os.environ['LIUQIN_NATIVE_INSTALL_TOKEN'] = lease['token']
    parts = check_chain(hardware, plan, 'before')
    if hardware.digest(parts['recovery_a']) != plan['previous_sha256']:
        raise RuntimeError('备份后 recovery_a 已改变。')
    url = urllib.parse.urlsplit(request['url'])
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or
            url.password or url.query or url.fragment or
            not re.fullmatch('/[a-f0-9]{32}/boot', url.path)):
        raise ValueError('Recovery 来源不是当前 ADB 环回端点。')
    work = directory(lease, root)
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    image = work/'recovery.img'
    with fetch(request['url'], timeout=60) as source, image.open('xb') as target:
        left = plan['image_bytes']
        while left:
            data = source.read(min(left, 4*1024**2))
            if not data:
                raise RuntimeError('固定 Recovery 传输中断，未开始写盘。')
            target.write(data);left -= len(data)
        if source.read(1):
            raise RuntimeError('固定 Recovery 超出目标容量。')
        target.flush();os.fsync(target.fileno())
    if file_hash(image) != plan['image_sha256']:
        raise RuntimeError('固定 Recovery 传输校验失败，未开始写盘。')
    previous = work/'previous.img'
    with previous.open('xb') as target:
        for offset in range(0, CAPACITY, 4*1024**2):
            data = hardware.read(parts['recovery_a'], offset, min(4*1024**2, CAPACITY-offset))
            if len(data) != min(4*1024**2, CAPACITY-offset):
                raise RuntimeError('Recovery 回退快照被截断。')
            target.write(data)
        target.flush();os.fsync(target.fileno())
    if file_hash(previous) != plan['previous_sha256']:
        raise RuntimeError('Recovery 回退快照与完整备份不同。')
    check_chain(hardware, plan, 'before')
    lease['fixed_recovery_staged'] = True
    store(lease, root)
    return dict(staged=True, sha256=plan['image_sha256'])


def ensure(hardware, root=Path('/')):
    """Keep/restore before ROM writes, so its Recovery is never written at all."""
    from liuqin_native_install import require,store
    lease, plan = active()
    if plan is None:
        return None
    parts = check_chain(hardware, plan, 'before')
    part = parts['recovery_a'];work = directory(lease, root)
    image, previous = work/'recovery.img', work/'previous.img'
    if (not lease.get('fixed_recovery_staged') or not image.is_file() or
            image.stat().st_size != CAPACITY or file_hash(image) != plan['image_sha256'] or
            not previous.is_file() or previous.stat().st_size != CAPACITY or
            file_hash(previous) != plan['previous_sha256']):
        raise RuntimeError('固定 Recovery 或回退材料尚未完整暂存。')
    current = hardware.digest(part)
    if current not in {plan['previous_sha256'], plan['image_sha256']}:
        raise RuntimeError('Recovery 在暂存后出现未知内容，停止写盘。')
    def write(path, expected):
        with hardware.writable(part, {'recovery_a'}) as fd, path.open('rb') as source:
            offset = 0
            while data := source.read(4*1024**2):
                hardware.write(fd, offset, data);offset += len(data)
        hardware.sealed(list(hardware.inventory().values()))
        if hardware.digest(part) != expected:
            raise RuntimeError('固定 Recovery 整分区回读失败。')
    if current != plan['image_sha256']:
        try:
            write(image, plan['image_sha256'])
        except Exception as error:
            try:
                write(previous, plan['previous_sha256'])
            except Exception as rollback:
                raise RuntimeError(f'Recovery 恢复失败，回退也未确认；保持当前 Rec。{error}；{rollback}') from error
            raise RuntimeError('固定 Recovery 恢复失败，原 Recovery 已回读恢复；ROM 尚未写入。') from error
    # writable() durably sets write_started. Never replace it with the lease
    # captured before the block writer opened.
    lease=require(root=root)
    lease['fixed_recovery_applied'] = True
    store(lease, root)
    return plan


def verify(hardware):
    from liuqin_native_install import store
    lease, plan = active()
    if plan is None:
        return None
    parts = check_chain(hardware, plan, 'after')
    if not lease.get('fixed_recovery_applied') or hardware.digest(parts['recovery_a']) != plan['image_sha256']:
        raise RuntimeError('安装后固定 Recovery 尚未确认完整；保持当前 Rec。')
    lease['fixed_recovery_verified'] = True
    store(lease)
    return dict(recovery_preserved=True, recovery_readback_verified=True, recovery_sha256=plan['image_sha256'])
