#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assemble an official Fedora ARM64 system in a private mount namespace."""
import argparse
import importlib.util
import json
import os
import re
import signal
import time
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'build/lib'))
from source_build import mounted_root, sha, arm64_execution
from fedora_oci import extract as extract_oci
from shell_defaults import check_skeleton, ensure as ensure_shell_defaults
spec=importlib.util.spec_from_file_location('ubuntu_root_common',PROJECT/'build/scripts/build-official-root.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)

RUNTIME_PACKAGES=['systemd','systemd-udev','systemd-pam','dbus-broker','dbus-daemon','python3','kmod','gdisk','e2fsprogs',
                  'libcap','tar','gzip','coreutils','which','ca-certificates','glib2','alsa-lib',
                  'glibc-langpack-en','glibc-langpack-zh']
DEVICE_PACKAGES=['btrfs-progs','NetworkManager','NetworkManager-wifi','wpa_supplicant','wireless-regdb','iw','iproute',
                 'bluez','chrony','sudo','systemd-resolved','polkit','upower','qrtr-libs','libqrtr-glib','protobuf-c',
                 'libqmi','libgudev','iio-sensor-proxy','pipewire','pipewire-pulseaudio',
                 'pipewire-alsa','wireplumber','alsa-ucm','alsa-utils','libcanberra',
                 'python3-gobject-base','python3-dbus','selinux-policy-targeted','policycoreutils']
# Fedora keeps KWin's upstream licenses in -doc; retain it with weak deps disabled.
PLASMA_PACKAGES=['plasma-desktop','plasma-workspace','kwin','kwin-doc','plasma-login-manager','plasma-nm',
                 'plasma-pa','polkit-kde','powerdevil','kscreen','bluedevil','xdg-desktop-portal-kde',
                 'plasma-keyboard','qt6-qtvirtualkeyboard','qt6-qtwayland','mesa-dri-drivers',
                 'mesa-vulkan-drivers','konsole','dolphin','qt6-qtlocation','qt6-qtpositioning',
                 'kf6-kirigami','kf6-kirigami-addons','kf6-kitemmodels','kf6-kcmutils','kf6-ksvg',
                 'zram-generator','zram-generator-defaults','ocean-sound-theme','sound-theme-freedesktop']
FIRST_BOOT_PACKAGES=['plasma-setup','shadow-utils']
MANUAL_PACKAGES=['bash','coreutils','coreutils-common','systemd','util-linux','util-linux-core',
                'shadow-utils','procps-ng','psmisc','openssh','openssh-clients','openssh-server',
                'iproute','iputils','curl','wget','rsync','nano','less','fastfetch','man-db','mandoc']


def enable_documentation(root):
    # The official container base omits docs. A desktop should retain manuals
    # during this build and future on-device DNF transactions.
    path=root/'etc/dnf/dnf.conf'
    text=path.read_text() if path.exists() else '[main]\n'
    def flags(match):
        remaining=[flag for flag in re.split(r'[,\s]+',match[2].strip()) if flag and flag!='nodocs']
        comment=(' '+match[3]) if match[3] else ''
        return match[1]+','.join(remaining)+comment
    text=re.sub(r'^([ \t]*tsflags[ \t]*=[ \t]*)([^#;\n]*)([^\n]*)$',flags,text,flags=re.M|re.I)
    common.write_config(root,'etc/dnf/dnf.conf',text)
    common.write_config(root,'etc/rpm/macros.liuqin','%_install_langs all\n%_excludedocs 0\n')


def restore_core_manpages(root):
    installed=set(chroot(root,['rpm','-qa','--qf','%{NAME}\n'],capture_output=True,text=True).stdout.splitlines())
    names=[name for name in MANUAL_PACKAGES if name in installed]
    def missing():
        if not names:return []
        listing=chroot(root,['rpm','-q','--qf','[%{=NAME}\t%{FILENAMES}\t%{FILEFLAGS}\n]',*names],capture_output=True,text=True).stdout
        absent=set()
        for line in listing.splitlines():
            name,path,file_flags=line.split('\t')
            # RPM ghost entries describe runtime-generated/optional files;
            # mandoc's makewhatis alias is one, not a missing payload page.
            if int(file_flags)&64:continue
            if path.startswith('/usr/share/man/') and not os.path.lexists(root/path.lstrip('/')):absent.add(name)
        return sorted(absent)
    absent=missing()
    if absent:
        # A pinned OCI base can contain an older update no longer in repo
        # metadata. Upgrade first, then reinstall only still-missing manuals.
        dnf(root,'upgrade',*absent)
        remaining=missing()
        if remaining:dnf(root,'reinstall',*remaining)
        if missing():raise ValueError('基础命令手册恢复不完整。')
        print('已补齐基础命令手册：'+', '.join(absent),flush=True)


def clean_ssh_identity(root):
    # Keys belong to an installed tablet, never to a reusable rootfs image.
    for path in (root/'etc/ssh').glob('ssh_host_*'):
        if path.is_file() or path.is_symlink():path.unlink()


def configure_flatpak(root):
    if not (root/'usr/bin/flatpak').is_file():return
    try:
        chroot(root,['flatpak','--system','remote-add','--if-not-exists','--from',
                     'flathub','https://dl.flathub.org/repo/flathub.flatpakrepo'],timeout=120)
    finally:stop_flatpak_agents(root)


def stop_flatpak_agents(root):
    # OSTree's temporary GPG import may leave agents holding /dev/null open.
    # Inspect the target's freshly mounted proc, never the host /proc view.
    proc=root/'proc'
    if not proc.is_dir():return
    stopped=[]
    for process in proc.iterdir():
        if not process.name.isdecimal():continue
        try:
            args=(process/'cmdline').read_bytes().split(b'\0')
            if not any(Path(os.fsdecode(arg)).name=='gpg-agent' for arg in args if arg):continue
            index=args.index(b'--homedir')
            if not args[index+1].startswith(b'/tmp/ostree-gpg-'):continue
            if os.getpid()!=1:raise RuntimeError('Flatpak 密钥导入清理需要独立 PID 命名空间。')
            os.kill(int(process.name),signal.SIGTERM);stopped.append(process)
        except (FileNotFoundError,ProcessLookupError,ValueError,IndexError):continue
    deadline=time.monotonic()+5
    for process in stopped:
        while process.exists():
            try:
                state=next(line for line in (process/'status').read_text().splitlines() if line.startswith('State:'))
                if state.split()[1]=='Z':break
            except FileNotFoundError:break
            if time.monotonic()>deadline:raise RuntimeError('Flatpak 临时签名进程未退出。')
            time.sleep(0.05)


def configure_firefox_languages(root):
    # Expose the distro's existing Chinese XPIs through Firefox's system
    # extension directory. No network download or profile rewrite is needed.
    base=root/'usr/lib64/firefox'
    if not (base/'firefox').is_file():return
    directory=base/'browser/extensions'
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError('Firefox 语言目录离开根文件系统。')
    for locale in ('zh-CN','zh-TW'):
        name='langpack-'+locale+'@firefox.mozilla.org.xpi'
        source=base/'langpacks'/name
        if not source.is_file():continue
        if not source.resolve().is_relative_to(root.resolve()):
            raise ValueError('Firefox 语言包离开根文件系统。')
        directory.mkdir(parents=True,exist_ok=True)
        target=directory/name
        if not target.exists() and not target.is_symlink():
            target.symlink_to('../../langpacks/'+name)


def configure_qrca_camera(root):
    # Qt's default FFmpeg backend only enumerates V4L2 cameras here. QRCA
    # needs GStreamer's libcamera provider for the tablet's CAMSS pipeline.
    # Converting to a CPU-backed grayscale frame also avoids the live scanner
    # compatibility failure seen with the default colour-buffer path.
    desktop=root/'usr/share/applications/org.kde.qrca.desktop'
    if not (root/'usr/bin/qrca').is_file() or not desktop.is_file():return
    lines=desktop.read_text().splitlines()
    command='env QT_MEDIA_BACKEND=gstreamer '
    command+='"QT_GSTREAMER_OVERRIDE_VIDEO_CONVERSION_ELEMENT=videoconvert ! capsfilter caps=video/x-raw,format=GRAY8" '
    common.write_config(root,'usr/local/share/applications/org.kde.qrca.desktop',
        '\n'.join('Exec='+command+line[5:]
                  if line.startswith('Exec=') else line for line in lines)+'\n')


def configure_login_output(root):
    # KWin returns to the manual transform when tablet-mode rotation stops.
    # The DSI panel is natively portrait; the pogo keyboard sits on its right.
    # Supply a greeter-only fallback, keeping user-saved output settings first.
    defaults=[dict(name='outputs',data=[dict(connectorName='DSI-1',
        transform='Rotated270',autoRotation='InTabletMode',scale=2)]),
        dict(name='setups',data=[])]
    common.write_config(root,'usr/share/liuqin/plasma-login/kwinoutputconfig.json',
                        json.dumps(defaults,indent=2)+'\n')
    # Official Plasma Setup and new desktop accounts use the same static
    # panel default; no compiled first-session helper is needed.
    common.write_config(root,'etc/xdg/kwinoutputconfig.json',json.dumps(defaults,indent=2)+'\n')
    common.write_config(root,'usr/lib/systemd/user/plasma-login-kwin_wayland.service.d/10-liuqin-display.conf',
        '[Service]\nEnvironment="XDG_CONFIG_DIRS=/usr/share/liuqin/plasma-login:%h/.config/kdedefaults:/etc/xdg"\n')


def configure_input(root):
    # The shared Ubuntu overlay still uses Maliit. Fedora ships Plasma
    # Keyboard as a desktop/login dependency and should select that keyboard.
    common.write_config(root,'etc/xdg/kwinrc',
        '[Wayland]\nInputMethod=/usr/share/applications/org.kde.plasma.keyboard.desktop\nVirtualKeyboardEnabled=true\n')
    if not (root/'usr/bin/fcitx5').is_file():return
    common.write_config(root,'etc/xdg/fcitx5/profile',
        '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=pinyin\n'
        '[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n'
        '[Groups/0/Items/1]\nName=pinyin\nLayout=\n[GroupOrder]\n0=Default\n')
    env=(PROJECT/'build/rootfs/fedora-input-env.sh').read_text()
    common.write_config(root,'etc/xdg/plasma-workspace/env/90-liuqin-fcitx5.sh',env)
    (root/'etc/xdg/plasma-workspace/env/90-liuqin-fcitx5.sh').chmod(0o755)
    common.write_config(root,'etc/profile.d/90-liuqin-fcitx5.sh',env)
    # Native Wayland Fcitx must be started by KWin when selected, rather than
    # racing the touch keyboard for the compositor's input-method socket.
    common.write_config(root,'usr/libexec/liuqin-fcitx5-autostart',
        '#!/bin/sh\n[ "${XDG_SESSION_TYPE:-}" = x11 ] || exit 0\nexec /usr/bin/fcitx5\n')
    (root/'usr/libexec/liuqin-fcitx5-autostart').chmod(0o755)
    common.write_config(root,'etc/xdg/autostart/org.fcitx.Fcitx5.desktop',
        '[Desktop Entry]\nType=Application\nName=Fcitx 5 (X11)\n'
        'Exec=/usr/libexec/liuqin-fcitx5-autostart\nTryExec=fcitx5\nNoDisplay=true\nOnlyShowIn=KDE;\n')
    # GTK's Wayland backend prefers its native text-input module; these
    # template settings select Fcitx for legacy GTK applications on X11.
    common.write_config(root,'etc/skel/.gtkrc-2.0','gtk-im-module="fcitx"\n')
    common.write_config(root,'etc/skel/.config/gtk-3.0/settings.ini','[Settings]\ngtk-im-module=fcitx\n')
    common.write_config(root,'etc/skel/.config/gtk-4.0/settings.ini','[Settings]\ngtk-im-module=fcitx\n')


def chroot(root, command, **kwargs):
    env=dict(os.environ,PATH='/usr/sbin:/usr/bin:/sbin:/bin',LC_ALL='C.UTF-8')
    variables=['PATH=/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL=C.UTF-8','HOME=/root']
    for name in ('http_proxy','https_proxy','no_proxy','HTTP_PROXY','HTTPS_PROXY','NO_PROXY'):
        if os.environ.get(name):variables.append(name+'='+os.environ[name])
    return subprocess.run([shutil.which('chroot'),str(root),'/usr/bin/env','-i',
        *variables,*command],env=env,check=True,**kwargs)


def dnf(root, *args):
    return chroot(root,['dnf','-y','--setopt=install_weak_deps=False','--setopt=localpkg_gpgcheck=False',
                        '--setopt=optional_metadata_types=',*args])


def install_desktop_packages(root, data):
    dnf(root,'install',*DEVICE_PACKAGES,*PLASMA_PACKAGES,*data.get('packages',[]),
        *(FIRST_BOOT_PACKAGES if data['first_boot_setup'] else []))
    dnf(root,'install','--allowerasing','fedora-release-kde','fedora-release-identity-kde')


def check_desktop_runtime(root, *, first_boot_setup=False):
    # With weak dependencies disabled, a package transaction can succeed while
    # the login session cannot start. Probe the actual ARM64 session bus and PAM
    # loader before publishing; the Wi-Fi identity helper also needs iproute.
    # PowerDevil talks to UPower over D-Bus, so shared-library dependencies
    # alone do not guarantee that battery reporting is available.
    required=['usr/bin/dbus-run-session','usr/lib64/security/pam_systemd.so',
                 'usr/sbin/ip','usr/bin/startplasma-wayland','usr/bin/startplasma-login-wayland',
                 'usr/bin/upower','usr/libexec/upowerd','usr/lib/systemd/system/upower.service',
                 'usr/share/dbus-1/system-services/org.freedesktop.UPower.service']
    if first_boot_setup:
        required += ['usr/libexec/plasma-setup','usr/libexec/plasma-setup-bootutil',
                     'usr/lib/systemd/system/plasma-setup.service']
    for name in required:
        path=root/name
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Fedora 桌面运行组件缺失：'+name)
    chroot(root,['/usr/bin/dbus-run-session','--','/usr/bin/true'],timeout=30)
    chroot(root,['/usr/bin/python3','-c',
        'import ctypes; ctypes.CDLL("/usr/lib64/security/pam_systemd.so")'],timeout=30)
    chroot(root,['/usr/sbin/ip','-Version'],timeout=30)
    # --version prints both client and daemon versions, requiring a live
    # system bus. The image assembly namespace deliberately has none.
    # --help still checks each executable's loader and libraries without
    # starting UPower or inspecting the build host's power devices.
    for binary in ('/usr/bin/upower','/usr/libexec/upowerd'):
        chroot(root,[binary,'--help'],timeout=30,stdout=subprocess.DEVNULL)


def boot_splash_files():
    return {
        'etc/plymouth/plymouthd.conf':'[Daemon]\nTheme=spinner\nShowDelay=0\nDeviceTimeout=5\n',
        # The initramfs may already have a daemon owning the display.
        'etc/systemd/system/plymouth-start.service.d/10-liuqin.conf':
            '[Service]\nExecStart=\nExecStart=/bin/sh -c '\
            "'/usr/bin/plymouth --ping || exec /usr/bin/plymouthd --mode=boot --pid-file=/run/plymouth/pid --attach-to-session'\n",
        'etc/systemd/system/plymouth-quit.service.d/10-liuqin.conf':
            '[Service]\nExecStart=\nExecStart=-/usr/bin/plymouth quit --retain-splash\n',
    }


def configure(root, data, output):
    check_skeleton(root/'etc/skel')
    configure_flatpak(root)
    configure_input(root)
    configure_login_output(root)
    configure_firefox_languages(root)
    configure_qrca_camera(root)
    legacy_panel=root/'etc/xdg/autostart/liuqin-plasma-panel-defaults.desktop'
    if not legacy_panel.is_symlink() and legacy_panel.is_file() and 'TryExec=/usr/libexec/liuqin-plasma-panel-defaults' in legacy_panel.read_text():
        legacy_panel.unlink()
    common.write_config(root,'etc/hostname',data['hostname']+'\n')
    common.write_config(root,'etc/hosts','127.0.0.1 localhost\n127.0.1.1 '+data['hostname']+'\n::1 localhost\n')
    common.write_config(root,'etc/locale.conf','LANG=en_US.UTF-8\n')
    common.write_config(root,'etc/liuqin-native-root','liuqin-native-root-v1\n')
    units=root/'etc/systemd/system';units.mkdir(parents=True,exist_ok=True)
    def link(name, value):
        path=units/name;path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() or path.is_symlink():path.unlink()
        path.symlink_to(value)
    link('basic.target.requires/liuqin-gnome-storage-guard.service','../liuqin-gnome-storage-guard.service')
    # Factory data is created by the distro-neutral RAM installer. Label that
    # narrow data set in Fedora before any service consumes it; installer.img
    # remains independent of the target distribution and its SELinux policy.
    common.write_config(root,'etc/systemd/system/liuqin-private-labels.service',
        '[Unit]\nDescription=Label Liuqin factory data with the Fedora policy\n'
        'DefaultDependencies=no\nRequires=liuqin-gnome-storage-guard.service\n'
        'After=local-fs.target liuqin-gnome-storage-guard.service\nBefore=basic.target\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        'ExecStart=/usr/sbin/setfiles -F /etc/selinux/targeted/contexts/files/file_contexts '
        '/usr/lib/firmware/cirrus /var/lib/liuqin-private /var/lib/liuqin-sensors\n')
    link('basic.target.requires/liuqin-private-labels.service','../liuqin-private-labels.service')
    for name in ('liuqin-gnome-usb-rescue','liuqin-slpi'):
        link('multi-user.target.wants/'+name+'.service','../'+name+'.service')
    link('multi-user.target.wants/iio-sensor-proxy.service','/usr/lib/systemd/system/iio-sensor-proxy.service')
    link('graphical.target.wants/liuqin-backlight-default.service','../liuqin-backlight-default.service')
    link('multi-user.target.wants/liuqin-display-policy.service','../liuqin-display-policy.service')
    link('display-manager.service','/usr/lib/systemd/system/plasmalogin.service')
    link('graphical.target.wants/plasmalogin.service','/usr/lib/systemd/system/plasmalogin.service')
    link('default.target','/usr/lib/systemd/system/graphical.target')
    if 'plymouth' in data.get('packages',[]):
        for name,content in boot_splash_files().items():common.write_config(root,name,content)
        for target,name in (('sysinit','start'),('multi-user','quit'),('multi-user','quit-wait')):
            link(target+'.target.wants/plymouth-'+name+'.service','/usr/lib/systemd/system/plymouth-'+name+'.service')
    for name in ('NetworkManager','bluetooth','chronyd','systemd-resolved'):
        link('multi-user.target.wants/'+name+'.service','/usr/lib/systemd/system/'+name+'.service')
    if (root/'usr/lib/systemd/system/sshd.service').is_file():
        services=['sshd.service']
        if (root/'usr/lib/systemd/system/sshd.socket').is_file():services.append('sshd.socket')
        chroot(root,['systemctl','--no-reload','disable',*services])
    for rel in ('etc/systemd/system/graphical.target.wants/liuqin-sensor-proxy-refresh.service',
                'etc/systemd/user/graphical-session.target.wants/liuqin-sensor-proxy-session-refresh.service'):
        (root/rel).unlink(missing_ok=True)
    common.write_config(root,'etc/systemd/logind.conf.d/90-liuqin.conf','[Login]\nHandlePowerKey=suspend\n')
    chroot(root,['systemd-sysusers'])
    if data['first_boot_setup']:
        regular=[line.split(':')[0] for line in (root/'etc/passwd').read_text().splitlines()
                 if 1000<=int(line.split(':')[2])<=60000]
        if regular or (root/'etc/plasma-setup-done').exists():raise ValueError('首次初始化镜像已有普通账号或完成标记。')
        common.write_config(root,'etc/xdg/plasmasetuprc','[Accounts]\nUserGroups=wheel\n')
        chroot(root,['usermod','--lock','root']);chroot(root,['useradd','-D','-s','/bin/bash'])
        link('multi-user.target.wants/plasma-setup.service','/usr/lib/systemd/system/plasma-setup.service')
    else:
        original=common.chroot;common.chroot=chroot
        try:common.configure_account(root,data,output,admin_group='wheel')
        finally:common.chroot=original
        entry=next(line.split(':') for line in (root/'etc/passwd').read_text().splitlines()
                   if line.split(':',1)[0]==data['user'])
        ensure_shell_defaults(root/entry[5].lstrip('/'),root/'etc/skel',int(entry[2]),int(entry[3]))
        common.write_config(root,'etc/plasma-setup-done','preconfigured\n')
    # Require a supported SELinux policy, rather than silently disabling Fedora's MAC.
    common.write_config(root,'etc/selinux/config','SELINUX=enforcing\nSELINUXTYPE=targeted\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('runtime','apps','extract','sensors','kernel-files','stats','fingerprint','splash'))
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--archive-sha256',required=True)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--recipe',type=Path)
    parser.add_argument('--rpms',type=Path);parser.add_argument('--rom-sensors',type=Path)
    parser.add_argument('--jobs',type=int,default=12)
    parser.add_argument('--stats-source',type=Path)
    parser.add_argument('--fingerprint-libfprint-source',type=Path)
    parser.add_argument('--fingerprint-fprintd-source',type=Path)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('Fedora 装配需要 root 或完整 UID/GID 映射的用户命名空间。')
    if os.environ.get('LIUQIN_SOURCE_ROOT_NS')!='1':
        subprocess.run(['unshare','--mount','--propagation','private','--pid','--fork',sys.executable,
            str(Path(__file__).resolve()),*sys.argv[1:]],check=True,env=dict(os.environ,LIUQIN_SOURCE_ROOT_NS='1'));return
    output=args.out.resolve()
    if not output.is_relative_to(PROJECT/'out') or output==PROJECT/'out' or output.exists():parser.error('需要 out/ 下的新目录。')
    output.mkdir(parents=True);root=output/'rootfs'
    if args.action=='runtime':extract_oci(args.archive,args.archive_sha256,root)
    else:common.extract(args.archive,args.archive_sha256,root)
    if args.action=='extract':return
    if args.action=='splash':
        with mounted_root(root):
            enable_documentation(root)
            dnf(root,'install','plymouth','plymouth-theme-spinner','systemd-udev')
            collector=root/'tmp/liuqin-plymouth-runtime.py'
            shutil.copyfile(PROJECT/'build/lib/plymouth_runtime.py',collector)
            chroot(root,['python3','/tmp/liuqin-plymouth-runtime.py','--out','/tmp/liuqin-splash-runtime'])
            for name in ('runtime.tar.gz','runtime.json'):
                shutil.copyfile(root/'tmp/liuqin-splash-runtime'/name,output/name)
        shutil.rmtree(root)
        return
    if args.action=='sensors':
        if not args.rom_sensors:parser.error('需要原厂传感器配置。')
        with arm64_execution():
            subprocess.run(['sh',str(PROJECT/'build/scripts/build-liuqin-sensors-stack.sh')],check=True,
                env=dict(os.environ,ROOTFS=str(root),ROM_SENSORS=str(args.rom_sensors),BUILD_DISTRIBUTION='fedora',
                         OUT_DIR=str(output/'sensors'),JOBS=str(args.jobs)))
        shutil.copyfile(output/'sensors/artifacts/sensor-stack.tar',output/'sensor-stack.tar');return
    with mounted_root(root):
        enable_documentation(root)
        if args.action=='runtime':dnf(root,'install',*RUNTIME_PACKAGES)
        elif args.action=='stats':
            if not args.stats_source:parser.error('需要 KSystemStats 源码。')
            from fedora_stats import build
            build(root,args.stats_source,output,args.jobs,chroot,dnf);return
        elif args.action=='fingerprint':
            if not args.fingerprint_libfprint_source or not args.fingerprint_fprintd_source:
                parser.error('需要固定的 libfprint/TOD 与 fprintd 源码。')
            from fedora_fingerprint import build,pack
            payload=build(root,dict(libfprint=args.fingerprint_libfprint_source,
                fprintd=args.fingerprint_fprintd_source),output,args.jobs,chroot,dnf)
            versions=chroot(root,['rpm','-q','--qf','%{NAME} >= %{VERSION}\n',
                'glibc','glib2','libgusb','libgudev','polkit-libs','systemd-libs','openssl-libs'],
                capture_output=True,text=True).stdout.splitlines()
            pack(payload,output,versions);return
        elif args.action in ('apps','kernel-files'):
            if not args.rpms or not args.rpms.is_dir():parser.error('需要已构建的 RPM 目录。')
            if args.action=='apps':
                data=common.recipe(args.recipe,'fedora')
                install_desktop_packages(root,data)
            staging=root/'tmp/liuqin-packages';staging.mkdir()
            for file in sorted(args.rpms.glob('*.rpm')):shutil.copyfile(file,staging/file.name)
            packages=['/tmp/liuqin-packages/'+p.name for p in sorted(staging.glob('*.rpm'))]
            if args.action=='kernel-files':
                # The runtime already contains kmod and Python. Install only
                # the audited local kernel payloads, retaining dependency and
                # transaction checks without refreshing online repositories.
                chroot(root,['rpm','-Uvh','--replacepkgs',*packages])
            else:
                dnf(root,'install',*packages)
            shutil.rmtree(staging)
            if args.action=='apps':
                if data.get('remove'):dnf(root,'remove',*data['remove'])
                restore_core_manpages(root)
                if data.get('overlay'):common.overlay(data['overlay'],root)
                configure(root,data,output)
                check_desktop_runtime(root,first_boot_setup=data['first_boot_setup'])
        inventory=chroot(root,['rpm','-qa','--qf','%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n'],capture_output=True).stdout
        (output/'packages.tsv').write_bytes(inventory)
        dnf(root,'clean','all')
    common.write_config(root,'etc/machine-id','');(root/'var/lib/dbus/machine-id').unlink(missing_ok=True)
    clean_ssh_identity(root)
    for directory in (root/'tmp',root/'var/cache/libdnf5'):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
                else:path.unlink()
    if args.action=='kernel-files':return
    subprocess.run(['sh',str(PROJECT/'build/lib/rootfs-archive.sh'),'pack',str(root),str(output/'root.tar.gz')],check=True)
    print('Fedora ARM64 系统装配完成：'+str(output/'root.tar.gz'),flush=True)


if __name__=='__main__':main()
