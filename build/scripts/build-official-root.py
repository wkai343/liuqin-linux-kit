#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assemble Ubuntu Base with declared packages and project-owned device packages."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from source_build import sha, mounted_root, arm64_execution

RUNTIME_PACKAGES = ['systemd-sysv','udev','dbus','python3','kmod','gdisk','e2fsprogs','btrfs-progs',
                    'libcap2-bin','tar','gzip','coreutils','debianutils','ca-certificates',
                    'libglib2.0-bin','libasound2t64']
DEVICE_PACKAGES = ['btrfs-progs','network-manager','wpasupplicant','wireless-regdb','iw','bluez','chrony','sudo','locales','systemd-resolved',
                   'dbus-user-session','libpam-systemd','polkitd','libqrtr1','libprotobuf-c1',
                   'iio-sensor-proxy','pipewire','pipewire-pulse','wireplumber','alsa-ucm-conf','alsa-utils','libcanberra-pulse',
                   'python3-gi','python3-dbus']
PLASMA_PACKAGES = ['kde-plasma-desktop','sddm','kwin-wayland','plasma-workspace','powerdevil',
                   'kscreen','plasma-nm','plasma-pa','polkit-kde-agent-1','xdg-desktop-portal-kde',
                   'sddm-theme-breeze','maliit-keyboard','qml6-module-qtquick-virtualkeyboard','qt6-virtualkeyboard-plugin',
                   'xserver-xorg','xwayland','libgl1-mesa-dri','mesa-vulkan-drivers','konsole']


