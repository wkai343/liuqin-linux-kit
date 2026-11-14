#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise FFmpeg bundle validation, image locks and source retention offline."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/lib'))
import fedora_ffmpeg as ffmpeg
from fedora_flow import FedoraFlow
from source_build import sha


class FFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.base = Path(cls.fixture.name)
        cls.info = ffmpeg.record()
        top = cls.base/'rpmbuild'
        for name in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
            (top/name).mkdir(parents=True)
        (top/'SOURCES/source.txt').write_text('Synthetic RPM fixture, not FFmpeg.\n')
        spec = ['Name: ffmpeg', 'Version: '+cls.info['version'],
                'Release: '+cls.info['release']+'.'+cls.info['dist'],
                'Summary: Test fixture', 'License: MIT', 'Source0: source.txt',
                'AutoReqProv: no', '%description', 'Test fixture']
        for name in (*ffmpeg.PACKAGES, *ffmpeg.DEVEL_PACKAGES, 'wrong-arch'):
            spec += ['%package -n '+name, 'Summary: Test fixture']
            if name == 'wrong-arch':spec += ['BuildArch: noarch']
            spec += ['%description -n '+name, 'Test fixture']
        spec += ['%install', 'mkdir -p %{buildroot}/usr/share/fixture']
        for name in (*ffmpeg.PACKAGES, *ffmpeg.DEVEL_PACKAGES, 'wrong-arch'):
            spec += ['cp %{SOURCE0} %{buildroot}/usr/share/fixture/'+name]
        for name in (*ffmpeg.PACKAGES, *ffmpeg.DEVEL_PACKAGES, 'wrong-arch'):
            spec += ['%files -n '+name, '/usr/share/fixture/'+name]
        path = top/'SPECS/fixture.spec'
        path.write_text('\n'.join(spec)+'\n')
        subprocess.run(['rpmbuild', '-ba', '--target', 'aarch64', '--define', '_topdir '+str(top),
                        '--define', '__os_install_post %{nil}', '--define', '_build_id_links none',
                        str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.bundle = ffmpeg.collect(top, cls.base/'bundle')

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.root = self.work/'root'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def copy_bundle(self):
        return Path(shutil.copytree(self.bundle, self.work/'bundle'))

    def locks(self, names=ffmpeg.PACKAGES):
        text = ''
        for name in names:
            text += ('\n[[packages]]\nname = '+json.dumps(name)+
                     '\n[[packages.conditions]]\nkey = "evr"\ncomparator = "="\nvalue = '+
                     json.dumps(ffmpeg.release(self.info))+'\n')
        return text

    def chroot(self, root, command, **kwargs):
        if command[0] == 'rpm':
            return subprocess.CompletedProcess(command, 0, stdout='\n'.join(
                '\t'.join((name, '0:'+ffmpeg.release(self.info), 'aarch64', '(none)'))
                for name in ffmpeg.PACKAGES)+'\n')
        if command[0] == 'ffmpeg':
            if '-h' in command:
                return subprocess.CompletedProcess(command, 0, stdout='Supported hardware devices: drm\n')
            return subprocess.CompletedProcess(command, 0,
                stdout=''.join(' V..... '+name+'_v4l2m2m fixture\n' for name in ('h264','hevc','vp9')))
        self.assertEqual(command, ['dnf','-y','--disable-repo=*','versionlock','add',*ffmpeg.PACKAGES])
        path = root/'etc/dnf/versionlock.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text() if path.exists() else 'version = "1.0"\n'
        path.write_text(old+self.locks())

    def test_real_rpms_are_checked_and_only_runtime_libraries_are_staged(self):
        manifest = ffmpeg.validate(self.bundle)
        staging = self.work/'staging'
        staging.mkdir()
        ffmpeg.stage(self.bundle, staging)
        self.assertEqual(set(p.name for p in staging.iterdir()), set(ffmpeg.filenames(self.info).values()))
        self.assertEqual(manifest['srpm']['sha256'], sha(self.bundle/ffmpeg.source_filename(self.info)))

    def test_development_packages_are_separate_and_only_staged_on_request(self):
        staging = self.work/'build-dependencies'
        staging.mkdir()
        ffmpeg.stage(self.bundle, staging, development=True)
        self.assertEqual({p.name for p in staging.iterdir()},
            set(ffmpeg.filenames(self.info, (*ffmpeg.PACKAGES, *ffmpeg.DEVEL_PACKAGES)).values()))
        bundle = self.copy_bundle()
        next((bundle/'devel-rpms').iterdir()).unlink()
        with self.assertRaisesRegex(ValueError, '开发包集合'):
            ffmpeg.validate(bundle)

    def test_missing_tampered_extra_and_symlinked_packages_stop_before_staging(self):
        for fault in ('missing', 'tampered', 'extra', 'symlink', 'source'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.work) as temp:
                bundle = Path(shutil.copytree(self.bundle, Path(temp)/'bundle'))
                target = next((bundle/'rpms').iterdir())
                if fault == 'missing':target.unlink()
                elif fault == 'tampered':target.write_bytes(target.read_bytes()+b'corrupt')
                elif fault == 'extra':(bundle/'rpms/extra.rpm').touch()
                elif fault == 'source':(bundle/ffmpeg.source_filename(self.info)).unlink()
                else:
                    backing = bundle/'backing'
                    target.rename(backing)
                    target.symlink_to(backing)
                staging = Path(temp)/'staging'
                staging.mkdir()
                with self.assertRaises((ValueError, FileNotFoundError)):
                    ffmpeg.stage(bundle, staging)
                self.assertEqual(list(staging.iterdir()), [])

    def test_real_rpm_architecture_version_and_source_type_are_enforced(self):
        runtime = self.bundle/'rpms'/ffmpeg.filenames(self.info)['ffmpeg-free']
        wrong = self.base/'rpmbuild/RPMS/noarch'/('wrong-arch-'+ffmpeg.release(self.info)+'.noarch.rpm')
        newer = dict(self.info, release='4.liuqin1')
        for path, name, info, source in ((wrong,'wrong-arch',self.info,False),
                (runtime,'ffmpeg-free',newer,False), (runtime,'ffmpeg-free',self.info,True)):
            with self.subTest(path=path,source=source),self.assertRaisesRegex(ValueError,'不匹配'):
                ffmpeg.check_rpm(path, name, info, source=source)

    def test_changed_patch_invalidates_existing_bundle(self):
        recipe = Path(shutil.copytree(ffmpeg.RECIPE, self.work/'recipe'))
        with patch.object(ffmpeg, 'RECIPE', recipe):
            ffmpeg.validate(self.bundle)
            path = recipe/'patches'/self.info['patches'][0]
            path.write_text(path.read_text()+'\n# new patch revision\n')
            with self.assertRaisesRegex(ValueError,'配方或补丁'):
                ffmpeg.validate(self.bundle)

    def test_image_preserves_unrelated_locks_and_corresponding_source(self):
        path = self.root/'etc/dnf/versionlock.toml'
        path.parent.mkdir(parents=True)
        path.write_text('version = "1.0"\n'+self.locks(['unrelated-package']))
        ffmpeg.configure(self.root, self.bundle, self.chroot)
        ffmpeg.check_image(self.root, self.chroot)
        names = {p['name'] for p in tomllib.loads(path.read_text())['packages']}
        self.assertEqual(names, {*ffmpeg.PACKAGES, 'unrelated-package'})
        docs = self.root/ffmpeg.LICENSE_DIR
        self.assertEqual(sha(docs/ffmpeg.source_filename(self.info)),
                         sha(self.bundle/ffmpeg.source_filename(self.info)))
        self.assertTrue((docs/'recipe/source.json').is_file())

    def test_wrong_installed_version_or_missing_codec_stops_before_locking(self):
        for fault in ('version','codec','drm'):
            def chroot(root, command, **kwargs):
                self.assertNotEqual(command[0], 'dnf')
                result = self.chroot(root, command, **kwargs)
                if fault == 'version':result.stdout = result.stdout.replace(self.info['release'],'0.invalid')
                elif command[0] == 'ffmpeg' and (fault == 'codec' or '-h' in command):result.stdout = ''
                return result
            with self.subTest(fault=fault),self.assertRaisesRegex(ValueError,'运行库|编解码接口|DRM PRIME'):
                ffmpeg.configure(self.root, self.bundle, chroot)
        self.assertFalse((self.root/ffmpeg.LICENSE_DIR).exists())

    def test_missing_conflicting_lock_or_missing_source_rejects_final_image(self):
        ffmpeg.configure(self.root, self.bundle, self.chroot)
        path = self.root/'etc/dnf/versionlock.toml'
        original = path.read_text()
        for content in ('version = "1.0"\n'+self.locks(ffmpeg.PACKAGES[1:]),
                        original+self.locks(ffmpeg.PACKAGES[:1]),
                        original+self.locks(['*avcodec*']),
                        original.replace(self.info['release'],'0.invalid'),
                        original.replace('version = "1.0"','version = "2.0"')):
            path.write_text(content)
            with self.assertRaisesRegex(ValueError,'版本锁'):
                ffmpeg.check_image(self.root, self.chroot)
        path.write_text(original)
        (self.root/ffmpeg.LICENSE_DIR/ffmpeg.source_filename(self.info)).unlink()
        with self.assertRaisesRegex(ValueError,'缺失'):
            ffmpeg.check_image(self.root, self.chroot)

    def test_final_rootfs_can_require_ffmpeg_even_when_source_directory_is_missing(self):
        flow = object.__new__(FedoraFlow)
        archive = self.work/'root.tar.gz'
        archive.touch()
        with patch.object(flow,'run',create=True) as run:
            flow.target_action('kernel-files',archive,self.work/'assembled',require_ffmpeg=True)
        self.assertIn('--require-ffmpeg',run.call_args.args[0])
        with self.assertRaisesRegex(ValueError,'缺失'):
            ffmpeg.check_image(self.root,self.chroot)

    def test_non_ffmpeg_recipe_does_not_download_or_compile(self):
        flow = object.__new__(FedoraFlow)
        flow.recipe = {'packages':['kate']}
        with patch('fedora_flow.download', side_effect=AssertionError('unexpected download')), \
             patch.object(flow,'target_runtime_root',side_effect=AssertionError('unexpected build')):
            self.assertIsNone(flow.ffmpeg_archive())
            self.assertIsNone(flow.ffmpeg_rpms())

    def test_playback_defaults_preserve_overlay_settings_and_require_applications(self):
        ffmpeg.configure_playback(self.root)
        self.assertFalse((self.root/'etc/mpv/mpv.conf').exists())
        for name in ('usr/bin/mpv', 'usr/bin/haruna',
                     'usr/lib64/qt6/plugins/multimedia/libgstreamermediaplugin.so'):
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        mpv = self.root/'etc/mpv/mpv.conf'
        mpv.parent.mkdir(parents=True)
        mpv.write_text('hwdec=no\n')
        ffmpeg.configure_playback(self.root)
        self.assertEqual(mpv.read_text(), 'hwdec=no\n')
        haruna = self.root/'etc/xdg/haruna/haruna.conf'
        self.assertIn('HWDecoding=v4l2m2m,auto', haruna.read_text())
        launcher = self.root/'usr/local/bin/liuqin-qt-video'
        result = subprocess.run([str(launcher), sys.executable, '-c',
            'import os,sys; print(os.environ["QT_MEDIA_BACKEND"]); print(sys.argv[1])',
            'argument with spaces'], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, 'gstreamer\nargument with spaces\n')
        ffmpeg.configure_playback(self.root)
        self.assertEqual(mpv.read_text(), 'hwdec=no\n')

    def test_default_recipe_builds_in_separate_stage_with_all_runtime_and_source_outputs(self):
        flow = object.__new__(FedoraFlow)
        flow.recipe = json.loads((PROJECT/'build/rootfs/fedora-plasma.json').read_text())
        flow.data = {'jobs':2}
        flow.refresh = False
        archive = self.work/'runtime.tar'
        archive.touch()
        source = self.work/'ffmpeg.src.rpm'
        source.touch()
        def stage(name, inputs, build, artifacts):
            self.assertEqual(name,'fedora-ffmpeg')
            self.assertEqual(inputs['recipe'],ffmpeg.inputs())
            self.assertIn('assembled/rpms',artifacts)
            self.assertIn('assembled/devel-rpms',artifacts)
            self.assertIn('assembled/'+ffmpeg.source_filename(self.info),artifacts)
            self.assertIn('assembled/build-packages.tsv',artifacts)
            build(self.work/'stage')
            return self.work/'stage'
        with patch.object(flow,'target_runtime_root',return_value=archive), \
             patch.object(flow,'ffmpeg_archive',return_value=source), \
             patch.object(flow,'stage',side_effect=stage),patch.object(flow,'target_action') as action:
            self.assertEqual(flow.ffmpeg_rpms(),self.work/'stage/assembled')
        action.assert_called_once_with('ffmpeg',archive,self.work/'stage/assembled',ffmpeg_source=source,jobs=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
