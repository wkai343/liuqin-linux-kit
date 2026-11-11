#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared Fedora rootfs assembly helpers (no distribution/tool-runtime CLI)."""
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
from source_build import sha


def recipe(path, distribution='fedora'):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get('distribution') != distribution or distribution != 'fedora' or data.get('desktop') != 'plasma':
        raise ValueError('目标系统只支持 Fedora + Plasma 配方。')
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
        protected={'plasma-workspace','plasma-setup','plasma-login-manager','kwin','plasma-keyboard','qt6-qtvirtualkeyboard'}
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
        env=dict(os.environ,PATH='/usr/sbin:/usr/bin:/sbin:/bin',LC_ALL='C'),**kwargs)


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


def configure_account(root, data, output, admin_group='wheel'):
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


def remove_desktop_rescue_activation(root):
    path=Path(root)/'etc/systemd/system/multi-user.target.wants/liuqin-gnome-usb-rescue.service'
    if path.parent.resolve()!=path.parent:
        raise ValueError('Retired USB rescue activation has a symlink parent.')
    if path.is_symlink() and os.readlink(path)=='../liuqin-gnome-usb-rescue.service':
        path.unlink()
    elif path.exists() or path.is_symlink():
        raise ValueError('Review the modified retired desktop USB rescue activation before building.')
