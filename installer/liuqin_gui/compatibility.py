# SPDX-License-Identifier: MIT
"""Read-only OS discovery and exact, locally curated Recovery qualification.

OS names are presentation only. A qualified profile matches the vendor build
and running kernel, and pins the complete paired image. No version ranges,
downloaded executable probes, or persistent device-compatibility cache.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from . import host, runtime
from .engine import InstallerError

PROJECT = Path(__file__).resolve().parents[2]
CATALOG = PROJECT / 'shared/liuqin-recovery-profiles.json'
PROPERTIES = {
    'device': 'ro.product.device', 'vendor_device': 'ro.product.vendor.device',
    'android': 'ro.build.version.release', 'sdk': 'ro.build.version.sdk',
    'build': 'ro.build.version.incremental', 'fingerprint': 'ro.build.fingerprint',
    'vendor_fingerprint': 'ro.vendor.build.fingerprint',
    'bootimage_fingerprint': 'ro.bootimage.build.fingerprint',
    'miui': 'ro.miui.ui.version.name', 'hyperos': 'ro.mi.os.version.name',
    'lineage': 'ro.lineage.version', 'miku': 'ro.miku.version',
    'bootmode': 'ro.bootmode', 'slot': 'ro.boot.slot_suffix',
    'twrp': 'ro.twrp.version',
}
# All shell text is constant. Device-provided strings are never executed.
PROBE = '\n'.join([
    'if command -v getprop >/dev/null 2>&1; then',
    *[f"printf '{key}='; getprop {prop}" for key, prop in PROPERTIES.items()],
    'fi',
    "printf 'kernel='; uname -r",
    "printf 'boot_id='; cat /proc/sys/kernel/random/boot_id",
    "printf 'paired_build='; if test -f /system/etc/liuqin-build-id; then cat /system/etc/liuqin-build-id; else echo; fi",
    "if ! command -v getprop >/dev/null 2>&1; then",
    "printf '\nOS_RELEASE_BEGIN\n'; cat /etc/os-release 2>/dev/null; printf '\nOS_RELEASE_END\n'",
    'fi',
])


def parse_probe(raw, serial):
    if isinstance(raw, bytes): raw = raw.decode('utf-8', errors='replace')
    if len(raw) > 65536: raise InstallerError('系统识别返回的数据过大。')
    values = {}
    allowed = set(PROPERTIES) | {'kernel', 'boot_id', 'paired_build'}
    for line in raw.split('OS_RELEASE_BEGIN', 1)[0].splitlines():
        key, sep, value = line.partition('=')
        if sep and key in allowed:
            if key in values or len(value) > 1024: raise InstallerError('系统识别返回的信息格式不正确。')
            values[key] = re.sub(r'[\x00-\x1f\x7f]', '', value)
    paired = values.get('paired_build', '')
    if paired and not re.fullmatch('[a-f0-9]{64}', paired):
        raise InstallerError('配套 Recovery 的版本标识无效。')
    if paired or values.get('twrp') or values.get('bootmode') in ('recovery', 'sideload'):
        kind = 'recovery'; label = '配套 Recovery' if paired else '其他 Recovery'
    elif values.get('android'):
        kind = 'android'
        label = ('HyperOS' if values.get('hyperos') else 'LineageOS' if values.get('lineage')
                 else 'MikuUI' if values.get('miku') or 'miku' in values.get('fingerprint', '').lower()
                 else 'MIUI' if values.get('miui') else 'Android / 第三方 ROM')
        label += ' · Android ' + values['android']
        if values.get('build'): label += ' · ' + values['build']
    elif 'OS_RELEASE_BEGIN' in raw:
        kind = 'linux'; label = 'Linux'
        for line in raw.split('OS_RELEASE_BEGIN', 1)[1].split('OS_RELEASE_END', 1)[0].splitlines():
            if line.startswith('PRETTY_NAME='):
                label = line.partition('=')[2].strip('"\'')[:200]; break
    else:
        kind = 'unknown'; label = '未识别的 ADB 环境'
    return dict(serial=serial, kind=kind, label=label, properties=values, transport='adb')


def adb_state(serial):
    result = host.run_process([host.adb_path(), 'devices'], capture_output=True, text=True, timeout=10, check=True)
    return next((f[1] for line in result.stdout.splitlines() if len(f := line.split()) >= 2 and f[0] == serial), '')


def read_adb(serial):
    result = host.run_process([host.adb_path(), '-s', serial, 'shell', '-T', PROBE],
                              capture_output=True, timeout=15)
    if result.returncode:
        raise InstallerError('无法只读识别当前系统：' + result.stderr.decode(errors='replace')[-500:])
    return parse_probe(result.stdout, serial)


def inspect_device(serial,native=False):
    state = adb_state(serial)
    if state in ('device', 'recovery'):
        from .native_adb import probe_adb
        try:
            if not native:raise InstallerError('native detection not requested')
            value=probe_adb(serial)
            return dict(serial=serial,kind='native-recovery',label='原生 ADB Recovery（安装候选）' if value.get('installer_version')==1 else '原生 ADB Recovery（只读）',properties=value,transport='adb-native')
        except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError):pass
        return read_adb(serial)
    if state:
        labels = {'unauthorized': '请在平板上允许 USB 调试', 'offline': 'ADB 尚未就绪', 'sideload': 'Recovery 的 Sideload 模式'}
        return dict(serial=serial, kind=state, label=labels.get(state, state), properties={}, transport='adb')
    result = host.run_process([host.fastboot_path(), 'devices'], capture_output=True, text=True, timeout=10, check=True)
    if any(line.split()[:2] == [serial, 'fastboot'] for line in result.stdout.splitlines()):
        return dict(serial=serial, kind='fastboot', label='Fastboot（当前系统版本未知）', properties={}, transport='fastboot')
    return dict(serial=serial, kind='disconnected', label='设备已断开', properties={}, transport='')


def profiles(path=CATALOG):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not isinstance(data.get('profiles'), list):
        raise InstallerError('Recovery 适配目录格式不正确。')
    ids = set()
    for p in data['profiles']:
        match = p.get('match', {})
        if (not isinstance(p.get('id'), str) or p['id'] in ids or
            not re.fullmatch(r'[a-f0-9]{64}', p.get('image_sha256', '')) or
            not re.fullmatch(r'[a-f0-9]{64}', p.get('build_id', '')) or
            p.get('device') != 'liuqin' or p.get('validation') != 'TEMPORARY_BOOT_VERIFIED' or
            not isinstance(p.get('evidence'), str) or not p['evidence'].strip() or
            not isinstance(match, dict) or not {'vendor_fingerprint', 'kernel'} <= set(match) or
            not set(match) <= set(PROPERTIES) | {'kernel'} or
            any(not isinstance(v, str) or not v or len(v) > 1024 for v in match.values()) or
            not isinstance(p.get('android_profile_sha256', []), list) or
            any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in p.get('android_profile_sha256', []))):
            raise InstallerError('Recovery 适配项缺少精确固件信息、镜像身份或临时启动验收记录。')
        ids.add(p['id'])
    return data['profiles']


def matching_profile(environment, manifest, digest, catalog=None):
    props = environment.get('properties', {})
    if environment.get('kind') != 'android' or 'liuqin' not in (props.get('device'), props.get('vendor_device')):
        return None
    matches = [p for p in (profiles() if catalog is None else catalog)
               if p['image_sha256'] == digest and p['build_id'] == manifest['build_id']
               and all(props.get(k) == v for k, v in p['match'].items())]
    if len(matches) > 1: raise InstallerError('多个 Recovery 适配项同时匹配，停止自动选择。')
    return matches[0] if matches else None


def assess(environment, manifest=None, digest='', catalog=None):
    props = environment.get('properties', {}); kind = environment['kind']
    result = dict(ready=False, profile_id='', reason='')
    if kind == 'recovery' and props.get('paired_build'):
        if manifest and props['paired_build'] == manifest['build_id']:
            return dict(ready=True, profile_id='running-paired', reason='已运行对应的配套 Recovery；读取时还会核验设备、会话与工具版本。')
        result['reason'] = '当前配套 Recovery 与选定镜像版本不一致，请选择相同版本的配套镜像。'
    elif kind == 'android':
        profile = matching_profile(environment, manifest, digest, catalog) if manifest else None
        if profile:
            return dict(ready=True, profile_id=profile['id'], reason='底层固件与已验收的临时 Recovery 匹配；点击读取分区后进入安装环境。')
        result['reason'] = '已识别系统，但没有与当前底层固件及所选镜像匹配的 Recovery 验收记录。保留当前系统，不自动重启。'
    elif kind == 'fastboot':
        result['reason'] = 'Fastboot 不能可靠报告完整底层固件。请先在 Android 中开启 USB 调试并识别系统，或连接已运行的配套 Recovery。'
    elif kind == 'linux':
        result['reason'] = '已识别 Linux；发行版版本不能说明 Android 固件基线。请连接已运行的配套 Recovery。'
    elif kind == 'recovery':
        result['reason'] = '当前为普通/原厂 Recovery，未提供配套安装服务；不会把它当作 Android 或自动重启。'
    elif kind == 'unauthorized': result['reason'] = '请解锁屏幕并允许这台电脑进行 USB 调试，再刷新识别。'
    else: result['reason'] = '当前连接不能建立安装会话，请检查 USB 连接和设备模式。'
    return result


def image_info(path):
    from liuqin_recovery_image import inspect_paired
    path = Path(path)
    if not path.is_file() or path.is_symlink(): raise InstallerError('配套 Recovery 文件不存在或为链接。')
    manifest = inspect_paired(path)
    if manifest is None: raise InstallerError('镜像没有配套 Recovery 安装服务。')
    return manifest, hashlib.sha256(path.read_bytes()).hexdigest()


def image_candidates(selected):
    paths = [Path(selected)] if selected else []
    for directory in (PROJECT/'installer/runtime/recoveries', PROJECT/'runtime/recoveries', PROJECT/'out/releases/recoveries'):
        if directory.is_dir(): paths += sorted(directory.glob('*/recovery.img'))[:32]
    return list(dict.fromkeys(str(p.resolve()) for p in paths if p.is_file() and not p.is_symlink()))


def select_image(environment, selected):
    if environment.get('kind')=='native-recovery' and environment.get('firmware'):
        from .native_profiles import selection
        from liuqin_firmware import validate_snapshot
        props=environment.get('properties',{})
        if not all(props.get(k) for k in ('boot_id','build_id')):raise InstallerError('缺少当前 Recovery 会话身份。')
        validate_snapshot(environment['firmware'],environment.get('serial'),props['boot_id'],props['build_id'])
        return selection(environment['firmware'],selected)
    catalog = profiles(); matching = []
    for path in image_candidates(selected):
        if runtime.backend_hint(path)!='adb':continue
        manifest, digest = image_info(path)
        result = assess(environment, manifest, digest, catalog)
        if result['ready']: matching.append((path, result))
    # An explicit, compatible choice takes precedence; otherwise require a
    # unique qualified candidate. Never guess between firmware variants.
    if selected:
        preferred = next((m for m in matching if m[0] == str(Path(selected).resolve())), None)
        if preferred: return preferred
    if len(matching) == 1: return matching[0]
    if len(matching) > 1: raise InstallerError('发现多个匹配的配套镜像，请启动安装器时用 --recovery-image 指定。')
    manifest, digest = image_info(selected) if selected and runtime.backend_hint(selected) == 'adb' else (None, '')
    if selected and runtime.backend_hint(selected) in ('network','adb-native'):
        from liuqin_artifacts import boot_interface
        info=boot_interface(Path(selected),installer=True)
        if 'native_recovery_manifest' in info:
            if info['native_recovery_manifest'].get('usb_transport')!='adb':
                return selected,dict(ready=False,profile_id='retired-network-runtime',reason='旧 USB 网络 Rec 已停用；请手动回到 Fastboot 后选择 ADB Rec。')
            props=environment.get('properties',{})
            ready=environment['kind']=='fastboot' or (environment['kind']=='android' and 'liuqin' in (props.get('device'),props.get('vendor_device')))
            if environment['kind']=='native-recovery':
                ready=props.get('build_id')==info['native_recovery_manifest']['build_id'] and props.get('sealed') is True
            from liuqin_native_install import capable
            install=capable(info['native_recovery_manifest'])
            if info['native_recovery_manifest'].get('fixed_installer_version')==1:
                ready=environment['kind']=='native-recovery' and props.get('build_id')==info['native_recovery_manifest']['build_id'] and props.get('sealed') is True
                return selected,dict(ready=ready,profile_id='fixed-recovery-installer',inspection_only=False,native=True,usb_transport='adb',automatic_boot_allowed=False,auto_selected=False,fixed_install_allowed=False,
                    reason='固定安装版已连接，可读取分区并准备安装方案。' if ready else '请先在平板启动对应的固定 Recovery。')
            return selected,dict(ready=ready,profile_id='native-recovery-prototype',inspection_only=True,native=True,usb_transport='adb',automatic_boot_allowed=False,auto_selected=False,fixed_install_allowed=False,
                firmware=dict(status='unavailable',reason='进入原生 Recovery 后才能读取底层固件指纹。'),
                reason=('已选择支持安装的 Rec 候选，尚未真机验收。点击读取分区仅临时启动并检查；备份和确认完整方案前保持只读。' if install else '固件指纹尚未读取。当前选择用于显式临时启动检查；进入原生 Recovery 后自动读取指纹并匹配适配表，仅开放读取。'))
        return selected, dict(ready=False,profile_id='retired-network-runtime',reason='旧 USB 网络安装环境已停用，请选择 ADB Recovery。')
    return selected, assess(environment, manifest, digest, catalog)


def inspect(engine, payload):
    serial = engine.require_serial(payload)
    failed_write=engine.state=='failed' and engine.receipt.get('write_dispatched')
    if failed_write:
        if engine.receipt.get('serial')!=serial:raise InstallerError('请先处理上次写入设备的执行记录。')
    else:engine.guard_operation()
    if engine.simulation:
        environment = dict(serial=serial, kind='android', label='演示 MIUI · Android 13', transport='simulation', properties={})
        path = str(engine.demo_bundle/'installer.img')
        status = dict(ready=True, profile_id='simulation', reason='演示环境，不连接平板。')
    else:
        acquired=engine.device_lock is None
        if acquired:engine.lock_device(serial)
        try:
            selected=payload.get('runtime_path','')
            engine.native_discovery=runtime.native_available()
            environment = inspect_device(serial,native=engine.native_discovery)
            witness=getattr(engine,'_running_recovery',{})
            if environment['kind']!='fastboot' and environment.get('properties',{}).get('paired_build')!=witness.get('build_id'):
                engine._running_recovery={}
            if environment['kind']=='native-recovery':
                from .native_profiles import inspect_running
                environment,path,status=inspect_running(engine,environment,selected)
            else:
                path, status = select_image(environment, selected)
        finally:
            if acquired:engine.release_device()
    engine.emit('environment', environment=environment, compatibility=status, runtime_path=path)


def remember_running(transport):
    transport.engine._running_recovery = dict(serial=transport.serial,
        digest=transport.engine.manifest['files']['recovery.img'],
        build_id=transport.engine.manifest['recovery_manifest']['build_id'], at=time.monotonic())


def has_running_witness(transport):
    witness = getattr(transport.engine, '_running_recovery', {})
    return (witness.get('serial') == transport.serial and
            witness.get('digest') == transport.engine.manifest['files']['recovery.img'] and
            witness.get('build_id') == transport.engine.manifest['recovery_manifest']['build_id'] and
            0 <= time.monotonic() - witness.get('at', -100000) < 900)
