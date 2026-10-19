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

    def test_supported_raw_and_oem_scales_only(self):
        source=(PROJECT/'device/boot/liuqin-boot-splash.sh').read_text()
        start=source.index('wait_backlight_scale() {')
        function=source[start:source.index('\n}',start)+2]
        script=self.root/'scale.sh'
        script.write_text('BB='+shlex.quote(str(self.box))+'\n'+function+'\nwait_backlight_scale "$1"\n')
        for maximum,curve,want in [(2047,None,True),(4095,'liuqin-oem',True),(4095,'linear',False),(255,None,False),(4095,None,False)]:
            with self.subTest(maximum=maximum,curve=curve):
                (self.root/'max_brightness').write_text(str(maximum))
                (self.root/'brightness_curve').unlink(missing_ok=True)
                if curve is not None:(self.root/'brightness_curve').write_text(curve)
                result=subprocess.run(['sh',str(script),str(self.root)],capture_output=True,timeout=3)
                self.assertEqual(result.returncode==0,want,result.stderr)

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

    def write_modules(self,bad_clock=False,backlight=False,bad_backlight=False,unsafe_dependency=False):
        prefix='usr/lib/modules/'+self.release+'/'
        clock='kernel/drivers/clk/qcom/dispcc-sm8450.ko'
        mux='kernel/drivers/usb/typec/mux/fsa4480.ko'
        dep='kernel/drivers/base/helper.ko'
        files={prefix+clock:elf(62 if bad_clock else 183),prefix+mux:elf(),prefix+dep:elf(),
               prefix+'modules.dep':f'{clock}: {dep}\n{mux}: {dep}\n{dep}:\n'.encode()}
        if backlight:
            light='kernel/drivers/video/backlight/ktz8866.ko'
            regmap='kernel/drivers/base/regmap/regmap-i2c.ko'
            files[prefix+light]=elf(62 if bad_backlight else 183)
            files[prefix+regmap]=elf()
            files[prefix+'modules.dep']=(
                f'{clock}: {dep}\n{mux}: {dep} {regmap}\n{dep}:\n{regmap}:\n'
                f'{light}: {clock if unsafe_dependency else regmap}\n').encode()
        manifest=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items())).encode()
        (self.modules/'modules.manifest').write_bytes(manifest)
        with tarfile.open(self.modules/'modules.tar','w') as archive:
            for name,data in files.items():
                item=tarfile.TarInfo(name);item.mode=0o644;item.size=len(data);archive.addfile(item,io.BytesIO(data))
        self.contract.write_text('LIUQIN_ROOT_INTERFACE_V2\nkernel_release='+self.release+'\nimage_sha256='+'0'*64+
                                 '\nmodules_sha256='+hashlib.sha256(manifest).hexdigest()+'\n')

    def run_stage(self,kernel_config=None):stage(self.runtime,self.modules,self.contract,self.output,kernel_config)

    def test_dependency_order_and_deduplication_preserve_the_complete_kernel_identity(self):
        self.run_stage()
        self.assertEqual((self.output/'etc/liuqin-splash-modules.order').read_text().splitlines(),
                         ['helper','fsa4480','dispcc_sm8450'])
        self.assertEqual(len(list((self.output/'usr/lib/liuqin-splash').glob('*.ko'))),3)
        self.assertFalse((self.output/'etc/liuqin-native-root.contract').exists())
        self.assertEqual(self.contract.read_text().splitlines()[1],'kernel_release='+self.release)
        self.assertEqual((self.output/'etc/liuqin-splash-backlight-modules.order').read_text(),'')

    def test_modular_backlight_is_a_separate_phase_with_shared_dependencies_loaded_once(self):
        self.write_modules(backlight=True)
        config=self.root/'kernel.config';config.write_text('CONFIG_BACKLIGHT_KTZ8866=m\n')
        self.run_stage(config)
        self.assertEqual((self.output/'etc/liuqin-splash-backlight-modules.order').read_text().splitlines(),
                         ['regmap_i2c','ktz8866'])
        self.assertEqual((self.output/'etc/liuqin-splash-modules.order').read_text().splitlines(),
                         ['helper','fsa4480','dispcc_sm8450'])
        self.assertEqual(len(list((self.output/'usr/lib/liuqin-splash').glob('*.ko'))),5)
        self.assertEqual(len((self.output/'etc/liuqin-splash-backlight-modules.sha256').read_text().splitlines()),2)
        self.assertEqual(len((self.output/'etc/liuqin-splash-modules.sha256').read_text().splitlines()),5)

    def test_missing_backlight_module_is_rejected_when_configuration_requires_it(self):
        config=self.root/'kernel.config';config.write_text('CONFIG_BACKLIGHT_KTZ8866=m\n')
        with self.assertRaisesRegex(ValueError,'kernel configuration'):self.run_stage(config)

    def test_builtin_configuration_cannot_use_a_different_modular_payload(self):
        self.write_modules(backlight=True)
        config=self.root/'kernel.config';config.write_text('CONFIG_BACKLIGHT_KTZ8866=y\n')
        with self.assertRaisesRegex(ValueError,'kernel configuration'):self.run_stage(config)

    def test_backlight_dependencies_cannot_start_display_clocks(self):
        self.write_modules(backlight=True,unsafe_dependency=True)
        with self.assertRaisesRegex(ValueError,'unaudited dependency'):self.run_stage()

    def test_foreign_backlight_module_is_refused(self):
        self.write_modules(backlight=True,bad_backlight=True)
        with self.assertRaisesRegex(ValueError,'not ARM64'):self.run_stage()

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


