#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the independent OBS RPM and its default/standalone build graph."""
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/lib'))
import fedora_obs as obs
from fedora_flow import FedoraFlow
from source_build import sha


class ObsPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.base = Path(cls.fixture.name)
        compiler = next((shutil.which(n) for n in ('aarch64-linux-gnu-gcc',
            'aarch64-unknown-linux-musl-gcc') if shutil.which(n)), None)
        if compiler is None and platform.machine() == 'aarch64':
            compiler = shutil.which('cc')
        if not compiler:
            cls.fixture.cleanup()
            raise RuntimeError('OBS 打包检查需要 ARM64 C 编译器；请使用 Devbox。')
        (cls.base/'dependency.c').write_text('int fixture(void) { return 1; }\n')
        (cls.base/'plugin.c').write_text('extern int fixture(void); int encoder(void) { return fixture(); }\n')
        subprocess.run([compiler, '-shared', '-fPIC', '-nostdlib', '-Wl,-soname,libobs-fixture.so.1',
            str(cls.base/'dependency.c'), '-o', str(cls.base/'libobs-fixture.so.1')], check=True)
        cls.plugin = cls.base/'plugin.so'
        link = [compiler, '-shared', '-fPIC', '-nostdlib', str(cls.base/'plugin.c'),
                '-L'+str(cls.base), '-Wl,--no-as-needed', '-l:libobs-fixture.so.1']
        subprocess.run([*link, '-o', str(cls.plugin)], check=True)
        subprocess.run([*link, '-Wl,-rpath,/tmp/build-only', '-o', str(cls.base/'rpath.so')], check=True)
        work = cls.base/'build'
        cls.payload = work/'payload'
        (cls.payload/obs.PLUGIN).parent.mkdir(parents=True)
        shutil.copyfile(cls.plugin, cls.payload/obs.PLUGIN)
        shutil.copytree(obs.RECIPE/'data', cls.payload/obs.DATA)
        for name in ('README.md', 'COPYING'):
            shutil.copyfile(obs.RECIPE/name, cls.payload/obs.DATA/name)
        shutil.copytree(obs.RECIPE, work/'source/build/obs-v4l2')
        (work/'source/tests').mkdir()
        for path in obs.TESTS:
            shutil.copyfile(path, work/'source/tests'/path.name)
        def chroot(root, command, **kwargs):
            if command[:2] == ['rpm', '-q']:
                return subprocess.CompletedProcess(command, 0,
                    stdout='obs-studio-libs >= 32.2.2\nglibc >= 2.44\n')
            assert command[:2] == ['rpm', '-qa']
            return subprocess.CompletedProcess(command, 0, stdout=b'obs-studio-libs\t32.2.2\taarch64\n')
        cls.bundle = cls.base/'bundle'
        cls.rpm = obs.pack(cls.base/'root', work, cls.bundle, chroot)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_payload_is_an_additional_module_with_source_and_no_official_replacements(self):
        manifest = obs.validate(self.bundle)
        files = set(subprocess.check_output(['rpm', '-qpl', str(self.rpm)], text=True).splitlines())
        allowed = {'/'+n for n in obs.payload_names()}
        allowed |= {'/'+obs.LICENSE+'/BUILD.json', '/'+obs.LICENSE, '/'+obs.DATA, '/'+obs.DATA+'/locale'}
        self.assertEqual(files, allowed)
        requires = subprocess.check_output(['rpm', '-qp', '--requires', str(self.rpm)], text=True).splitlines()
        self.assertIn('obs-studio', requires)
        self.assertIn('obs-studio-libs >= 32.2.2', requires)
        self.assertIn('libobs-fixture.so.1()(64bit)', requires)
        self.assertIn('libavcodec-free >= '+obs.fedora_ffmpeg.release(obs.fedora_ffmpeg.record()), requires)
        self.assertFalse(any('-devel' in r or r.startswith('obs-studio =') for r in requires))
        for query in ('--conflicts', '--obsoletes'):
            self.assertEqual(subprocess.check_output(['rpm', '-qp', query, str(self.rpm)], text=True).strip(), '')
        provides = subprocess.check_output(['rpm', '-qp', '--provides', str(self.rpm)], text=True).splitlines()
        self.assertFalse(any(p.startswith('obs-studio ') for p in provides))
        with tarfile.open(self.payload/obs.LICENSE/'source.tar.xz') as archive:
            self.assertEqual(archive.extractfile('source/build/obs-v4l2/liuqin-v4l2.c').read(),
                             (obs.RECIPE/'liuqin-v4l2.c').read_bytes())
            for path in obs.TESTS:
                self.assertEqual(archive.extractfile('source/tests/'+path.name).read(), path.read_bytes())
        self.assertEqual(manifest['sha256'], sha(self.rpm))
        staging = self.work/'staging';staging.mkdir()
        obs.stage(self.bundle, staging)
        self.assertEqual([p.name for p in staging.iterdir()], [obs.filename()])
        with self.assertRaisesRegex(ValueError, '重复'):
            obs.stage(self.bundle, staging)

    def test_wrong_elf_architecture_rpath_and_links_are_rejected(self):
        wrong = self.work/'wrong.so'
        content = bytearray(self.plugin.read_bytes());content[18:20] = b'\x3e\x00'
        wrong.write_bytes(content)
        with self.assertRaisesRegex(ValueError, 'ARM64'):
            obs.plugin_dependencies(wrong)
        with self.assertRaisesRegex(ValueError, 'RPATH'):
            obs.plugin_dependencies(self.base/'rpath.so')
        link = self.work/'link.so';link.symlink_to(self.plugin)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            obs.plugin_dependencies(link)

    def test_tampered_package_or_provenance_stops_staging(self):
        for fault in ('rpm', 'metadata', 'version', 'symlink'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.work) as temp:
                bundle = Path(shutil.copytree(self.bundle, Path(temp)/'bundle'))
                manifest = json.loads((bundle/'manifest.json').read_text())
                if fault == 'rpm':
                    (bundle/obs.filename()).write_bytes(b'corrupt rpm')
                elif fault == 'symlink':
                    (bundle/obs.filename()).unlink()
                    (bundle/obs.filename()).symlink_to(self.rpm)
                elif fault == 'version':
                    manifest['build']['version'] = '0.0.0'
                else:
                    manifest['build']['files']['usr/bin/obs'] = 'invalid'
                (bundle/'manifest.json').write_text(json.dumps(manifest))
                staging = Path(temp)/'staging';staging.mkdir()
                with self.assertRaises(ValueError):
                    obs.stage(bundle, staging)
                self.assertEqual(list(staging.iterdir()), [])

    def test_changed_sources_and_regressions_invalidate_bundle(self):
        recipe = Path(shutil.copytree(obs.RECIPE, self.work/'recipe'))
        tests = tuple(self.work/p.name for p in obs.TESTS)
        for source, destination in zip(obs.TESTS, tests):
            shutil.copyfile(source, destination)
        with patch.object(obs, 'RECIPE', recipe), patch.object(obs, 'TESTS', tests):
            obs.validate(self.bundle)
            for path in (recipe/'liuqin-v4l2.c', tests[0]):
                previous = path.read_bytes()
                path.write_bytes(previous+b'\n/* new revision */\n')
                with self.assertRaisesRegex(ValueError, '源码'):
                    obs.validate(self.bundle)
                path.write_bytes(previous)

    def test_image_checks_payload_installed_version_and_actual_loader(self):
        root = Path(shutil.copytree(self.payload, self.work/'root'))
        identity = '\t'.join((obs.NAME, '0:'+obs.VERSION+'-'+obs.RELEASE, 'aarch64', '(none)'))
        def chroot(_root, command, **kwargs):
            self.assertEqual(command[:2], ['rpm', '-q'])
            return subprocess.CompletedProcess(command, 0, stdout=identity+'\n')
        with patch.object(obs, 'probe') as probe:
            obs.check_image(root, chroot)
            probe.assert_called_once_with(root, chroot)
            identity = identity.replace(obs.VERSION, '0.0.0')
            with self.assertRaisesRegex(ValueError, 'RPM 版本'):
                obs.check_image(root, chroot)
            (root/obs.PLUGIN).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, '摘要'):
                obs.check_image(root, chroot)
            (root/obs.LICENSE/'BUILD.json').unlink()
            with self.assertRaisesRegex(ValueError, '缺失'):
                obs.check_image(root, chroot)

    def test_default_selection_and_standalone_force_use_only_runtime_and_ffmpeg(self):
        default = json.loads((PROJECT/'build/rootfs/fedora-plasma.json').read_text())
        self.assertTrue(obs.selected(default))
        with self.assertRaisesRegex(ValueError, 'ffmpeg-free'):
            obs.selected({'packages':['obs-studio']})
        with self.assertRaisesRegex(ValueError, '移除'):
            obs.selected(dict(default, remove=[obs.NAME]))
        flow = object.__new__(FedoraFlow)
        flow.data = {'jobs':2};flow.refresh = False
        archive = self.work/'runtime.tar';archive.touch()
        dependency = self.work/'ffmpeg';dependency.mkdir()
        (dependency/'manifest.json').write_text('matching FFmpeg bundle')
        def stage(name, inputs, build, artifacts):
            self.assertEqual(name, 'fedora-obs-v4l2')
            self.assertEqual(inputs['source'], obs.inputs())
            self.assertEqual(inputs['ffmpeg_manifest'], sha(dependency/'manifest.json'))
            self.assertIn('assembled/'+obs.filename(), artifacts)
            build(self.work/'stage')
            return self.work/'stage'
        for force in (False, True):
            flow.recipe = {'packages':[]} if force else default
            with self.subTest(force=force), \
                 patch.object(flow, 'target_runtime_root', return_value=archive), \
                 patch.object(flow, 'ffmpeg_rpms', return_value=dependency) as ffmpeg, \
                 patch.object(flow, 'stage', side_effect=stage), patch.object(flow, 'target_action') as action:
                self.assertEqual(flow.obs_v4l2_rpm(force=force), self.work/'stage/assembled')
            ffmpeg.assert_called_once_with(force=force)
            action.assert_called_once_with('obs-v4l2', archive, self.work/'stage/assembled',
                                           ffmpeg_bundle=dependency, jobs=2)
        flow.recipe = {'packages':['kate']}
        with patch.object(flow, 'target_runtime_root', side_effect=AssertionError('unexpected build')):
            self.assertIsNone(flow.obs_v4l2_rpm())


if __name__ == '__main__':
    unittest.main(verbosity=2)
