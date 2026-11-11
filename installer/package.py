#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export the desktop installer without build sources, tools, images or device data."""
import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import re
import sys
from urllib.parse import quote, unquote, urlsplit
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'shared'))
from liuqin_identity import PROJECT_NAME, INSTALLER_ARCHIVE_ROOT, UPSTREAM_PROJECT, PROJECT_URL


def kernel_build_records(entries, components):
    """Export the sources belonging to this image, including shared patches."""
    prefix='usr/share/licenses/liuqin-recovery-kernel/'
    names={'kernel.config','aligned-entry.S','kernel-manifest.json'}
    if components.get('schema') == 2:
        names.add('source-lock.json')
        names.update(name for name in components['files']
                     if name.endswith('.patch') or name == 'shared-source-lock.json')
    else:
        names.add('kernel-log-fixes.patch')
    result={}
    for name in sorted(names):
        if Path(name).name != name or prefix+name not in entries:
            raise RuntimeError('Recovery 内核构建记录缺失或格式无效。')
        fields,body=entries[prefix+name]
        if fields[1]&0o170000!=0o100000 or not 0<len(body)<16*1024**2:
            raise RuntimeError('Recovery 内核构建记录缺失或格式无效。')
        expected=components.get('files',{}).get(name)
        if expected and hashlib.sha256(body).hexdigest()!=expected:
            raise RuntimeError('Recovery 内核构建记录摘要不匹配。')
        if name=='kernel-manifest.json' and json.loads(body)!=components:
            raise RuntimeError('Recovery 内核组件清单不匹配。')
        result['installer/runtime/kernel-build/'+name]=body
    return result


def export_markdown(name, body, available):
    """Keep bundled links local; point full-project sources at the repository."""
    def link(match):
        target=match.group(1)
        url=urlsplit(target)
        if url.scheme or url.netloc or not url.path:return match.group(0)
        relative=posixpath.normpath(posixpath.join(posixpath.dirname(name),unquote(url.path)))
        if relative in available or relative=='README.md' or relative.startswith('../'):return match.group(0)
        remote=PROJECT_URL+'/blob/main/'+quote(relative,safe='/')
        if url.fragment:remote+='#'+quote(url.fragment,safe='-')
        return match.group(0).replace(target,remote,1)
    return ''.join(part if part.startswith('```') else re.sub(r'\[[^\]]*\]\(([^)\n]+)\)',link,part)
                   for part in re.split(r'(```[^\n]*\n[\s\S]*?```)',body))


