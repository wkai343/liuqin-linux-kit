# SPDX-License-Identifier: MIT
"""Assemble a clean, reproducible kernel checkout without editing user sources."""
from pathlib import Path
import os
import subprocess
from source_build import PROJECT, sha


def assemble(source, commit, patches, output):
    output=Path(output);target=output/'source'
    subprocess.run(['git','clone','--no-hardlinks','--no-checkout',str(source),str(target)],check=True)
    subprocess.run(['git','-C',str(target),'checkout','--detach',commit],check=True)
    for patch in patches:
        subprocess.run(['git','-C',str(target),'apply','--check',str(patch)],check=True)
        subprocess.run(['git','-C',str(target),'apply','--index',str(patch)],check=True)
    tree=subprocess.check_output(['git','-C',str(target),'write-tree'],text=True).strip()
    identity='Liuqin Linux Kit build'
    env=dict(os.environ,GIT_AUTHOR_NAME=identity,GIT_COMMITTER_NAME=identity,
             GIT_AUTHOR_EMAIL='build@liuqin.invalid',GIT_COMMITTER_EMAIL='build@liuqin.invalid',
             GIT_AUTHOR_DATE='2026-10-13T00:00:00+00:00',GIT_COMMITTER_DATE='2026-10-13T00:00:00+00:00')
    message='Assemble Liuqin board adaptation for local build\n\nBase: '+commit+'\n'
    for patch in patches:message+='Patch: '+Path(patch).name+' '+sha(patch)+'\n'
    derived=subprocess.check_output(['git','-C',str(target),'-c','commit.gpgsign=false','commit-tree',
        tree,'-p',commit],input=message,text=True,env=env).strip()
    subprocess.run(['git','-C',str(target),'update-ref','HEAD',derived],check=True)
    if subprocess.check_output(['git','-C',str(target),'status','--porcelain']):
        raise ValueError('内核适配快照不是干净目录。')
    (output/'source.commit').write_text(derived+'\n')
    return derived
