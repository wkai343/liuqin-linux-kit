# SPDX-License-Identifier: MIT
"""Build and install Fedora FFmpeg with Iris fixes in an isolated target root."""
import fnmatch
import json
from pathlib import Path
import shutil
import subprocess
import tomllib

from source_build import PROJECT, sha, tree_identity

RECIPE = PROJECT/'build/ffmpeg'
BUILDER = PROJECT/'build/scripts/build-fedora-ffmpeg.py'
PACKAGES = ('ffmpeg-free', 'libavcodec-free', 'libavdevice-free', 'libavfilter-free',
            'libavformat-free', 'libavutil-free', 'libswresample-free', 'libswscale-free')
DEVEL_PACKAGES = ('libavcodec-free-devel', 'libavutil-free-devel', 'libswresample-free-devel')
LICENSE_DIR = 'usr/share/licenses/liuqin-ffmpeg'
QUERY = '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\t%{SOURCEPACKAGE}\n'


def record():
    return json.loads((RECIPE/'source.json').read_text())


def selected(recipe):
    return 'ffmpeg-free' in recipe.get('packages', [])


def release(info):
    return info['version']+'-'+info['release']+'.'+info['dist']


def filenames(info, packages=PACKAGES):
    return {name:name+'-'+release(info)+'.aarch64.rpm' for name in packages}


def source_filename(info):
    return 'ffmpeg-'+release(info)+'.src.rpm'


def inputs():
    return dict(recipe=tree_identity(RECIPE), builder=sha(BUILDER))


def regular(path):
    if not path.is_file() or path.resolve() != path.absolute():
        raise ValueError('FFmpeg 输入缺失或含符号链接：'+str(path))
    return path


def check_rpm(path, name, info, *, source=False):
    regular(path)
    actual = subprocess.check_output(['rpm', '-qp', '--qf', QUERY, str(path)], text=True).strip()
    expected = '\t'.join((name, '0:'+release(info), 'aarch64', '1' if source else '(none)'))
    if actual != expected:
        raise ValueError('FFmpeg RPM 名称、版本或架构不匹配：'+str(path))


def collect(rpmbuild, output):
    """Keep runtime and matching development RPMs in separate payload groups."""
    info = record()
    source = Path(rpmbuild)/'SRPMS'/source_filename(info)
    groups = {'rpms': PACKAGES, 'devel-rpms': DEVEL_PACKAGES}
    for packages in groups.values():
        for name, file in filenames(info, packages).items():
            check_rpm(Path(rpmbuild)/'RPMS/aarch64'/file, name, info)
    check_rpm(source, 'ffmpeg', info, source=True)
    output = Path(output)
    hashes = {}
    for directory, packages in groups.items():
        (output/directory).mkdir(parents=True, exist_ok=False)
        for file in filenames(info, packages).values():
            shutil.copyfile(Path(rpmbuild)/'RPMS/aarch64'/file, output/directory/file)
        hashes[directory] = {p.name:sha(p) for p in sorted((output/directory).iterdir())}
    shutil.copyfile(source, output/source.name)
    manifest = dict(schema=2, inputs=inputs(), source=info, **hashes,
        srpm=dict(file=source.name, sha256=sha(output/source.name)))
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return output


def validate(bundle):
    bundle = Path(bundle).absolute()
    manifest = json.loads(regular(bundle/'manifest.json').read_text())
    info = record()
    if (manifest.get('schema') != 2 or manifest.get('source') != info or
            manifest.get('inputs') != inputs()):
        raise ValueError('FFmpeg 构建包与当前配方或补丁不一致。')
    for directory, packages in {'rpms':PACKAGES, 'devel-rpms':DEVEL_PACKAGES}.items():
        expected = set(filenames(info, packages).values())
        if (set(manifest.get(directory, {})) != expected or
                {p.name for p in (bundle/directory).iterdir()} != expected):
            raise ValueError('FFmpeg 运行库或开发包集合不完整或包含额外文件。')
        for name, file in filenames(info, packages).items():
            path = regular(bundle/directory/file)
            if sha(path) != manifest[directory][file]:
                raise ValueError('FFmpeg RPM 摘要不一致：'+file)
            check_rpm(path, name, info)
    src = manifest.get('srpm', {})
    if src.get('file') != source_filename(info):
        raise ValueError('FFmpeg 对应源码包缺失。')
    path = regular(bundle/src['file'])
    if sha(path) != src.get('sha256'):
        raise ValueError('FFmpeg 源码包摘要不一致。')
    check_rpm(path, 'ffmpeg', info, source=True)
    return manifest


