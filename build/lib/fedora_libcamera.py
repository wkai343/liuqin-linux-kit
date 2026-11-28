# SPDX-License-Identifier: MIT
"""Build and install Fedora libcamera with front sensor and autofocus support in an isolated target root."""
import fnmatch
import json
from pathlib import Path
import shutil
import subprocess
import tomllib

from source_build import PROJECT, sha, tree_identity

RECIPE = PROJECT/'build/libcamera'
BUILDER = PROJECT/'build/scripts/build-fedora-libcamera.py'
PACKAGES = ('libcamera', 'libcamera-ipa', 'libcamera-tools', 'libcamera-gstreamer')
EXTRA_PACKAGES = ('libcamera-devel', 'libcamera-qcam', 'libcamera-v4l2', 'python3-libcamera')
LICENSE_DIR = 'usr/share/licenses/liuqin-libcamera'
QUERY = '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\t%{SOURCEPACKAGE}\n'


def record():
    return json.loads((RECIPE/'source.json').read_text())


def selected(recipe):
    return any(name.startswith('libcamera') or name in EXTRA_PACKAGES for name in recipe.get('packages', []))


def requested_extras(recipe):
    return tuple(name for name in EXTRA_PACKAGES if name in recipe.get('packages', []))


def release(info):
    return info['version']+'-'+info['release']+'.'+info['dist']


def filenames(info, packages=PACKAGES):
    return {name:name+'-'+release(info)+'.aarch64.rpm' for name in packages}


def source_filename(info):
    return 'libcamera-'+release(info)+'.src.rpm'


def inputs():
    return dict(recipe=tree_identity(RECIPE), builder=sha(BUILDER))


def regular(path):
    if not path.is_file() or path.resolve() != path.absolute():
        raise ValueError('libcamera 输入缺失或含符号链接：'+str(path))
    return path


def check_rpm(path, name, info, *, source=False):
    regular(path)
    actual = subprocess.check_output(['rpm', '-qp', '--qf', QUERY, str(path)], text=True).strip()
    expected = '\t'.join((name, '0:'+release(info), 'aarch64', '1' if source else '(none)'))
    if actual != expected:
        raise ValueError('libcamera RPM 名称、版本或架构不匹配：'+str(path))


def collect(rpmbuild, output):
    """Keep runtime and matching development RPMs in separate payload groups."""
    info = record()
    source = Path(rpmbuild)/'SRPMS'/source_filename(info)
    groups = {'rpms': PACKAGES, 'extra-rpms': EXTRA_PACKAGES}
    for packages in groups.values():
        for name, file in filenames(info, packages).items():
            check_rpm(Path(rpmbuild)/'RPMS/aarch64'/file, name, info)
    check_rpm(source, 'libcamera', info, source=True)
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
        raise ValueError('libcamera 构建包与当前配方或补丁不一致。')
    for directory, packages in {'rpms':PACKAGES, 'extra-rpms':EXTRA_PACKAGES}.items():
        expected = set(filenames(info, packages).values())
        if (set(manifest.get(directory, {})) != expected or
                {p.name for p in (bundle/directory).iterdir()} != expected):
            raise ValueError('libcamera 运行库或开发包集合不完整或包含额外文件。')
        for name, file in filenames(info, packages).items():
            path = regular(bundle/directory/file)
            if sha(path) != manifest[directory][file]:
                raise ValueError('libcamera RPM 摘要不一致：'+file)
            check_rpm(path, name, info)
    src = manifest.get('srpm', {})
    if src.get('file') != source_filename(info):
        raise ValueError('libcamera 对应源码包缺失。')
    path = regular(bundle/src['file'])
    if sha(path) != src.get('sha256'):
        raise ValueError('libcamera 源码包摘要不一致。')
    check_rpm(path, 'libcamera', info, source=True)
    return manifest


