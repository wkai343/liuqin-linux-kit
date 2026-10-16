# SPDX-License-Identifier: MIT
"""Read image interfaces without executing anything from an installation package."""
from __future__ import annotations

import gzip
import hashlib
import io
from pathlib import PurePosixPath
import re
import struct
import tarfile
from liuqin_kernel import ROOT_MARKER, parse_contract, module_records
from liuqin_filesystems import CAPABILITY_PATH, parse_support


class ArtifactError(ValueError):
    pass


INSTALLER_INTERFACE = b'liuqin-installer-v2\n'


ROOT_DEVICES = {'dev/console': (5, 1), 'dev/null': (1, 3), 'dev/zero': (1, 5),
                'dev/full': (1, 7), 'dev/random': (1, 8), 'dev/urandom': (1, 9),
                'dev/tty': (5, 0), 'dev/ptmx': (5, 2)}


def bounded_gzip(data, maximum):
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        result = stream.read(maximum + 1)
    if len(result) > maximum:
        raise ArtifactError('镜像解压后过大。')
    return result


def bounded_initramfs(data, maximum):
    if data.startswith(b'\x1f\x8b'):
        return bounded_gzip(data, maximum)
    if not data.startswith(b'\x28\xb5\x2f\xfd'):
        raise ArtifactError('启动环境需要 gzip 或 Zstd 压缩格式。')
    try:
        import zstandard
    except ImportError as error:
        raise ArtifactError('读取 Zstd 启动镜像需要 zstandard，请更新安装器或引导工具的依赖。') from error
    try:
        parameters = zstandard.get_frame_parameters(data)
    except zstandard.ZstdError as error:
        raise ArtifactError('Zstd 启动环境头损坏。') from error
    # Known frame sizes take precedence over max_output_size in the decoder.
    # Check the content and window bounds before allocating decoder output.
    if (parameters.content_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR)
            and parameters.content_size > maximum):
        raise ArtifactError('镜像解压后过大。')
    if parameters.window_size > maximum:
        raise ArtifactError('Zstd 启动环境的解压窗口过大。')
    try:
        result = zstandard.ZstdDecompressor().decompress(
            data, max_output_size=maximum, allow_extra_data=False)
    except zstandard.ZstdError as error:
        raise ArtifactError('Zstd 启动环境不完整、损坏或解压后过大。') from error
    if len(result) > maximum:
        raise ArtifactError('镜像解压后过大。')
    return result


def cpio_files(data):
    position, files = 0, {}
    while position + 110 <= len(data):
        header = data[position:position + 110]
        if header[:6] not in (b'070701', b'070702'):
            break
        try:
            size = int(header[54:62], 16)
            namesize = int(header[94:102], 16)
            mode = int(header[14:22], 16)
        except ValueError as error:
            raise ArtifactError('安装环境 CPIO 头损坏。') from error
        if not 1 <= namesize <= 4096:
            raise ArtifactError('安装环境 CPIO 路径损坏。')
        name = data[position + 110:position + 110 + namesize - 1].decode('utf-8')
        start = (position + 110 + namesize + 3) & ~3
        if start + size > len(data):
            raise ArtifactError('安装环境 CPIO 不完整。')
        if name == 'TRAILER!!!':
            return files
        name = name.removeprefix('./')
        if name in files:
            raise ArtifactError('安装环境包含重复文件：' + name)
        # Store only interface files; the installer runtime can be large.
        files[name] = (mode, data[start:start + size] if name.startswith('etc/liuqin-') else b'')
        position = (start + size + 3) & ~3
    raise ArtifactError('安装环境缺少完整的 CPIO 结束记录。')


