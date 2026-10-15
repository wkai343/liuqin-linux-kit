# SPDX-License-Identifier: MIT
"""Extract the pinned Fedora single-layer base image, without a container daemon."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from source_build import sha


def extract(archive, expected, root):
    archive, root = Path(archive), Path(root)
    if root.exists() or root.is_symlink() or sha(archive) != expected:
        raise ValueError('Fedora 基础镜像校验失败或输出已存在。')
    root.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as outer:
        entries = {m.name: m for m in outer.getmembers()}
        if len(entries) != len(outer.getmembers()) or any(not (m.isdir() or m.isfile()) for m in entries.values()):
            raise ValueError('OCI 外层归档包含链接、特殊文件或重复路径。')
        def blob(record):
            digest = record['digest']
            if not re.fullmatch('sha256:[0-9a-f]{64}', digest): raise ValueError('OCI 摘要格式无效。')
            stream = outer.extractfile('blobs/sha256/'+digest.split(':')[1])
            data = stream.read()
            if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != digest.split(':')[1]:
                raise ValueError('OCI 文件摘要不一致。')
            return data
        index = json.load(outer.extractfile('index.json'))
        if index.get('schemaVersion') != 2 or len(index.get('manifests', [])) != 1:
            raise ValueError('需要 Fedora 单一 OCI 镜像。')
        manifest = json.loads(blob(index['manifests'][0]))
        config = json.loads(blob(manifest['config']))
        if config.get('architecture') != 'arm64' or config.get('os') != 'linux' or len(manifest['layers']) != 1:
            raise ValueError('需要 ARM64 Linux 单层基础镜像。')
        data = blob(manifest['layers'][0])
    with tempfile.TemporaryDirectory(prefix='.fedora-layer-', dir=root.parent) as work:
        layer = Path(work)/'root.tar'; layer.write_bytes(data)
        with tarfile.open(layer) as tar:
            members = tar.getmembers(); names = {}; links = set()
            for m in members:
                name = os.path.normpath(m.name)
                if name.startswith('/') or '..' in Path(name).parts or any(p.startswith('.wh.') for p in Path(name).parts):
                    raise ValueError('OCI 层含越界路径或非基础镜像白化操作。')
                if name in names or not (m.isdir() or m.isfile() or m.issym() or m.islnk()):
                    raise ValueError('OCI 层含重复路径或特殊设备。')
                names[name] = m
                if m.issym(): links.add(name)
                if m.islnk() and (m.linkname.startswith('/') or '..' in Path(m.linkname).parts):
                    raise ValueError('OCI 硬链接离开镜像。')
            if any(str(p) in links for name in names for p in Path(name).parents):
                raise ValueError('OCI 文件通过归档中的符号链接写入。')
        root.mkdir()
        subprocess.run(['tar','--extract','--file='+str(layer),'--directory='+str(root),
                        '--numeric-owner','--same-owner','--same-permissions','--acls','--xattrs',
                        '--xattrs-include=*'], check=True)
    # The marker is for container runtime detection, not a physical device boot.
    (root/'run/.containerenv').unlink(missing_ok=True)
