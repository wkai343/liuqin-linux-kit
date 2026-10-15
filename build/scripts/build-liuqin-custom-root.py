#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package a customized, device-adapted ARM64 systemd root (no desktop or Snap requirement)."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared'))
from liuqin_artifacts import contract_hashes, inspect_root
from liuqin_kernel import build_contract, extract_modules, ROOT_MARKER


@contextlib.contextmanager
def build_step(label):
    started = time.monotonic()
    print('rootfs：' + label + '……', flush=True)
    yield
    print(f'rootfs：{label}完成，耗时 {time.monotonic() - started:.1f} 秒。', flush=True)


def label_selinux_root(root):
    """Validate and look up target labels without setting host filesystem xattrs."""
    root = Path(root).resolve()
    if root == Path('/'):
        raise ValueError('不能对主机根目录执行镜像标记。')
    contexts = root / 'etc/selinux/targeted/contexts/files/file_contexts'
    for path in (contexts, root/'usr/sbin/setfiles', root/'usr/sbin/matchpathcon', root/'usr/bin/xargs'):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Fedora SELinux 标记组件缺失或越界：' + str(path))
    policies = []
    for path in (root / 'etc/selinux/targeted/policy').glob('policy.*'):
        version = re.fullmatch(r'policy\.([0-9]+)', path.name)
        if version:
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Fedora SELinux 策略不是普通目标文件：' + str(path))
            policies.append((int(version[1]), path))
    if not policies:
        raise ValueError('Fedora SELinux 编译策略缺失。')
    policy = max(policies)[1]
    # A bind mount below the image could expose host files to traversal.
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        mount = Path(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4]))
        if mount == root or root in mount.parents:
            raise ValueError('SELinux 标记前需卸载目标中的挂载：' + str(mount))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
    from source_build import arm64_execution
    paths = [Path('.')]
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        paths.extend((Path(directory)/name).relative_to(root) for name in sorted(dirs + files))
    # Validate every matching context against the compiled target policy.
    # -n is essential: the host kernel need not know target-only types.
    # xargs uses NUL-delimited paths, and -n on matchpathcon emits only labels,
    # so whitespace/newlines in filenames cannot corrupt the association.
    with arm64_execution():
        subprocess.run(['chroot', str(root), '/usr/sbin/setfiles', '-n', '-F', '-m',
                        '-c', '/' + policy.relative_to(root).as_posix(),
                        '/' + contexts.relative_to(root).as_posix(), '/'], check=True)
        result = subprocess.run(['chroot', str(root), '/usr/bin/xargs', '-0', '-r',
                                 '/usr/sbin/matchpathcon', '-N', '-n', '-f',
                                 '/' + contexts.relative_to(root).as_posix()],
                                input=b''.join(os.fsencode('/' if p == Path('.') else '/'+p.as_posix()) + b'\0'
                                               for p in paths), stdout=subprocess.PIPE, check=True)
    values = result.stdout.decode('ascii').splitlines()
    if len(values) != len(paths):
        raise ValueError('SELinux 目标标签数量与根目录条目不一致。')
    labels = {}
    for path, value in zip(paths, values):
        if value == '<<none>>':
            labels[path.as_posix()] = None
        elif re.fullmatch(r'[A-Za-z0-9_]+:[A-Za-z0-9_]+:[A-Za-z0-9_]+:[A-Za-z0-9_.,:-]+', value):
            labels[path.as_posix()] = value
        else:
            raise ValueError('SELinux 目标工具返回无效标签：' + path.as_posix())
    return labels