@unittest.skipUnless(os.name=='posix' and shutil.which('timeout'), 'POSIX timeout required')
class OwnershipOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.events=self.root/'events';self.state=self.root/'state';self.state.mkdir()
        self.backlight=self.root/'backlight';self.panel=self.root/'panel';self.panel.mkdir()
        self.modules=self.root/'modules';self.modules.mkdir();self.sysmodules=self.root/'sysmodules';self.sysmodules.mkdir()
        self.cmdline=self.root/'cmdline';self.cmdline.write_text('rhgb\n')
        self.mode=self.root/'mode';self.mode.write_text('persistent\n')
        self.installer=self.root/'installer'
        self.pre=self.root/'pre.order';self.pre.write_text('regmap_i2c\nktz8866\n')
        self.post=self.root/'post.order';self.post.write_text('fsa4480\ndispcc_sm8450\n')
        names=['regmap_i2c','ktz8866','fsa4480','dispcc_sm8450']
        for name in names:(self.modules/(name+'.ko')).write_bytes(elf()+name.encode())
        self.prehash=self.root/'pre.sha';self.allhash=self.root/'all.sha'
        def hashes(selected):return ''.join(hashlib.sha256((self.modules/(n+'.ko')).read_bytes()).hexdigest()+'  '+str(self.modules/(n+'.ko'))+'\n' for n in selected)
        self.prehash.write_text(hashes(names[:2]));self.allhash.write_text(hashes(names))
        self.box=self.root/'busybox'
        self.box.write_text(r"""#!/bin/sh
case $1 in
 timeout) shift; shift; exec "$@" ;;
 insmod)
  name=${2##*/}; name=${name%.ko}
  printf 'insmod:%s\n' "$name" >>"$EVENTS"
  [ "${FAIL_MODULE:-}" != "$name" ] || exit 1
  if [ "$name" = ktz8866 ]; then
   mkdir -p "$LIGHT"
   printf '4\n' >"$LIGHT/bl_power"
   printf '4095\n' >"$LIGHT/max_brightness"
   printf 'liuqin-oem\n' >"$LIGHT/brightness_curve"
  fi
  if [ "$name" = dispcc_sm8450 ]; then
   [ "$(cat "$LIGHT/bl_power")" = 4 ] || exit 99
   grep -q '^log:backlight blanked before display clock takeover$' "$EVENTS" || exit 98
   printf 'connected\n' >"$PANEL/status"
   printf '1800x2880\n' >"$PANEL/modes"
  fi
  mkdir -p "$SYSMODULES/$name"
  exit 0 ;;
 *) exec "$@" ;;
esac
""");self.box.chmod(0o755)
        self.programs={}
        for name in ['plymouth','plymouthd','udevadm','udevd']:
            path=self.root/name
            path.write_text('#!/bin/sh\nprintf "%s\\n" "'+name+':$*" >>"$EVENTS"\n')
            path.chmod(0o755);self.programs[name]=path
        source=(PROJECT/'device/boot/liuqin-boot-splash.sh').read_text()
        replacements={
            '/etc/liuqin-splash-backlight-modules.order':self.pre,
            '/etc/liuqin-splash-backlight-modules.sha256':self.prehash,
            '/etc/liuqin-splash-modules.order':self.post,
            '/etc/liuqin-splash-modules.sha256':self.allhash,
            '/sys/class/backlight/ktz8866-backlight':self.backlight,
            '/sys/class/drm/card0-DSI-1':self.panel,
            '/usr/lib/systemd/systemd-udevd':self.programs['udevd'],
            '/usr/bin/udevadm':self.programs['udevadm'],
            '/usr/bin/plymouthd':self.programs['plymouthd'],
            '/usr/bin/plymouth':self.programs['plymouth'],
            '/usr/lib/liuqin-splash':self.modules,
            '/run/liuqin-boot-splash':self.state,
            '/run/plymouth':self.root/'plymouth-run',
            '/etc/liuqin-installer':self.installer,
            '/etc/liuqin-storage-mode':self.mode,
            '/sys/module':self.sysmodules,
            '/proc/cmdline':self.cmdline,
            '/bin/busybox':self.box,
        }
        for original,replacement in sorted(replacements.items(),key=lambda x:-len(x[0])):
            source=source.replace(original,str(replacement))
        source=source.replace('log() { echo "liuqin-splash: $*" >/dev/kmsg 2>/dev/null || :; }',
                              'log() { printf "log:%s\\n" "$*" >>"$EVENTS"; }')
        self.script=self.root/'splash.sh';self.script.write_text(source)
        self.env=dict(os.environ,EVENTS=str(self.events),LIGHT=str(self.backlight),PANEL=str(self.panel),SYSMODULES=str(self.sysmodules))

    def tearDown(self):self.temp.cleanup()

    def start(self):
        result=subprocess.run(['sh',str(self.script),'start'],env=self.env,capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_backlight_blanking_precedes_display_clocks_and_animation(self):
        events=self.start()
        required=['insmod:regmap_i2c','insmod:ktz8866','log:backlight blanked before display clock takeover',
                  'insmod:fsa4480','insmod:dispcc_sm8450','plymouth:show-splash']
        positions=[events.index(x) for x in required]
        self.assertEqual(positions,sorted(positions));self.assertTrue((self.state/'ready').exists())

    def test_charging_blanks_without_loading_graphics(self):
        self.cmdline.write_text('rhgb androidboot.mode=charger\n');events=self.start()
        self.assertIn('insmod:ktz8866',events);self.assertEqual((self.backlight/'bl_power').read_text().strip(),'4')
        self.assertFalse(any('dispcc_sm8450' in x or x.startswith('plymouth') for x in events))

    def test_animation_optout_keeps_the_backlight_phase(self):
        self.cmdline.write_text('rhgb plymouth.enable=0\n');events=self.start()
        self.assertIn('insmod:ktz8866',events);self.assertNotIn('insmod:dispcc_sm8450',events)

    def test_installer_never_loads_target_backlight_or_graphics(self):
        self.installer.touch();self.assertEqual(self.start(),[])

    def test_corrupt_backlight_stops_before_any_module_load(self):
        (self.modules/'ktz8866.ko').write_bytes(b'corrupt');events=self.start()
        self.assertFalse(any(x.startswith('insmod:') for x in events));self.assertFalse((self.state/'ready').exists())

    def test_display_clock_cannot_be_smuggled_into_backlight_list(self):
        self.pre.write_text('dispcc_sm8450\n');events=self.start()
        self.assertIn('log:invalid backlight dependency',events)
        self.assertFalse(any(x.startswith('insmod:') for x in events))

    def test_failed_backlight_never_advances_to_display_clocks(self):
        self.env['FAIL_MODULE']='ktz8866';events=self.start()
        self.assertIn('insmod:ktz8866',events);self.assertNotIn('insmod:dispcc_sm8450',events)

    def test_builtin_backlight_keeps_the_existing_path(self):
        self.pre.write_text('');self.backlight.mkdir()
        for name,value in [('bl_power','4'),('max_brightness','2047')]:
            (self.backlight/name).write_text(value+'\n')
        events=self.start();self.assertNotIn('insmod:ktz8866',events)
        self.assertLess(events.index('log:backlight blanked before display clock takeover'),events.index('insmod:dispcc_sm8450'))
        self.assertTrue((self.state/'ready').exists())

    def test_modular_native_backlight_requires_the_early_runtime_at_build_time(self):
        source=(PROJECT/'build/lib/build-initramfs.sh').read_text()
        start=source.index('if [ "$root_profile" = native-updatable ] &&\n   grep -qx \'CONFIG_BACKLIGHT_KTZ8866=m\'')
        guard=source[start:source.index('\nfi',start)+3]
        config=self.root/'kernel.config';script=self.root/'guard.sh';script.write_text(guard+'\n')
        for profile,choice,runtime,success in [('native-updatable','m','',False),('native-updatable','m','runtime',True),('native-updatable','y','',True),('installer','m','',True)]:
            with self.subTest(profile=profile,choice=choice,runtime=runtime):
                config.write_text('CONFIG_BACKLIGHT_KTZ8866='+choice+'\n')
                result=subprocess.run(['sh',str(script)],env=dict(self.env,root_profile=profile,KERNEL_CONFIG=str(config),BOOT_SPLASH_RUNTIME=runtime),capture_output=True)
                self.assertEqual(result.returncode==0,success)


if __name__=='__main__':unittest.main()
