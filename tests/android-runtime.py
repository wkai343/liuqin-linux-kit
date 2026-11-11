#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract the new ARM64 runtime on ELF fixtures; never run target programs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
# These ELF fixtures are never executed; CI can also create them with GNU libc's
# cross compiler. The dynamic fixture explicitly declares the musl interpreter.
CC=shutil.which('aarch64-unknown-linux-musl-gcc') or shutil.which('aarch64-linux-gnu-gcc')
spec=importlib.util.spec_from_file_location('runtime_extract',PROJECT/'build/lib/build-installer-runtime.py')
extractor=importlib.util.module_from_spec(spec);spec.loader.exec_module(extractor)


class TargetPathTests(unittest.TestCase):
    def test_absolute_target_links_stay_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'lib').mkdir();(root/'usr/lib').mkdir(parents=True)
            (root/'lib/loader').symlink_to('/usr/lib/libc.so')
            self.assertEqual(extractor.target_path(root,'lib/loader'),root/'usr/lib/libc.so')
            (root/'lib/escape').symlink_to('../../outside')
            with self.assertRaisesRegex(ValueError,'escapes root'):
                extractor.target_path(root,'lib/escape')
            (root/'lib/loop').symlink_to('loop')
            with self.assertRaisesRegex(ValueError,'loop'):
                extractor.target_path(root,'lib/loop')

@unittest.skipUnless(CC,'ARM64 cross compiler required')
class RuntimeTests(unittest.TestCase):
    def test_interpreter_stdlib_extensions_and_elf_dependencies_are_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);root=base/'input';out=base/'runtime'
            source=base/'main.c';source.write_text('int main(void){return 0;}\n')
            binary=base/'aarch64'
            subprocess.run([CC,'-static','-Os',str(source),'-o',str(binary)],check=True,capture_output=True)
            tools=('bin/busybox','lib/ld-musl-aarch64.so.1','usr/bin/tar','usr/bin/install','usr/sbin/mkfs.ext4','usr/sbin/mkfs.f2fs','usr/sbin/mkfs.btrfs',
                   'usr/bin/btrfs','usr/sbin/e2fsck','usr/sbin/e2label','usr/sbin/getcap','usr/sbin/sgdisk','usr/bin/python3.12')
            for name in tools:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(binary,path)
            (root/'usr/bin/python3').symlink_to('python3.12')
            standard=root/'usr/lib/python3.12';(standard/'encodings').mkdir(parents=True)
            (standard/'encodings/__init__.py').write_text('# stdlib fixture\n')
            (standard/'json.py').write_text('# stdlib fixture\n')
            (standard/'site-packages').mkdir();(standard/'site-packages/exclude.py').write_text('raise RuntimeError("not stdlib")\n')
            (standard/'__pycache__').mkdir();(standard/'__pycache__/exclude.pyc').write_bytes(b'cache')
            libraries=root/'usr/lib';libraries.mkdir(exist_ok=True)
            lib_source=base/'lib.c';lib_source.write_text('int fixture(void){return 1;}\n')
            library=libraries/'libfixture.so.1'
            subprocess.run([CC,'-shared','-fPIC','-nostdlib',str(lib_source),'-Wl,-soname,libfixture.so.1','-o',str(library)],check=True,capture_output=True)
            extension_source=base/'extension.c';extension_source.write_text('extern int fixture(void); int call(void){return fixture();}\n')
            extension=standard/'lib-dynload/fcntl.cpython-312-aarch64-linux-musl.so';extension.parent.mkdir()
            subprocess.run([CC,'-shared','-fPIC','-nostdlib',str(extension_source),'-L'+str(libraries),'-l:libfixture.so.1','-o',str(extension)],check=True,capture_output=True)
            # A real dynamic executable exercises PT_INTERP as well as NEEDED.
            subprocess.run([CC,'-nostdlib','-Wl,-e,call','-Wl,-pie',
                '-Wl,--dynamic-linker=/lib/ld-musl-aarch64.so.1',str(extension_source),
                '-L'+str(libraries),'-l:libfixture.so.1','-o',str(root/'usr/bin/python3.12')],check=True,capture_output=True)
            (root/'etc').mkdir();(root/'etc/mke2fs.conf').write_text('[defaults]\n')
            env=dict(os.environ,CROSS_COMPILE=CC[:-3])
            result=subprocess.run([sys.executable,str(PROJECT/'build/lib/build-installer-runtime.py'),
                '--root',str(root),'--out',str(out)],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            manifest=json.loads((out/'runtime-files.json').read_text())
            for name in ('bin/busybox','lib/ld-musl-aarch64.so.1','etc/passwd','etc/group','usr/bin/python3','usr/sbin/mkfs.f2fs','usr/lib/python3.12/json.py',
                         'usr/lib/python3.12/encodings/__init__.py','usr/lib/python3.12/lib-dynload/fcntl.cpython-312-aarch64-linux-musl.so',
                         'lib/libfixture.so.1'):
                self.assertIn(name,manifest)
                self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(),manifest[name])
            self.assertFalse((out/'usr/lib/python3.12/site-packages').exists())
            self.assertFalse((out/'usr/lib/python3.12/__pycache__').exists())
            subprocess.run([CC,'-nostdlib','-Wl,-e,call','-Wl,-pie',
                '-Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1',str(extension_source),
                '-L'+str(libraries),'-l:libfixture.so.1','-o',str(root/'usr/bin/python3.12')],check=True,capture_output=True)
            with self.assertRaisesRegex(ValueError,'expected musl interpreter'):
                extractor.extract(root,base/'wrong-libc')
            self.assertFalse((base/'wrong-libc').exists())

if __name__=='__main__':unittest.main(verbosity=2)