def pack_labeled_root(root, archive, labels):
    """Stream GNU tar metadata through PAX label injection into one gzip pass."""
    root, archive = Path(root).resolve(), Path(archive).resolve()
    if root == Path('/') or root in archive.parents or archive.exists() or archive.is_symlink():
        raise ValueError('SELinux 根归档需要根目录以外的新输出文件。')
    # GNU tar retains binary xattrs, capabilities and ACLs. Replace only
    # SELinux metadata; inherited host labels must never enter the image.
    command = ['tar', '--create', '--file=-', '--directory='+str(root), '--format=pax',
               '--numeric-owner', '--acls', '--xattrs', '--xattrs-include=*',
               '--xattrs-exclude=security.selinux', '--sort=name',
               '--pax-option=delete=atime,delete=ctime', '.']
    compressor = ['pigz', '-1', '-n', '-p', str(min(os.cpu_count() or 1, 8))] if shutil.which('pigz') else ['gzip', '-1', '-n']
    print('rootfs：压缩带 SELinux 标签的归档（'+' '.join(compressor)+'）……', flush=True)
    fd, temporary = tempfile.mkstemp(prefix=archive.name+'.', dir=archive.parent)
    temporary = Path(temporary)
    producer = consumer = None
    try:
        with os.fdopen(fd, 'wb') as output:
            producer = subprocess.Popen(command, stdout=subprocess.PIPE)
            consumer = subprocess.Popen(compressor, stdin=subprocess.PIPE, stdout=output)
            seen, hardlinks = set(), []
            with tarfile.open(fileobj=producer.stdout, mode='r|', bufsize=1024*1024) as source, \
                 tarfile.open(fileobj=consumer.stdin, mode='w|', format=tarfile.PAX_FORMAT,
                              copybufsize=1024*1024, bufsize=1024*1024) as target:
                for entry in source:
                    name = Path(entry.name).as_posix()
                    if name not in labels or name in seen:
                        raise ValueError('归档条目缺少唯一的 SELinux 标签记录：'+name)
                    seen.add(name)
                    # Never retain the host's label, including GNU --selinux's
                    # alternate PAX representation on older input trees.
                    entry.pax_headers.pop('SCHILY.xattr.security.selinux', None)
                    entry.pax_headers.pop('RHT.security.selinux', None)
                    if labels[name] is not None:
                        entry.pax_headers['SCHILY.xattr.security.selinux'] = labels[name]+'\0'
                    if entry.islnk():
                        hardlinks.append((name, Path(entry.linkname).as_posix()))
                    target.addfile(entry, source.extractfile(entry) if entry.isfile() else None)
            if seen != set(labels):
                raise ValueError('生成标签后根目录条目发生变化，未发布归档。')
            for name, link in hardlinks:
                if link not in labels or labels[name] != labels[link]:
                    raise ValueError('硬链接的 SELinux 标签不一致：'+name)
            producer.stdout.close(); consumer.stdin.close()
            for process, argv in ((producer, command), (consumer, compressor)):
                if process.wait():
                    raise subprocess.CalledProcessError(process.returncode, argv)
            output.flush(); os.fsync(output.fileno())
        temporary.chmod(0o644)
        os.link(temporary, archive)
    finally:
        for process in (producer, consumer):
            if process is not None and process.poll() is None:
                process.terminate(); process.wait()
        for stream in (producer.stdout if producer else None, consumer.stdin if consumer else None):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        temporary.unlink(missing_ok=True)


