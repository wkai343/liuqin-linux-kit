#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build-stage routing checks without Nix, compilation, sudo or device access."""
import importlib.util
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('devbox_build', project / 'build/devbox/build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
sys.path.insert(0, str(project / 'installer'))
from liuqin_gui.demo import demo_image
from image_export import export_images


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = patch.object(builder, 'PROJECT', self.root)
        self.project.start()
        (self.root/'legacy.json').write_text(json.dumps({'root_source':'old-root'}))

    def tearDown(self):
        self.project.stop()
        self.temporary.cleanup()

    @unittest.skipUnless(os.name == 'posix', 'Devbox builds use a Linux shell')
    def test_named_scripts_forward_custom_arguments_without_shell_expansion(self):
        bin = self.root / 'bin'
        bin.mkdir()
        capture = self.root / 'arguments.json'
        python = bin / 'python3'
        python.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                          'open(os.environ["TEST_ARGUMENTS"],"w").write(json.dumps(sys.argv[1:]))\n')
        python.chmod(0o755)
        scripts = json.loads((project / 'devbox.json').read_text())['shell']['scripts']
        arguments = ['--config', '配置 目录;$(false).json', '--configure-only']
        env = dict(os.environ, PATH=str(bin) + os.pathsep + os.environ['PATH'],
                   TEST_ARGUMENTS=str(capture))
        for name, body in scripts.items():
            with self.subTest(script=name):
                subprocess.run(['sh', '-c', body, name, *arguments], env=env, check=True)
                received = json.loads(capture.read_text())
                self.assertEqual(received[-len(arguments):], arguments)

    def test_help_and_outputs_need_no_config_and_never_build(self):
        before = set(self.root.rglob('*'))
        for action in ('help', 'outputs'):
            with self.subTest(action=action), \
                 patch.object(sys, 'argv', ['build.py', action]), \
                 patch.object(builder, 'config', side_effect=AssertionError('configuration read')), \
                 patch.object(builder, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
                builder.main()
                self.assertTrue(output.getvalue().strip())
                run.assert_not_called()
        self.assertEqual(set(self.root.rglob('*')), before)

    def output_fixture(self, stage, filename, contract=None, *, completed=True):
        directory = self.root / 'out/build/from-source' / stage
        path = directory / filename; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic image')
        artifacts = {filename: {'file': hashlib.sha256(path.read_bytes()).hexdigest()}}
        if contract is not None:
            name = str(Path(filename).parent / 'native-root.contract')
            (directory / name).write_bytes(contract)
            artifacts[name] = {'file': hashlib.sha256(contract).hexdigest()}
        if completed:
            (directory / 'build-receipt.json').write_text(json.dumps(dict(inputs='0' * 64, artifacts=artifacts)))
        return path

    def test_outputs_group_by_full_kernel_identity_instead_of_release_or_recency(self):
        contract = (b'LIUQIN_ROOT_INTERFACE_V2\nkernel_release=7.3.0-liuqin-test\nimage_sha256='
                    + b'1' * 64 + b'\nmodules_sha256=' + b'2' * 64 + b'\n')
        other = contract.replace(b'1' * 64, b'3' * 64)
        boot = self.output_fixture('boot-first', 'image/boot-liuqin-native.img', contract)
        root = self.output_fixture('fedora-rootfs-first', 'packed/rootfs.tar.gz', contract)
        alternate = self.output_fixture('boot-second', 'image/boot-liuqin-native.img', other)
        installer = self.output_fixture('installer-first', 'image/installer.img')
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            builder.show_outputs()
        text = output.getvalue()
        first = text.split('配套组 ' + hashlib.sha256(contract).hexdigest()[:12], 1)[1].split('\n配套组 ', 1)[0]
        self.assertIn(str(boot), first); self.assertIn(str(root), first)
        self.assertNotIn(str(alternate), first)
        self.assertNotIn(str(installer), text)
        self.assertEqual(text.count('配套组 '), 2)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_outputs_exclude_unpublished_missing_linked_and_changed_contracts(self):
        contract = (b'LIUQIN_ROOT_INTERFACE_V2\nkernel_release=7.3.0-liuqin-test\nimage_sha256='
                    + b'1' * 64 + b'\nmodules_sha256=' + b'2' * 64 + b'\n')
        self.output_fixture('.attempt-boot-failed', 'image/boot-liuqin-native.img', contract)
        self.output_fixture('boot-incomplete', 'image/boot-liuqin-native.img', contract, completed=False)
        missing = self.output_fixture('boot-missing', 'image/boot-liuqin-native.img', contract); missing.unlink()
        corrupt = self.output_fixture('boot-corrupt', 'image/boot-liuqin-native.img', contract)
        (corrupt.parent / 'native-root.contract').write_bytes(contract.replace(b'1' * 64, b'3' * 64))
        linked = self.output_fixture('boot-linked', 'image/boot-liuqin-native.img', contract)
        outside=self.root/'outside-boot.img';outside.write_bytes(b'untrusted external image')
        linked.unlink(); linked.symlink_to(outside)
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            builder.show_outputs()
        self.assertIn('尚无已完成', output.getvalue())
        self.assertIn('内核标识与完成记录不一致', errors.getvalue())
        self.assertNotIn(str(corrupt), output.getvalue())

    def test_build_routes_to_system_recipe_and_preserves_options(self):
        config = self.root / 'build.json'
        config.write_text(json.dumps(dict(rootfs_config='build/rootfs/fedora-plasma.json')))
        with patch.object(sys,'argv',['build.py','build','--config',str(config),'--refresh']), \
             patch('source_flow.execute') as execute,patch.object(builder,'run') as run:
            builder.main()
        self.assertEqual(execute.call_args.args[1],'rootfs')
        self.assertTrue(execute.call_args.kwargs['refresh'])
        run.assert_not_called()

    def test_export_option_only_accepts_complete_system_builds(self):
        config = self.root/'build.json'
        config.write_text(json.dumps({'rootfs_config':'build/rootfs/fedora-plasma.json'}))
        for options in ([],['--target','system']):
            with self.subTest(options=options), \
                 patch.object(sys,'argv',['build.py','build',*options,'--config',str(config),
                                          '--export-dir',str(self.root/'export')]), \
                 patch('source_flow.execute') as execute:
                builder.main()
            self.assertEqual(execute.call_args.args[1],'rootfs')
            self.assertEqual(execute.call_args.kwargs['export_dir'],self.root/'export')
        with patch.object(sys,'argv',['build.py','outputs','--export-dir','export']), \
             patch.object(builder,'config') as config,contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            builder.main()
        config.assert_not_called()

    def export_fixture(self):
        contract = (b'LIUQIN_ROOT_INTERFACE_V2\nkernel_release=7.3.0-liuqin-test\nimage_sha256='
                    + b'1' * 64 + b'\nmodules_sha256=' + b'2' * 64 + b'\n')
        boot = self.output_fixture('boot-selected', 'image/boot-liuqin-native.img', contract)
        root = self.output_fixture('fedora-rootfs-selected', 'packed/rootfs.tar.gz', contract)
        return boot, root, contract

    def test_export_selects_exact_pair_and_omits_other_caches_and_private_files(self):
        boot, root, contract = self.export_fixture()
        self.output_fixture('boot-other', 'image/boot-liuqin-native.img', contract.replace(b'1'*64, b'3'*64))
        self.output_fixture('rootfs-other', 'packed/rootfs.tar.gz', contract)
        (root.parent/'login.txt').write_text('private fixture')
        (root.parent/'local-config.json').write_text('private configuration')
        out = export_images(boot, root, self.root/'export')
        self.assertEqual({p.name for p in out.iterdir()},
                         {'boot.img', 'rootfs.tar.gz', 'native-root.contract', 'SHA256SUMS'})
        self.assertEqual((out/'boot.img').read_bytes(), boot.read_bytes())
        self.assertEqual((out/'rootfs.tar.gz').read_bytes(), root.read_bytes())
        self.assertEqual((out/'native-root.contract').read_bytes(), contract)
        for line in (out/'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(), digest)
        # Published files must not be hardlinks to mutable cache files.
        boot.write_bytes(b'changed cache')
        self.assertNotEqual((out/'boot.img').read_bytes(), boot.read_bytes())
        with self.assertRaisesRegex(ValueError, '已存在'):
            export_images(boot, root, out)
        self.assertEqual((out/'rootfs.tar.gz').read_bytes(), root.read_bytes())

    def test_export_rejects_damaged_incomplete_or_mismatched_pairs_without_publishing(self):
        for failure in ('missing-rootfs', 'missing-receipt', 'linked-image', 'changed-image',
                        'changed-contract', 'mismatched-contract'):
            with self.subTest(failure=failure):
                boot, root, contract = self.export_fixture()
                if failure == 'missing-rootfs': root.unlink()
                elif failure == 'missing-receipt': (root.parent.parent/'build-receipt.json').unlink()
                elif failure == 'linked-image':
                    root.unlink(); root.symlink_to(boot)
                elif failure == 'changed-image': root.write_bytes(b'corrupt')
                elif failure == 'changed-contract':
                    (root.parent/'native-root.contract').write_bytes(contract.replace(b'1'*64, b'3'*64))
                elif failure == 'mismatched-contract':
                    self.output_fixture('fedora-rootfs-selected', 'packed/rootfs.tar.gz',
                                        contract.replace(b'1'*64, b'3'*64))
                with self.assertRaises(ValueError):
                    export_images(boot, root, self.root/'export')
                self.assertFalse((self.root/'export').exists())
                self.assertEqual(list(self.root.glob('.image-export-*')), [])
                if root.is_symlink(): root.unlink()

    def test_legacy_configs_cannot_enter_any_build_target(self):
        for target in ('system','kernel','modules','boot','packages'):
            with self.subTest(target=target), \
                 patch.object(sys,'argv',['build.py','build','--target',target,'--config',str(self.root/'legacy.json')]), \
                 patch('source_flow.execute') as execute,self.assertRaisesRegex(ValueError,'不支持的构建配置项'):
                builder.main()
            execute.assert_not_called()

    def test_retired_stages_are_rejected_before_any_build(self):
        for action in ('all','build-target','kernel','modules','rootfs','boot','kernel-debs','kernel-packages',
                       'init-build','images','installer','installer-kernel','build-installer','runtime'):
            with self.subTest(action=action), patch.object(sys,'argv',['build.py',action]), \
                 patch.object(builder,'config') as config, contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as error:
                builder.main()
            self.assertEqual(error.exception.code,2)
            config.assert_not_called()
        scripts=json.loads((project/'devbox.json').read_text())['shell']['scripts']
        self.assertEqual(set(scripts), {'help','init','doctor','fetch-rom','plan','build','outputs','test'})
        self.assertFalse({'images','installer','installer-kernel','build-installer','runtime'} & scripts.keys())

    def test_build_targets_dispatch_to_matching_recipe_stages(self):
        config = self.root/'official.json'
        config.write_text(json.dumps({'rootfs_config':'build/rootfs/fedora-plasma.json'}))
        for target, expected in (('system','rootfs'),('kernel','kernel'),('modules','modules'),
                                 ('boot','boot'),('packages','kernel-debs')):
            with self.subTest(target=target), \
                 patch.object(sys,'argv',['build.py','build','--target',target,'--config',str(config)]), \
                 patch('source_flow.execute') as execute,patch.object(builder,'run') as run:
                builder.main()
            self.assertEqual(execute.call_args.args[1],expected)
            run.assert_not_called()
        with patch.object(sys,'argv',['build.py','build','--target','kernel','--configure-only',
                                     '--config',str(config)]),patch('source_flow.execute') as execute:
            builder.main()
        self.assertEqual(execute.call_args.args[1],'kernel')
        self.assertTrue(execute.call_args.kwargs['configure_only'])

    def test_invalid_build_target_options_stop_before_reading_config(self):
        for options in (['outputs','--target','kernel'],['build','--target','unknown'],
                        ['build','--target','boot','--configure-only'],
                        ['build','--target','packages','--export-dir','export']):
            with self.subTest(options=options),patch.object(sys,'argv',['build.py',*options]), \
                 patch.object(builder,'config') as config,contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                builder.main()
            config.assert_not_called()











    def test_init_does_not_overwrite_local_config(self):
        content = '{"rootfs_config": "my-rootfs.json", "rom_images": "/my/rom", "jobs": 3}'
        config = self.root / 'build.json'; config.write_text(content)
        with patch.object(sys, 'argv', ['build.py', 'init', '--config', str(config)]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            builder.main()
        self.assertEqual(config.read_text(), content)
        self.assertIn('已保留全部设置', output.getvalue())
        self.assertIn('无需重复初始化', output.getvalue())

    def test_init_rejects_legacy_config_without_modifying_it(self):
        config=self.root/'build.json';original='{"root_source":"my-root"}';config.write_text(original)
        with patch.object(sys,'argv',['build.py','init','--config',str(config)]), \
             self.assertRaisesRegex(ValueError,'不支持的构建配置项'):
            builder.main()
        self.assertEqual(config.read_text(),original)

    def test_init_creates_current_config_and_rejects_obsolete_flag(self):
        config=self.root/'new.json'
        with patch.object(sys,'argv',['build.py','init','--config',str(config)]), \
             contextlib.redirect_stdout(io.StringIO()):
            builder.main()
        self.assertEqual(json.loads(config.read_text()),json.loads(builder.EXAMPLE.read_text()))
        rejected=self.root/'must-not-exist.json'
        with patch.object(sys,'argv',['build.py','init','--from-source','--config',str(rejected)]), \
             contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            builder.main()
        self.assertFalse(rejected.exists())

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = patch.object(builder, 'PROJECT', self.root); self.project.start()
        self.file = self.root / 'build.json'

    def tearDown(self):
        self.project.stop(); self.temporary.cleanup()

    def load(self, values):
        self.file.write_text(json.dumps({'rootfs_config':'build/rootfs/fedora-plasma.json',**values})); return builder.config(self.file)

    def test_usb_names_have_defaults_and_allow_literal_unicode_and_shell_characters(self):
        defaults = self.load({})
        self.assertEqual(defaults['usb_manufacturer'], 'Xiaomi')
        self.assertEqual(defaults['usb_product'], 'Pad 6 Pro')
        name = '小米 Pad "$HOME" $(true) `true` \\!'
        self.assertEqual(self.load({'usb_product': name})['usb_product'], name)
        self.assertEqual(self.load({'usb_product': '米' * 42})['usb_product'], '米' * 42)

    def test_usb_names_reject_invalid_or_unrepresentable_descriptors(self):
        for value in (None, 123, '', '   ', 'Pad\nName', 'Pad\rName', 'Pad\0Name',
                      'Pad\x7fName', 'x' * 127, '米' * 43, '\ud800'):
            for key in ('usb_product', 'usb_manufacturer'):
                with self.subTest(key=key, value=repr(value)):
                    with self.assertRaisesRegex(ValueError, key):
                        self.load({key: value})

    def test_minimal_configuration_keeps_explicit_target_kernel_choices(self):
        data = self.load({'kernel_source': 'my-kernel',
                          'kernel_fragments': [], 'kernel_localversion': '-liuqin2'})
        self.assertEqual(data['kernel_source'], 'my-kernel')
        self.assertEqual(data['kernel_fragments'], [])
        self.assertEqual(data['kernel_localversion'], '-liuqin2')
        for key in ('installer_kernel_source','installer_kernel_out','installer_image','runtime_out','bundle_out','image_work'):
            self.assertNotIn(key,data)









    def test_obsolete_or_unknown_configuration_fields_are_rejected(self):
        for name in ('root_source','kernel_out','modules_out','root_out','boot_out','kernel_debs_out',
                     'inputs','inputs_file','installer_kernel_source','installer_image','runtime_out','bundle_out'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'不支持的构建配置项'):
                self.load({name:'obsolete'})
        for value in (None,'',3):
            with self.subTest(rootfs_config=value),self.assertRaisesRegex(ValueError,'rootfs_config'):
                self.load({'rootfs_config':value})

    def test_current_overrides_and_literal_paths_are_preserved(self):
        values={'kernel_source':'custom/source','kernel_source_lock':'custom/lock.json',
                'kernel_config':'custom/config','kernel_fragments':['custom/fragment.config'],
                'package_version':'2.0','jobs':3,'rom_images':'custom/rom'}
        loaded=self.load(values)
        for key,value in values.items():self.assertEqual(loaded[key],value)

    def test_show_config_is_readonly_and_works_before_inputs_exist(self):
        self.file.write_text('{"rootfs_config":"not-yet-prepared.json"}')
        before = set(self.root.rglob('*'))
        with patch.object(sys, 'argv', ['build.py', 'show-config', '--config', str(self.file)]), \
             patch.object(builder, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            builder.main()
        data = json.loads(output.getvalue())
        self.assertEqual(data['rootfs_config'],'not-yet-prepared.json')
        self.assertNotIn('root_out',data)
        self.assertEqual(set(self.root.rglob('*')), before)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
