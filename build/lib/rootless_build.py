#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run a rootfs stage as mapped root, with private mounts, PIDs and binfmt."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def mapped_ids(text):
    rows=[]
    for line in text.splitlines():
        inside,outside,count=map(int,line.split())
        if min(inside,outside)<0 or count<=0:raise ValueError('invalid UID/GID map')
        rows.append((inside,outside,count))
    if not any(a==0 and b!=0 for a,b,n in rows):
        return False
    covered=0
    for start,outside,count in sorted(rows):
        if start>covered:return False
        covered=max(covered,start+count)
        if covered>=65535:return True
    return False


def main():
    command=sys.argv[1:]
    if command[:1]==['--']:command=command[1:]
    if not command:raise ValueError('rootless_build.py -- COMMAND [ARG...]')
    if os.geteuid()!=0 or not all(mapped_ids(Path('/proc/self/'+kind+'_map').read_text())
                                 for kind in ('uid','gid')):
        raise RuntimeError('rootless 构建需要 Podman 完整 UID/GID 映射，不能只映射当前用户。')
    if os.environ.get('LIUQIN_ROOTLESS_BUILD_NS')!='1':
        unshare=shutil.which('unshare')
        if not unshare:raise RuntimeError('缺少 util-linux unshare。')
        os.execvpe(unshare,[unshare,'--mount','--propagation','private','--pid','--fork',
                           '--kill-child=SIGKILL','--mount-proc',sys.executable,
                           str(Path(__file__).resolve()),'--',*command],
                   dict(os.environ,LIUQIN_ROOTLESS_BUILD_NS='1'))
    if os.getpid()!=1:
        raise RuntimeError('rootless 构建缺少独立 PID 命名空间。')
    # Shadow the host's registry even when it already has a register file.
    # Kernel >= 6.7 keeps these registrations in the Podman user namespace,
    # never in the host's initial user namespace.
    subprocess.run(['mount','-t','binfmt_misc','binfmt_misc','/proc/sys/fs/binfmt_misc'],check=True)
    environment=dict(os.environ,LIUQIN_SOURCE_ROOT_NS='1')
    os.execvpe(command[0],command,environment)


if __name__=='__main__':main()
