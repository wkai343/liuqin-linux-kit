#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise Libcamera bundle validation, image locks and source retention offline."""
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
import fedora_libcamera as libcamera
from fedora_flow import FedoraFlow
from source_build import sha


class LibcameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.base = Path(cls.fixture.name)
        cls.info = libcamera.record()
        top = cls.base/'rpmbuild'
        for name in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
            (top/name).mkdir(parents=True)
        (top/'SOURCES/source.txt').write_text('Synthetic RPM fixture, not Libcamera.\n')
        spec = ['Name: libcamera', 'Version: '+cls.info['version'],
                'Release: '+cls.info['release']+'.'+cls.info['dist'],
                'Summary: Test fixture', 'License: MIT', 'Source0: source.txt',
                'AutoReqProv: no', '%description', 'Test fixture']
        for name in (*libcamera.PACKAGES, *libcamera.EXTRA_PACKAGES, 'wrong-arch'):
            if name == 'libcamera':continue
            spec += ['%package -n '+name, 'Summary: Test fixture']
            if name == 'wrong-arch':spec += ['BuildArch: noarch']
            spec += ['%description -n '+name, 'Test fixture']
        spec += ['%install', 'mkdir -p %{buildroot}/usr/share/fixture']
        for name in (*libcamera.PACKAGES, *libcamera.EXTRA_PACKAGES, 'wrong-arch'):
            spec += ['cp %{SOURCE0} %{buildroot}/usr/share/fixture/'+name]
        for name in (*libcamera.PACKAGES, *libcamera.EXTRA_PACKAGES, 'wrong-arch'):
            spec += ['%files -n '+name, '/usr/share/fixture/'+name]
        path = top/'SPECS/fixture.spec'
        path.write_text('\n'.join(spec)+'\n')
        subprocess.run(['rpmbuild', '-ba', '--target', 'aarch64', '--define', '_topdir '+str(top),
                        '--define', '__os_install_post %{nil}', '--define', '_build_id_links none',
                        str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.bundle = libcamera.collect(top, cls.base/'bundle')

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.root = self.work/'root'
        self.root.mkdir()
        for name in ('usr/bin/cam', 'usr/lib64/libcamera.so.0.7.2',
                     'usr/lib64/gstreamer-1.0/libgstlibcamera.so',
                     'usr/lib64/libcamera/ipa/ipa_soft_simple.so'):
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def tearDown(self):
        self.temp.cleanup()

    def copy_bundle(self):
        return Path(shutil.copytree(self.bundle, self.work/'bundle'))

    def locks(self, names=libcamera.PACKAGES):
        text = ''
        for name in names:
            text += ('\n[[packages]]\nname = '+json.dumps(name)+
                     '\n[[packages.conditions]]\nkey = "evr"\ncomparator = "="\nvalue = '+
                     json.dumps(libcamera.release(self.info))+'\n')
        return text

    def chroot(self, root, command, **kwargs):
        if command[0] == 'rpm':
            return subprocess.CompletedProcess(command, 0, stdout='\n'.join(
                '\t'.join((name, '0:'+libcamera.release(self.info), 'aarch64', '(none)'))
                for name in command[4:])+'\n')
        if command[0] == 'cam':
            return subprocess.CompletedProcess(command, 0, stdout='libcamera 0.7.2')
        self.assertEqual(command[:5], ['dnf','-y','--disable-repo=*','versionlock','add'])
        self.assertTrue(set(command[5:]).issubset((*libcamera.PACKAGES, *libcamera.EXTRA_PACKAGES)))
        path = root/'etc/dnf/versionlock.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text() if path.exists() else 'version = "1.0"\n'
        path.write_text(old+self.locks(command[5:]))

    def test_real_rpms_are_checked_and_only_runtime_libraries_are_staged(self):
        manifest = libcamera.validate(self.bundle)
        staging = self.work/'staging'
        staging.mkdir()
        libcamera.stage(self.bundle, staging)
        self.assertEqual(set(p.name for p in staging.iterdir()), set(libcamera.filenames(self.info).values()))
        self.assertEqual(manifest['srpm']['sha256'], sha(self.bundle/libcamera.source_filename(self.info)))

    def test_recipe_with_only_python_uses_matching_subpackage(self):
        recipe = {'packages': ['python3-libcamera']}
        self.assertTrue(libcamera.selected(recipe))
        extras = libcamera.requested_extras(recipe)
        staging = self.work/'selected'
        staging.mkdir()
        libcamera.stage(self.bundle, staging, extras=extras)
        self.assertEqual({p.name for p in staging.iterdir()},
                         set(libcamera.filenames(self.info, (*libcamera.PACKAGES, 'python3-libcamera')).values()))
        libcamera.configure(self.root, self.bundle, self.chroot, extras)
        libcamera.check_image(self.root, self.chroot)
        locks = tomllib.loads((self.root/'etc/dnf/versionlock.toml').read_text())
        self.assertIn('python3-libcamera', [entry['name'] for entry in locks['packages']])

    def test_development_packages_are_separate_and_only_staged_on_request(self):
        staging = self.work/'build-dependencies'
        staging.mkdir()
        libcamera.stage(self.bundle, staging, development=True)
        self.assertEqual({p.name for p in staging.iterdir()},
            set(libcamera.filenames(self.info, (*libcamera.PACKAGES, *libcamera.EXTRA_PACKAGES)).values()))
        bundle = self.copy_bundle()
        next((bundle/'extra-rpms').iterdir()).unlink()
        with self.assertRaisesRegex(ValueError, '开发包集合'):
            libcamera.validate(bundle)

    def test_missing_tampered_extra_and_symlinked_packages_stop_before_staging(self):
        for fault in ('missing', 'tampered', 'extra', 'symlink', 'source'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.work) as temp:
                bundle = Path(shutil.copytree(self.bundle, Path(temp)/'bundle'))
                target = next((bundle/'rpms').iterdir())
                if fault == 'missing':target.unlink()
                elif fault == 'tampered':target.write_bytes(target.read_bytes()+b'corrupt')
                elif fault == 'extra':(bundle/'rpms/extra.rpm').touch()
                elif fault == 'source':(bundle/libcamera.source_filename(self.info)).unlink()
                else:
                    backing = bundle/'backing'
                    target.rename(backing)
                    target.symlink_to(backing)
                staging = Path(temp)/'staging'
                staging.mkdir()
                with self.assertRaises((ValueError, FileNotFoundError)):
                    libcamera.stage(bundle, staging)
                self.assertEqual(list(staging.iterdir()), [])

    def test_real_rpm_architecture_version_and_source_type_are_enforced(self):
        runtime = self.bundle/'rpms'/libcamera.filenames(self.info)['libcamera']
        wrong = self.base/'rpmbuild/RPMS/noarch'/('wrong-arch-'+libcamera.release(self.info)+'.noarch.rpm')
        newer = dict(self.info, release='4.liuqin1')
        for path, name, info, source in ((wrong,'wrong-arch',self.info,False),
                (runtime,'libcamera',newer,False), (runtime,'libcamera',self.info,True)):
            with self.subTest(path=path,source=source),self.assertRaisesRegex(ValueError,'不匹配'):
                libcamera.check_rpm(path, name, info, source=source)

    def test_changed_patch_invalidates_existing_bundle(self):
        recipe = Path(shutil.copytree(libcamera.RECIPE, self.work/'recipe'))
        with patch.object(libcamera, 'RECIPE', recipe):
            libcamera.validate(self.bundle)
            path = recipe/'patches'/self.info['patches'][0]
            path.write_text(path.read_text()+'\n# new patch revision\n')
            with self.assertRaisesRegex(ValueError,'配方或补丁'):
                libcamera.validate(self.bundle)

    def test_image_preserves_unrelated_locks_and_corresponding_source(self):
        path = self.root/'etc/dnf/versionlock.toml'
        path.parent.mkdir(parents=True)
        path.write_text('version = "1.0"\n'+self.locks(['unrelated-package']))
        libcamera.configure(self.root, self.bundle, self.chroot)
        libcamera.check_image(self.root, self.chroot)
        names = {p['name'] for p in tomllib.loads(path.read_text())['packages']}
        self.assertEqual(names, {*libcamera.PACKAGES, 'unrelated-package'})
        docs = self.root/libcamera.LICENSE_DIR
        self.assertEqual(sha(docs/libcamera.source_filename(self.info)),
                         sha(self.bundle/libcamera.source_filename(self.info)))
        self.assertTrue((docs/'recipe/source.json').is_file())

    def test_wrong_installed_version_stops_before_locking(self):
        def chroot(root, command, **kwargs):
            self.assertNotEqual(command[0], 'dnf')
            result = self.chroot(root, command, **kwargs)
            result.stdout = result.stdout.replace(self.info['release'], '0.invalid')
            return result
        with self.assertRaisesRegex(ValueError, '运行库'):
            libcamera.configure(self.root, self.bundle, chroot)
        self.assertFalse((self.root/libcamera.LICENSE_DIR).exists())

    def test_missing_ipa_stops_before_locking(self):
        (self.root/'usr/lib64/libcamera/ipa/ipa_soft_simple.so').unlink()
        with self.assertRaisesRegex(ValueError, '运行组件缺失'):
            libcamera.configure(self.root, self.bundle, self.chroot)
        self.assertFalse((self.root/'etc/dnf/versionlock.toml').exists())

    def test_missing_conflicting_lock_or_missing_source_rejects_final_image(self):
        libcamera.configure(self.root, self.bundle, self.chroot)
        path = self.root/'etc/dnf/versionlock.toml'
        original = path.read_text()
        for content in ('version = "1.0"\n'+self.locks(libcamera.PACKAGES[1:]),
                        original+self.locks(libcamera.PACKAGES[:1]),
                        original+self.locks(['libcamera*']),
                        original.replace(self.info['release'],'0.invalid'),
                        original.replace('version = "1.0"','version = "2.0"')):
            path.write_text(content)
            with self.assertRaisesRegex(ValueError,'版本锁'):
                libcamera.check_image(self.root, self.chroot)
        path.write_text(original)
        (self.root/libcamera.LICENSE_DIR/libcamera.source_filename(self.info)).unlink()
        with self.assertRaisesRegex(ValueError,'缺失'):
            libcamera.check_image(self.root, self.chroot)

    def test_final_rootfs_can_require_libcamera_even_when_source_directory_is_missing(self):
        flow = object.__new__(FedoraFlow)
        archive = self.work/'root.tar.gz'
        archive.touch()
        with patch.object(flow,'run',create=True) as run:
            flow.target_action('kernel-files',archive,self.work/'assembled',require_libcamera=True)
        self.assertIn('--require-libcamera',run.call_args.args[0])
        with self.assertRaisesRegex(ValueError,'缺失'):
            libcamera.check_image(self.root,self.chroot)

    def test_non_libcamera_recipe_does_not_download_or_compile(self):
        flow = object.__new__(FedoraFlow)
        flow.recipe = {'packages':['kate']}
        with patch('fedora_flow.download', side_effect=AssertionError('unexpected download')), \
             patch.object(flow,'target_runtime_root',side_effect=AssertionError('unexpected build')):
            self.assertIsNone(flow.libcamera_archive())
            self.assertIsNone(flow.libcamera_rpms())

    def test_default_recipe_builds_in_separate_stage_with_all_runtime_and_source_outputs(self):
        flow = object.__new__(FedoraFlow)
        flow.recipe = json.loads((PROJECT/'build/rootfs/fedora-plasma.json').read_text())
        flow.data = {'jobs':2}
        flow.refresh = False
        archive = self.work/'runtime.tar'
        archive.touch()
        source = self.work/'libcamera.src.rpm'
        source.touch()
        def stage(name, inputs, build, artifacts):
            self.assertEqual(name,'fedora-libcamera')
            self.assertEqual(inputs['recipe'],libcamera.inputs())
            self.assertIn('assembled/rpms',artifacts)
            self.assertIn('assembled/extra-rpms',artifacts)
            self.assertIn('assembled/'+libcamera.source_filename(self.info),artifacts)
            self.assertIn('assembled/build-packages.tsv',artifacts)
            build(self.work/'stage')
            return self.work/'stage'
        with patch.object(flow,'target_runtime_root',return_value=archive), \
             patch.object(flow,'libcamera_archive',return_value=source), \
             patch.object(flow,'stage',side_effect=stage),patch.object(flow,'target_action') as action:
            self.assertEqual(flow.libcamera_rpms(),self.work/'stage/assembled')
        action.assert_called_once_with('libcamera',archive,self.work/'stage/assembled',libcamera_source=source,jobs=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
