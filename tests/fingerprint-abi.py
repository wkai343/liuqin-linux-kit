#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the pinned host libfprint ABI and test real APIs using fake hardware.

No daemon is started. No USB/sensor/TEE or real fingerprint store is opened.
Requires meson/ninja/pkg-config and GLib, gusb, gudev, D-Bus/Polkit development
packages; downloaded public source archives are checked against SOURCE_LOCK.
"""
import argparse
import json
import os
import resource
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'build/lib'))
from source_build import download
from fedora_fingerprint import extract_source


def run(argv,env=None):
 subprocess.run(list(map(str,argv)),cwd=PROJECT,env=env,check=True)


def main():
 resource.setrlimit(resource.RLIMIT_CORE,(0,0))
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--cache',type=Path,default=PROJECT/'inputs/fingerprint-src')
 args=parser.parse_args()
 lock=json.loads((PROJECT/'device/fingerprint/oem/SOURCE_LOCK.json').read_text())
 for command in ('cc','meson','ninja','pkg-config','tar'):
  if not shutil.which(command):raise RuntimeError('Missing host ABI build tool: '+command)
 run(['pkg-config','--exists','glib-2.0','gio-2.0','gusb','gudev-1.0','polkit-gobject-1','dbus-1'])
 with tempfile.TemporaryDirectory(prefix='liuqin-fpc-abi-') as temporary:
  root=Path(temporary);sources=root/'upstream';sources.mkdir()
  paths={}
  for name in ('libfprint','fprintd'):
   info=lock[name];path=args.cache/info['source_url'].rsplit('/',1)[1]
   download(info['source_url'],path,info['orig_sha256'])
   paths[name]=extract_source(path,info['orig_sha256'],sources)
  sdk=root/'sdk';build=root/'lib-build'
  run(['meson','setup',build,paths['libfprint'],'--prefix='+str(sdk),'--libdir=lib',
       '-Ddrivers=virtual_image','-Dtod=true','-Dintrospection=false','-Ddoc=false',
       '-Dgtk-examples=false','-Dinstalled-tests=false','-Dudev_rules=disabled','-Dudev_hwdb=disabled'])
  run(['ninja','-C',build,'-j2']);run(['meson','install','-C',build])
  env=dict(os.environ)
  env['PKG_CONFIG_PATH']=str(sdk/'lib/pkgconfig')+(':'+env['PKG_CONFIG_PATH'] if env.get('PKG_CONFIG_PATH') else '')
  env['LD_LIBRARY_PATH']=str(sdk/'lib')+(':'+env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
  host=root/'host';env['OUT_DIR']=str(host)
  run(['bash','device/fingerprint/oem/tools/build-tod.sh'],env)
  run(['cc','-std=c11','-D_GNU_SOURCE','-O2','-Wall','-Wextra','-Werror',
       PROJECT/'device/fingerprint/oem/src/fingerprint/oem/fpc_build_info.c',
       '-o',host/'fpc_build_info'],env)
  for name in ('fpc_build_info','fpc-oem-print','libfprint-tod-fpc1264-oem.so'):
   if b'LIUQIN_FPC_GALLERY_V1\0' not in (host/name).read_bytes():
    raise RuntimeError('Compiled gallery protocol marker is missing: '+name)
  env['FPC_GALLERY_FIXTURE']=str(PROJECT/'tests/fixtures/fingerprint-gallery-runtime.py')
  env['FPC_PRINT_DECODER']=str(host/'fpc-oem-print')
  packages=['libfprint-2','libfprint-2-tod-1','gio-2.0','gusb']
  flags=shlex.split(subprocess.check_output(['pkg-config','--cflags',*packages],env=env,text=True))
  libs=shlex.split(subprocess.check_output(['pkg-config','--libs',*packages],env=env,text=True))
  for name in ('fingerprint-gallery-tod',):
   binary=host/name
   run(['cc','-std=c11','-D_GNU_SOURCE','-O2','-Wall','-Wextra','-Werror',*flags,
        PROJECT/'tests'/(name+'.c'),'-o',binary,*libs],env)
   command=[binary] if os.geteuid()==0 else ['unshare','-Ur',binary]
   run(command,env)
  env['FPRINTD_SOURCE']=str(paths['fprintd']);env['OUT_DIR']=str(root/'daemon')
  run(['bash','device/fingerprint/oem/tools/build-fprintd-oem.sh'],env)
 print('PASS: pinned libfprint/TOD ABI, enrollment/verify/identify/decoder API tests, fprintd build')


if __name__=='__main__':main()
