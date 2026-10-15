# SPDX-License-Identifier: MIT
"""Package an audited payload tree; no device access or host package installation."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile


def build(payload, name, version, output, *, requires=(), prerequires=(), provides=(),
          release='1.liuqin', architecture='aarch64', license='MIT', summary=None,
          post='', posttrans='', conflicts=()):
    payload, output = Path(payload).resolve(), Path(output).resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]*', name) or not re.fullmatch(r'[0-9][A-Za-z0-9.+~^_]*', version):
        raise ValueError('RPM 包名称或版本无效。')
    if architecture not in ('aarch64','noarch'): raise ValueError('RPM 架构无效。')
    for value in (*requires,*prerequires,*provides,*conflicts,release,license,summary or name):
        if any(c in value for c in ('\n','\r','%')):raise ValueError('RPM 元数据包含特殊字符。')
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.rpm-', dir=output.parent) as temp:
        work=Path(temp)
        for directory in ('BUILD','BUILDROOT','RPMS','SOURCES','SPECS','SRPMS'):(work/directory).mkdir()
        source=work/'SOURCES/payload';shutil.copytree(payload,source,symlinks=True)
        files=[]
        for path in sorted(source.rglob('*')):
            if path.is_dir() and not path.is_symlink():continue
            rel=path.relative_to(source).as_posix()
            if any(c in rel for c in ('\n','\r','%','"')):raise ValueError('RPM 文件路径无效。')
            tag='%license ' if rel.startswith(('usr/share/doc/','usr/share/licenses/')) else ''
            if rel.startswith('etc/') and not path.is_symlink():tag='%config(noreplace) '
            files.append(tag+'"/'+rel+'"')
        if not files:raise ValueError('RPM 文件树为空。')
        spec=[f'Name: {name}',f'Version: {version}',f'Release: {release}',
              f'Summary: {summary or name}',f'License: {license}','AutoReqProv: no']
        if architecture=='noarch':spec.append('BuildArch: noarch')
        # With AutoReqProv disabled RPM 4.20 omits the ISA self-provide.
        # Fedora consumers (e.g. plasma-workspace) require this capability,
        # independently of the package header's architecture.
        if architecture=='aarch64':
            spec.append(f'Provides: {name}(aarch-64) = {version}-{release}')
        for label,values in [('Requires',requires),('Requires(pre)',prerequires),('Provides',provides),('Conflicts',conflicts)]:
            if values:spec.append(label+': '+', '.join(values))
        spec+=['','%description',summary or name,'','%install','mkdir -p "%{buildroot}"',
               'cp -a "%{_sourcedir}/payload/." "%{buildroot}/"','',
               '%files','%defattr(-,root,root,-)',*files]
        for label,body in [('post',post),('posttrans',posttrans)]:
            if body:spec+=['','%'+label,body]
        path=work/'SPECS/package.spec';path.write_text('\n'.join(spec)+'\n')
        subprocess.run(['rpmbuild','-bb','--target',architecture if architecture!='noarch' else 'aarch64',
            '--define','_topdir '+str(work),'--define','_build_id_links none',
            '--define','__os_install_post %{nil}','--define','__debug_install_post %{nil}',
            '--define','_enable_debug_packages 0',str(path)],check=True,
            env=dict(os.environ,SOURCE_DATE_EPOCH='0'))
        built=list((work/'RPMS').rglob('*.rpm'))
        if len(built)!=1:raise ValueError('RPM 构建产物缺失或重复。')
        destination=output/built[0].name
        if destination.exists():raise ValueError('RPM 输出已存在。')
        shutil.copyfile(built[0],destination)
        return destination