def recipe(path, distribution='ubuntu'):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get('distribution') != distribution or distribution not in ('ubuntu','fedora') or data.get('desktop') != 'plasma':
        raise ValueError('本入口需要 '+distribution+' + Plasma 配方。')
    for field in ('packages','remove'):
        values = data.get(field, [])
        if not isinstance(values, list) or any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+._-]*(?:=[A-Za-z0-9.+:~_-]+)?',name) for name in values):
            raise ValueError(field + ' 必须是软件包名称列表。')
    if set(data.get('remove',[])) & {'systemd','systemd-sysv','systemd-udev','udev','sudo','python3','kmod','network-manager','NetworkManager'}:
        raise ValueError('不能移除必要的启动或管理组件。')
    # Explicit old account recipes retain their preconfigured-account behavior.
    data.setdefault('first_boot_setup', 'user' not in data)
    if not isinstance(data['first_boot_setup'],bool):raise ValueError('first_boot_setup 必须是 true 或 false。')
    data.setdefault('hostname','liuqin')
    if data['first_boot_setup']:
        if data.get('user') is not None or data.get('password_file') is not None:
            raise ValueError('首次开机创建账号时，不要设置 user 或 password_file。')
        protected={'plasma-workspace'}
        if distribution=='fedora':
            protected.update({'plasma-setup','plasma-login-manager','kwin','plasma-keyboard','qt6-qtvirtualkeyboard'})
        else:
            protected.update({'liuqin-plasma-setup','sddm','kwin-wayland','maliit-keyboard'})
        if set(data.get('remove',[])) & protected:
            raise ValueError('不能移除首次初始化所需的组件。')
    elif not isinstance(data.get('user'),str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',data['user']) or data['user'] in ('root','fastrpc','plasma-setup'):
        raise ValueError('需要有效的普通用户名称。')
    if not isinstance(data['hostname'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}',data['hostname']): raise ValueError('主机名无效。')
    for name in ('overlay','password_file'):
        if data.get(name) is not None:
            if not isinstance(data[name],str) or not data[name]: raise ValueError(name + ' 必须是路径或 null。')
            path = Path(data[name]).expanduser()
            data[name] = str(path.resolve() if path.is_absolute() else (PROJECT/path).resolve())
    return data


def extract(archive, expected, root):
    archive, root = Path(archive), Path(root)
    if root.exists() or sha(archive) != expected: raise ValueError('基础归档校验失败或目标已存在。')
    root.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['sh',str(PROJECT/'build/lib/rootfs-archive.sh'),'extract',str(root),str(archive)],check=True)


def chroot(root, command, **kwargs):
    executable = shutil.which('chroot')
    if not executable: raise ValueError('缺少宿主 chroot，请使用 Devbox 环境。')
    return subprocess.run([executable,str(root),*command],check=True,
        env=dict(os.environ,DEBIAN_FRONTEND='noninteractive',PATH='/usr/sbin:/usr/bin:/sbin:/bin',LC_ALL='C'),**kwargs)


def apt(root, packages):
    chroot(root,['apt-get','install','-y','--no-install-recommends',*packages])


def overlay(source, root):
    source, root = Path(source), Path(root)
    if not source.is_dir(): raise ValueError('rootfs overlay 不存在。')
    # Copy only ordinary configuration files and links, refusing symlink parents.
    for path in sorted(source.rglob('*'),key=lambda p:(len(p.parts),str(p))):
        rel=path.relative_to(source); target=root/rel
        if rel.parts[0] in ('dev','proc','sys','run'): raise ValueError('overlay 不能写入运行时目录。')
        if any(parent.is_symlink() for parent in target.parents if parent != root and root in parent.parents):
            raise ValueError('overlay 目标的父目录含符号链接：'+str(rel))
        if target.is_symlink():
            if path.is_dir():raise ValueError('overlay 目录不能覆盖符号链接：'+str(rel))
            target.unlink()
        if path.is_symlink():
            link=os.readlink(path)
            normalized=os.path.normpath(str(rel.parent/link))
            if link.startswith('/') or normalized=='..' or normalized.startswith('../'):
                raise ValueError('overlay 链接不能离开目标根目录。')
            if target.exists(): raise ValueError('overlay 链接不能覆盖现有文件或目录。')
            target.parent.mkdir(parents=True,exist_ok=True);target.symlink_to(os.readlink(path))
        elif path.is_dir(): target.mkdir(parents=True,exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
            target.chmod(stat.S_IMODE(path.stat().st_mode));os.chown(target,0,0)
        else: raise ValueError('overlay 含特殊文件。')


def write_config(root,name,content):
    path=root/name
    if path.is_symlink() or path.resolve()!=path or (path.exists() and not path.is_file()):
        raise ValueError('配置写入路径含链接或特殊文件：'+name)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content);path.chmod(0o644)


def prepare_desktop_content(root):
    # Ubuntu Base excludes gettext catalogs by default. Fonts and generic
    # language packs cannot restore files dpkg never extracts from KDE packages.
    # Keep Base's manpage/documentation trimming while retaining desktop catalogs
    # and the project packages' own attribution and license files.
    write_config(root,'etc/dpkg/dpkg.cfg.d/zz-liuqin-desktop-content',
        'path-include=/usr/share/locale/*/LC_MESSAGES/*.mo\n'
        'path-include=/usr/share/doc/liuqin-*/*\n')


def configure_locales(root):
    locale_gen=root/'etc/locale.gen'
    if locale_gen.is_symlink() or locale_gen.resolve()!=locale_gen:raise ValueError('locale.gen 不能通过符号链接写入。')
    lines=locale_gen.read_text().splitlines() if locale_gen.exists() else []
    for entry in ('en_US.UTF-8 UTF-8','zh_CN.UTF-8 UTF-8'):
        if entry not in lines:lines.append(entry)
    write_config(root,'etc/locale.gen','\n'.join(lines)+'\n')
    chroot(root,['locale-gen'])


def configure_account(root, data, output, admin_group='sudo'):
    user=data['user']; chroot(root,['useradd','-m','-s','/bin/bash','-G',admin_group,user])
    if data.get('password_file'):
        path=Path(data['password_file'])
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError('密码文件必须是仅拥有者可访问的普通文件。')
        password=path.read_text().rstrip('\n')
        if '\n' in password or ':' in password or len(password)<8: raise ValueError('密码文件需要至少 8 字符的单行密码。')
    else: password=secrets.token_urlsafe(18)
    chroot(root,['chpasswd'],input=(user+':'+password+'\n').encode())
    credentials=output/'login.txt'
    fd=os.open(credentials,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write('user='+user+'\npassword='+password+'\n')
    os.chown(credentials,int(os.environ.get('SUDO_UID',os.getuid())),int(os.environ.get('SUDO_GID',os.getgid())))


def configure_first_boot(root):
    # This mode is for a freshly assembled image, never for an in-place upgrade.
    for line in (root/'etc/passwd').read_text().splitlines():
        fields=line.split(':')
        if len(fields)==7 and 1000<=int(fields[2])<=60000:
            raise ValueError('首次初始化镜像不能包含预置普通用户：'+fields[0])
    if (root/'etc/plasma-setup-done').exists():raise ValueError('首次初始化镜像已有完成标记。')
    if not (root/'usr/libexec/plasma-setup').is_file() or not (root/'usr/lib/systemd/system/plasma-setup.service').is_file():
        raise ValueError('首次初始化组件未完整安装。')
    write_config(root,'etc/xdg/plasmasetuprc','[Accounts]\nUserGroups=sudo\n')
    chroot(root,['usermod','--lock','root'])
    chroot(root,['useradd','-D','-s','/bin/bash'])
    # The transient setup user is created by the package's sysusers definition.
    chroot(root,['systemd-sysusers'])
    service=root/'etc/systemd/system/multi-user.target.wants/plasma-setup.service'
    service.parent.mkdir(parents=True,exist_ok=True)
    if service.parent.resolve()!=service.parent or service.exists() or service.is_symlink():
        raise ValueError('初始化服务启用路径已存在或包含链接。')
    service.symlink_to('/usr/lib/systemd/system/plasma-setup.service')


def configure(root, data, output):
    configure_locales(root)
    if data['first_boot_setup']:configure_first_boot(root)
    else:configure_account(root,data,output)
    write_config(root,'etc/hostname',data['hostname']+'\n')
    write_config(root,'etc/hosts','127.0.0.1 localhost\n127.0.1.1 '+data['hostname']+'\n::1 localhost ip6-localhost\n')
    write_config(root,'etc/liuqin-native-root','liuqin-native-root-v1\n')
    units=root/'etc/systemd/system'
    def link(name,target):
        path=units/name
        if path.parent.resolve()!=path.parent:raise ValueError('服务链接的父目录不能是符号链接。')
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() or path.is_symlink():path.unlink()
        path.symlink_to(target)
    link('basic.target.requires/liuqin-gnome-storage-guard.service','../liuqin-gnome-storage-guard.service')
    for name in ('liuqin-gnome-usb-rescue','liuqin-slpi'):
        link('multi-user.target.wants/'+name+'.service','../'+name+'.service')
    # KWin subscribes to the running proxy. GNOME's repeated owner refresh can
    # cancel KWin's pending accelerometer claim during the first login.
    link('multi-user.target.wants/iio-sensor-proxy.service','/usr/lib/systemd/system/iio-sensor-proxy.service')
    link('graphical.target.wants/liuqin-backlight-default.service','../liuqin-backlight-default.service')
    link('multi-user.target.wants/liuqin-display-policy.service','../liuqin-display-policy.service')
    link('default.target','/usr/lib/systemd/system/graphical.target')
    link('display-manager.service','/usr/lib/systemd/system/sddm.service')
    (root/'etc/X11').mkdir(exist_ok=True)
    write_config(root,'etc/X11/default-display-manager','/usr/bin/sddm\n')
    (root/'etc/sddm.conf.d').mkdir(parents=True,exist_ok=True)
    write_config(root,'etc/sddm.conf.d/10-liuqin.conf','[General]\nInputMethod=qtvirtualkeyboard\n[Theme]\nCurrent=breeze\n')
    locale=root/'etc/default/locale';locale.parent.mkdir(exist_ok=True)
    if not locale.exists() or not any(line.startswith('LANG=') for line in locale.read_text().splitlines()):
        write_config(root,'etc/default/locale','LANG=C.UTF-8\n')
    (root/'etc/systemd/logind.conf.d').mkdir(parents=True,exist_ok=True)
    write_config(root,'etc/systemd/logind.conf.d/90-liuqin.conf','[Login]\nHandlePowerKey=suspend\n')
    # PowerDevil owns Plasma's power-button handling; do not enable GNOME's daemon.
    for rel in ('etc/systemd/system/multi-user.target.wants/liuqin-power-keyd.service',
                'etc/systemd/system/graphical.target.wants/liuqin-sensor-proxy-refresh.service',
                'etc/systemd/user/graphical-session.target.wants/liuqin-sensor-proxy-session-refresh.service'):
        (root/rel).unlink(missing_ok=True)
    bluetooth=root/'etc/bluetooth/main.conf'
    if bluetooth.is_symlink() or bluetooth.resolve()!=bluetooth:raise ValueError('蓝牙配置不能通过符号链接写入。')
    with bluetooth.open('a') as stream:stream.write('\n[Policy]\nAutoEnable=true\n')
    chroot(root,['systemd-sysusers'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('runtime','apps','extract','sensors','kernel-files','setup'))
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--archive-sha256',required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--recipe',type=Path)
    parser.add_argument('--debs',type=Path)
    parser.add_argument('--rom-sensors',type=Path)
    parser.add_argument('--setup-source',type=Path)
    parser.add_argument('--setup-deb',type=Path)
    parser.add_argument('--jobs',type=int,default=12)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('rootfs 装配需要 root 保留所有权。')
    if args.jobs<1:parser.error('jobs 必须是正整数。')
    if os.environ.get('LIUQIN_SOURCE_ROOT_NS')!='1':
        command=['unshare','--mount','--propagation','private','--pid','--fork',sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]]
        subprocess.run(command,env=dict(os.environ,LIUQIN_SOURCE_ROOT_NS='1'),check=True);return
    output=args.out.resolve()
    if not output.is_relative_to(PROJECT/'out') or output==PROJECT/'out' or output.exists():
        parser.error('需要项目 out/ 下不存在的新输出目录。')
    output.mkdir(parents=True);root=output/'rootfs'
    extract(args.archive,args.archive_sha256,root)
    if args.action=='extract':return
    if args.action=='sensors':
        if not args.rom_sensors:parser.error('传感器构建需要 ROM 配置。')
        with arm64_execution():
            subprocess.run(['sh',str(PROJECT/'build/scripts/build-liuqin-sensors-stack.sh')],check=True,
                env=dict(os.environ,ROOTFS=str(root),ROM_SENSORS=str(args.rom_sensors),
                         OUT_DIR=str(output/'sensors'),UPDATE_APT='1',JOBS=str(args.jobs)))
        shutil.copyfile(output/'sensors/artifacts/sensor-stack.tar',output/'sensor-stack.tar')
        return
    if args.action in ('apps','kernel-files') and (not args.debs or not args.debs.is_dir()):parser.error('需要已构建的设备包目录。')
    with mounted_root(root):
        if args.action in ('runtime','apps','setup'):
            source=json.loads((PROJECT/'build/rootfs/ubuntu-source.json').read_text())
            (root/'etc/apt/sources.list.d').mkdir(parents=True,exist_ok=True)
            (root/'etc/apt/sources.list.d/ubuntu.sources').write_text(
                'Types: deb\nURIs: '+source['apt_uri']+'\nSuites: '+source['suite']+' '+source['suite']+'-updates '+source['suite']+'-security\n'
                'Components: main universe restricted multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
            chroot(root,['apt-get','update'])
        if args.action=='runtime':apt(root,RUNTIME_PACKAGES)
        elif args.action=='setup':
            if not args.setup_source:parser.error('需要已校验的 Plasma Setup 源码归档。')
            from plasma_setup import build as build_setup
            build_setup(root,args.setup_source,output,args.jobs,chroot)
            return
        elif args.action=='apps':
            if not args.recipe:parser.error('需要 rootfs 配置。')
            data=recipe(args.recipe)
            prepare_desktop_content(root)
            apt(root,DEVICE_PACKAGES+PLASMA_PACKAGES+data.get('packages',[]))
            staging=root/'tmp/device-packages';staging.mkdir()
            for path in args.debs.glob('*.deb'):shutil.copyfile(path,staging/path.name)
            if data['first_boot_setup']:
                if not args.setup_deb or not args.setup_deb.is_file():parser.error('首次初始化需要已构建的 Plasma Setup 软件包。')
                shutil.copyfile(args.setup_deb,staging/args.setup_deb.name)
            chroot(root,['apt-get','install','-y',*[str('/tmp/device-packages/'+p.name) for p in sorted(staging.glob('*.deb'))]])
            if data.get('remove'):chroot(root,['apt-get','purge','-y',*data['remove']])
            if data.get('overlay'):overlay(data['overlay'],root)
            configure(root,data,output)
            shutil.rmtree(staging)
        elif args.action=='kernel-files':
            staging=root/'tmp/kernel-packages';staging.mkdir()
            for pattern in ('liuqin-boot-tools_*.deb','liuqin-kernel-modules-*.deb','liuqin-kernel-image-*.deb','liuqin-kernel-updates_*.deb'):
                files=list(args.debs.glob(pattern))
                if len(files)!=1:raise ValueError('内核更新包缺失或重复：'+pattern)
                path=files[0];shutil.copyfile(path,staging/path.name)
                chroot(root,['dpkg','-i','/tmp/kernel-packages/'+path.name])
            shutil.rmtree(staging)
        chroot(root,['apt-get','clean'])
        packages=chroot(root,['dpkg-query','-W','-f=${binary:Package}\t${Version}\t${Architecture}\n'],capture_output=True).stdout
        (output/'packages.tsv').write_bytes(packages)
    for rel in ('var/lib/apt/lists','tmp'):
        directory=root/rel
        for path in list(directory.iterdir()):
            if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
            else:path.unlink()
    write_config(root,'etc/machine-id','')
    (root/'var/lib/dbus/machine-id').unlink(missing_ok=True)
    if args.action=='kernel-files':return
    subprocess.run(['sh',str(PROJECT/'build/lib/rootfs-archive.sh'),'pack',str(root),str(output/'root.tar.gz')],check=True)
    print('官方基础系统装配完成：'+str(output/'root.tar.gz'),flush=True)


if __name__=='__main__':main()
