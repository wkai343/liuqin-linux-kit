# SPDX-License-Identifier: MIT
"""Stage an audited userland runtime and matching early display modules."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_kernel import extract_modules, parse_contract
from plymouth_runtime import permitted


def safe_name(name):
    return (not name.startswith('/') and '..' not in PurePosixPath(name).parts and
            str(PurePosixPath(name))==name and name not in ('','.'))


def put(root,name,content,mode,link=None):
    if not safe_name(name):raise ValueError('invalid splash path: '+name)
    path=root/name
    for parent in [path,*path.parents]:
        if parent==root.parent:break
        if parent.is_symlink():raise ValueError('splash destination traverses a symlink: '+name)
    if path.exists():raise ValueError('splash file collides with initramfs: '+name)
    path.parent.mkdir(parents=True,exist_ok=True)
    if link is None:
        path.write_bytes(content);path.chmod(mode)
    else:
        if not link.is_relative_to(root) or link.is_symlink() or not link.is_file():
            raise ValueError('invalid splash hardlink source')
        os.link(link,path)
    return path


def stage(runtime,modules,contract,staging):
    runtime,modules,contract,staging=map(Path,(runtime,modules,contract,staging))
    staging=staging.resolve()
    if staging in (Path('/'),PROJECT) or not staging.is_dir():
        raise ValueError('splash staging needs an existing build directory')
    identity=parse_contract(contract.read_bytes())
    manifest=json.loads((runtime/'runtime.json').read_text())
    records=manifest.get('files',{})
    if (manifest.get('schema')!=1 or manifest.get('architecture')!='aarch64' or
        manifest.get('theme')!='spinner' or not records or len(records)>4096):
        raise ValueError('invalid Plymouth runtime manifest')
    required={'usr/bin/plymouth','usr/bin/plymouthd','usr/bin/udevadm',
              'usr/lib/systemd/systemd-udevd','etc/plymouth/plymouthd.conf',
              'usr/share/plymouth/themes/spinner/spinner.plymouth'}
    if not required.issubset(records) or not any(name.endswith('/renderers/drm.so') for name in records):
        raise ValueError('Plymouth runtime lacks required programs or renderer')
    found=set();total=0;elf_files={};shared_bytes=0
    with tarfile.open(runtime/'runtime.tar.gz','r:gz') as archive:
        for item in archive:
            name=item.name
            if (not safe_name(name) or not permitted(name) or name in found or name not in records or
                not item.isfile() or item.uid!=0 or item.gid!=0 or item.mode not in (0o644,0o755)):
                raise ValueError('invalid Plymouth runtime entry: '+name)
            expected=records[name];total+=item.size
            if total>128*1024*1024 or item.size!=expected['size'] or item.mode!=expected['mode']:
                raise ValueError('Plymouth runtime metadata differs: '+name)
            content=archive.extractfile(item).read()
            if hashlib.sha256(content).hexdigest()!=expected['sha256']:
                raise ValueError('Plymouth runtime content differs: '+name)
            if content.startswith(b'\x7fELF') and (content[4:6]!=b'\x02\x01' or int.from_bytes(content[18:20],'little')!=183):
                raise ValueError('Plymouth runtime contains a foreign ELF: '+name)
            if name in {'usr/bin/plymouth','usr/bin/plymouthd','usr/bin/udevadm','usr/lib/systemd/systemd-udevd'} and not content.startswith(b'\x7fELF'):
                raise ValueError('Plymouth runtime program is not ELF: '+name)
            # All members keep their content/architecture checks. Share only
            # immutable ELF data, never configs; GNU newc preserves aliases.
            key=(expected['sha256'],item.mode,item.size)
            share=content.startswith(b'\x7fELF') and not name.startswith('etc/')
            link=elf_files.get(key) if share else None
            destination=put(staging,name,content,item.mode,link=link)
            if share:elf_files.setdefault(key,destination)
            if link is not None:shared_bytes+=item.size
            found.add(name)
    if found!=set(records):raise ValueError('Plymouth runtime is incomplete')
    order=[];hashes=[]
    with tempfile.TemporaryDirectory(prefix='.splash-modules-',dir=staging.parent) as temporary:
        root=Path(temporary)
        extract_modules(modules,root,contract.read_bytes())
        tree=root/'usr/lib/modules'/identity['release']
        dependencies={}
        for line in (tree/'modules.dep').read_text().splitlines():
            name,values=line.split(':',1)
            dependencies[name]=values.split()
        selected=[];visited=set();pending=set()
        def visit(name):
            if name in visited:return
            if not safe_name(name) or not name.endswith('.ko') or name not in dependencies or name in pending:
                raise ValueError('invalid early module dependency: '+name)
            pending.add(name)
            for dependency in dependencies[name]:visit(dependency)
            pending.remove(name);visited.add(name);selected.append(name)
        for filename in ('fsa4480.ko','dispcc-sm8450.ko'):
            matches=[name for name in dependencies if PurePosixPath(name).name==filename]
            if len(matches)>1 or (not matches and filename=='dispcc-sm8450.ko'):
                raise ValueError('early splash requires the native display-clock module')
            if matches:visit(matches[0])
        names=set()
        for name in selected:
            module=PurePosixPath(name).stem.replace('-','_')
            if not re.fullmatch(r'[A-Za-z0-9_]+',module) or module in names:
                raise ValueError('early module name collision')
            names.add(module);content=(tree/name).read_bytes()
            if not content.startswith(b'\x7fELF') or int.from_bytes(content[18:20],'little')!=183:
                raise ValueError('early module is not ARM64')
            destination='usr/lib/liuqin-splash/'+module+'.ko'
            put(staging,destination,content,0o644)
            order.append(module);hashes.append(hashlib.sha256(content).hexdigest()+'  '+destination)
    put(staging,'etc/liuqin-splash-modules.order',('\n'.join(order)+'\n').encode(),0o644)
    put(staging,'etc/liuqin-splash-modules.sha256',('\n'.join(hashes)+'\n').encode(),0o644)
    put(staging,'usr/libexec/liuqin-boot-splash',(PROJECT/'device/boot/liuqin-boot-splash.sh').read_bytes(),0o755)
    print('Plymouth runtime: '+str(len(records))+' files; shared ELF bytes: '+str(shared_bytes)+
          '; early modules: '+', '.join(order))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('runtime','modules','contract','staging'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();stage(args.runtime,args.modules,args.contract,args.staging)
