# SPDX-License-Identifier: MIT
"""Download pinned stock ROM input and extract only the build's four images."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import urllib.request

from source_build import sha


def verify_images(directory, required, reference):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('ROM images 必须是普通目录：' + str(directory))
    for name in required:
        path = directory / name
        expected = reference['images'][name]
        if (path.is_symlink() or not path.is_file() or
                path.stat().st_size != expected['bytes'] or sha(path) != expected['sha256']):
            raise ValueError('原厂镜像缺失或校验失败，保留现有目录：' + str(path))
    return directory


def verify_archive(path, record):
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or
            path.stat().st_size != record['bytes'] or sha(path) != record['sha256']):
        raise ValueError('ROM 完整包与固定 SHA256 不一致，保留现有文件：' + str(path))
    return path


def download_archive(record, cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / record['archive']
    if path.exists() or path.is_symlink():
        return verify_archive(path, record)
    errors = []
    for index, url in enumerate(record['mirrors'], 1):
        if not url.startswith('https://'):
            raise ValueError('ROM 下载地址必须使用 HTTPS。')
        print('下载固定 ROM，镜像源 %d/%d：%s' % (index, len(record['mirrors']), url), flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix='.rom-download-', dir=cache) as temporary:
                candidate = Path(temporary) / record['archive']
                request = urllib.request.Request(url, headers={'User-Agent': 'Liuqin-Linux-Kit/1.0'})
                digest = hashlib.sha256()
                size = 0
                with urllib.request.urlopen(request, timeout=60) as source, candidate.open('xb') as target:
                    while data := source.read(8 * 1024 * 1024):
                        size += len(data)
                        if size > record['bytes']:
                            raise ValueError('下载超过固定 ROM 包大小。')
                        digest.update(data)
                        target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                if size != record['bytes'] or digest.hexdigest() != record['sha256']:
                    raise ValueError('下载结果不是固定 ROM 包，未使用。')
                os.link(candidate, path)
            return path
        except (OSError, ValueError) as error:
            errors.append(str(error))
            print('此下载源失败：' + str(error), flush=True)
    raise ValueError('所有 ROM 下载源均失败：' + '; '.join(errors))


def extract_images(archive, output, record, reference):
    output = Path(output)
    if output.is_symlink():
        raise ValueError('ROM 输出目录不能是符号链接。')
    output = output.resolve()
    if output.exists():
        return verify_images(output, record['required_images'], reference)
    output.parent.mkdir(parents=True, exist_ok=True)
    expected_paths = {
        record['archive_root'] + '/images/' + name: name
        for name in record['required_images']
    }
    with tempfile.TemporaryDirectory(prefix='.rom-images-', dir=output.parent) as temporary:
        work = Path(temporary) / 'images'
        work.mkdir()
        found = set()
        # Never execute flash scripts or extract arbitrary archive paths.
        with tarfile.open(archive, 'r|gz') as tar:
            for member in tar:
                name = expected_paths.get(member.name)
                if name is None:
                    continue
                if not member.isfile() or member.issparse() or name in found:
                    raise ValueError('ROM 所需镜像不是唯一普通文件：' + member.name)
                expected = reference['images'][name]
                if member.size != expected['bytes']:
                    raise ValueError('ROM 镜像大小不符：' + name)
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError('ROM 镜像无法读取：' + name)
                digest = hashlib.sha256()
                with source, (work / name).open('xb') as target:
                    remaining = member.size
                    while remaining:
                        data = source.read(min(8 * 1024 * 1024, remaining))
                        if not data:
                            raise ValueError('ROM 镜像截断：' + name)
                        target.write(data)
                        digest.update(data)
                        remaining -= len(data)
                (work / name).chmod(0o644)
                if digest.hexdigest() != expected['sha256']:
                    raise ValueError('ROM 镜像 SHA256 不符：' + name)
                found.add(name)
        if found != set(record['required_images']):
            raise ValueError('ROM 缺少构建镜像：' + ', '.join(sorted(set(record['required_images']) - found)))
        if output.exists():
            raise ValueError('ROM 输出目录已被其他任务创建，未覆盖。')
        os.rename(work, output)
    return output


def fetch(output, cache, record, reference, *, archive=None):
    if record['rom'] != reference['rom']:
        raise ValueError('ROM 下载记录与设备版本记录不一致。')
    if PurePosixPath(record['archive']).name != record['archive']:
        raise ValueError('ROM 包名必须是单一文件名。')
    required = record['required_images']
    if set(required) != {'vendor_boot.img', 'dtbo.img', 'super.img', 'NON-HLOS.bin'}:
        raise ValueError('ROM 构建输入需要三份板级镜像和指纹固件。')
    output = Path(output)
    if output.exists() or output.is_symlink():
        verify_images(output, required, reference)
        print('复用已校验的原厂镜像：' + str(output.resolve()), flush=True)
        return output.resolve()
    package = verify_archive(archive, record) if archive is not None else download_archive(record, cache)
    result = extract_images(package, output, record, reference)
    print('ROM 构建镜像已提取并校验：' + str(result), flush=True)
    return result
