# SPDX-License-Identifier: MIT
"""Stage an audited userland runtime and matching early display modules."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'shared'))
from liuqin_kernel import extract_modules, parse_contract
from plymouth_runtime import permitted
from splash_assets import supports_firmware, theme_files


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


def stage(runtime,modules,contract,staging,kernel_config=None,cc=None):
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
    order=[];hashes=[];backlight_order=[];backlight_hashes=[]
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
        backlights=[name for name in dependencies if PurePosixPath(name).name=='ktz8866.ko']
        if len(backlights)>1:
            raise ValueError('ambiguous early backlight module')
        if kernel_config is not None:
            config=Path(kernel_config).read_text().splitlines()
            if ('CONFIG_BACKLIGHT_KTZ8866=m' in config) != bool(backlights):
                raise ValueError('backlight module does not match the kernel configuration')
        if backlights:
            visit(backlights[0])
            # These are the audited dependencies of the Liuqin backlight.
            # No display-clock or mux driver may run before blanking succeeds.
            if any(PurePosixPath(name).stem.replace('-','_') not in
                   {'ktz8866','regmap_i2c'} for name in selected):
                raise ValueError('unaudited dependency in the early backlight phase')
        backlight_count=len(selected)
        for filename in ('fsa4480.ko','dispcc-sm8450.ko'):
            matches=[name for name in dependencies if PurePosixPath(name).name==filename]
            if len(matches)>1 or (not matches and filename=='dispcc-sm8450.ko'):
                raise ValueError('early splash requires the native display-clock module')
            if matches:visit(matches[0])
        names=set()
        for index,name in enumerate(selected):
            module=PurePosixPath(name).stem.replace('-','_')
            if not re.fullmatch(r'[A-Za-z0-9_]+',module) or module in names:
                raise ValueError('early module name collision')
            names.add(module);content=(tree/name).read_bytes()
            if not content.startswith(b'\x7fELF') or int.from_bytes(content[18:20],'little')!=183:
                raise ValueError('early module is not ARM64')
            destination='usr/lib/liuqin-splash/'+module+'.ko'
            put(staging,destination,content,0o644)
            checksum=hashlib.sha256(content).hexdigest()+'  '+destination
            hashes.append(checksum)
            if index<backlight_count:
                backlight_order.append(module);backlight_hashes.append(checksum)
            else:
                order.append(module)
    put(staging,'etc/liuqin-splash-backlight-modules.order',('\n'.join(backlight_order)+('\n' if backlight_order else '')).encode(),0o644)
    put(staging,'etc/liuqin-splash-backlight-modules.sha256',('\n'.join(backlight_hashes)+('\n' if backlight_hashes else '')).encode(),0o644)
    put(staging,'etc/liuqin-splash-modules.order',('\n'.join(order)+'\n').encode(),0o644)
    put(staging,'etc/liuqin-splash-modules.sha256',('\n'.join(hashes)+'\n').encode(),0o644)
    put(staging,'usr/libexec/liuqin-boot-splash',(PROJECT/'device/boot/liuqin-boot-splash.sh').read_bytes(),0o755)
    if kernel_config is not None and supports_firmware(runtime,kernel_config):
        if 'CONFIG_FB_SIMPLE=y' not in Path(kernel_config).read_text().splitlines():
            raise ValueError('firmware splash requires builtin simplefb')
        if not cc or not shutil.which(str(cc)):
            raise ValueError('firmware splash requires the ARM64 compiler')
        required=set(theme_files())|{'usr/lib64/plymouth/script.so',
                                    'usr/lib64/plymouth/renderers/frame-buffer.so','usr/bin/fc-match'}
        if not required.issubset(records):
            raise ValueError('firmware splash runtime is incomplete')
        source=PROJECT/'device/boot/splash'
        checks=[]
        for filename,name in [('framebuffer-check.c','liuqin-fb-check'),
                              ('framebuffer-renderer.c','liuqin-fw-plymouthd'),
                              ('logo-orientation.c','liuqin-logo-orientation')]:
            with tempfile.TemporaryDirectory(prefix='.splash-helper-',dir=staging.parent) as tmp:
                binary=Path(tmp)/name
                subprocess.run([str(cc),'-static','-Os','-Wall','-Wextra','-Werror',
                                str(source/filename),'-o',str(binary)],check=True)
                content=binary.read_bytes()
            if content[:6]!=b'\x7fELF\x02\x01' or int.from_bytes(content[18:20],'little')!=183:
                raise ValueError('splash helper is not ARM64')
            path='usr/libexec/'+name
            put(staging,path,content,0o755)
            checks.append(hashlib.sha256(content).hexdigest()+'  '+path)
        renderer=staging/'usr/lib64/plymouth/renderers/frame-buffer.so'
        path='usr/lib/liuqin-firmware-renderers/frame-buffer.so'
        put(staging,path,renderer.read_bytes(),renderer.stat().st_mode&0o777)
        checks.append(hashlib.sha256(renderer.read_bytes()).hexdigest()+'  '+path)
        # Both Fedora lookup paths exist inside the child-only renderer view.
        (staging/'lib64/plymouth/renderers').mkdir(parents=True,exist_ok=True)
        put(staging,'etc/liuqin-fb-check.sha256',('\n'.join(checks)+'\n').encode(),0o644)
        put(staging,'usr/libexec/liuqin-firmware-splash',(source/'firmware-stage.sh').read_bytes(),0o755)
        (staging/'etc/plymouth/plymouthd.conf').write_text(
            '[Daemon]\nTheme=liuqin-rotate-90\nShowDelay=0\nDeviceTimeout=5\nDeviceScale=2\n')
    print('Plymouth runtime: '+str(len(records))+' files; shared ELF bytes: '+str(shared_bytes)+
          '; backlight modules: '+', '.join(backlight_order)+'; display modules: '+', '.join(order))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('runtime','modules','contract','staging'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--kernel-config',type=Path)
    parser.add_argument('--cc')
    args=parser.parse_args();stage(args.runtime,args.modules,args.contract,args.staging,args.kernel_config,args.cc)
