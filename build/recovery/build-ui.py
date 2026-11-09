#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build pinned LVGL Recovery UI for ARM64, or a hardware-free host preview."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import urllib.request

PROJECT=Path(__file__).resolve().parents[2]


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def run(args,**kwargs):return subprocess.run(list(map(str,args)),check=True,**kwargs)


def dependencies():
    lock=json.loads((PROJECT/'build/recovery/ui-sources.json').read_text())
    lvgl=PROJECT/'inputs/sources'/('lvgl-'+lock['lvgl']['version']+'-src')
    if not lvgl.exists():
        run(['git','clone','--depth','1','--branch',lock['lvgl']['tag'],'--filter=blob:none','--sparse',lock['lvgl']['repository'],lvgl])
        run(['git','-C',lvgl,'sparse-checkout','set','src','include','env_support'])
    commit=subprocess.check_output(['git','-C',lvgl,'rev-parse','HEAD'],text=True).strip()
    if commit!=lock['lvgl']['commit'] or subprocess.check_output(['git','-C',lvgl,'status','--porcelain']):
        raise ValueError('LVGL source differs from the pinned clean revision')
    cjson=PROJECT/'inputs/sources'/('cJSON-'+lock['cjson']['version'])
    archive=PROJECT/'inputs/downloads'/('cJSON-'+lock['cjson']['version']+'.tar.gz')
    if not cjson.exists():
        if not archive.exists():
            with urllib.request.urlopen(lock['cjson']['url'],timeout=45) as stream:data=stream.read(4*1024**2)
            if hashlib.sha256(data).hexdigest()!=lock['cjson']['sha256']:raise ValueError('cJSON download checksum mismatch')
            archive.parent.mkdir(parents=True,exist_ok=True);archive.write_bytes(data)
        if sha(archive)!=lock['cjson']['sha256']:raise ValueError('cJSON archive checksum mismatch')
        with tarfile.open(archive) as tar:tar.extractall(PROJECT/'inputs/sources',filter='data')
    for name,digest in lock['cjson']['files'].items():
        if sha(cjson/name)!=digest:raise ValueError('cJSON source changed: '+name)
    fonts=PROJECT/'device/recovery/ui/fonts'
    for name,digest in json.loads((fonts/'provenance.json').read_text())['files'].items():
        if sha(fonts/name)!=digest:raise ValueError('prepared UI font changed: '+name)
    return lock,lvgl,cjson,fonts


def build(args):
    out=args.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new output directory below project/out')
    lock,lvgl,cjson,fonts=dependencies();out.mkdir(parents=True)
    cache=args.build_dir.resolve() if args.build_dir else out/'build'
    if not cache.is_relative_to(PROJECT/'out'):raise ValueError('build cache must be below project/out')
    source=PROJECT/'device/recovery/ui'
    source_files={str(p.relative_to(PROJECT)):sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
    cc=shutil.which('gcc' if args.host else 'aarch64-unknown-linux-musl-gcc')
    cxx=shutil.which('g++' if args.host else 'aarch64-unknown-linux-musl-g++')
    if not cc or not cxx:raise ValueError('missing compiler; use the project Devbox toolchain')
    flags=['-DCMAKE_BUILD_TYPE=Release','-DCMAKE_C_COMPILER='+cc,'-DCMAKE_CXX_COMPILER='+cxx]
    if not args.host:flags+=['-DCMAKE_SYSTEM_NAME=Linux','-DCMAKE_SYSTEM_PROCESSOR=aarch64','-DSTATIC_UI=ON']
    if args.sanitize:
        if not args.host:raise ValueError('sanitizers are for host previews')
        flags+=['-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer',
            '-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer','-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined']
    run(['cmake','-S',source,'-B',cache,'-G','Ninja','-DLVGL_SOURCE='+str(lvgl),
         '-DCJSON_SOURCE='+str(cjson),'-DFONT_SOURCE='+str(fonts),*flags])
    run(['cmake','--build',cache,'-j',str(args.jobs)])
    binary=out/'liuqin-recovery-ui';shutil.copy2(cache/'liuqin-recovery-ui',binary)
    power=out/'liuqin-ui-power'
    run([cc,'-Os','-static','-s',source/'power.c','-o',power])
    if not args.host:
        from elftools.elf.elffile import ELFFile
        for p in (binary,power):
            with p.open('rb') as stream:
                elf=ELFFile(stream)
                if elf['e_machine']!='EM_AARCH64' or any(s['p_type']=='PT_INTERP' for s in elf.iter_segments()):
                    raise ValueError('UI runtime must be static ARM64 ELF')
    licenses=out/'licenses';licenses.mkdir()
    for name,path in [('LVGL-MIT.txt',lvgl/'LICENCE.txt'),('cJSON-MIT.txt',cjson/'LICENSE'),('Noto-OFL.txt',fonts/'OFL.txt')]:shutil.copy2(path,licenses/name)
    if any(sha(PROJECT/name)!=digest for name,digest in source_files.items()):raise ValueError('UI source changed during build')
    metadata=dict(schema=1,architecture='host' if args.host else 'aarch64',lvgl=lock['lvgl'],cjson=lock['cjson'],
        files={p.name:sha(p) for p in (binary,power)},
        source_files=source_files,
        source_lock_sha256=sha(PROJECT/'build/recovery/ui-sources.json'),builder_sha256=sha(__file__))
    (out/'ui-manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(out),'architecture':metadata['architecture'],'files':metadata['files']},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--host',action='store_true');p.add_argument('--sanitize',action='store_true');p.add_argument('--jobs',type=int,default=8)
    p.add_argument('--build-dir',type=Path,help='Reuse a compiler cache below project/out')
    build(p.parse_args())