def main():
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Already customized root with the liuqin device layer installed')
    parser.add_argument('--kernel-out', type=Path, required=True)
    parser.add_argument('--modules-dir', type=Path, required=True, help='Output of build-liuqin-kernel-modules.sh')
    parser.add_argument('--out', type=Path, default=project / 'out/custom-root')
    parser.add_argument('--selinux-labels',action='store_true',help='apply target Fedora file contexts before archiving')
    args = parser.parse_args()
    root, kernel, modules, out = (p.resolve() for p in (args.root, args.kernel_out, args.modules_dir, args.out))
    if os.geteuid() != 0:
        parser.error('run as root to preserve ownership, ACLs and capabilities')
    if root == Path('/') or not root.is_dir() or project / 'out' not in out.parents or out.exists() or root in out.parents:
        parser.error('use an existing input root and a fresh output below the project out directory')
    if (root / 'etc/liuqin/boot-updates.json').exists() or (root / 'var/lib/liuqin/boot-updates').exists():
        parser.error('root contains private boot-update state; use a clean uninstalled root template')
    release = (kernel / 'include/config/kernel.release').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9._+-]+', release) or (modules / 'kernel.release').read_text().strip() != release:
        parser.error('kernel and module package release differ')
    identity = json.loads((kernel / 'build-info.json').read_text())
    if (modules / 'kernel.commit').read_text().strip() != identity['commit']:
        parser.error('module package and kernel source commit differ')
    subprocess.run(['sha256sum', '-c', '--quiet', 'SHA256SUMS'], cwd=kernel, check=True)
    module_identity = json.loads((modules / 'kernel.build.json').read_text())
    versioned_contract = build_contract(kernel, modules)
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    expected = dict(commit=identity['commit'], release=release,
                    image_sha256=sha(kernel / 'arch/arm64/boot/Image'), config_sha256=sha(kernel / '.config'),
                    archive_sha256=sha(modules / 'modules.tar'), manifest_sha256=sha(modules / 'modules.manifest'))
    if module_identity != expected or identity['config_sha256'] != expected['config_sha256']:
        parser.error('module package was built for a different kernel Image or configuration')
    module_records = {}
    for row in (modules / 'modules.manifest').read_text().splitlines():
        digest, separator, name = row.partition('  ')
        if not separator or not re.fullmatch(r'[0-9a-f]{64}', digest) or not name.startswith('usr/lib/modules/' + release + '/') or '..' in Path(name).parts:
            parser.error('invalid module package manifest')
        module_records[name] = digest
    # This file was produced locally by the module builder. Verify before extraction.
    with tarfile.open(modules / 'modules.tar') as archive:
        found = {}
        for item in archive:
            if item.name.startswith('/') or '..' in Path(item.name).parts or item.name in found:
                parser.error('module package contains an unsafe or duplicate path')
            if item.isdir():
                if item.name != 'usr/lib/modules' and not item.name.startswith('usr/lib/modules/' + release):
                    parser.error('module package directory outside the selected release')
                continue
            if not item.isfile() or item.name not in module_records:
                parser.error('module package has an unexpected file or link')
            found[item.name] = hashlib.sha256(archive.extractfile(item).read()).hexdigest()
        if found != module_records:
            parser.error('module package hashes differ from its manifest')
    out.mkdir(parents=True)
    target = out / 'rootfs'
    with build_step('准备独立打包目录（优先写时复制）'):
        subprocess.run(['cp', '-a', '--reflink=auto', str(root), str(target)], check=True)
    for name in ('etc', 'usr', 'usr/lib', 'usr/share', 'usr/share/liuqin'):
        path = target / name
        if path.is_symlink() or not path.resolve().is_relative_to(target):
            parser.error('mutable build path must stay inside root: ' + name)
    # Preserve other kernel releases for rollback.
    module_root = target / 'usr/lib/modules'
    if module_root.is_symlink():
        parser.error('usr/lib/modules must be a directory, not a symlink')
    current_modules = module_root / release
    if current_modules.is_symlink():
        parser.error('kernel release directory must not be a symlink')
    if current_modules.exists():
        shutil.rmtree(current_modules)
    with build_step('装入并校验内核模块'):
        verified_manifest = extract_modules(modules, target, versioned_contract)
    share = target / 'usr/share/liuqin'
    share.mkdir(parents=True, exist_ok=True)
    marker = target / 'etc/liuqin-native-root'
    for path in (marker,):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            parser.error('output interface must be a regular file: ' + str(path))
    marker.write_text('liuqin-native-root-v1\n')
    for path in (marker,):
        path.chmod(0o644)
        os.chown(path, 0, 0)
    snap = target / 'usr/lib/snapd/snap-confine'
    if snap.is_file() and not subprocess.check_output(['getcap', str(snap)]).strip():
        parser.error('retained snap-confine has lost its capability')
    destination = share
    for part in ('kernels', release):
        destination = destination / part
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            parser.error('versioned kernel directory must stay inside root')
        destination.mkdir(exist_ok=True)
    for path, data in ((destination / 'boot.contract', versioned_contract),
                       (destination / 'modules.manifest', verified_manifest),
                       (target / 'etc/liuqin-root-interface', ROOT_MARKER)):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            parser.error('kernel interface must be a regular file')
        path.write_bytes(data); path.chmod(0o644); os.chown(path, 0, 0)
    admission = out / 'native-root.contract'
    admission.write_bytes(versioned_contract)
    if args.selinux_labels:
        # Include the newly generated root interface and per-release
        # metadata in the same final labeling pass.
        with build_step('生成并校验 Fedora SELinux 归档标签（ARM64 目标工具）'):
            labels = label_selinux_root(target)
    archive = out / 'rootfs.tar.gz'
    if args.selinux_labels:
        with build_step('压缩并写入 SELinux 归档标签'):
            pack_labeled_root(target, archive, labels)
    else:
        subprocess.run(['sh', str(project / 'build/lib/rootfs-archive.sh'), 'pack', str(target), str(archive)], check=True)
    with build_step('检查最终根归档与内核接口'):
        inspect_root(archive, contract_hashes(versioned_contract), profile='native-updatable')
    print('Updatable root complete: ' + str(archive))
    return


if __name__ == '__main__':
    main()
