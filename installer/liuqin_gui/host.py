# SPDX-License-Identifier: MIT
"""Host portability helpers; importing this module never accesses USB."""
import base64
import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

IS_WINDOWS = sys.platform == 'win32'
INSTALLER = Path(__file__).resolve().parents[1]
WINDOWS_HTTP_PORT = 37631


def configure_stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')


def fastboot_path():
    explicit = os.environ.get('LIUQIN_FASTBOOT')
    name = 'fastboot.exe' if IS_WINDOWS else 'fastboot'
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError('LIUQIN_FASTBOOT 指定的文件不存在。')
        return str(path.resolve())
    candidates = [INSTALLER.parent/'platform-tools'/name, INSTALLER/'platform-tools'/name]
    for variable in ('ANDROID_SDK_ROOT', 'ANDROID_HOME'):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable])/'platform-tools'/name)
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError('未找到 Fastboot。请将官方 platform-tools 目录放在 installer 旁边，或加入 PATH。')


def run_process(arguments, **kwargs):
    if IS_WINDOWS:
        kwargs.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
    if kwargs.get('text'):
        kwargs.setdefault('encoding', 'utf-8')
        kwargs.setdefault('errors', 'replace')
    return subprocess.run(arguments, **kwargs)


def device_lock_directory():
    if IS_WINDOWS:
        directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Liuqin/LinuxInstaller/locks'
    else:
        directory = Path(os.environ.get('XDG_RUNTIME_DIR', tempfile.gettempdir()))/('liuqin-installer-'+str(os.getuid()))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or (hasattr(directory, 'is_junction') and directory.is_junction()):
        raise RuntimeError('设备锁目录不能是链接。')
    if not IS_WINDOWS and directory.stat().st_uid != os.getuid():
        raise RuntimeError('设备锁目录所有权不正确。')
    return directory


class FileLock:
    """Hold an OS lock until close, including across separate worker processes."""
    def __init__(self, path, *, blocking=True, timeout=30):
        path = Path(path)
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise OSError('锁文件不能是链接。')
        self.fd = os.open(path, os.O_CREAT|os.O_RDWR|getattr(os, 'O_NOFOLLOW', 0), 0o600)
        deadline = time.monotonic()+timeout
        try:
            while True:
                try:
                    if IS_WINDOWS:
                        import msvcrt
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking or time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def check_installer_support(manifest):
    if IS_WINDOWS and manifest.get('installer_usb_network') != 'rndis':
        raise RuntimeError('Windows 需要新版 installer.img 的 RNDIS 安装通道，请先重新构建安装镜像。')


def server_address(address):
    if IS_WINDOWS and address != '192.168.7.1':
        raise RuntimeError('请点击“设置 USB 网络”，为平板专用网卡配置地址后重试。')
    return address, WINDOWS_HTTP_PORT if IS_WINDOWS else 0


def windows_network_command():
    script = INSTALLER/'windows-network.ps1'
    if not script.is_file():
        raise FileNotFoundError('缺少 Windows USB 网络设置脚本。')
    # ArgumentList becomes a Windows command line. Quote the file argument,
    # then quote that literal for PowerShell; use UTF-16 for Unicode paths.
    argument = ('"'+str(script)+'"').replace("'", "''")
    command = ("$ErrorActionPreference='Stop'; try { $p=Start-Process -FilePath "
               "(Join-Path $PSHOME 'powershell.exe') -Verb RunAs -Wait -PassThru "
               "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','"+argument+
               "'); exit $p.ExitCode } catch { Write-Error $_; exit 1 }")
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    return [str(powershell), '-NoProfile', '-EncodedCommand',
            base64.b64encode(command.encode('utf-16le')).decode('ascii')]
