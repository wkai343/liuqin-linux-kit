#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build pinned standalone AOSP adbd for the native Recovery (static musl ARM64)."""
import argparse,hashlib,json,os,shutil,subprocess,sys,tarfile
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def run(args,**kwargs):subprocess.run(list(map(str,args)),check=True,**kwargs)
def build(a):
    out=a.out.resolve()
    if out.exists() or not out.is_relative_to(PROJECT/'out'):raise ValueError('use a new output directory below project/out')
    lock=json.loads((PROJECT/'build/recovery/adbd-sources.json').read_text());src=PROJECT/'inputs/sources'
    for name,record in lock['sources'].items():
        root=src/name
        if subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()!=record['commit'] or subprocess.check_output(['git','-C',str(root),'status','--porcelain']):raise ValueError('adbd source differs: '+name)
    cc=shutil.which('gcc' if a.host else 'aarch64-unknown-linux-musl-gcc');cxx=shutil.which('g++' if a.host else 'aarch64-unknown-linux-musl-g++')
    if not cc or not cxx:raise ValueError('use Devbox compiler environment')
    out.mkdir(parents=True);prefix=out/'sysroot';common=['-G','Ninja','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_C_COMPILER='+cc,'-DCMAKE_CXX_COMPILER='+cxx,'-DCMAKE_INSTALL_PREFIX='+str(prefix),'-DCMAKE_INSTALL_LIBDIR=lib','-DBUILD_SHARED_LIBS=OFF','-DBUILD_TESTING=OFF']
    patched_source = out/'source/adbd4linux'
    shutil.copytree(src/'adbd4linux', patched_source, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__'))
    descriptor_patch = PROJECT/'build/recovery/adbd-usb-descriptors.patch'
    run(['git','apply','--check','--unsafe-paths','--directory='+str(patched_source),descriptor_patch],
        cwd=PROJECT)
    run(['git','apply','--unsafe-paths','--directory='+str(patched_source),descriptor_patch],cwd=PROJECT)
    if not a.host:common+=['-DCMAKE_SYSTEM_NAME=Linux','-DCMAKE_SYSTEM_PROCESSOR=aarch64','-DCMAKE_EXE_LINKER_FLAGS=-static']
    builds=[('brotli',src/'adbd-brotli',[]),('lz4',src/'adbd-lz4/build/cmake',['-DLZ4_BUILD_CLI=OFF','-DLZ4_BUILD_LEGACY_LZ4C=OFF']),('zstd',src/'adbd-zstd/build/cmake',['-DZSTD_BUILD_PROGRAMS=OFF','-DZSTD_BUILD_TESTS=OFF','-DZSTD_BUILD_SHARED=OFF','-DZSTD_BUILD_STATIC=ON'])]
    for name,path,flags in builds:
        directory=out/('build-'+name);run(['cmake','-S',path,'-B',directory,*common,*flags]);run(['cmake','--build',directory,'-j',str(a.jobs)]);run(['cmake','--install',directory])
    env=dict(os.environ,PKG_CONFIG_LIBDIR=str(prefix/'lib/pkgconfig'),PKG_CONFIG_PATH='')
    compat='-include '+str(PROJECT/'build/recovery/adbd-compat.h')
    directory=out/'build-adbd';run(['cmake','-S',patched_source,'-B',directory,*common,'-DFFMPEG_EXECUTABLE=/bin/true','-DCMAKE_C_FLAGS='+compat,'-DCMAKE_CXX_FLAGS='+compat,
        '-DCMAKE_CXX_STANDARD_LIBRARIES='+str(prefix/'lib/libbrotlicommon.a')],env=env)
    run(['cmake','--build',directory,'--target','adbd','-j',str(a.jobs)],env=env)
    binary=out/'adbd';shutil.copy2(directory/'adbd',binary)
    if not a.host:
        from elftools.elf.elffile import ELFFile
        with binary.open('rb') as f:
            e=ELFFile(f)
            if e['e_machine']!='EM_AARCH64' or any(s['p_type']=='PT_INTERP' for s in e.iter_segments()):raise ValueError('adbd must be static ARM64')
    licenses=out/'licenses';licenses.mkdir()
    for name,path in [('adbd4linux-GPL-3.0.txt',src/'adbd4linux/LICENSE'),('brotli-MIT.txt',src/'adbd-brotli/LICENSE'),('lz4-BSD.txt',src/'adbd-lz4/lib/LICENSE'),('zstd-BSD.txt',src/'adbd-zstd/LICENSE')]:shutil.copy2(path,licenses/name)
    shutil.copy2(PROJECT/'build/recovery/adbd-sources.json',out/'sources.json')
    (out/'NOTICE.txt').write_text('Standalone AOSP adbd with Roboparty Linux portability integration.\nPort/build integration: GPL-3.0-only; AOSP components: Apache-2.0.\nSource commits and URLs: sources.json. Dependency notices: licenses/.\nOnly USB FunctionFS is enabled by the Recovery launcher; no TCP listener.\nThis maintenance endpoint trusts the physically connected USB computer.\n')
    with tarfile.open(out/'adbd-source.tar.xz','w:xz') as archive:
        archive.add(patched_source,arcname='adbd4linux')
        def source_filter(info):
            return None if '.git' in Path(info.name).parts or '__pycache__' in Path(info.name).parts else info
        for name in ('adbd-brotli','adbd-lz4','adbd-zstd'):
            archive.add(src/name,arcname=name,filter=source_filter)
        for name in ('build-adbd.py','adbd-compat.h','adbd-sources.json','adbd-usb-descriptors.patch'):
            archive.add(PROJECT/'build/recovery'/name,arcname='build/'+name)
    record=dict(schema=1,architecture='host' if a.host else 'aarch64',sha256=sha(binary),sources=lock,builder_sha256=sha(__file__),lock_sha256=sha(PROJECT/'build/recovery/adbd-sources.json'),compat_sha256=sha(PROJECT/'build/recovery/adbd-compat.h'),
                patches={'adbd-usb-descriptors.patch':sha(descriptor_patch)})
    (out/'adbd-manifest.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(dict(path=str(binary),sha256=record['sha256'],architecture=record['architecture']),indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--host',action='store_true');p.add_argument('--jobs',type=int,default=8);build(p.parse_args())