def package(output, runtime_image=None, recovery_image=None, recovery_variants=(),native_recovery=None,native_variants=(),universal_candidate=None,native_installer=None,adbd_sources=None,fixed_installer=None):
    output = output.resolve()
    if output.exists():
        raise RuntimeError('输出已存在，未覆盖：' + str(output))
    sources = {}
    embedded = {}
    paired=False
    native_connection='USB ADB';contains_adbd=False;default_installer=False
    if native_installer is not None and native_recovery is None:raise RuntimeError('安装候选必须与默认只读原生 Rec 分开打包。')
    if universal_candidate is not None and native_recovery is None:raise RuntimeError('通用候选只作为原生维护包的独立附加产物。')
    if native_variants and native_recovery is None:raise RuntimeError('额外原生镜像需要原生 Recovery 主包。')
    if fixed_installer is not None and native_recovery is None:raise RuntimeError('固定安装版需要同时打包默认临时安装环境。')
    if native_recovery is not None:
        if runtime_image is not None or recovery_image is not None or recovery_variants:
            raise RuntimeError('原生 Recovery 测试包与其他默认安装环境分开打包。')
        from liuqin_artifacts import boot_interface
        native_recovery=Path(native_recovery).resolve()
        native_info=boot_interface(native_recovery,installer=True)
        if 'native_recovery_manifest' not in native_info:
            raise RuntimeError('这不是带完整身份的原生 Recovery 镜像。')
        if native_info['native_recovery_manifest'].get('usage')!='temporary-boot-only':raise RuntimeError('默认启动镜像需要临时版；固定安装版请使用 --fixed-installer。')
        if native_info['native_recovery_manifest'].get('inspection_only') is not True:
            from liuqin_native_install import capable
            if not capable(native_info['native_recovery_manifest']) or native_info['native_recovery_manifest'].get('generic_ota_version',0)<2:raise RuntimeError('默认安装环境需要完整 ROM v2 的受控执行能力。')
            default_installer=True
        contains_adbd=native_info['native_recovery_manifest'].get('native_adb_version')==1
        if not contains_adbd:raise RuntimeError('原生安装器不再打包 USB 网络 Rec，请提供 ADB 镜像。')
        sources['installer/runtime/native-recovery.img']=native_recovery
        sources['installer/runtime/NOTICE.md']=PROJECT/'build/recovery/NATIVE-NOTICE.md'
        sources['docs/NATIVE-RECOVERY.md']=PROJECT/'docs/NATIVE-RECOVERY.md'
        manifest=native_recovery.parent/'native-recovery-manifest.json'
        if manifest.is_file():sources['installer/runtime/native-recovery-manifest.json']=manifest
        validation=native_recovery.parent/'hardware-boot-validation.json'
        if validation.is_file():
            import hashlib,json
            checked=json.loads(validation.read_text())
            if (checked.get('schema')!=1 or checked.get('device')!='liuqin' or
                    checked.get('image_sha256')!=hashlib.sha256(native_recovery.read_bytes()).hexdigest() or
                    checked.get('build_id')!=native_info['native_recovery_manifest']['build_id']):
                raise RuntimeError('临时 Recovery 的硬件验收记录与镜像不一致。')
            sources['installer/runtime/hardware-boot-validation.json']=validation
        if native_info['native_recovery_manifest'].get('ui_version')==1:
            import struct
            from liuqin_artifacts import bounded_initramfs
            from liuqin_recovery_image import records
            data=native_recovery.read_bytes();kernel_size=struct.unpack_from('<I',data,8)[0];ramdisk_size=struct.unpack_from('<I',data,16)[0]
            start=4096+(kernel_size+4095)//4096*4096
            entries=records(bounded_initramfs(data[start:start+ramdisk_size],256*1024**2))
            for name in ('LVGL-MIT.txt','cJSON-MIT.txt','Noto-OFL.txt'):
                fields,body=entries['usr/share/licenses/liuqin-recovery-ui/'+name]
                if fields[1]&0o170000!=0o100000 or not 0<len(body)<65536:raise RuntimeError('UI 许可证缺失或格式无效。')
                embedded['installer/runtime/ui-licenses/'+name]=body
            if contains_adbd:
                for name,(fields,body) in entries.items():
                    if name.startswith('usr/share/licenses/liuqin-adbd/') and fields[1]&0o170000==0o100000:
                        embedded['installer/runtime/adbd-licenses/'+name.rsplit('/',1)[1]]=body
            kernel_components=native_info['native_recovery_manifest'].get('identity',{}).get('recovery_kernel_components')
            if kernel_components:
                embedded.update(kernel_build_records(entries,kernel_components))
    if universal_candidate is not None:
        from liuqin_universal_recovery import inspect as inspect_universal,load_catalog
        candidate=Path(universal_candidate).resolve();checked=inspect_universal(candidate);catalog=load_catalog();artifact=catalog['artifact']
        if (checked['image_sha256']!=artifact['image_sha256'] or checked['manifest']['build_id']!=artifact['build_id'] or
            checked['manifest']['universal_policy']!=catalog['policy']):raise RuntimeError('通用候选与离线验收目录不一致。')
        prefix='installer/runtime/universal-candidates/'+checked['image_sha256']+'/'
        sources[prefix+'recovery.img']=candidate
        for name in ('STATUS.txt','universal-recovery-manifest.json','policy.json','recovery.avbpubkey'):
            if (candidate.parent/name).is_file():sources[prefix+name]=candidate.parent/name
    if native_installer is not None:
        import hashlib
        from liuqin_artifacts import boot_interface
        from liuqin_native_install import capable
        candidate=Path(native_installer).resolve();info=boot_interface(candidate,installer=True)
        if not capable(info.get('native_recovery_manifest',{})):raise RuntimeError('这不是具备完整受控写入能力的原生安装候选。')
        if info['native_recovery_manifest'].get('fixed_installer_version')==1:raise RuntimeError('固定安装版请使用 --fixed-installer 打包。')
        if info['native_recovery_manifest'].get('native_adb_version')!=1:raise RuntimeError('安装候选必须使用 ADB。')
        contains_adbd=True
        prefix='installer/runtime/native-installers/'+hashlib.sha256(candidate.read_bytes()).hexdigest()+'/'
        sources[prefix+'recovery.img']=candidate
        for name in ('native-recovery-manifest.json','STATUS.txt','NOTICE.md','SHA256SUMS'):
            if (candidate.parent/name).is_file():sources[prefix+name]=candidate.parent/name
    if fixed_installer is not None:
        from liuqin_native_install import FIXED_USAGE,capable
        from liuqin_universal_recovery import inspect
        candidate=Path(fixed_installer).resolve();checked=inspect(candidate);manifest=checked['manifest']
        if manifest['usage']!=FIXED_USAGE or not capable(manifest):raise RuntimeError('附加固定镜像缺少安装能力。')
        import json
        metadata=json.loads((candidate.parent/'native-recovery-manifest.json').read_text())
        if metadata.get('image_sha256')!=checked['image_sha256'] or any(metadata.get(k)!=v for k,v in manifest.items()):raise RuntimeError('固定安装版镜像与外部清单不一致。')
        prefix='installer/runtime/fixed-installers/'+checked['image_sha256']+'/'
        sources[prefix+'recovery.img']=candidate
        for name in ('native-recovery-manifest.json','STATUS.txt','NOTICE.md','SHA256SUMS','policy.json','recovery.avbpubkey'):
            path=candidate.parent/name
            if not path.is_file():raise RuntimeError('固定安装版缺少分发文件：'+name)
            sources[prefix+name]=path
        qualification=candidate.parent/'hardware-qualification.json'
        if qualification.is_file():
            qualified=json.loads(qualification.read_text())
            if qualified.get('image_sha256')!=checked['image_sha256'] or qualified.get('build_id')!=manifest['build_id']:raise RuntimeError('固定安装版验收记录与镜像不一致。')
            sources[prefix+'hardware-qualification.json']=qualification
        compatibility=candidate.parent/'restore-compatibility.json'
        if compatibility.is_file():
            record=json.loads(compatibility.read_text())
            if record.get('image_sha256')!=checked['image_sha256'] or record.get('build_id')!=manifest['build_id']:raise RuntimeError('固定 Rec 恢复适配记录与镜像不一致。')
            sources[prefix+'restore-compatibility.json']=compatibility
        contains_adbd=True
    if contains_adbd:
        if adbd_sources is None:raise RuntimeError('ADB 分发包必须提供对应源码归档 --adbd-sources。')
        import hashlib
        archive=Path(adbd_sources).resolve()
        if not archive.is_file() or archive.stat().st_size>256*1024**2:raise RuntimeError('adbd 源码归档缺失或过大。')
        sources['installer/runtime/adbd-source.tar.xz']=archive
        embedded['installer/runtime/adbd-source.tar.xz.sha256']=(hashlib.sha256(archive.read_bytes()).hexdigest()+'  adbd-source.tar.xz\n').encode()
    if native_variants:
        from liuqin_artifacts import boot_interface
        import hashlib
        for variant in native_variants:
            variant=Path(variant).resolve();info=boot_interface(variant,installer=True)
            if info.get('native_recovery_manifest',{}).get('native_adb_version')!=1:raise RuntimeError('额外原生镜像必须提供 ADB 安装通道。')
            if info['native_recovery_manifest'].get('inspection_only') is not True:raise RuntimeError('安装候选不能进入只读原生镜像目录。')
            digest=hashlib.sha256(variant.read_bytes()).hexdigest()
            if digest==hashlib.sha256(Path(native_recovery).read_bytes()).hexdigest():continue
            sources['installer/runtime/native-recoveries/'+digest+'/recovery.img']=variant
    if runtime_image is not None:
        from liuqin_artifacts import boot_interface
        runtime_image=Path(runtime_image).expanduser().resolve()
        interface=boot_interface(runtime_image,installer=True)
        if interface.get('native_recovery_manifest',{}).get('fixed_installer_version')==1:raise RuntimeError('固定安装版请使用 --fixed-installer 打包。')
        if interface['usb_network'] not in ('adb','adb-native'):raise RuntimeError('不再打包旧 USB 网络安装环境。')
        if interface['android_version']!=1 or interface['multiboot_version']!=1:raise RuntimeError('RAM 环境缺少分区管理能力。')
        sources['installer/runtime/installer.img']=runtime_image
    if recovery_image is not None:
        sys.path.insert(0,str(PROJECT/'installer'))
        from liuqin_gui.recovery import inspect_image
        record=inspect_image(Path(recovery_image))
        sources['installer/runtime/recovery.img']=Path(record['path'])
        from liuqin_recovery_image import inspect_paired
        paired=inspect_paired(Path(record['path'])) is not None
        if not paired and runtime_image is None:
            raise RuntimeError('普通 Recovery 不含安装服务，请提供配套 Recovery 或另行提供旧 RAM 环境。')
        if paired:
            notice=Path(record['path']).parent/'NOTICE.md'
            sources['installer/runtime/NOTICE.md']=notice if notice.is_file() else PROJECT/'build/recovery/NOTICE.md'
            manifest=Path(record['path']).parent/'recovery-manifest.json'
            if manifest.is_file():sources['installer/runtime/recovery-manifest.json']=manifest
    if recovery_variants:
        from liuqin_recovery_image import inspect_paired
        import hashlib
        for variant in recovery_variants:
            variant=Path(variant).resolve()
            if inspect_paired(variant) is None:raise RuntimeError('额外适配镜像缺少配套安装服务。')
            digest=hashlib.sha256(variant.read_bytes()).hexdigest()
            sources['installer/runtime/recoveries/'+digest+'/recovery.img']=variant
            notice=variant.parent/'NOTICE.md'
            if notice.is_file():sources['installer/runtime/recoveries/'+digest+'/NOTICE.md']=notice
    # Only source directories belong in the standalone package. In particular,
    # installer/.venv is a local environment, not part of the application.
    for name in ('installer/liuqin_gui', 'shared', 'device/installer','device/recovery'):
        for path in (PROJECT / name).rglob('*'):
            parts = path.relative_to(PROJECT).parts
            if (path.is_file() and not path.is_symlink() and
                not any(part.startswith('.') or part == '__pycache__' for part in parts) and
                path.suffix != '.pyc'):
                sources[path.relative_to(PROJECT).as_posix()] = path
    for name in ('install-gui.py', 'install-liuqin.py', 'package.py', 'README.md', 'STATUS.txt',
                 'pyproject.toml', 'uv.lock', '.python-version', 'start-windows.cmd'):
        sources['installer/' + name] = PROJECT / 'installer' / name
    for name in ('device/boot/boot-target-v1.sh', 'LICENSE', 'NOTICE', 'CREDITS.md', 'installer/requirements.txt'):
        sources[name] = PROJECT / name
    # Bundle maintained guides and images once; rewrite links to omitted source files.
    for path in sorted((PROJECT/'docs').glob('*.md')):
        if path.is_file() and not path.is_symlink():sources[path.relative_to(PROJECT).as_posix()]=path
    for path in sorted((PROJECT/'docs/assets').glob('*.png')):
        if path.is_file() and not path.is_symlink():sources[path.relative_to(PROJECT).as_posix()]=path
    if native_recovery is not None:sources['installer/STATUS.txt']=PROJECT/'build/recovery/NATIVE-STATUS.txt'
    if native_recovery is not None and native_info['native_recovery_manifest'].get('native_adb_version')==1:
        status=Path(native_recovery).parent/'STATUS.txt'
        if not status.is_file():raise RuntimeError('ADB 原生 Rec 缺少候选状态说明。')
        sources['installer/STATUS.txt']=status
    if native_installer is not None:
        embedded['installer/STATUS.txt']=sources.pop('installer/STATUS.txt').read_bytes()+('\n本文件上述只读限制对应默认镜像。另附原生安装候选位于 installer/runtime/native-installers/。启动安装器时可用 --recovery-image 指定镜像，再临时启动。能力与验收范围见 docs/NATIVE-RECOVERY-INSTALLER.md。\n').encode()
    required = ('installer/install-gui.py', 'installer/install-liuqin.py', 'installer/liuqin_gui/worker.py',
                'installer/liuqin_gui/qml/Main.qml', 'shared/liuqin_artifacts.py', 'shared/liuqin_layout.py',
                'shared/liuqin-rom-images.json', 'shared/liuqin_multiboot.py', 'shared/liuqin_boot_target.py',
                'device/installer/install-root.sh', 'device/installer/multiboot-layout.sh',
                'shared/liuqin_ota.py','shared/liuqin_ota_v1.py','shared/liuqin_super.py','installer/liuqin_gui/native_ota.py','shared/liuqin_rom_admission.py','shared/liuqin-android-roms.json','shared/liuqin-lineage-profile.json','shared/liuqin_rom_analysis.py','shared/liuqin_rom_preview.py','shared/liuqin-rom-preview-profiles.json','installer/liuqin_gui/rom_preview.py',
                'shared/liuqin_recovery_preserve.py','installer/liuqin_gui/fixed_recovery.py',
                'device/installer/multiboot-store.sh', 'device/installer/android-device.py',
                'device/installer/android-operation.sh', 'shared/liuqin_android.py',
                'shared/liuqin_android_images.py', 'shared/liuqin-android-profile.json', 'shared/liuqin_managed.py', 'shared/liuqin_partitions.py',
                'shared/liuqin_partition_editor.py', 'shared/liuqin_block_identity.py', 'shared/liuqin-miku-profile.json',
                'shared/liuqin_payload.py', 'shared/liuqin_super.py',
                'installer/liuqin_gui/wizard.py', 'installer/liuqin_gui/workflow.py', 'installer/liuqin_gui/boot_switch.py', 'installer/liuqin_gui/boot_store.py', 'device/recovery/native-store.py', 'installer/liuqin_gui/recovery.py',
                'shared/liuqin_partition_names.py', 'device/boot/boot-target-v1.sh',
                'shared/liuqin-recovery-profiles.json', 'installer/liuqin_gui/compatibility.py',
                'shared/liuqin_board_identity.py', 'shared/liuqin_native_recovery.py','shared/liuqin_firmware.py','shared/liuqin-native-recovery-profiles.json','installer/liuqin_gui/firmware.py','installer/liuqin_gui/native_profiles.py')
    if any(name not in sources or not sources[name].is_file() for name in required):
        raise RuntimeError('安装器源码缺少必要入口、公共模块或设备脚本。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, source in sorted(sources.items()):
            if source.suffix=='.md':
                archive.writestr(INSTALLER_ARCHIVE_ROOT + '/' + name,
                    export_markdown(name,source.read_text(),sources.keys()|embedded.keys()))
            else:
                archive.write(source, INSTALLER_ARCHIVE_ROOT + '/' + name)
        for name,body in embedded.items():archive.writestr(INSTALLER_ARCHIVE_ROOT+'/'+name,body)
        archive.writestr(INSTALLER_ARCHIVE_ROOT + '/README.md',
            '# ' + PROJECT_NAME + ' 安装助手\n\n'
            '[项目仓库](' + PROJECT_URL + ') · [问题反馈](' + PROJECT_URL + '/issues)。\n\n'
            '使用 uv 管理安装器依赖和 Python，首次运行自动准备环境。\n\n'
            '启动：`uv run --project installer installer/install-gui.py`。\n\n'
            'Windows 10/11 x64：双击 `installer/start-windows.cmd`；官方 platform-tools 目录与 installer 并列。\n\n'
            'Windows 使用 Fastboot/ADB 驱动，无需 USB 网卡；操作见 docs/INSTALLER-GUI.md，逐镜像验收见 installer/STATUS.txt 和随包记录。\n\n'
            '指定镜像目录：`uv run --project installer installer/install-gui.py --bundle /path/to/images`。\n\n'
            '连接后只读识别当前系统；匹配运行环境后点击读取分区，再选 userdata/rootfs 和 Android/Linux 材料。连接或切换设备不会自动重启。\n\n' +
            ('原生 Recovery 以自带启动资源提供分区管理和系统安装。具体镜像的实测范围和 SHA256 见 installer/STATUS.txt，不代表所有固件兼容。\n\n' if native_recovery is not None else
             '使用配套原生 ADB Rec，或连接已有且能核验本地镜像的安装环境。ROM 名称不提供兼容资格，实际角色、固件、运行载荷与备份按流程检查。\n\n') +
            '分区名最多 16 个 ASCII 字符，Linux rootfs 使用 lqr_ 加 12 位十六进制标识。\n\n' +
            ('本包内置完整 ROM 安装环境，通过 ADB 连接。连接和读取分区保持只读，备份并确认完整方案后才刷写。选择 ROM 即按包处理系统、固件和 Recovery；分区及 Linux 功能保留。默认启动镜像通过 Fastboot 临时加载。说明见 docs/FULL-ROM-FLASH.md。\n\n' if default_installer else
             '本包主镜像为只读原生 Rec，通过 '+native_connection+' 连接，提供维护菜单和日志导出。写入需要实际运行具有对应能力的安装版；固定版另看自身角色与资格，不能直接刷入本临时主镜像。见 installer/STATUS.txt 和 docs/NATIVE-RECOVERY-INSTALLER.md。\n\n' if native_recovery is not None else
             '本包为历史 TWRP 配套后端的临时镜像与 ADB 服务；不能把该镜像作为已验收的原生固定版。当前日常使用原生 ADB 包，见 docs/NATIVE-RECOVERY.md。\n\n' if paired else
             '本包已内置 RAM 安装环境；OS 材料另行选择。\n\n' if runtime_image else '源码包未内置运行环境，请先构建配套 Recovery，或使用内置 Recovery 的分发包。\n\n') +
            ('连接正在运行的原生 Recovery 后，可进入 ROM 内容与安装后 Rec 条件预览。完整 OTA v2 与历史基线分析分开，预览不授予写入权限。见 docs/FULL-ROM-FLASH.md。\n\n' if native_recovery is not None else '') +
            ('本包另含独立的原生安装版。可用 --recovery-image 指定，再从 Fastboot 临时启动；实际范围以该镜像状态和对应验收为准。见 docs/NATIVE-RECOVERY-INSTALLER.md。\n\n' if native_installer is not None else '') +
            ('本包附带固定安装版的校验镜像。平板进入对应固定 Rec 后，GUI 自动识别并连接；安装与默认引导切换沿用确认流程。固定镜像位于 installer/runtime/fixed-installers/。\n\n' if fixed_installer is not None else '') +
            ('从 Bootloader Fastboot 临时启动本包内的新版 Rec。在平板点「系统列表」，选择目标并确认切换；「启动默认系统」退出并开机。维护工具提供信息、只读分区、日志和亮度。固定版资格以其对应记录为准，不继承临时版验收。\n\n' if native_recovery is not None and native_info['native_recovery_manifest'].get('tablet_boot_switch_version')==1 else '') +
            ('新版 Rec 在完整确认 UFS 枚举后提前结束启动等待。存储验证通过后再启动触控与 ADB。启动耗时可在 Recovery 日志中复核。\n\n' if native_recovery is not None and native_info['native_recovery_manifest'].get('recovery_fast_start_version')==1 else '') +
            ('Android 材料页默认启用固定 Rec 自动保护：核对包内设备绑定的固定资格文件和安装后启动链，兼容时保留/恢复 recovery_a 并跳过包内 Recovery。否则可明确按 ROM 原样处理。旧固定版不会因更新 GUI 获得新协议，跨 ROM 重装资格另看实际记录。\n\n' if fixed_installer is not None and native_info['native_recovery_manifest'].get('fixed_recovery_restore_version')==1 else '') +
            ('「设置与诊断 → 引导存储与中断恢复」提供事务、空间、恢复/清理预览。操作需重新确认，清理保留系统镜像和事务记录；完整范围见 docs/NATIVE-RECOVERY-INSTALLER.md 及随包硬件记录。\n\n' if native_recovery is not None and native_info['native_recovery_manifest'].get('boot_store_version')==1 else '') +
            ('本包另含 v2 内嵌 DTBO 通用候选，独立存放在 universal-candidates 目录，只通过离线验证，尚未开放启动或安装。MIUI、HyperOS、Miku 的条件见 docs/RECOVERY-FIRMWARE-PROFILES.md#固定启动条件。安装器不会修改 AVB。\n\n' if universal_candidate is not None else '') +
            ('本包包含已检查机型的 Recovery 镜像，仍需在平板验证兼容性。\n\n' if recovery_image else '') +
            '镜像构建使用完整项目的 build/ 与 Devbox。\n\n'
            '本项目衍生自 yzddmr6 的 ' + UPSTREAM_PROJECT + '。\n'
            '原作者与第三方署名见 CREDITS.md；原许可证及第三方声明见 LICENSE 和 NOTICE。\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=PROJECT / 'out/releases/liuqin-linux-kit-installer-source.zip')
    parser.add_argument('--runtime', type=Path, help='Bundle a verified installer.img for automatic device reading')
    parser.add_argument('--recovery', type=Path, help='Bundle a verified liuqin Recovery image')
    parser.add_argument('--recovery-variant',type=Path,action='append',default=[],help='Bundle an additional paired image for exact-firmware selection; repeatable')
    parser.add_argument('--native-recovery',type=Path,help='Bundle a validated temporary native ADB Recovery; capabilities come from its manifest')
    parser.add_argument('--native-recovery-variant',type=Path,action='append',default=[],help='Bundle another native temporary image for exact firmware routing; repeatable')
    parser.add_argument('--universal-recovery-candidate',type=Path,help='Bundle a separately isolated, offline-validated v2 candidate; never auto-boot it')
    parser.add_argument('--native-installer-candidate',type=Path,help='Bundle a separately selected temporary native installer; hardware scope comes from matching evidence')
    parser.add_argument('--adbd-sources',type=Path,help='Corresponding source archive for bundled standalone adbd')
    parser.add_argument('--fixed-installer',type=Path,help='Bundle a fixed installer image for attaching to that running Recovery')
    args = parser.parse_args()
    print(package(args.out,args.runtime,args.recovery,args.recovery_variant,args.native_recovery,args.native_recovery_variant,args.universal_recovery_candidate,args.native_installer_candidate,args.adbd_sources,args.fixed_installer))


if __name__ == '__main__':
    main()
