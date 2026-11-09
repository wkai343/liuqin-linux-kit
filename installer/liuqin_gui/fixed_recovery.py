# SPDX-License-Identifier: MIT
"""Plan preservation/restoration of a locally qualified fixed Recovery."""
import hashlib
import json
from pathlib import Path

from liuqin_firmware import validate_snapshot,validate_match
from liuqin_native_install import FIXED_USAGE, identity
from liuqin_recovery_preserve import validate
from liuqin_universal_recovery import assess, inspect
from .engine import InstallerError


def candidates(engine):
    from .native_profiles import image_paths
    found = []
    for filename in image_paths(str(getattr(engine,'runtime_path',None) or getattr(engine,'installer_snapshot',''))):
        path = Path(filename)
        qualification = path.parent/'hardware-qualification.json'
        if qualification.is_symlink() or not qualification.is_file() or qualification.stat().st_size > 1024**2:
            continue
        owned=False
        try:
            qualification_raw=qualification.read_bytes()
            q = json.loads(qualification_raw)
            if not isinstance(q,dict) or q.get('serial') != engine.device['serial']:
                continue
            owned=True
            if (q.get('schema') != 1 or q.get('device') != 'liuqin' or q.get('slot') != 'a' or
                    q.get('partition') != 'recovery_a' or
                    any(q.get(k) is not True for k in ('hardware_fixed_boot_verified',
                        'hardware_cold_boot_verified', 'readback_verified', 'storage_sealed_verified'))):
                raise InstallerError('本机固定 Rec 的验收记录不完整，不能自动覆盖 Recovery；请补齐安装包或选择按 ROM 原样处理。')
            firmware=q.get('firmware',{})
            if not isinstance(firmware,dict):raise ValueError('invalid qualification firmware')
            validate_match(firmware.get('match'))
            checked = inspect(path)
            if (checked['manifest']['usage'] != FIXED_USAGE or
                    checked['manifest'].get('fixed_path_external_dtbo_required') is not False or
                    checked['image_sha256'] != q.get('image_sha256') or
                    checked['manifest']['build_id'] != q.get('build_id')):
                raise InstallerError('本机固定 Rec 镜像与验收记录不匹配，不能自动保护；请补齐正确安装包。')
            compatibility_path=path.parent/'restore-compatibility.json'
            if (compatibility_path.is_symlink() or not compatibility_path.is_file() or
                    compatibility_path.stat().st_size>1024**2):
                raise InstallerError('本机固定 Rec 缺少恢复适配记录，请使用附带完整适配资料的新版安装包。')
            compatibility_raw=compatibility_path.read_bytes()
            compatibility=json.loads(compatibility_raw)
            if not isinstance(compatibility,dict):raise ValueError('invalid restore compatibility')
            from liuqin_ota import CRITICAL
            dependencies=compatibility.get('firmware_dependencies',{})
            if (compatibility.get('schema')!=1 or compatibility.get('serial')!=q['serial'] or
                    compatibility.get('device')!='liuqin' or compatibility.get('image_sha256')!=q['image_sha256'] or
                    compatibility.get('build_id')!=q['build_id'] or
                    not isinstance(dependencies,dict) or set(dependencies)!={n+'_a' for n in CRITICAL}):
                raise InstallerError('固定 Rec 的恢复适配记录未绑定本机镜像及完整底层固件。')
            found.append(dict(path=str(path.resolve()), qualification_path=str(qualification.resolve()),
                qualification_sha256=hashlib.sha256(qualification_raw).hexdigest(), qualification=q, checked=checked,
                compatibility_path=str(compatibility_path.resolve()),compatibility_sha256=hashlib.sha256(compatibility_raw).hexdigest(),
                dependencies=dependencies))
        except (OSError, ValueError, KeyError, TypeError):
            if owned:
                raise InstallerError('本机固定 Rec 的镜像或适配资料损坏，不能自动保护；请补齐正确安装包。')
            continue
    unique = {r['checked']['image_sha256']:r for r in found}
    return list(unique.values())