def boot_interface(path, installer=False):
    data = path.read_bytes()
    if len(data) < 4096 or len(data) > 192 * 1024 * 1024 or data[:8] != b'ANDROID!':
        raise ArtifactError(path.name + ' 不是有效的 liuqin 启动镜像。')
    integer = lambda offset: struct.unpack_from('<I', data, offset)[0]
    kernel_size, ramdisk_size, second_size = integer(8), integer(16), integer(24)
    page = integer(36)
    if (page, integer(40), integer(1644)) != (4096, 2, 1660) or not integer(1648):
        raise ArtifactError(path.name + ' 需要项目使用的 Android v2 嵌入设备树格式。')
    align = lambda size: (size + page - 1) // page * page
    ramdisk_start = page + align(kernel_size)
    dtb_start = ramdisk_start + align(ramdisk_size) + align(second_size)
    if not kernel_size or not ramdisk_size or dtb_start + integer(1648) > len(data):
        raise ArtifactError(path.name + ' 的内核、安装环境或设备树不完整。')
    if b'xiaomi,liuqin\0' not in data[dtb_start:dtb_start + integer(1648)]:
        raise ArtifactError(path.name + ' 的设备树不支持小米平板 6 Pro。')
    files = cpio_files(bounded_initramfs(data[ramdisk_start:ramdisk_start + ramdisk_size], 256 * 1024 * 1024))
    def regular(name):
        mode, content = files.get(name, (0, b''))
        if mode & 0o170000 != 0o100000:
            raise ArtifactError(path.name + ' 缺少启动接口：' + name)
        return content
    if regular('etc/liuqin-storage-mode') != (b'readonly\n' if installer else b'persistent\n'):
        raise ArtifactError(path.name + ' 的 RAM 安装/持久启动角色不正确。')
    if not installer and 'etc/liuqin-installer' in files:
        raise ArtifactError('boot.img 不能使用临时 RAM 安装镜像。')
    if installer and regular('etc/liuqin-installer') != b'liuqin\n':
        raise ArtifactError('installer.img 不是临时 RAM 安装环境。')
    kernel = data[page:page + kernel_size]
    if kernel.startswith(b'\x1f\x8b'):
        kernel = bounded_gzip(kernel, 256 * 1024 * 1024)
    support = files.get(CAPABILITY_PATH)
    if support is not None and support[0] & 0o177777 != 0o100644:
        raise ArtifactError('镜像文件系统支持信息必须是普通文件。')
    result = dict(kernel_sha256=hashlib.sha256(kernel).hexdigest(),
                  filesystems=parse_support(support[1] if support else None))
    if installer:
        interface = regular('etc/liuqin-installer-interface')
        if interface != INSTALLER_INTERFACE:
            raise ArtifactError('安装环境接口版本不受支持。')
        release = regular('etc/liuqin-installer-kernel.release').decode('ascii').strip()
        if not re.fullmatch(r'[A-Za-z0-9._+-]{1,255}', release):
            raise ArtifactError('安装环境缺少有效的独立内核版本。')
        if 'etc/liuqin-native-root.contract' in files or 'etc/liuqin-root-profile' in files:
            raise ArtifactError('独立安装环境不能内置目标根文件系统的接口或清单。')
        network = files.get('etc/liuqin-installer-usb-network')
        if network is not None:
            if network[0] & 0o177777 != 0o100644 or network[1] != b'rndis\n':
                raise ArtifactError('安装镜像的 USB 网络能力信息无效。')
        return dict(**result, interface=interface.decode().strip(), kernel_release=release,
                    usb_network='rndis' if network else 'ncm')
    profile = regular('etc/liuqin-root-profile')
    if profile != b'native-updatable\n':
        raise ArtifactError('启动镜像的目标系统接口不受支持。')
    contract = regular('etc/liuqin-native-root.contract')
    if parse_contract(contract)['image_sha256'] != result['kernel_sha256']:
        raise ArtifactError('启动内核与内核身份不一致。')
    return dict(**result, profile=profile, contract=contract)


def contract_hashes(content):
    identity = parse_contract(content)
    base = 'usr/share/liuqin/kernels/' + identity['release']
    return {'etc/liuqin-root-interface': hashlib.sha256(ROOT_MARKER).hexdigest(),
            'etc/liuqin-native-root': hashlib.sha256(b'liuqin-native-root-v1\n').hexdigest(),
            base + '/boot.contract': hashlib.sha256(content).hexdigest(),
            base + '/modules.manifest': identity['modules_sha256']}


