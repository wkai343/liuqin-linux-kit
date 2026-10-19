#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise framebuffer bounds, renderer isolation, fallback and owned-child cleanup."""
from pathlib import Path
import os,subprocess,tempfile,unittest,zlib
PROJECT=Path(__file__).resolve().parents[1]
B=PROJECT/'device/boot/splash'
F=PROJECT/'tests/fixtures/boot-splash'
class Framebuffer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();w=Path(cls.tmp.name);cls.exe=w/'probe'
        subprocess.run(['cc','-Wall','-Wextra','-Werror','-Dmain=check_main','-c',str(B/'framebuffer-check.c'),'-o',str(w/'probe.o')],check=True)
        subprocess.run(['cc','-Wall','-Wextra','-Wno-misleading-indentation','-Werror',str(F/'framebuffer-check.c'),str(w/'probe.o'),'-o',str(cls.exe),'-Wl,--wrap=open,--wrap=read,--wrap=close,--wrap=lstat,--wrap=fstat,--wrap=ioctl,--wrap=mmap,--wrap=munmap'],check=True)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def check(self,scenario,status,maps):
        r=subprocess.run([str(self.exe),scenario],text=True,capture_output=True,timeout=3)
        self.assertEqual(r.returncode,status,r.stdout+r.stderr);self.assertIn(f'maps={maps} ',r.stderr);return r.stdout
    def test_black_samples_are_not_a_retained_logo(self):self.assertIn(f'crc32={zlib.crc32(bytes(65536)):08x}',self.check('black',9,1))
    def test_white_samples(self):self.assertIn(f'crc32={zlib.crc32(bytes([255])*65536):08x} samples=16384 nonblack=16384',self.check('white',0,1))
    def test_last_pixel_is_within_mapping(self):self.assertIn('nonblack=1',self.check('last-pixel',0,1))
    def test_wrong_board_never_maps(self):self.check('wrong-board',3,0)
    def test_existing_or_unknown_owner_never_maps(self):
        for s in ['owned','unknown-owner']:self.check(s,4,0)
    def test_wrong_device_and_ioctl_failure_never_map(self):
        for s in ['wrong-device','ioctl-error']:self.check(s,6,0)
    def test_invalid_geometry_never_maps(self):
        for s in ['wrong-address','wrong-size','wrong-stride','wrong-driver','wrong-width','wrong-format','offset']:self.check(s,7,0)
    def test_mapping_failure(self):self.check('map-error',8,1)
    def test_arguments_cannot_select_devices(self):self.check('arguments',2,0)

