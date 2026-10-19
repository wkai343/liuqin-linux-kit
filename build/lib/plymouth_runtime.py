# SPDX-License-Identifier: MIT
"""Collect an independent ARM64 Plymouth runtime using Fedora's own helper.

Run inside the ARM64 build root, after installing Plymouth and its spinner theme.
The exported files contain no device data, root account data or kernel modules.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile


def rooted(root, path):
    """Resolve usrmerge and absolute symlinks inside an exported filesystem."""
    root=Path(root)
    pending=list(PurePosixPath(path).parts);parts=[];links=0
    while pending:
        part=pending.pop(0)
        if part in ('','/','.'):continue
        if part=='..':
            if not parts:raise ValueError('runtime symlink escapes its root')
            parts.pop();continue
        candidate=root.joinpath(*parts,part)
        if candidate.is_symlink():
            links+=1
            if links>40:raise ValueError('runtime symlink loop')
            target=os.readlink(candidate)
            if target.startswith('/'):parts=[]
            pending=list(PurePosixPath(target).parts)+pending
        else:parts.append(part)
    result=root.joinpath(*parts)
    if not result.exists():raise ValueError('missing runtime link target: '+str(path))
    return result


def permitted(path):
    if path.startswith(('lib/','bin/')):path='usr/'+path
    exact={'etc/system-release','etc/os-release','usr/lib/os-release',
           'usr/lib/fedora-release',
           'etc/udev/rules.d/70-liuqin-splash.rules',
           'usr/bin/plymouth','usr/bin/plymouthd','usr/bin/udevadm','usr/bin/fc-match',
           'bin/plymouth','bin/plymouthd','bin/udevadm',
           'lib/ld-linux-aarch64.so.1','usr/lib/ld-linux-aarch64.so.1',
           'usr/lib/systemd/systemd-udevd','usr/libexec/plymouth/plymouthd-fd-escrow',
           'usr/share/xml/fontconfig/fonts.dtd'}
    prefixes=('etc/plymouth/','etc/fonts/','usr/share/plymouth/','usr/share/fonts/',
              'usr/share/fontconfig/','usr/share/pixmaps/','usr/share/licenses/',
              'usr/share/X11/','usr/lib64/','lib64/','usr/lib/plymouth/')
    return (path in exact or path.startswith(prefixes) or
            bool(re.fullmatch(r'usr/lib/systemd/libsystemd-shared-[A-Za-z0-9.+_-]+\.so',path)))


def materialize(raw, output):
    """Flatten usrmerge links without replacing the initramfs /bin or /lib."""
    output=Path(output);output.mkdir()
    records={};owners=set();total=0
    def visit(logical, ancestors):
        nonlocal total
        real=rooted(raw,logical)
        if real.is_dir():
            if real in ancestors:raise ValueError('recursive runtime directory')
            (output/logical).mkdir(parents=True,exist_ok=True)
            for child in sorted(real.iterdir()):visit(str(PurePosixPath(logical)/child.name),ancestors|{real})
            return
        if not real.is_file() or not permitted(logical):
            raise ValueError('unexpected runtime file: '+logical)
        content=real.read_bytes();total+=len(content)
        if total>128*1024*1024 or len(records)>=4096:raise ValueError('runtime exceeds its size limit')
        if content.startswith(b'\x7fELF'):
            if content[4:6]!=b'\x02\x01' or int.from_bytes(content[18:20],'little')!=183:
                raise ValueError('runtime contains non-AArch64 ELF: '+logical)
        mode=0o755 if real.stat().st_mode&0o111 else 0o644
        target=output/logical;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content);target.chmod(mode)
        records[logical]={'sha256':hashlib.sha256(content).hexdigest(),'mode':mode,'size':len(content)}
        owners.add('/'+str(real.relative_to(raw)))
    for child in sorted(Path(raw).iterdir()):visit(child.name,set())
    return records,owners


def collect(output, themes_dir=Path('/usr/share/plymouth/themes')):
    output=Path(output)
    if output.exists():raise ValueError('runtime output already exists')
    output.mkdir(parents=True)
    raw=output/'raw';raw.mkdir()
    helper=next((p for p in (Path('/usr/lib/plymouth/plymouth-populate-initrd'),
                            Path('/usr/libexec/plymouth/plymouth-populate-initrd')) if p.is_file()),None)
    if helper is None:raise ValueError('Fedora Plymouth population helper is missing')
    env=dict(os.environ,LC_ALL='C',PLYMOUTH_THEME_NAME='spinner',SYSTEMD_UNIT_DIR='/nonexistent',
             HOME='/nonexistent',XDG_CONFIG_HOME='/nonexistent',XDG_DATA_HOME='/nonexistent')
    subprocess.run([str(helper),'-t',str(raw)],env=env,check=True)
    subprocess.run(['/usr/lib/dracut/dracut-install','-D',str(raw),'-l','-a',
                    '/usr/bin/udevadm','/usr/lib/systemd/systemd-udevd',
                    '/usr/lib64/plymouth/script.so','/usr/bin/fc-match'],env=env,check=True)
    # The official script plugin supplies image/text rendering; include its
    # font configuration as well as the four generated project themes.
    for directory in ('etc/fonts','usr/share/fontconfig'):
        shutil.copytree(Path('/')/directory,raw/directory,symlinks=True,dirs_exist_ok=True)
    dtd=raw/'usr/share/xml/fontconfig/fonts.dtd';dtd.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile('/usr/share/xml/fontconfig/fonts.dtd',dtd)
    for angle in (0,90,180,270):
        name='liuqin-rotate-'+str(angle)
        for filename in ('theme.script',name+'.plymouth'):
            source=Path(themes_dir)/name/filename
            if source.is_symlink() or not source.is_file():
                raise ValueError('generated splash theme is missing: '+str(source))
            destination=raw/'usr/share/plymouth/themes'/name/filename
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,destination)
    conf=raw/'etc/plymouth/plymouthd.conf';conf.parent.mkdir(parents=True,exist_ok=True)
    conf.write_text('[Daemon]\nTheme=spinner\nShowDelay=0\nDeviceTimeout=5\n')
    # The early daemon needs graphics metadata only, never storage or custom
    # host rules. Modules are selected separately from the target kernel tree.
    rules=raw/'etc/udev/rules.d/70-liuqin-splash.rules';rules.parent.mkdir(parents=True,exist_ok=True)
    rules.write_text('SUBSYSTEM=="drm", KERNEL=="card[0-9]*", TAG+="seat", TAG+="master-of-seat"\n'
                     'SUBSYSTEM=="input", KERNEL=="event[0-9]*", TAG+="seat"\n')
    # This path is generated here, rather than copied from the source system.
    data=rules.read_bytes();rules.unlink()
    normalized=output/'runtime'
    records,sources=materialize(raw,normalized)
    target=normalized/'etc/udev/rules.d/70-liuqin-splash.rules';target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(data);target.chmod(0o644)
    records[str(target.relative_to(normalized))]={'sha256':hashlib.sha256(data).hexdigest(),'mode':0o644,'size':len(data)}
    query=subprocess.run(['rpm','-qf','--qf','%{NAME}\n',*sorted(sources)],env=env,text=True,capture_output=True)
    packages=sorted({line for line in query.stdout.splitlines() if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.-]*',line)})
    metadata=subprocess.check_output(['rpm','-q','--qf','%{NEVRA}\t%{SOURCERPM}\n',*packages],env=env,text=True)
    # Retain licenses next to the runtime's package provenance.
    for package in packages:
        for directory in sorted(Path('/usr/share/licenses').glob(package+'*')):
            for file in sorted(directory.rglob('*')):
                if not file.is_file():continue
                name=str(file.relative_to('/'));destination=normalized/name
                destination.parent.mkdir(parents=True,exist_ok=True);content=file.read_bytes()
                destination.write_bytes(content);destination.chmod(0o644)
                records[name]={'sha256':hashlib.sha256(content).hexdigest(),'mode':0o644,'size':len(content)}
    required=['usr/bin/plymouth','usr/bin/plymouthd','usr/bin/udevadm',
              'usr/lib/systemd/systemd-udevd','etc/plymouth/plymouthd.conf',
              'usr/share/plymouth/themes/spinner/spinner.plymouth',
              'usr/lib64/plymouth/script.so','usr/bin/fc-match',
              'usr/lib64/plymouth/renderers/frame-buffer.so']
    for name in required:
        if name not in records:raise ValueError('required runtime file is missing: '+name)
    if not any(name.endswith('/renderers/drm.so') for name in records):
        raise ValueError('Plymouth DRM renderer is missing')
    manifest={'schema':1,'architecture':'aarch64','theme':'spinner','firmware_splash':True,
              'files':records,'packages':metadata.splitlines()}
    (output/'runtime.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    with (output/'runtime.tar.gz').open('wb') as file:
        with gzip.GzipFile(filename='',mode='wb',fileobj=file,mtime=0) as compressed:
            with tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as archive:
                for name in sorted(records):
                    path=normalized/name;info=archive.gettarinfo(str(path),arcname=name)
                    info.uid=info.gid=0;info.uname=info.gname='root';info.mtime=0
                    with path.open('rb') as stream:archive.addfile(info,stream)
    print(json.dumps({'files':len(records),'uncompressed_bytes':sum(v['size'] for v in records.values()),
                      'archive_bytes':(output/'runtime.tar.gz').stat().st_size,'output':str(output)},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--themes-dir',type=Path,default=Path('/usr/share/plymouth/themes'))
    args=parser.parse_args();collect(args.out,args.themes_dir)
