#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package desktop-neutral firmware, board services and native Fedora SSC binaries."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

PROJECT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(PROJECT/'build/lib'))
from rpm_package import build as rpm
from source_build import sha
from speaker_firmware import install as install_speakers


def docs(root, name):
    target=root/'usr/share/licenses'/name;target.mkdir(parents=True,exist_ok=True)
    for document in ('LICENSE','NOTICE','CREDITS.md'):shutil.copyfile(PROJECT/document,target/document)


def board_overlay(root, busybox):
    overlay=PROJECT/'device/gnome-overlay'
    excluded=('etc/dconf','etc/gdm3','etc/flash-kernel','usr/lib/firmware','usr/share/liuqin/power',
              'etc/systemd/logind.conf.d','usr/local/libexec/liuqin-power-',
              'etc/systemd/system/liuqin-power-keyd','etc/systemd/system/liuqin-snap-root-admission',
              'usr/local/sbin/liuqin-snap-root-admission','usr/local/sbin/liuqin-switch',
              'usr/local/share/applications/liuqin-reboot-to-android','usr/share/polkit-1/actions/io.github.liuqin.switch')
    for source in sorted(overlay.rglob('*')):
        if not source.is_file() or source.is_symlink():continue
        rel=source.relative_to(overlay).as_posix()
        if rel=='etc/liuqin-gnome-root' or source.name=='README.md' or any(rel.startswith(p) for p in excluded) or 'org.gnome.Snapshot' in rel:continue
        target=root/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        target.chmod(0o755 if source.stat().st_mode&0o111 else 0o644)
    guard=root/'usr/local/sbin/liuqin-gnome-storage-guard';text=guard.read_text()
    old='marker=${LIUQIN_GNOME_GUARD_TEST_MARKER:-/etc/liuqin-gnome-root}'
    if text.count(old)!=1:raise ValueError('存储保护脚本标记不符。')
    text=text.replace(old,'marker=/etc/liuqin-native-root').replace(
        'bd86a359f5b6bf05f09abf544967489e924c251ab7c07684df62b0dbda4c3fca',
        '4fdae4f7a27af8b0d4a2bbc168c7f01c3c5c6b5e245fcc521389d662f8212c5b')
    guard.write_text(text)
    binary=root/'usr/local/bin/busybox';binary.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(busybox,binary);binary.chmod(0o755)


def sensor_payload(archive, root):
    with tarfile.open(archive) as tar:
        for member in tar:
            name=Path(member.name.removeprefix('./'))
            if name.is_absolute() or '..' in name.parts or not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError('传感器层有不安全的文件。')
            # Distro libraries belong to Fedora RPMs, not to this device package.
            if len(name.parts)>=3 and name.parts[:2]==('usr','lib64'):
                if not name.name.startswith(('libssc.so.','libhexagonrpc.so.')):continue
                target=root/'usr/local/lib64'/name.name
            else:target=root/name
            if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            if member.issym():
                if name.as_posix().endswith('/sensors/registry'):
                    if member.linkname!='/var/lib/liuqin-sensors/registry':raise ValueError('私有注册表链接不符。')
                else:
                    normalized=os.path.normpath(str(name.parent/member.linkname))
                    if member.linkname.startswith('/') or normalized=='..' or normalized.startswith('../'):
                        raise ValueError('传感器链接离开包目录：'+str(name))
                target.symlink_to(member.linkname)
            else:
                target.write_bytes(tar.extractfile(member).read());target.chmod(member.mode&0o755)
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.suffix in ('.service','.conf'):
            text=path.read_text();path.write_text(text.replace('/usr/local/lib/aarch64-linux-gnu','/usr/local/lib64'))
    conf=root/'etc/ld.so.conf.d/liuqin-sensors.conf';conf.parent.mkdir(parents=True,exist_ok=True)
    conf.write_text('/usr/local/lib64\n')
    for name in ('usr/bin/hexagonrpcd','usr/bin/ssccli','usr/local/libexec/liuqin-iio-sensor-proxy'):
        path=root/name;header=path.read_bytes()[:20]
        if header[:6]!=b'\x7fELF\x02\x01' or header[18:20]!=b'\xb7\x00':raise ValueError('传感器包不是 ARM64。')
    if any((root/'var/lib/liuqin-sensors').rglob('*')):
        if any(p.is_file() for p in (root/'var/lib/liuqin-sensors').rglob('*')):raise ValueError('传感器包含本机私有数据。')
    # Upstream copying is required independently of the project's MIT notice.
    for component in ('hexagonrpc','libssc','iio-sensor-proxy'):
        source=PROJECT/'inputs/sensor-stack-src'/component
        target=root/'usr/share/licenses/liuqin-sensors'/component;target.mkdir(parents=True,exist_ok=True)
        found=False
        for name in ('COPYING','LICENSE','LICENSES'):
            if (source/name).is_dir():shutil.copytree(source/name,target/name);found=True
            elif (source/name).is_file():shutil.copyfile(source/name,target/name);found=True
        if not found:raise ValueError('传感器源码缺少上游许可证：'+component)


