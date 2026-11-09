# SPDX-License-Identifier: MIT
"""Bounded boot history and resumable, store-only garbage collection.

Only terminal, recognized objects are retired. The registry and its current
images are never rewritten. A durable journal pins every protected file before
atomic directory moves; interrupted deletion resumes from that same journal.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

POLICY = dict(version=1, current_systems='all', previous_linux_kernels=1,
              android_boot_only_rollback=False, partition_audit_records=8,pending_transactions='defer')
ACTIVE = '.retention/active.json'
LAST = '.retention/last.json'
IMAGE = re.compile(r'systems/([0-9a-f-]{36})/images/([0-9a-f-]{36})\.img\Z')


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or str(p) != name or '..' in p.parts:
        raise RuntimeError('清理路径无效。')
    current = root
    for segment in p.parts:
        current = current/segment
        if current.is_symlink():
            raise RuntimeError('清理路径包含链接。')
    return current


def verify(root, files, missing=False):
    for name, entry in files.items():
        path = safe_path(root, name)
        if missing and not path.exists():
            continue
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_size != entry['bytes'] or sha(path) != entry['sha256']):
            raise RuntimeError('清理材料发生变化：'+name)


def build(root, state, rows, read_json, generation, padded):
    """Produce deterministic retention units from a fully inspected store."""
    if (root/ACTIVE).exists():
        active = read_json(root/ACTIVE)
        if active.get('schema') != 1:
            raise RuntimeError('未知引导清理记录。')
        plan = active['plan']
        verify(root, plan['protected'])
        return plan
    if any(r['status'] == 'pending' for r in rows):
        raise RuntimeError('存在未完成事务，保留全部恢复材料。')
    files = state['files']
    registry = state['registry']
    protected = {'registry.json', 'SHA256SUMS'}
    keep_journals = set()
    keep_updates = set()
    kept = []
    # Authenticate all current images, not only the default system.
    for system in registry['systems']:
        image = system['image']
        match = IMAGE.fullmatch(image)
        if not match or match[1] != system['id'] or match[2] != generation(system):
            raise RuntimeError('当前系统引导路径无效。')
        if image not in files or files[image]['sha256'] != system['boot_sha256']:
            raise RuntimeError('当前系统引导校验失败。')
        padded(root/image, system['boot_sha256'])
        protected.add(image)
        manifest = 'systems/'+system['id']+'/manifest.json'
        if manifest in files:
            protected.add(manifest)
        journal = 'transactions/'+system['type']+'-'+generation(system)
        keep_journals.add(journal)
        kept.append(dict(id=system['id'], name=system['name'], image=image, role='current'))
        update = 'kernel-updates/'+generation(system)
        if not (root/update).exists():
            continue
        # A previous kernel is useful only for the same installed filesystem.
        complete = read_json(root/update/'complete.json')
        old_registry = read_json(root/update/'previous-registry.json')
        plan = read_json(root/update/'plan.json')
        previous = next((s for s in old_registry['systems'] if s['id']==system['id']), None)
        fields = ('os_id','root_partuuid','root_fsuuid','root_fstype','root_path')
        if (system['type'] != 'linux' or not previous or previous['type'] != 'linux' or
                any(previous['target'][k] != system['target'][k] for k in fields) or
                complete.get('registry_sha256') != hashlib.sha256(encoded(plan['new_registry'])).hexdigest() or
                plan['new_system'] != system or plan['old_registry'] != old_registry or
                complete.get('boot_sha256') != padded(root/image, system['boot_sha256'])):
            raise RuntimeError('当前内核更新记录不完整，暂不清理。')
        old_image = previous['image']
        if not IMAGE.fullmatch(old_image) or old_image not in files:
            raise RuntimeError('缺少最近内核的回退镜像。')
        old_digest = padded(root/old_image, previous['boot_sha256'])
        if old_digest != plan['previous_boot_sha256']:
            raise RuntimeError('最近内核回退校验失败。')
        from liuqin_boot_target import encode
        if (root/update/'previous-root-target').read_bytes() != encode(previous['target']):
            raise RuntimeError('最近内核回退的根文件系统绑定无效。')
        protected.add(old_image)
        keep_updates.add(update)
        # A previous receipt remains usable after restoring its old registry.
        old_journal = 'transactions/linux-'+generation(previous)
        for suffix in ('root-installed.json','complete.json'):
            name = old_journal+'/'+suffix
            if name in files:
                protected.add(name)
        kept.append(dict(id=system['id'],name=system['name'],image=old_image,role='previous-kernel'))
    for name in files:
        if any(name.startswith(p+'/') for p in keep_updates):
            protected.add(name)
        if any(name.startswith(p+'/') for p in keep_journals):
            # Completed installation backups aren't an Android ROM rollback.
            if not name.endswith(('/previous-boot.img','/previous-boot-reference.json')):
                protected.add(name)
    units = []
    selected = set()

    def unit(name, directory=False):
        names = {n for n in files if n.startswith(name+'/')} if directory else {name}
        if not names or names & protected:
            return
        selected.update(names)
        units.append(dict(path=name, directory=directory,
                          files={n:files[n] for n in sorted(names)},
                          allocated_bytes=sum((root/n).stat().st_blocks*512 for n in names)))

    for row in rows:
        prefix = 'transactions/'+row['name']
        if row['status'] not in ('complete','rolled-back','superseded'):
            continue
        if not any(n.startswith(prefix+'/') for n in protected):
            unit(prefix, True)
    audits = []
    for row in rows:
        if row['kind'] not in ('edit','partition'):
            continue
        directory = root/'transactions'/row['name']
        receipt = directory/'complete.json'
        if receipt.is_file() and read_json(receipt).get('status')=='complete':
            audits.append((receipt.stat().st_mtime_ns,row['name']))
    for _, name in sorted(audits,reverse=True)[POLICY['partition_audit_records']:]:
        unit('transactions/'+name,True)
    for directory in sorted((root/'kernel-updates').glob('*')):
        prefix = directory.relative_to(root).as_posix()
        if prefix in keep_updates:
            continue
        # Unknown or unfinished kernel updates pin the entire store.
        complete = read_json(directory/'complete.json')
        if not re.fullmatch('[a-f0-9]{64}',complete.get('registry_sha256','')):
            raise RuntimeError('存在未完成的内核更新，暂不清理。')
        unit(prefix, True)
    for name in sorted(files):
        if name in selected or name in protected:
            continue
        if IMAGE.fullmatch(name):
            unit(name)
        elif name.startswith('transactions/') and name.endswith(('/previous-boot.img','/previous-boot-reference.json')):
            if any(name.startswith('transactions/'+r['name']+'/') and r['status'] in ('complete','rolled-back') for r in rows):
                unit(name)
        elif name.endswith('/upgrade.json') and name.startswith('transactions/linux-'):
            source = read_json(root/name).get('source')
            if source and source not in keep_updates:
                unit(name)
        elif re.fullmatch(r'systems/[0-9a-f-]{36}/manifest.json',name):
            sid = name.split('/')[1]
            if sid not in {s['id'] for s in registry['systems']}:
                unit(name)
    preserved = {n:v for n,v in files.items() if n not in selected and n not in (ACTIVE,LAST,'SHA256SUMS')}
    old_sums = (root/'SHA256SUMS').read_bytes()
    sums = b''.join(line for line in old_sums.splitlines(keepends=True)
                    if not any(line.rstrip(b'\r\n').endswith(b'  '+n.encode()) for n in selected))
    return dict(schema=1, policy=POLICY, registry_sha256=files['registry.json']['sha256'],
                kept=kept, protected=preserved, units=units, sums=sums.decode('utf-8'),
                reclaimable_bytes=max(0,sum(u['allocated_bytes'] for u in units)-64*1024))


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply(root, plan, atomic, read_json):
    """Caller holds the operation lock and scoped linux_boot write lease."""
    verify(root, plan['protected'])
    if sha(root/'registry.json') != plan['registry_sha256']:
        raise RuntimeError('清理期间系统登记已变化。')
    journal = root/ACTIVE
    if journal.exists():
        if read_json(journal) != dict(schema=1,plan=plan):
            raise RuntimeError('清理记录与确认方案不一致。')
    else:
        for unit in plan['units']:
            verify(root,unit['files'])
        journal.parent.mkdir(mode=0o700,exist_ok=True)
        atomic(journal,encoded(dict(schema=1,plan=plan)))
        fsync_dir(root)
    trash = root/'.retention/trash'
    trash.mkdir(mode=0o700,exist_ok=True)
    for index,unit in enumerate(plan['units']):
        source = safe_path(root,unit['path'])
        target = trash/str(index)
        if source.exists():
            if target.exists():
                raise RuntimeError('清理暂存出现冲突。')
            verify(root,unit['files'])
            os.rename(source,target)
            fsync_dir(source.parent);fsync_dir(trash)
        if target.exists():
            moved = {str(target.relative_to(root))+(n[len(unit['path']):] if unit['directory'] else ''):v
                     for n,v in unit['files'].items()}
            verify(root,moved,missing=True)
            for name in moved:
                safe_path(root,name).unlink(missing_ok=True)
            if unit['directory']:
                for path in sorted(target.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                    path.rmdir()
                target.rmdir()
            fsync_dir(trash)
    atomic(root/'SHA256SUMS',plan['sums'].encode('utf-8'))
    verify(root,plan['protected'])
    result = dict(completed=True,policy=POLICY,kept=plan['kept'],
                  retired_files=sum(len(u['files']) for u in plan['units']),
                  reclaimed_bytes=plan['reclaimable_bytes'],boot_written=False)
    atomic(root/LAST,encoded(result))
    journal.unlink();fsync_dir(journal.parent)
    # Remove empty image/system directories without touching unknown content.
    for base in ('systems','transactions','kernel-updates'):
        for path in sorted((root/base).glob('**/*'),key=lambda p:len(p.parts),reverse=True):
            if path.is_dir() and not path.is_symlink():
                try:path.rmdir()
                except OSError:pass
    return result
