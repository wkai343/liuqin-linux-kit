#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run real ARM64 Rec tools in a disposable user/mount/PID namespace via QEMU.

Only newly created regular disk files are formatted. No tablet or host block
nodes are exposed. Run with Devbox; no sudo or global binfmt registration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from source_build import arm64_registration

APPLETS = '''awk base64 basename blockdev cat chmod chown chroot cp cttyhack cut date dd
 dirname dmesg du echo find findfs grep gzip head hostname id ip kill
 killall ln losetup ls mkdir mknod mount mv poweroff readlink reboot rm rmdir sed
 setsid sh sha256sum sleep sort stat switch_root sync tail tar test touch tr
 truncate udhcpd umount uname uniq wc wget xargs mdev telnetd
 chvt insmod rmmod modprobe od realpath timeout true'''.split()


def run(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args}: exit {result.returncode}\n{result.stdout[-2000:]}{result.stderr[-2000:]}')
    return result.stdout


def check(runtime, baseline=None):
    manifest = json.loads((runtime / 'runtime-files.json').read_text())
    for name, expected in manifest.items():
        if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != expected:
            raise ValueError('runtime hash mismatch: ' + name)
    with tempfile.TemporaryDirectory(prefix='liuqin-musl-test-') as temporary:
        work = Path(temporary); root = work / 'root'
        shutil.copytree(runtime, root)
        for directory in ('proc', 'dev', 'tmp', 'bin', 'fixtures', 'archive-in', 'archive-out'):
            (root / directory).mkdir(exist_ok=True)
        binfmt = work / 'binfmt'; binfmt.mkdir()
        subprocess.run(['mount', '-t', 'binfmt_misc', 'binfmt_misc', str(binfmt)], check=True)
        (binfmt / 'register').write_bytes(arm64_registration('liuqin-runtime-test', shutil.which('qemu-aarch64')))
        subprocess.run(['mount', '-t', 'proc', 'proc', str(root / 'proc')], check=True)
        for name in ('null', 'zero', 'random', 'urandom'):
            (root / 'dev' / name).touch()
            subprocess.run(['mount', '--bind', '/dev/' + name, str(root / 'dev' / name)], check=True)
        try:
            chroot = lambda *argv: run(['chroot', str(root), *argv],
                env=dict(os.environ, PATH='/bin:/usr/bin:/usr/sbin:/sbin', LC_ALL='C',
                         PYTHONDONTWRITEBYTECODE='1'))
            applets = set(chroot('/bin/busybox', '--list').split())
            missing = set(APPLETS) - applets
            if missing:
                raise ValueError('missing BusyBox applets: ' + ', '.join(sorted(missing)))
            for name in APPLETS:
                (root / 'bin' / name).symlink_to('busybox')
            code = '''import array,bz2,concurrent.futures,ctypes,decimal,fcntl,gzip,hashlib,ipaddress,json,lzma,mmap,multiprocessing,select,socket,ssl,struct,tarfile,tempfile,termios,urllib.request,uuid,xml.etree.ElementTree,zlib
from compression import zstd
for module in (bz2,gzip,lzma,zlib,zstd):
    assert module.decompress(module.compress(b'rec-runtime'*100)) == b'rec-runtime'*100
assert hashlib.sha256(b'test').hexdigest() == '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08'
print(ssl.OPENSSL_VERSION)
'''
            python_result = chroot('/usr/bin/python3', '-I', '-B', '-c', code)
            # Import the actual installer/guard/UI services, without calling main.
            scripts = root / 'usr/lib/liuqin'; scripts.mkdir(parents=True, exist_ok=True)
            for directory in ('shared', 'device/installer', 'device/recovery'):
                for source in (PROJECT / directory).glob('*'):
                    if source.suffix in ('.py', '.json'):
                        shutil.copyfile(source, scripts / source.name)
            modules = ['android-device', 'native-status', 'native-install', 'native-touch',
                       'native-ui-service', 'native-bcb', 'native-power', 'native-root-guard']
            chroot('/usr/bin/python3', '-B', '-c',
                   "import sys,runpy;sys.path.insert(0,'/usr/lib/liuqin');" +
                   ';'.join("runpy.run_path('/usr/lib/liuqin/" + name + ".py',run_name='runtime_smoke')" for name in modules))
            versions = {}
            for name, size in (('ext4', 64), ('btrfs', 128), ('f2fs', 256), ('gpt', 64)):
                with (root / 'fixtures' / name).open('wb') as stream:
                    stream.truncate(size * 1024**2)
            chroot('/usr/sbin/mkfs.ext4', '-q', '-F', '-L', 'rec-fixture', '/fixtures/ext4')
            chroot('/usr/sbin/e2fsck', '-fn', '/fixtures/ext4')
            assert chroot('/usr/sbin/e2label', '/fixtures/ext4').strip() == 'rec-fixture'
            chroot('/usr/sbin/mkfs.btrfs', '-q', '-f', '/fixtures/btrfs')
            chroot('/usr/bin/btrfs', 'check', '--readonly', '/fixtures/btrfs')
            chroot('/usr/sbin/mkfs.f2fs', '-f', '/fixtures/f2fs')
            chroot('/usr/sbin/sgdisk', '-o', '-n', '1:2048:4095', '-c', '1:rec-fixture', '/fixtures/gpt')
            assert 'rec-fixture' in chroot('/usr/sbin/sgdisk', '-i', '1', '/fixtures/gpt')
            # Same /proc/self/fd route used by the scoped partition editor.
            chroot('/usr/bin/python3', '-I', '-B', '-c',
                   "import os,subprocess;fd=os.open('/fixtures/gpt',os.O_RDONLY);"
                   "subprocess.run(['/usr/sbin/sgdisk','-p','/proc/self/fd/'+str(fd)],pass_fds=(fd,),check=True)")
            fixture = root / 'archive-in/file'; fixture.write_text('metadata fixture\n'); fixture.chmod(0o751)
            assert chroot('/bin/stat', '-c', '%u:%g:%s', '/archive-in/file').strip() == '0:0:17'
            assert chroot('/bin/stat', '-f', '-c', '%T', '/').strip()
            chroot('/usr/bin/install', '-m', '0640', '/archive-in/file', '/fixtures/installed')
            assert (root / 'fixtures/installed').stat().st_mode & 0o777 == 0o640
            os.setxattr(fixture, 'user.liuqin-test', b'preserved')
            run(['setfacl', '-m', 'u:0:r--', str(fixture)])
            run(['setcap', 'cap_net_raw=ep', str(fixture)])
            (root / 'archive-in/link').symlink_to('file')
            os.link(fixture, root / 'archive-in/hardlink')
            # Archive creation uses host GNU tar; extraction uses the ARM64 tool.
            run(['tar', '--numeric-owner', '--acls', '--xattrs', '--xattrs-include=*',
                 '-czf', str(root / 'fixtures/metadata.tar.gz'), '-C', str(root / 'archive-in'), '.'])
            chroot('/usr/bin/tar', '-xzf', '/fixtures/metadata.tar.gz', '-C', '/archive-out',
                   '--numeric-owner', '--same-owner', '--same-permissions', '--acls', '--xattrs',
                   '--xattrs-include=*', '--warning=no-timestamp')
            restored = root / 'archive-out/file'
            assert restored.read_bytes() == fixture.read_bytes()
            assert restored.stat().st_mode == fixture.stat().st_mode
            assert restored.stat().st_uid == fixture.stat().st_uid
            assert restored.stat().st_gid == fixture.stat().st_gid
            for name in ('user.liuqin-test', 'system.posix_acl_access', 'security.capability'):
                assert os.getxattr(restored, name) == os.getxattr(fixture, name), name
            assert (root / 'archive-out/link').readlink() == Path('file')
            assert restored.stat().st_ino == (root / 'archive-out/hardlink').stat().st_ino
            assert 'cap_net_raw=ep' in chroot('/usr/sbin/getcap', '/archive-out/file')
            versions['tar'] = chroot('/usr/bin/tar', '--version').splitlines()[0]
            versions['python'] = chroot('/usr/bin/python3', '--version').strip()
            versions['btrfs'] = chroot('/usr/bin/btrfs', '--version').strip()
            versions['sgdisk'] = chroot('/usr/sbin/sgdisk', '--version').strip()
            compatibility = None
            if baseline:
                previous = json.loads((baseline / 'runtime-files.json').read_text())
                for name, digest in previous.items():
                    assert hashlib.sha256((baseline / name).read_bytes()).hexdigest() == digest
                shutil.copytree(baseline, root / 'previous')
                loader = '/previous/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1'
                legacy = lambda program, *argv: chroot(loader, '--library-path',
                    '/previous/lib/aarch64-linux-gnu', '/previous/' + program, *argv)
                for name, size in (('ext4-previous', 64), ('btrfs-previous', 128)):
                    with (root / 'fixtures' / name).open('wb') as stream:
                        stream.truncate(size * 1024**2)
                # Use the original ext4 defaults, not the new root's config.
                chroot('/bin/busybox', 'env', 'MKE2FS_CONFIG=/previous/etc/mke2fs.conf',
                       loader, '--library-path', '/previous/lib/aarch64-linux-gnu',
                       '/previous/usr/sbin/mkfs.ext4', '-q', '-F', '/fixtures/ext4-previous')
                chroot('/usr/sbin/e2fsck', '-fn', '/fixtures/ext4-previous')
                legacy('usr/sbin/mkfs.btrfs', '-q', '-f', '/fixtures/btrfs-previous')
                chroot('/usr/bin/btrfs', 'check', '--readonly', '/fixtures/btrfs-previous')
                compatibility = dict(status='PASS', checks=['previous ext4 defaults', 'previous Btrfs defaults'],
                    previous_runtime_sha256=hashlib.sha256((baseline / 'runtime-files.json').read_bytes()).hexdigest())
            return dict(status='PASS', runtime_files_sha256=hashlib.sha256((runtime / 'runtime-files.json').read_bytes()).hexdigest(),
                        checks=['ELF execution', 'BusyBox applets', 'Python stdlib and services',
                                'ext4', 'Btrfs', 'F2FS', 'GPT and inherited FD',
                                'tar ownership/mode/ACL/xattrs/capabilities/links'], versions=versions,
                        openssl=python_result.strip(), previous_filesystems=compatibility, device_access=False)
        finally:
            for name in ('urandom', 'random', 'zero', 'null'):
                subprocess.run(['umount', str(root / 'dev' / name)], check=True)
            subprocess.run(['umount', str(root / 'proc')], check=True)
            subprocess.run(['umount', str(binfmt)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--baseline', type=Path, help='Optional previous glibc runtime for filesystem compatibility checks')
    args = parser.parse_args()
    if os.environ.get('LIUQIN_RUNTIME_TEST_NS') != '1':
        subprocess.run(['unshare', '-Urmpf', '--mount-proc', sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                       env=dict(os.environ, LIUQIN_RUNTIME_TEST_NS='1'), check=True)
    else:
        result = check(args.runtime.resolve(), args.baseline.resolve() if args.baseline else None)
        if args.report:
            args.report.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