def prepare(engine, profile, source, state, images, backups, records=None):
    """Return the bound device plan and its independently validated material.

    Reusing an embedded-DTBO image needs no private signing key. A ROM with new
    metadata can reuse it only with verification already disabled by that ROM,
    the same qualified ABL, and byte-identical bundled lower firmware.
    """
    records = candidates(engine) if records is None else records
    if not records:
        running = engine.manifest.get('native_recovery_manifest', {})
        if running.get('usage') == FIXED_USAGE:
            raise InstallerError('当前固定 Rec 缺少本机冷启动验收镜像或记录，不能自动保护。请补齐对应安装包，或明确选择使用 ROM 自带 Recovery。')
        return None
    if profile.get('generic_ota_version') != 2:
        raise InstallerError('此 ROM 的旧安装后端未接入固定 Rec 保护，请使用完整 OTA，或选择使用 ROM 自带 Recovery。')
    if engine.manifest.get('native_recovery_manifest', {}).get('fixed_recovery_restore_version') != 1:
        raise InstallerError('当前 Rec 版本不支持自动保护固定 Rec。请从 Fastboot 临时启动新版配套安装环境后继续；现有固定 Rec 保留。也可明确选择使用 ROM 自带 Recovery。')
    from .native_ota import project_firmware
    from liuqin_ota import FIRMWARE,CRITICAL
    snapshot = validate_snapshot(engine.receipt['firmware_fingerprint'], serial=engine.device['serial'],
        boot_id=engine.transport.boot_id)
    after, _ = project_firmware(profile, Path(next(iter(source.values()))).parent, snapshot)
    before_slot, after_slot = snapshot['slots']['a'], after['slots']['a']
    inventory = state['inventory']
    if 'recovery_a' not in backups:
        raise InstallerError('固定 Rec 保护缺少当前 recovery_a 的完整备份。')
    by_name = {p['partition']['name']:p for p in images}
    firmware_names = {n+'_a' for n in FIRMWARE}
    changed = [n for n, image in by_name.items() if n in firmware_names and
        (n not in backups or backups[n]['sha256'] != image['whole_sha256'])]
    reasons = []
    # Prefer keeping the exact currently installed image over a different one.
    records = sorted(records, key=lambda r:r['checked']['image_sha256'] != backups['recovery_a']['sha256'])
    for record in records:
        checked = record['checked']; manifest = checked['manifest']; policy = manifest['universal_policy']
        baseline = record['qualification'].get('firmware', {})
        current = before_slot.get('match', {})
        if baseline.get('match', {}).get('abl') != current.get('abl'):
            reasons.append('当前 ABL 与本机固定 Rec 的冷启动验收基线不同。');continue
        if changed:
            reasons.append('ROM 将改变固定 Rec 尚未验收的底层固件：'+'、'.join(changed));continue
        dependencies=record['dependencies']
        mismatch=[]
        if set(dependencies)!={n+'_a' for n in CRITICAL}:
            raise InstallerError('固定 Rec 缺少冷启动基线的底层固件校验记录。')
        from .native_ota import _partition_digest
        for name, dependency in dependencies.items():
            p=inventory.get(name)
            if (not isinstance(dependency,dict) or set(dependency)!={'bytes','sha256'} or
                    p is None or dependency['bytes']!=p['bytes']):
                mismatch.append(name);continue
            actual=backups.get(name,{}).get('sha256') or _partition_digest(engine,p)
            if actual!=dependency['sha256']:mismatch.append(name)
        if mismatch:
            reasons.append('底层固件与固定 Rec 冷启动验收基线不同：'+'、'.join(mismatch));continue
        initial = assess(snapshot, dict(policy=policy, artifact={}))
        if (not initial['requirements_satisfied'] and not (
                initial['status']=='unknown-metadata' and before_slot.get('avb',{}).get('flags',0)&2 and
                manifest.get('fixed_path_external_dtbo_required') is False and
                baseline.get('match',{}).get('layout')==current.get('layout'))):
            reasons.append('当前启动链不符合此固定 Rec 的验收条件。');continue
        projected = assess(after, dict(policy=policy, artifact={}))
        adaptation = 'reuse'
        if not projected['requirements_satisfied']:
            # Disabled verification bypasses the ROM-specific AVB chain. The
            # known ABL, built-in kernel and DTBO still have to remain exact.
            if (projected['status'] != 'unknown-metadata' or
                    after_slot.get('avb', {}).get('flags', 0) & 2 == 0 or
                    current.get('abl') != after_slot.get('match', {}).get('abl') or
                    manifest.get('fixed_path_external_dtbo_required') is not False or
                    after_slot.get('match', {}).get('layout') != current.get('layout')):
                reasons.append(projected['reason']);continue
            adaptation = 'rom-metadata'
        def checks(slot):
            result = []
            for role in ('abl','dtbo','vbmeta','vbmeta_system'):
                measurement = slot['match'][role]
                result.append(dict(partition=identity(inventory[role+'_a']), bytes=measurement['hash_bytes'], sha256=measurement['sha256']))
            for n in sorted((set(by_name)&firmware_names | set(dependencies)) - {'abl_a'}):
                digest=backups[n]['sha256'] if n in backups else dependencies[n]['sha256']
                result.append(dict(partition=identity(inventory[n]), bytes=inventory[n]['bytes'], sha256=digest))
            return result
        previous = backups['recovery_a']['sha256']
        plan = validate(dict(schema=1, image_sha256=checked['image_sha256'], image_bytes=checked['image_bytes'],
            build_id=manifest['build_id'], partition=identity(inventory['recovery_a']), previous_sha256=previous,
            backup=dict(identity=identity(inventory['recovery_a']), sha256=previous),
            before=checks(before_slot), after=checks(after_slot), adaptation=adaptation))
        return dict(plan=plan, path=record['path'], qualification_path=record['qualification_path'],
            qualification_sha256=record['qualification_sha256'], action='保留' if previous==plan['image_sha256'] else '恢复',
            compatibility_path=record['compatibility_path'],compatibility_sha256=record['compatibility_sha256'],
            message=('保留已验收的固定 Rec，跳过 ROM 自带 Recovery。' if previous==plan['image_sha256'] else
                '备份当前 Recovery 后恢复本机已验收固定 Rec，跳过 ROM 自带 Recovery。')+
                (' 新 ROM 的 AVB 元数据已重新核对，继续使用自带内核和 DTBO。' if adaptation=='rom-metadata' else ''),
            hardware_boot_after_rom_verified=False)
    raise InstallerError('无法证明安装后固定 Rec 可启动：'+'；'.join(dict.fromkeys(reasons))+
        ' 可改选“使用 ROM 自带 Recovery”继续普通刷机；安装器不会自动修改 ABL 或 AVB。')
