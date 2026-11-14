# SPDX-License-Identifier: MIT
"""Build a separate OBS encoder RPM with matching FFmpeg development packages."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import fedora_ffmpeg
from rpm_package import build as rpm
from source_build import PROJECT, sha, tree_identity

RECIPE = PROJECT/'build/obs-v4l2'
TESTS = (PROJECT/'tests/obs-v4l2-packets.c', PROJECT/'tests/obs-v4l2-record.py')
NAME, VERSION, RELEASE = 'obs-studio-plugin-liuqin-v4l2', '0.1.0', '1.liuqin'
PLUGIN = 'usr/lib64/obs-plugins/liuqin-v4l2.so'
DATA = 'usr/share/obs/obs-plugins/liuqin-v4l2'
LICENSE = 'usr/share/licenses/'+NAME
BUILD_PACKAGES = ('gcc', 'cmake', 'ninja-build', 'pkgconf-pkg-config',
                  'obs-studio-devel', 'simde-devel', 'hedley-devel')


def filename():
    return NAME+'-'+VERSION+'-'+RELEASE+'.aarch64.rpm'


def selected(recipe):
    if 'obs-studio' not in recipe.get('packages', []):
        return False
    if not fedora_ffmpeg.selected(recipe):
        raise ValueError('OBS V4L2 默认构建需要配方同时选择 ffmpeg-free。')
    if {'obs-studio', NAME} & set(recipe.get('remove', [])):
        raise ValueError('OBS 软件包不能同时选择安装和移除。')
    return True


def inputs():
    return dict(recipe=tree_identity(RECIPE), tests={p.name:sha(p) for p in TESTS},
                builder=sha(Path(__file__)), packager=sha(PROJECT/'build/lib/rpm_package.py'))


def regular(path):
    if not path.is_file() or path.resolve() != path.absolute():
        raise ValueError('OBS 输入缺失或含符号链接：'+str(path))
    return path


def payload_names():
    return {PLUGIN, LICENSE+'/source.tar.xz', DATA+'/README.md', DATA+'/COPYING',
            *(DATA+'/'+p.relative_to(RECIPE/'data').as_posix()
              for p in (RECIPE/'data').rglob('*') if p.is_file())}


def plugin_dependencies(path):
    from elftools.elf.elffile import ELFFile
    with regular(path).open('rb') as stream:
        elf = ELFFile(stream)
        if (elf.elfclass != 64 or not elf.little_endian or
                elf['e_machine'] != 'EM_AARCH64' or elf['e_type'] != 'ET_DYN'):
            raise ValueError('OBS 插件必须是 ARM64 ELF 共享库。')
        dynamic = elf.get_section_by_name('.dynamic')
        if dynamic is None:
            raise ValueError('OBS 插件缺少动态依赖。')
        if any(tag.entry.d_tag in ('DT_RPATH', 'DT_RUNPATH') for tag in dynamic.iter_tags()):
            raise ValueError('OBS 插件不能保留构建目录的 RPATH。')
        names = sorted({tag.needed for tag in dynamic.iter_tags('DT_NEEDED')})
    if not names or any(not re.fullmatch(r'[A-Za-z0-9_+.-]+\.so(?:\.[A-Za-z0-9_+.-]+)?', n) for n in names):
        raise ValueError('OBS 插件包含无效共享库依赖。')
    return [n+'()(64bit)' for n in names]


def probe(root, chroot, path='/'+PLUGIN):
    # Resolve all real shared libraries without a display, GPU or OBS session.
    chroot(root, ['python3', '-c',
        'import ctypes as c,os,sys; '
        'm=c.CDLL(sys.argv[1],mode=os.RTLD_NOW); '
        'm.obs_module_ver.restype=m.obs_get_version.restype=c.c_uint32; '
        'assert (m.obs_module_ver() & 0xffff0000)<=m.obs_get_version(); '
        'assert m.obs_module_load and m.obs_module_set_pointer; '
        'print("PASS: OBS module ABI and shared libraries")', path], timeout=30)


def pack(root, work, output, chroot):
    payload = work/'payload'
    plugin = payload/PLUGIN
    dependencies = plugin_dependencies(plugin)
    versions = chroot(root, ['rpm', '-q', '--qf', '%{NAME} >= %{VERSION}\n',
                            'obs-studio-libs', 'glibc'], capture_output=True, text=True).stdout.splitlines()
    if len(versions) != 2 or not all(re.fullmatch(r'(obs-studio-libs|glibc) >= [0-9][A-Za-z0-9.+~^_-]*', v)
                                      for v in versions):
        raise ValueError('OBS 构建依赖版本记录无效。')
    docs = payload/LICENSE
    docs.mkdir(parents=True)
    with tarfile.open(docs/'source.tar.xz', 'w:xz') as archive:
        def normalized(info):
            if not (info.isfile() or info.isdir()):
                raise ValueError('OBS 源码含链接或特殊文件。')
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            return info
        archive.add(work/'source', arcname='source', filter=normalized)
    requires = ['obs-studio', *dependencies, *versions,
                'libavcodec-free >= '+fedora_ffmpeg.release(fedora_ffmpeg.record())]
    files = {p.relative_to(payload).as_posix():sha(p) for p in sorted(payload.rglob('*')) if p.is_file()}
    if set(files) != payload_names():
        raise ValueError('OBS 安装文件集合与独立插件布局不一致。')
    metadata = dict(schema=1, inputs=inputs(), ffmpeg=fedora_ffmpeg.record(),
                    name=NAME, version=VERSION, release=RELEASE, files=files, requires=requires)
    (docs/'BUILD.json').write_text(json.dumps(metadata, indent=2)+'\n')
    output = Path(output)
    result = rpm(payload, NAME, VERSION, output, release=RELEASE, requires=requires,
                 license='GPL-2.0-or-later', summary='Iris V4L2 H.264 and HEVC encoders for official OBS Studio',
                 owned_directories=(DATA, LICENSE))
    manifest = dict(schema=1, package=filename(), sha256=sha(result), build=metadata)
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    inventory = chroot(root, ['rpm', '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
                       capture_output=True).stdout
    (output/'build-packages.tsv').write_bytes(inventory)
    return result


def build(root, ffmpeg_bundle, output, jobs, chroot, dnf):
    if jobs < 1:
        raise ValueError('OBS 构建并行数必须为正整数。')
    identity = inputs()
    fedora_ffmpeg.validate(ffmpeg_bundle)
    work = root/'tmp/liuqin-obs-v4l2'
    source = work/'source'
    (source/'build').mkdir(parents=True)
    shutil.copytree(RECIPE, source/'build/obs-v4l2')
    (source/'tests').mkdir()
    for path in TESTS:
        shutil.copyfile(path, source/'tests'/path.name)
    if (tree_identity(source/'build/obs-v4l2') != identity['recipe'] or
            {p.name:sha(p) for p in (source/'tests').iterdir()} != identity['tests']):
        raise ValueError('OBS 源码在暂存期间变化。')
    staging = work/'rpms'
    staging.mkdir()
    fedora_ffmpeg.stage(ffmpeg_bundle, staging, development=True)
    guest = '/tmp/liuqin-obs-v4l2'
    dnf(root, 'install', *BUILD_PACKAGES, *(guest+'/rpms/'+p.name for p in sorted(staging.iterdir())))
    fedora_ffmpeg.check_installed(root, chroot)
    chroot(root, ['cmake', '-S', guest+'/source/build/obs-v4l2', '-B', guest+'/build', '-G', 'Ninja',
        '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_SKIP_RPATH=ON', '-DBUILD_TESTING=ON',
        '-DOBS_INSTALL_SYSTEM=ON', '-DCMAKE_INSTALL_PREFIX=/usr'])
    chroot(root, ['cmake', '--build', guest+'/build', '--parallel', str(jobs)])
    chroot(root, ['ctest', '--test-dir', guest+'/build', '--output-on-failure'])
    chroot(root, ['env', 'DESTDIR='+guest+'/payload', 'cmake', '--install', guest+'/build'])
    probe(root, chroot, guest+'/payload/'+PLUGIN)
    if inputs() != identity:
        raise ValueError('OBS 源码在构建期间变化，未发布产物。')
    return pack(root, work, output, chroot)


def validate(bundle):
    bundle = Path(bundle).absolute()
    manifest = json.loads(regular(bundle/'manifest.json').read_text())
    build = manifest.get('build', {})
    if (manifest.get('schema') != 1 or manifest.get('package') != filename() or
            build.get('schema') != 1 or build.get('inputs') != inputs() or
            (build.get('name'), build.get('version'), build.get('release')) != (NAME, VERSION, RELEASE) or
            build.get('ffmpeg') != fedora_ffmpeg.record() or set(build.get('files', {})) != payload_names()):
        raise ValueError('OBS 构建包与当前源码或 FFmpeg 配方不一致。')
    path = regular(bundle/filename())
    if sha(path) != manifest.get('sha256'):
        raise ValueError('OBS RPM 摘要不一致。')
    actual = subprocess.check_output(['rpm', '-qp', '--qf', fedora_ffmpeg.QUERY, str(path)], text=True).strip()
    if actual != '\t'.join((NAME, '0:'+VERSION+'-'+RELEASE, 'aarch64', '(none)')):
        raise ValueError('OBS RPM 名称、版本或架构不一致。')
    return manifest


def stage(bundle, staging):
    validate(bundle)
    destination = staging/filename()
    if destination.exists() or destination.is_symlink():
        raise ValueError('OBS RPM 暂存路径重复。')
    shutil.copyfile(Path(bundle)/filename(), destination)


def check_image(root, chroot):
    metadata = json.loads(regular(root/LICENSE/'BUILD.json').read_text())
    if (metadata.get('schema') != 1 or metadata.get('inputs') != inputs() or
            (metadata.get('name'), metadata.get('version'), metadata.get('release')) != (NAME, VERSION, RELEASE) or
            metadata.get('ffmpeg') != fedora_ffmpeg.record() or
            set(metadata.get('files', {})) != payload_names()):
        raise ValueError('镜像 OBS 插件来源记录与配方不一致。')
    for name, digest in metadata['files'].items():
        if sha(regular(root/name)) != digest:
            raise ValueError('镜像 OBS 插件文件摘要不一致：'+name)
    actual = chroot(root, ['rpm', '-q', '--qf', fedora_ffmpeg.QUERY, NAME],
                    capture_output=True, text=True).stdout.strip()
    if actual != '\t'.join((NAME, '0:'+VERSION+'-'+RELEASE, 'aarch64', '(none)')):
        raise ValueError('镜像 OBS 插件 RPM 版本不匹配。')
    plugin_dependencies(root/PLUGIN)
    probe(root, chroot)
