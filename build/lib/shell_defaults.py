#!/usr/bin/python3 -I
# SPDX-License-Identifier: MIT
"""Seed missing/empty Fedora Bash startup files for a newly created account."""
import argparse
import os
from pathlib import Path
import pwd
import secrets
import stat

FILES = ('.bashrc', '.bash_profile')


def check_skeleton(skeleton):
    """Read the distro's own templates; do not invent a replacement prompt."""
    fd = os.open(skeleton, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.fstat(fd)
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
            raise ValueError('Bash 模板目录归属或权限不正确。')
        result = {}
        for name in FILES:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise ValueError('Bash 模板必须是受保护的普通文件：' + name)
                data = stream.read()
                if not data or len(data) != info.st_size:
                    raise ValueError('Bash 模板为空或读取不完整：' + name)
                result[name] = data
        return result
    finally:
        os.close(fd)


def ensure(home, skeleton, uid, gid):
    templates = check_skeleton(skeleton)
    home = Path(home).absolute()
    if home.resolve() != home:
        raise ValueError('新账号的主目录不能通过符号链接写入。')
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    restored = []
    try:
        directory = os.fstat(fd)
        if directory.st_uid != uid or directory.st_mode & 0o022:
            raise ValueError('新账号主目录归属或权限不正确。')
        for name, data in templates.items():
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is not None:
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('用户 Bash 配置不是普通文件：' + name)
                if before.st_size:
                    if before.st_uid != uid:
                        raise ValueError('用户 Bash 配置归属不正确：' + name)
                    continue
            temporary = '.liuqin-shell-' + secrets.token_hex(12)
            try:
                output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                with os.fdopen(output, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchown(stream.fileno(), uid, gid)
                    os.fchmod(stream.fileno(), 0o644)
                    os.fsync(stream.fileno())
                try:
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                identity = lambda value: None if value is None else (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                if identity(current) != identity(before):
                    raise ValueError('用户 Bash 配置在初始化时发生变化：' + name)
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
                check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(check, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if stream.read() != data or info.st_uid != uid or info.st_gid != gid:
                        raise ValueError('用户 Bash 配置回读不一致：' + name)
                restored.append(name)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(fd)
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('此初始化助手需要 root。')
    account = pwd.getpwnam(args.username)
    if not 1000 <= account.pw_uid <= 60000:
        parser.error('不能为系统账号初始化用户 Bash 配置。')
    ensure(account.pw_dir, '/etc/skel', account.pw_uid, account.pw_gid)


if __name__ == '__main__':
    main()