def build(firmware, speakers, sensors, busybox, output, version):
    output=Path(output).resolve()
    if output.exists():raise ValueError('RPM 输出已存在。')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.device-rpms-',dir=output.parent) as temp:
        work=Path(temp);packages=work/'packages';packages.mkdir()
        root=work/'firmware';shutil.copytree(Path(firmware)/'usr',root/'usr',symlinks=True)
        for row in (Path(firmware)/'firmware.manifest').read_text().splitlines():
            expected,name=row.split('  ',1)
            if sha(root/name.lstrip('/'))!=expected:raise ValueError('固件清单校验失败：'+name)
        install_speakers(speakers,root)
        # Linux searches updates/ first. Keep distribution firmware coinstallable.
        cirrus=root/'usr/lib/firmware/cirrus';cirrus.rename(root/'usr/lib/firmware/updates/cirrus')
        for rel in ['regulatory.db','regulatory.db.p7s',*[f'ath11k/WCN6855/{rev}/{name}.zst'
                    for rev in ('hw2.0','hw2.1') for name in ('amss.bin','m3.bin','board-2.bin','regdb.bin')]]:
            (root/'usr/lib/firmware'/rel).unlink(missing_ok=True)
        docs(root,'liuqin-firmware')
        rpm(root,'liuqin-firmware',version,packages,architecture='noarch',license='LicenseRef-Proprietary',
            requires=['wireless-regdb'],summary='Liuqin stock firmware and protected speaker tuning')
        root=work/'board';board_overlay(root,busybox);docs(root,'liuqin-device-support')
        rpm(root,'liuqin-device-support',version,packages,requires=['systemd','systemd-udev','python3',
            'NetworkManager','bluez','glib2','iproute'],summary='Liuqin board services independent of the desktop')
        root=work/'sensors';root.mkdir();sensor_payload(sensors,root);docs(root,'liuqin-sensors')
        rpm(root,'liuqin-sensors',version,packages,requires=['glib2','libgudev','polkit-libs','libqmi',
            'libqrtr-glib','qrtr-libs','protobuf-c','iio-sensor-proxy','systemd'],
            license='GPL-3.0-or-later',summary='Native Fedora Liuqin SSC sensor stack',post='/sbin/ldconfig')
        output.mkdir()
        for path in packages.iterdir():shutil.copyfile(path,output/path.name)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware',type=Path,required=True);parser.add_argument('--speakers',type=Path,required=True)
    parser.add_argument('--sensors',type=Path,required=True);parser.add_argument('--busybox',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--version',required=True)
    args=parser.parse_args();print(build(args.firmware,args.speakers,args.sensors,args.busybox,args.out,args.version))