def build(root, archive, output, jobs, chroot, dnf):
    info = record()
    identity = inputs()
    if jobs < 1 or sha(regular(Path(archive))) != info['sha256']:
        raise ValueError('libcamera 源码校验失败或构建并行数无效。')
    work = root/'tmp/liuqin-libcamera-build'
    (work/'build/scripts').mkdir(parents=True)
    shutil.copytree(RECIPE, work/'build/libcamera')
    shutil.copyfile(BUILDER, work/'build/scripts'/BUILDER.name)
    shutil.copyfile(archive, work/info['srpm'])
    if (tree_identity(work/'build/libcamera') != identity['recipe'] or
            sha(work/'build/scripts'/BUILDER.name) != identity['builder']):
        raise ValueError('libcamera 配方在暂存期间变化。')
    dnf(root, 'install', 'rpm-build', 'redhat-rpm-config', 'gpgverify', 'cpio', 'dnf5-plugins')
    guest = '/tmp/liuqin-libcamera-build'
    chroot(root, ['python3', guest+'/build/scripts/'+BUILDER.name,
        '--prepare-only', '--srpm', guest+'/'+info['srpm'], '--out', guest+'/rpmbuild'])
    spec = guest+'/rpmbuild/SPECS/libcamera.spec'
    dnf(root, 'builddep', spec)
    # Fedora byte-compiles the Python bindings in parallel at RPM install time.
    # POSIX semaphores live in the build chroot's private /dev tmpfs.
    shm = root/'dev/shm'
    if not shm.exists():
        shm.mkdir()
        shm.chmod(0o1777)
    chroot(root, ['rpmbuild', '-ba', '--define', '_topdir '+guest+'/rpmbuild',
        '--define', '_smp_mflags -j'+str(jobs), spec])
    if inputs() != identity:
        raise ValueError('libcamera 配方在编译期间变化，未发布产物。')
    collect(work/'rpmbuild', output)
    inventory = chroot(root, ['rpm', '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],
                       capture_output=True).stdout
    (Path(output)/'build-packages.tsv').write_bytes(inventory)


def stage(bundle, staging, *, development=False, extras=()):
    """Validate before including libcamera in the desktop's single RPM transaction."""
    manifest = validate(bundle)
    names = EXTRA_PACKAGES if development else extras
    if not set(names).issubset(EXTRA_PACKAGES):
        raise ValueError('Unknown libcamera subpackage')
    for directory, files in [('rpms', manifest['rpms']), ('extra-rpms', filenames(record(), names).values())]:
        for file in files:
            target = staging/file
            if target.exists() or target.is_symlink():
                raise ValueError('libcamera RPM 暂存路径重复：'+file)
            shutil.copyfile(Path(bundle)/directory/file, target)


def check_installed(root, chroot, extras=()):
    info = record()
    actual = chroot(root, ['rpm', '-q', '--qf', QUERY, *PACKAGES, *extras],
                    capture_output=True, text=True).stdout.splitlines()
    expected = ['\t'.join((name, '0:'+release(info), 'aarch64', '(none)')) for name in (*PACKAGES, *extras)]
    if sorted(actual) != sorted(expected):
        raise ValueError('镜像未安装完整的 libcamera 修订版运行库。')
    for name in ('usr/bin/cam', 'usr/lib64/libcamera.so.0.7.2',
                 'usr/lib64/gstreamer-1.0/libgstlibcamera.so',
                 'usr/lib64/libcamera/ipa/ipa_soft_simple.so'):
        if not (root/name).is_file():
            raise ValueError('libcamera 运行组件缺失：'+name)
    chroot(root, ['cam', '--version'], capture_output=True, timeout=30)


def check_locks(root, extras=()):
    config = regular(root/'etc/dnf/versionlock.toml')
    locks = tomllib.loads(config.read_text())
    if locks.get('version') != '1.0':
        raise ValueError('镜像 libcamera 版本锁格式不受支持。')
    condition = [dict(key='evr', comparator='=', value=release(record()))]
    for name in (*PACKAGES, *extras):
        entries = [p for p in locks.get('packages', []) if fnmatch.fnmatchcase(name,p.get('name',''))]
        if len(entries) != 1 or entries[0].get('name') != name or entries[0].get('conditions') != condition:
            raise ValueError('镜像 libcamera 版本锁不完整或存在冲突：'+name)


def configure(root, bundle, chroot, extras=()):
    manifest = validate(bundle)
    check_installed(root, chroot, extras)
    # Preserve unrelated locks; DNF resolves these names against installed RPMs.
    chroot(root, ['dnf', '-y', '--disable-repo=*', 'versionlock', 'add', *PACKAGES, *extras])
    check_locks(root, extras)
    docs = root/LICENSE_DIR
    if docs.exists() or docs.is_symlink():
        raise ValueError('libcamera 源码保留目录已存在：'+str(docs))
    docs.mkdir(parents=True)
    shutil.copyfile(Path(bundle)/manifest['srpm']['file'], docs/manifest['srpm']['file'])
    (docs/'BUILD.json').write_text(json.dumps(dict(manifest, installed_extras=list(extras)), indent=2)+'\n')
    shutil.copytree(RECIPE, docs/'recipe')
    shutil.copyfile(BUILDER, docs/BUILDER.name)


def check_image(root, chroot):
    """Recheck the final kernel/rootfs assembly without a GPU or video device."""
    docs = root/LICENSE_DIR
    manifest = json.loads(regular(docs/'BUILD.json').read_text())
    extras = manifest.get('installed_extras', [])
    if not isinstance(extras, list) or not set(extras).issubset(EXTRA_PACKAGES):
        raise ValueError('Unknown installed libcamera subpackage')
    check_installed(root, chroot, extras)
    check_locks(root, extras)
    if manifest.get('inputs') != inputs() or manifest.get('source') != record():
        raise ValueError('镜像 libcamera 来源记录与配方不一致。')
    src = manifest.get('srpm', {})
    if src.get('file') != source_filename(record()) or sha(regular(docs/src['file'])) != src.get('sha256'):
        raise ValueError('镜像 libcamera 对应源码不完整。')
