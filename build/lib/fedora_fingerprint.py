# SPDX-License-Identifier: MIT
"""Build the Liuqin OEM fingerprint stack against Fedora, in an isolated root."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

PROJECT=Path(__file__).resolve().parents[2]
OEM=PROJECT/'device/fingerprint/oem'
PREFIX='/usr/local/lib/liuqin-fpc-oem'
BUILD_PACKAGES=['bash','coreutils','findutils','tar','gzip','rpm','gcc','gcc-c++','glibc-static','make',
    'meson','ninja-build','pkgconf-pkg-config','glib2-devel','libgusb-devel','libgudev-devel',
    'polkit-devel','systemd-devel','dbus-devel','gettext','python3','openssl-devel']
RUNTIME_PACKAGES=['fprintd','fprintd-pam','glib2','libgusb','libgudev','openssl-libs','polkit-libs','systemd-libs',
    'python3','kmod','util-linux','policycoreutils','authselect','selinux-policy-targeted']


def extract_source(archive, expected, destination):
    archive=Path(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
        raise ValueError('指纹源码校验失败：'+archive.name)
    with tarfile.open(archive) as tar:
        roots=set()
        for item in tar.getmembers():
            parts=Path(item.name).parts
            if not parts or Path(item.name).is_absolute() or '..' in parts or not (item.isfile() or item.isdir()):
                raise ValueError('指纹源码归档含特殊或不安全文件。')
            roots.add(parts[0])
        if len(roots)!=1:raise ValueError('指纹源码归档应只有一个根目录。')
        tar.extractall(destination,filter='data')
    return destination/roots.pop()


def build(root, archives, output, jobs, chroot, dnf):
    work=root/'tmp/liuqin-fingerprint-build';work.mkdir()
    lock=json.loads((OEM/'SOURCE_LOCK.json').read_text())
    lib=extract_source(archives['libfprint'],lock['libfprint']['orig_sha256'],work)
    daemon=extract_source(archives['fprintd'],lock['fprintd']['orig_sha256'],work)
    shutil.copytree(OEM,work/'oem')
    spec=importlib.util.spec_from_file_location('fpc_prepare',OEM/'src/fingerprint/fprintd-oem/prepare.py')
    prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)
    for name,digest in json.loads((OEM/'FPRINTD_SOURCE_HASHES.json').read_text()).items():
        if hashlib.sha256((daemon/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('fprintd 补丁输入不一致：'+name)
    prepare.prepare(daemon,work/'fprintd-source',work/'fprintd-oem-update.patch')
    dnf(root,'install',*BUILD_PACKAGES)
    guest='/tmp/liuqin-fingerprint-build'
    chroot(root,['meson','setup',guest+'/lib-build',guest+'/'+lib.name,
        '--prefix='+PREFIX,'--libdir=lib','--buildtype=release','-Ddrivers=virtual_image',
        '-Dtod=true','-Dintrospection=false','-Ddoc=false','-Dgtk-examples=false',
        '-Dinstalled-tests=false','-Dudev_rules=disabled','-Dudev_hwdb=disabled'])
    chroot(root,['ninja','-C',guest+'/lib-build','-j',str(jobs)])
    chroot(root,['meson','install','-C',guest+'/lib-build'])
    env=['env','PKG_CONFIG_PATH='+PREFIX+'/lib/pkgconfig','LD_LIBRARY_PATH='+PREFIX+'/lib',
         'LDFLAGS=-Wl,-rpath,'+PREFIX+'/lib','CFLAGS=-O2',
         'OUT_DIR='+guest+'/native','FPRINTD_SOURCE='+guest+'/'+daemon.name]
    chroot(root,[*env,'bash',guest+'/oem/tools/build-tod.sh'])
    chroot(root,[*env,'bash',guest+'/oem/tools/build-fprintd-oem.sh'])
    chroot(root,['env','CC=gcc','OUT_DIR='+guest+'/clients','bash',guest+'/oem/tools/build-userspace.sh','--with-rpmb-provider'])
    return stage(root,work,output)


def stage(root,work,output):
    payload=Path(output)/'payload';runtime=payload/PREFIX.lstrip('/');runtime.mkdir(parents=True)
    libdir=runtime/'lib';libdir.mkdir()
    for source in (root/PREFIX.lstrip('/')/'lib').glob('*.so*'):
        shutil.copy2(source,libdir/source.name,follow_symlinks=False)
    for name in ['fprintd','fpc-oem-print']:
        shutil.copy2(work/'native'/name,runtime/name)
    tod=runtime/'tod';tod.mkdir();shutil.copy2(work/'native/libfprint-tod-fpc1264-oem.so',tod)
    for name in ['fpc_build_info','qsee-gatekeeper','qsee-supplicant','qsee-app-loader']:
        shutil.copy2(work/'clients'/name,runtime/name)
    shutil.copytree(work/'clients/ufs-rpmb-provider',runtime/'ufs-rpmb-provider')
    for path in (OEM/'src/fingerprint/oem').glob('*.py'):shutil.copy2(path,runtime/path.name)
    for name in ['stable_module.py','provision_firmware.py']:
        shutil.copy2(OEM/'src/fingerprint/deployment'/name,runtime/name)
    shutil.copy2(PROJECT/'shared/liuqin_kernel.py',runtime/'liuqin_kernel.py')
    firmware=runtime/'firmware';firmware.mkdir()
    for name in ['SHA256.json','NOTICE']:shutil.copy2(OEM/'firmware'/name,firmware/name)
    candidate=dict(schema=1,id='liuqin-fpc-oem-fedora-v1',deployment_mode='installed',
        enrollment_interface='fprintd',account_profile='primary-uid-1000',linux_uid=1000,
        native_uid=0x600003e8,kernel_profile='liuqin-root-v2',display_service='display-manager',
        firmware_source='modem_a-read-only',program_path=PREFIX,supports_one_finger_per_user=True)
    (runtime/'CANDIDATE.json').write_text(json.dumps(candidate,indent=2)+'\n')
    for path in runtime.rglob('*'):
        if path.is_symlink():continue
        path.chmod(0o700 if path.is_dir() or path.suffix=='.py' or path.stat().st_mode&0o111 else 0o600)
    runtime.chmod(0o711)
    docs=payload/'usr/share/licenses/liuqin-fingerprint';docs.mkdir(parents=True)
    shutil.copytree(OEM,docs/'oem-source')
    for name in ['LICENSE','NOTICE','CREDITS.md']:shutil.copy2(PROJECT/name,docs/name)
    for name in ['libfprint','fprintd']:
        record=json.loads((OEM/'SOURCE_LOCK.json').read_text())[name]
        archive=PROJECT/'inputs/fingerprint-src'/record['source_url'].rsplit('/',1)[1]
        if archive.is_file():shutil.copy2(archive,docs/archive.name)
    shutil.copy2(Path(__file__),docs/'fedora_fingerprint.py')
    shutil.copytree(PROJECT/'device/fingerprint/fedora',docs/'fedora-integration')
    for upstream in [work/'fprintd-source',next(work.glob('libfprint-v*'))]:
        for name in ['COPYING','COPYING.LIB','AUTHORS']:
            if (upstream/name).is_file():shutil.copy2(upstream/name,docs/(upstream.name+'-'+name))
    def config(name,text):
        path=payload/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text);path.chmod(0o644)
    config('usr/lib/systemd/system/liuqin-fpc-stable-module.service',
        '[Unit]\nDescription=Liuqin verified fingerprint module and local firmware\n'
        'After=display-manager.service iio-sensor-proxy.service\nWants=display-manager.service iio-sensor-proxy.service\n'
        'ConditionPathExists=/dev/tee0\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 '+PREFIX+'/stable_module.py\n'
        'RemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n')
    dropin=(OEM/'src/fingerprint/libfprint-tod/fprintd-fpc1264-oem.conf').read_text()
    dropin=dropin.replace('[Service]','ConditionPathExists=/sys/bus/spi/devices/spi1.0/irq_interface\n\n[Service]',1)
    dropin+='\nEnvironment=LD_LIBRARY_PATH='+PREFIX+'/lib\n'
    dropin+='Environment=PYTHONDONTWRITEBYTECODE=1\n'
    config('etc/systemd/system/fprintd.service.d/60-liuqin-fpc-oem.conf',dropin)
    config('etc/modprobe.d/liuqin-fpc-late-load.conf','blacklist fpc1264_spi_diag\n')
    config('etc/polkit-1/rules.d/60-liuqin-fingerprint-enroll.rules',
        (OEM/'src/fingerprint/deployment/60-liuqin-fingerprint-enroll.rules').read_text())
    config('usr/share/selinux/packages/liuqin-fingerprint.cil',
        (PROJECT/'device/fingerprint/fedora/liuqin-fingerprint.cil').read_text())
    script=payload/'usr/libexec/liuqin-fingerprint-activate'
    script.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(PROJECT/'device/fingerprint/fedora/activate.sh',script);script.chmod(0o755)
    enabled=payload/'etc/systemd/system/multi-user.target.wants/liuqin-fpc-stable-module.service'
    enabled.parent.mkdir(parents=True,exist_ok=True)
    enabled.symlink_to('/usr/lib/systemd/system/liuqin-fpc-stable-module.service')
    return payload


def pack(payload, output, versions=()):
    from rpm_package import build as rpm
    for path in Path(payload).rglob('*'):
        if not path.is_file() or path.is_symlink():continue
        with path.open('rb') as stream:header=stream.read(20)
        if header[:4]==b'\x7fELF' and (header[4:6]!=b'\x02\x01' or header[18:20]!=b'\xb7\x00'):
            raise ValueError('指纹组件不是 ARM64 ELF：'+str(path))
    tool=shutil.which('patchelf')
    if not tool:raise ValueError('指纹私有库需要 patchelf 设置运行时路径。')
    runtime=Path(payload)/PREFIX.lstrip('/')
    for path in [runtime/'fprintd',runtime/'fpc-oem-print',runtime/'tod/libfprint-tod-fpc1264-oem.so',
                 *[p for p in (runtime/'lib').glob('*.so*') if p.is_file() and not p.is_symlink()]]:
        subprocess.run([tool,'--set-rpath',PREFIX+'/lib',str(path)],check=True)
    return rpm(payload,'liuqin-fingerprint','0.1.0',output,release='7.liuqin',
        requires=[*RUNTIME_PACKAGES,*versions],license='MIT AND LGPL-2.1-or-later AND GPL-2.0-or-later AND BSD-2-Clause AND BSD-3-Clause-Clear AND Apache-2.0',
        summary='Native FPC1264 fingerprint authentication for Fedora on Liuqin',
        posttrans='/usr/libexec/liuqin-fingerprint-activate')
