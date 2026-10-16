#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise runtime isolation, dependency closure and authenticated staging."""
from pathlib import Path
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'build/lib'),str(PROJECT/'shared')]
from boot_splash import stage
from plymouth_runtime import rooted, materialize


@unittest.skipUnless(os.name=='posix' and shutil.which('timeout'), 'POSIX timeout required')
class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        source=(PROJECT/'device/boot/liuqin-boot-splash.sh').read_text()
        start=source.index('wait_writable() {')
        self.function=source[start:source.index('\n}',start)+2]
        # Delegate BusyBox applets to host tools so the production shell
        # function can run against disposable files without touching sysfs.
        self.slept=self.root/'slept';self.box=self.root/'applets'
        self.box.write_text('#!/bin/sh\n[ "$1" != sleep ] || : >'+shlex.quote(str(self.slept))+
                            '\nexec "$@"\n');self.box.chmod(0o755)

    def tearDown(self):self.temp.cleanup()

    def run_wait(self,path,asynchronous=False):
        marker=self.root/'ready';script=self.root/'wait.sh'
        script.write_text('BB='+shlex.quote(str(self.box))+'\n'+self.function+'\n'+
                          'wait_writable "$1" || exit $?\n: >"$2"\n')
        command=['sh',str(script),str(path),str(marker)]
        if asynchronous:
            process=subprocess.Popen(command,cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            # Create the node only after the production loop has found it
            # absent and started waiting, rather than racing process startup.
            deadline=time.monotonic()+4
            while not self.slept.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(0.01)
            if not self.slept.exists():
                process.kill();process.communicate();self.fail('readiness loop did not wait for the node')
            path.write_text('ready\n')
            out,err=process.communicate(timeout=7)
            result=process.returncode
        else:
            process=subprocess.run(command,cwd=self.root,capture_output=True,timeout=7);result=process.returncode
        return result,marker

    def test_already_ready_device_proceeds_without_a_mandatory_sleep(self):
        path=self.root/'node';path.touch()
        self.assertEqual(self.run_wait(path)[0],0)
        self.assertFalse(self.slept.exists())

    def test_node_appearing_asynchronously_is_detected_and_path_is_literal(self):
        path=self.root/'node $(touch escaped) with spaces'
        result,marker=self.run_wait(path,asynchronous=True)
        self.assertEqual(result,0);self.assertTrue(marker.exists())
        self.assertFalse((self.root/'escaped').exists())

    def test_absent_device_times_out_without_advancing(self):
        result,marker=self.run_wait(self.root/'missing')
        self.assertNotEqual(result,0);self.assertFalse(marker.exists())


def elf(machine=183):
    data=bytearray(64);data[:6]=b'\x7fELF\x02\x01';data[18:20]=machine.to_bytes(2,'little')
    return bytes(data)


class SplashTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.modules=self.root/'modules';self.modules.mkdir()
        self.output=self.root/'staging';self.output.mkdir()
        self.release='6.17-test-liuqin';self.contract=self.root/'contract'
        self.files={name:elf() for name in ('usr/bin/plymouth','usr/bin/plymouthd','usr/bin/udevadm',
            'usr/lib/systemd/systemd-udevd','usr/lib64/plymouth/renderers/drm.so')}
        self.files.update({'etc/plymouth/plymouthd.conf':b'[Daemon]\nTheme=spinner\n',
            'usr/share/plymouth/themes/spinner/spinner.plymouth':b'ModuleName=two-step\n'})
        self.modes={}
        self.write_runtime();self.write_modules()

    def tearDown(self):self.temp.cleanup()

    def write_runtime(self,extra=None):
        records={}
        with tarfile.open(self.runtime/'runtime.tar.gz','w:gz') as archive:
            for name,data in self.files.items():
                item=tarfile.TarInfo(name);item.mode=self.modes.get(name,0o755);item.size=len(data)
                archive.addfile(item,io.BytesIO(data))
                records[name]=dict(sha256=hashlib.sha256(data).hexdigest(),mode=item.mode,size=len(data))
            if extra:archive.addfile(extra)
        (self.runtime/'runtime.json').write_text(json.dumps(dict(schema=1,architecture='aarch64',theme='spinner',files=records)))

    def write_modules(self,bad_clock=False):
        prefix='usr/lib/modules/'+self.release+'/'
        clock='kernel/drivers/clk/qcom/dispcc-sm8450.ko'
        mux='kernel/drivers/usb/typec/mux/fsa4480.ko'
        dep='kernel/drivers/base/helper.ko'
        files={prefix+clock:elf(62 if bad_clock else 183),prefix+mux:elf(),prefix+dep:elf(),
               prefix+'modules.dep':f'{clock}: {dep}\n{mux}: {dep}\n{dep}:\n'.encode()}
        manifest=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items())).encode()
        (self.modules/'modules.manifest').write_bytes(manifest)
        with tarfile.open(self.modules/'modules.tar','w') as archive:
            for name,data in files.items():
                item=tarfile.TarInfo(name);item.mode=0o644;item.size=len(data);archive.addfile(item,io.BytesIO(data))
        self.contract.write_text('LIUQIN_ROOT_INTERFACE_V2\nkernel_release='+self.release+'\nimage_sha256='+'0'*64+
                                 '\nmodules_sha256='+hashlib.sha256(manifest).hexdigest()+'\n')

    def run_stage(self):stage(self.runtime,self.modules,self.contract,self.output)

    def test_dependency_order_and_deduplication_preserve_the_complete_kernel_identity(self):
        self.run_stage()
        self.assertEqual((self.output/'etc/liuqin-splash-modules.order').read_text().splitlines(),
                         ['helper','fsa4480','dispcc_sm8450'])
        self.assertEqual(len(list((self.output/'usr/lib/liuqin-splash').glob('*.ko'))),3)
        self.assertFalse((self.output/'etc/liuqin-native-root.contract').exists())
        self.assertEqual(self.contract.read_text().splitlines()[1],'kernel_release='+self.release)

    def test_corrupt_runtime_is_not_accepted(self):
        manifest=self.runtime/'runtime.json';data=json.loads(manifest.read_text())
        data['files']['usr/bin/plymouth']['sha256']='0'*64;manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'content differs'):self.run_stage()

    def test_traversal_and_symlinks_are_rejected_without_writing_outside_staging(self):
        for kind in ('traversal','link','hardlink'):
            with self.subTest(kind=kind):
                extra=tarfile.TarInfo('../escaped' if kind=='traversal' else 'usr/bin/extra')
                if kind=='link':extra.type=tarfile.SYMTYPE;extra.linkname='/etc/passwd'
                if kind=='hardlink':extra.type=tarfile.LNKTYPE;extra.linkname='usr/bin/plymouth'
                self.write_runtime(extra)
                with self.assertRaises(ValueError):self.run_stage()
                self.assertFalse((self.root/'escaped').exists())
                import shutil
                shutil.rmtree(self.output);self.output.mkdir()

    def test_destination_symlink_cannot_escape_staging(self):
        (self.output/'usr').symlink_to(self.root/'outside')
        with self.assertRaisesRegex(ValueError,'symlink'):self.run_stage()
        self.assertFalse((self.root/'outside').exists())

    def test_foreign_runtime_and_foreign_kernel_module_are_refused(self):
        self.files['usr/bin/plymouth']=elf(62);self.write_runtime()
        with self.assertRaisesRegex(ValueError,'foreign ELF'):self.run_stage()
        self.files['usr/bin/plymouth']=elf();self.write_runtime();self.write_modules(bad_clock=True)
        with self.assertRaisesRegex(ValueError,'not ARM64'):self.run_stage()

    def test_module_manifest_mismatch_is_refused(self):
        (self.modules/'modules.manifest').write_text('changed\n')
        with self.assertRaises(ValueError):self.run_stage()

    @unittest.skipUnless(os.name=='posix' and shutil.which('cpio'), 'CPIO required')
    def test_shared_elf_payloads_survive_newc_roundtrip_without_sharing_configs_or_modes(self):
        content=elf()+b'library-alias-test'
        first='lib64/libexample.so';second='usr/lib64/libexample.so.1'
        different_mode='usr/lib64/libexample-readonly.so';config='etc/plymouth/independent.conf'
        for name in (first,second,different_mode,config):self.files[name]=content
        self.modes[different_mode]=0o644
        self.write_runtime();self.run_stage()
        self.assertTrue((self.output/first).samefile(self.output/second))
        self.assertFalse((self.output/first).samefile(self.output/different_mode))
        self.assertFalse((self.output/first).samefile(self.output/config))
        paths=['.']+sorted('./'+str(p.relative_to(self.output)) for p in self.output.rglob('*'))
        archive=subprocess.run(['cpio','--null','--create','--format=newc','--owner=0:0','--reproducible'],
            cwd=self.output,input=('\0'.join(paths)+'\0').encode(),capture_output=True,check=True).stdout
        restored=self.root/'restored';restored.mkdir()
        subprocess.run(['cpio','--extract','--make-directories','--no-absolute-filenames','--no-preserve-owner'],
            cwd=restored,input=archive,capture_output=True,check=True)
        for name,data in self.files.items():
            self.assertEqual((restored/name).read_bytes(),data,name)
            self.assertEqual((restored/name).stat().st_mode&0o777,self.modes.get(name,0o755),name)
        self.assertTrue((restored/first).samefile(restored/second))
        self.assertEqual((restored/config).stat().st_nlink,1)
        self.assertEqual((restored/'etc/liuqin-splash-modules.order').stat().st_nlink,1)

    def test_rooted_absolute_and_usrmerge_links_never_follow_the_host_filesystem(self):
        raw=self.root/'raw';(raw/'usr/lib64').mkdir(parents=True)
        (raw/'usr/lib64/test.so').write_bytes(elf())
        (raw/'lib64').symlink_to('usr/lib64')
        (raw/'usr/lib64/alias.so').symlink_to('/usr/lib64/test.so')
        self.assertEqual(rooted(raw,'lib64/alias.so'),raw/'usr/lib64/test.so')
        out=self.root/'normalized';records,_=materialize(raw,out)
        self.assertFalse((out/'lib64').is_symlink())
        self.assertEqual(records['lib64/alias.so']['sha256'],hashlib.sha256(elf()).hexdigest())
        (raw/'escape').symlink_to('../outside')
        with self.assertRaisesRegex(ValueError,'escapes'):rooted(raw,'escape')

if __name__=='__main__':unittest.main()