class Stage(unittest.TestCase):
    def run_stage(self,args='rhgb liuqin.firmware_splash=1',owner=False,existing=False,hashfail=False,probefail=False,showfail=False,unchanged=False,hang=False,angle="90",anglefail=False):
        with tempfile.TemporaryDirectory() as tmp:
            w=Path(tmp);(w/'state').mkdir();(w/'module').mkdir();(w/'card').mkdir()
            if owner:(w/'module/ktz8866').touch()
            if existing:(w/'ready').touch()
            (w/'cmdline').write_text(args);(w/'order').write_text('ktz8866\n')
            box=w/'bb';box.write_text('#!/bin/sh\ncase "$1" in\nsha256sum) exit "$HASHFAIL";;\ntimeout) shift; shift; exec "$@";;\nsleep) exec sleep 0.01;;\n*) exit 90;;\nesac\n');box.chmod(0o755)
            client=w/'plymouth';client.write_text('''#!/bin/sh
case "$1" in
--ping) [ -e "$W/ready" ];;
show-splash) echo show >>"$W/trace"; [ "$SHOWFAIL" = 0 ] || exit 1; touch "$W/shown";;
quit) echo quit >>"$W/trace"; [ "$HANG" = 0 ] || exit 1; touch "$W/quit"; rm -f "$W/ready";;
*) exit 90;;
esac
''');client.chmod(0o755)
            daemon=w/'plymouthd';daemon.write_text('''#!/bin/sh
echo daemon >>"$W/trace"
echo $$ >"$W/pid"
touch "$W/ready"
while [ ! -e "$W/quit" ]; do sleep 0.01; done
echo exited >>"$W/trace"
''');daemon.chmod(0o755)
            probe=w/'probe';probe.write_text('''#!/bin/sh
[ "$PROBEFAIL" = 0 ] || exit 7
if [ -e "$W/shown" ] && [ "$UNCHANGED" = 0 ]; then
 echo 'crc32=22222222 samples=16384 nonblack=200'
else
 echo 'crc32=11111111 samples=16384 nonblack=181'
fi
''');probe.chmod(0o755)
            detector=w/'detector';detector.write_text('#!/bin/sh\nprintf \"%s\\n\" \"$ANGLE\"\nexit \"$ANGLEFAIL\"\n');detector.chmod(0o755)
            s=(B/'firmware-stage.sh').read_text()
            replacements={'/usr/libexec/liuqin-logo-orientation':str(detector),'/etc/plymouth/plymouthd.conf':str(w/'plymouth.conf'),'/bin/busybox':str(box),'/run/liuqin-boot-splash':str(w/'state'),'/run/plymouth':str(w/'runplymouth'),'/usr/bin/plymouthd':str(daemon),'/usr/libexec/liuqin-fw-plymouthd':str(daemon),'/usr/bin/plymouth':str(client),'/sys/module':str(w/'module'),'/proc/cmdline':str(w/'cmdline'),'/dev/dri/card':str(w/'card/node'),'/etc/liuqin-splash-backlight-modules.order':str(w/'order'),'/usr/libexec/liuqin-fb-check':str(probe),'/etc/liuqin-fb-check.sha256':str(w/'probe.sha'),'/dev/kmsg':str(w/'kmsg')}
            for old,new in replacements.items():s=s.replace(old,new)
            s=s.replace('>'+str(w/'kmsg'),'>>'+str(w/'kmsg'))
            runner=w/'runner';runner.write_text(s)
            env=dict(os.environ,W=str(w),ANGLE=angle,ANGLEFAIL=str(int(anglefail)),HASHFAIL=str(int(hashfail)),PROBEFAIL=str(int(probefail)),SHOWFAIL=str(int(showfail)),UNCHANGED=str(int(unchanged)),HANG=str(int(hang)))
            r=subprocess.run(['sh',str(runner)],text=True,capture_output=True,env=env,timeout=5);self.assertEqual(r.returncode,0,r.stderr)
            if (w/'pid').exists():
                with self.assertRaises(ProcessLookupError):os.kill(int((w/'pid').read_text()),0)
            return {'angle':(w/'state/orientation').read_text().strip() if (w/'state/orientation').exists() else None,'config':(w/'plymouth.conf').read_text() if (w/'plymouth.conf').exists() else '', 'trace':(w/'trace').read_text() if (w/'trace').exists() else '', 'log':(w/'kmsg').read_text() if (w/'kmsg').exists() else '', 'rendered':(w/'state/firmware-rendered').exists()}
    def test_first_frame_and_owned_renderer_release(self):
        r=self.run_stage();self.assertTrue(r['rendered']);self.assertIn('renderer released',r['log']);self.assertIn('show\nquit',r['trace'])
    def test_no_daemon_for_optout_or_charger(self):
        for args in ['rhgb','liuqin.firmware_splash=1','rhgb liuqin.firmware_splash=1 androidboot.mode=charger','rhgb liuqin.firmware_splash=1 plymouth.enable=0','rhgb liuqin.firmware_splash=1 rd.plymouth=0']:self.assertEqual(self.run_stage(args=args)['trace'],'')
    def test_native_owner_or_existing_daemon_is_not_touched(self):
        self.assertEqual(self.run_stage(owner=True)['trace'],'');self.assertEqual(self.run_stage(existing=True)['trace'],'')
    def test_invalid_check_or_hardware_falls_back(self):
        self.assertEqual(self.run_stage(hashfail=True)['trace'],'');self.assertEqual(self.run_stage(probefail=True)['trace'],'')
    def test_failed_show_still_releases_owned_child(self):
        r=self.run_stage(showfail=True);self.assertFalse(r['rendered']);self.assertIn('quit',r['trace'])
    def test_unchanged_pixels_not_reported_as_frame(self):self.assertFalse(self.run_stage(unchanged=True)['rendered'])
    def test_hung_quit_terminates_only_owned_child(self):self.assertIn('terminating owned child',self.run_stage(hang=True)['log'])

    def test_all_recognized_angles_and_same_configuration_for_both_stages(self):
        for a in ['0','90','180','270']:
            r=self.run_stage(angle=a);self.assertEqual(r['angle'],a);self.assertIn('Theme=liuqin-rotate-'+a+'\n',r['config']);self.assertIn('DeviceScale=2',r['config'])
    def test_unknown_failed_or_invalid_orientation_is_bounded_fallback(self):
        for a,fail in [('45',False),('90\n270',False),('$(false)',False),('',False),('270',True)]:
            r=self.run_stage(angle=a,anglefail=fail);self.assertEqual(r['angle'],'90');self.assertIn('uncertain',r['log'])


class Launcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();w=Path(cls.tmp.name);cls.exe=w/'launcher'
        subprocess.run(['cc','-Wall','-Wextra','-Werror','-Dmain=launcher_main','-c',str(B/'framebuffer-renderer.c'),'-o',str(w/'launcher.o')],check=True)
        subprocess.run(['cc','-Wall','-Wextra','-Wno-misleading-indentation','-Werror',str(F/'framebuffer-renderer.c'),str(w/'launcher.o'),'-o',str(cls.exe),'-Wl,--wrap=geteuid,--wrap=unshare,--wrap=mount,--wrap=setenv,--wrap=unsetenv,--wrap=execv'],check=True)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def check(self,case,status,counts):
        r=subprocess.run([str(self.exe),case],text=True,capture_output=True,timeout=3)
        self.assertEqual(r.returncode,status,r.stdout+r.stderr);self.assertIn(counts,r.stderr)
    def test_private_readonly_views_before_exec(self):self.check('normal',0,'shares=1 mounts=5 execs=1')
    def test_arguments_and_nonroot_refused(self):
        for case in ['arguments','nonroot']:self.check(case,2,'shares=0 mounts=0 execs=0')
    def test_namespace_failure_never_mounts(self):self.check('unshare-error',3,'shares=1 mounts=0 execs=0')
    def test_any_failed_mount_never_executes(self):
        for n in range(1,6):self.check('mount-error-'+str(n),4,f'shares=1 mounts={n} execs=0')
    def test_environment_failure_never_executes(self):self.check('env-error',5,'shares=1 mounts=5 execs=0')
    def test_exec_failure_reported(self):self.check('exec-error',6,'shares=1 mounts=5 execs=1')

if __name__=='__main__':unittest.main()