def inspect_root(path, pins, check_cancel=lambda: None, profile='native-updatable'):
    if profile != 'native-updatable':
        raise ArtifactError('根接口不受支持，请使用当前构建流程生成安装文件。')
    found, paths, symlinks, hardlinks, size = {}, set(), set(), [], 0
    release = None
    runtime, identities, links = {}, {}, {}
    kernel_contract, manifest_content, modules_found = None, None, {}
    versions = [name.split('/')[-2] for name in pins if name.startswith('usr/share/liuqin/kernels/') and name.endswith('/boot.contract')]
    if len(versions) != 1:
        raise ArtifactError('目标内核身份缺失或重复。')
    module_prefix = 'usr/lib/modules/' + versions[0] + '/'
    with tarfile.open(path, mode='r|gz') as archive:
        for entry in archive:
            check_cancel()
            name = entry.name.removeprefix('./').rstrip('/')
            if name in ('', '.'):
                continue
            if (name == 'etc/liuqin/boot-updates.json' or
                    name == 'var/lib/liuqin/boot-updates' or name.startswith('var/lib/liuqin/boot-updates/')):
                raise ArtifactError('根归档含设备私有引导更新状态，请使用干净模板。')
            parts = PurePosixPath(name).parts
            if name.startswith('/') or '..' in parts or name in paths:
                raise ArtifactError('根文件系统含有不安全或重复路径：' + name)
            paths.add(name)
            if entry.issym():
                symlinks.add(name)
                links[name] = entry.linkname
            elif entry.islnk():
                target = entry.linkname.removeprefix('./')
                if target.startswith('/') or '..' in PurePosixPath(target).parts:
                    raise ArtifactError('根文件系统硬链接超出归档：' + name)
                hardlinks.append(target)
            elif entry.ischr() and (ROOT_DEVICES.get(name) == (entry.devmajor, entry.devminor)
                                    or (entry.devmajor, entry.devminor) == (0, 0)):
                # Overlay exports can retain harmless 0:0 whiteout markers.
                pass
            elif not entry.isfile() and not entry.isdir():
                raise ArtifactError('根文件系统请移除设备节点和特殊文件：' + name)
            if entry.isfile():
                size += entry.size
            if module_prefix and name.startswith(module_prefix) and not entry.isdir():
                if not entry.isfile() or (entry.mode, entry.uid, entry.gid) != (0o644, 0, 0):
                    raise ArtifactError('模块必须是 root 拥有的普通文件：' + name)
                modules_found[name] = hashlib.file_digest(archive.extractfile(entry), 'sha256').hexdigest()
            if name in ('usr/local/sbin/liuqin-gnome-storage-guard',
                    'usr/local/bin/liuqin-gnome-storage-guard',
                    'etc/systemd/system/liuqin-gnome-storage-guard.service'):
                expected_mode = 0o755 if name.startswith('usr/') else 0o644
                if not entry.isfile() or (entry.mode, entry.uid, entry.gid) != (expected_mode, 0, 0):
                    raise ArtifactError('存储保护文件无效：' + name)
                runtime[name] = b'present'
            if name in ('etc/passwd', 'etc/group'):
                if not entry.isfile() or entry.size > 1024 * 1024:
                    raise ArtifactError('根文件系统账号文件无效。')
                identities[name] = archive.extractfile(entry).read().decode('utf-8')
            if name in (CAPABILITY_PATH, 'etc/liuqin-root-filesystem'):
                if not entry.isfile() or (entry.mode, entry.uid, entry.gid) != (0o644, 0, 0) or entry.size > 64:
                    raise ArtifactError('根文件系统支持信息必须是 root 拥有的普通文件。')
                runtime[name] = archive.extractfile(entry).read()
            if name == 'usr/lib/systemd/systemd':
                if not entry.isfile() or entry.mode != 0o755:
                    raise ArtifactError('系统程序不可执行：' + name)
                runtime[name] = archive.extractfile(entry).read(20)
            if name in pins:
                if not entry.isfile() or (entry.uid, entry.gid) != (0, 0):
                    raise ArtifactError('设备接口必须是 root 拥有的普通文件：' + name)
                executable = (name.startswith(('usr/local/bin/', 'usr/local/sbin/', 'usr/local/libexec/'))
                              or name in ('usr/bin/hexagonrpcd', 'usr/libexec/iio-sensor-proxy',
                                          'usr/libexec/liuqin-ssc-sample-gate'))
                if entry.mode != (0o755 if executable else 0o644):
                    raise ArtifactError('设备接口文件权限不正确：' + name)
                source, digest = archive.extractfile(entry), hashlib.sha256()
                content = bytearray()
                while chunk := source.read(1024 * 1024):
                    check_cancel()
                    digest.update(chunk)
                    if name.endswith(('/boot.contract', '/modules.manifest')):
                        if len(content) + len(chunk) > 4 * 1024 * 1024:
                            raise ArtifactError('根文件系统内核清单过大。')
                        content.extend(chunk)
                found[name] = digest.hexdigest()
                if name.endswith('/boot.contract'):
                    kernel_contract = parse_contract(bytes(content))
                    release = kernel_contract['release']
                if name.endswith('/modules.manifest'):
                    manifest_content = bytes(content)
    for name in paths:
        if any(str(parent) in symlinks for parent in PurePosixPath(name).parents):
            raise ArtifactError('归档中的文件不能写入符号链接目录：' + name)
    if any(target not in paths or target in symlinks for target in hardlinks):
        raise ArtifactError('根文件系统存在无效硬链接。')
    for name, expected in pins.items():
        if found.get(name) != expected:
            raise ArtifactError('根文件系统与启动镜像的设备接口不匹配：' + name)
    # Fedora merges local sbin into bin. Admit only that exact relative
    # alias; the canonical executable above still has root/mode checks.
    if links.get('usr/local/sbin')=='bin' and 'usr/local/bin/liuqin-gnome-storage-guard' in runtime:
        runtime['usr/local/sbin/liuqin-gnome-storage-guard']=runtime['usr/local/bin/liuqin-gnome-storage-guard']
    if kernel_contract is None or manifest_content is None:
        raise ArtifactError('根文件系统缺少版本化内核身份。')
    expected = module_records(manifest_content, release)
    prefix = 'usr/lib/modules/' + release + '/'
    if {k: v for k, v in modules_found.items() if k.startswith(prefix)} != expected:
        raise ArtifactError('根文件系统中的内核模块与目标内核不匹配。')
    if not all(name in runtime for name in ('usr/local/sbin/liuqin-gnome-storage-guard',
                'etc/systemd/system/liuqin-gnome-storage-guard.service')):
        raise ArtifactError('根文件系统缺少存储保护文件。')
    if not release or not re.fullmatch(r'[A-Za-z0-9._+-]+', release):
        raise ArtifactError('根文件系统缺少有效内核版本。')
    systemd = runtime.get('usr/lib/systemd/systemd', b'')
    if len(systemd) < 20 or systemd[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', systemd, 18)[0] != 183:
        raise ArtifactError('根文件系统需要 ARM64 systemd；普通 PC 镜像不能用于平板。')
    for name in ('etc/passwd', 'etc/group'):
        if not any(re.fullmatch(r'fastrpc:[^:]*:[0-9]+:.*', row) for row in identities.get(name, '').splitlines()):
            raise ArtifactError('根文件系统缺少设备校准所需的 fastrpc 账号或组。')
    if links.get('etc/systemd/system/basic.target.requires/liuqin-gnome-storage-guard.service') != '../liuqin-gnome-storage-guard.service':
        raise ArtifactError('根文件系统未启用平板存储保护服务。')
    support = parse_support(runtime.get(CAPABILITY_PATH))
    if 'btrfs' in support and runtime.get('etc/liuqin-root-filesystem') not in (b'ext4\n', b'btrfs\n'):
        raise ArtifactError('Btrfs 根归档缺少预先标记的文件系统配置模板。')
    return dict(kernel_release=release, expanded_bytes=size, root_filesystems=support)


def inspect_package(directory, check_cancel=lambda: None):
    boot = boot_interface(directory / 'boot.img')
    installer = boot_interface(directory / 'installer.img', installer=True)
    try:
        root = inspect_root(directory / 'rootfs.tar.gz', contract_hashes(boot['contract']), check_cancel,
                            profile=boot['profile'].decode().strip())
    except tarfile.TarError as error:
        raise ArtifactError('根文件系统归档损坏或不是 tar.gz。') from error
    return dict(**root, root_profile=boot['profile'].decode().strip(),
                filesystems=[name for name in boot['filesystems']
                             if name in installer['filesystems'] and name in root['root_filesystems']],
                root_contract_sha256=hashlib.sha256(boot['contract']).hexdigest(),
                installer_interface=installer['interface'],
                installer_usb_network=installer['usb_network'],
                installer_kernel_release=installer['kernel_release'],
                installer_kernel_sha256=installer['kernel_sha256'])