def build(root, archive, output, jobs, chroot, dnf):
    info = record()
    identity = inputs()
    if jobs < 1 or sha(regular(Path(archive))) != info['sha256']:
        raise ValueError('FFmpeg 源码校验失败或构建并行数无效。')
    work = root/'tmp/liuqin-ffmpeg-build'
    (work/'build/scripts').mkdir(parents=True)
    shutil.copytree(RECIPE, work/'build/ffmpeg')
    shutil.copyfile(BUILDER, work/'build/scripts'/BUILDER.name)
    shutil.copyfile(archive, work/info['srpm'])
    if (tree_identity(work/'build/ffmpeg') != identity['recipe'] or
            sha(work/'build/scripts'/BUILDER.name) != identity['builder']):
        raise ValueError('FFmpeg 配方在暂存期间变化。')
    dnf(root, 'install', 'rpm-build', 'redhat-rpm-config', 'gpgverify', 'cpio', 'dnf5-plugins')
    guest = '/tmp/liuqin-ffmpeg-build'
    chroot(root, ['python3', guest+'/build/scripts/'+BUILDER.name,
        '--prepare-only', '--srpm', guest+'/'+info['srpm'], '--out', guest+'/rpmbuild'])
    spec = guest+'/rpmbuild/SPECS/ffmpeg.spec'
    dnf(root, 'builddep', spec)
    chroot(root, ['rpmbuild', '-ba', '--define', '_topdir '+guest+'/rpmbuild',
        '--define', '_smp_mflags -j'+str(jobs), spec])
    if inputs() != identity:
        raise ValueError('FFmpeg 配方在编译期间变化，未发布产物。')
    collect(work/'rpmbuild', output)
    inventory = chroot(root, ['rpm', '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
                       capture_output=True).stdout
    (Path(output)/'build-packages.tsv').write_bytes(inventory)


def stage(bundle, staging, *, development=False):
    """Validate before including FFmpeg in the desktop's single RPM transaction."""
    manifest = validate(bundle)
    for directory in ('rpms', 'devel-rpms') if development else ('rpms',):
        for file in manifest[directory]:
            target = staging/file
            if target.exists() or target.is_symlink():
                raise ValueError('FFmpeg RPM 暂存路径重复：'+file)
            shutil.copyfile(Path(bundle)/directory/file, target)


def check_installed(root, chroot):
    info = record()
    actual = chroot(root, ['rpm', '-q', '--qf', QUERY, *PACKAGES],
                    capture_output=True, text=True).stdout.splitlines()
    expected = ['\t'.join((name, '0:'+release(info), 'aarch64', '(none)')) for name in PACKAGES]
    if sorted(actual) != sorted(expected):
        raise ValueError('镜像未安装完整的 FFmpeg 修订版运行库。')
    for flag, codecs in [('-decoders', ('h264', 'hevc', 'vp9')),
                         ('-encoders', ('h264', 'hevc'))]:
        result = chroot(root, ['ffmpeg', '-hide_banner', flag], capture_output=True, text=True, timeout=30)
        available = {line.split()[1] for line in result.stdout.splitlines() if len(line.split()) > 1}
        if not all(codec+'_v4l2m2m' in available for codec in codecs):
            raise ValueError('镜像 FFmpeg 缺少所需的 V4L2 编解码接口。')
    for codec in ('h264', 'hevc', 'vp9'):
        result = chroot(root, ['ffmpeg', '-hide_banner', '-h', 'decoder='+codec+'_v4l2m2m'],
                        capture_output=True, text=True, timeout=30)
        devices = [line.split(':', 1)[1].split() for line in result.stdout.splitlines()
                   if line.strip().startswith('Supported hardware devices:')]
        if not any('drm' in names for names in devices):
            raise ValueError('镜像 FFmpeg 缺少 DRM PRIME 解码接口。')


def check_locks(root):
    config = regular(root/'etc/dnf/versionlock.toml')
    locks = tomllib.loads(config.read_text())
    if locks.get('version') != '1.0':
        raise ValueError('镜像 FFmpeg 版本锁格式不受支持。')
    condition = [dict(key='evr', comparator='=', value=release(record()))]
    for name in PACKAGES:
        entries = [p for p in locks.get('packages', []) if fnmatch.fnmatchcase(name,p.get('name',''))]
        if len(entries) != 1 or entries[0].get('name') != name or entries[0].get('conditions') != condition:
            raise ValueError('镜像 FFmpeg 版本锁不完整或存在冲突：'+name)


def configure_playback(root):
    # These are defaults; per-user application preferences retain precedence.
    # Preserve an overlay's existing system configuration as well.
    files = (
        ('usr/bin/mpv', 'mpv.conf', 'etc/mpv/mpv.conf', 0o644),
        ('usr/bin/haruna', 'haruna.conf', 'etc/xdg/haruna/haruna.conf', 0o644),
        ('usr/lib64/qt6/plugins/multimedia/libgstreamermediaplugin.so',
         'liuqin-qt-video', 'usr/local/bin/liuqin-qt-video', 0o755),
    )
    for prerequisite, source, target, mode in files:
        path = root/target
        if not (root/prerequisite).is_file() or path.exists() or path.is_symlink():
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('视频播放配置目录离开根文件系统：'+target)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(RECIPE/'playback'/source, path)
        path.chmod(mode)


def configure(root, bundle, chroot):
    manifest = validate(bundle)
    check_installed(root, chroot)
    # Preserve unrelated locks; DNF resolves these names against installed RPMs.
    chroot(root, ['dnf', '-y', '--disable-repo=*', 'versionlock', 'add', *PACKAGES])
    check_locks(root)
    configure_playback(root)
    docs = root/LICENSE_DIR
    if docs.exists() or docs.is_symlink():
        raise ValueError('FFmpeg 源码保留目录已存在：'+str(docs))
    docs.mkdir(parents=True)
    shutil.copyfile(Path(bundle)/manifest['srpm']['file'], docs/manifest['srpm']['file'])
    shutil.copyfile(Path(bundle)/'manifest.json', docs/'BUILD.json')
    shutil.copytree(RECIPE, docs/'recipe')
    shutil.copyfile(BUILDER, docs/BUILDER.name)


def check_image(root, chroot):
    """Recheck the final kernel/rootfs assembly without a GPU or video device."""
    check_installed(root, chroot)
    check_locks(root)
    docs = root/LICENSE_DIR
    manifest = json.loads(regular(docs/'BUILD.json').read_text())
    if manifest.get('inputs') != inputs() or manifest.get('source') != record():
        raise ValueError('镜像 FFmpeg 来源记录与配方不一致。')
    src = manifest.get('srpm', {})
    if src.get('file') != source_filename(record()) or sha(regular(docs/src['file'])) != src.get('sha256'):
        raise ValueError('镜像 FFmpeg 对应源码不完整。')
