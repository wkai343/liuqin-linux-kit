#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build-stage routing checks without Nix, compilation, sudo or device access."""
import importlib.util
import contextlib
import copy
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


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = patch.object(builder, 'PROJECT', self.root)
        self.project.start()
        legacy = self.root / 'legacy.json'
        legacy.write_text(json.dumps({'root_source': 'out/build/root-custom-work'}))
        self.data = builder.config(legacy)
        self.data.pop('inputs_file')
        for key in ('kernel_source', 'installer_kernel_source', 'kernel_out', 'modules_out', 'root_source', 'runtime_root', 'root_out', 'runtime_out', 'boot_out'):
            (self.root / self.data[key]).mkdir(parents=True, exist_ok=True)
        (self.root / self.data['installer_kernel_out']).mkdir(parents=True)
        image = self.root / self.data['installer_image']
        image.parent.mkdir(parents=True)
        image.write_bytes(b'prepared installer fixture')
        for key, name in self.data['inputs'].items():
            path = self.root / name
            if key in ('AUDIO_TOPOLOGY', 'BUSYBOX'):
                path.parent.mkdir(parents=True, exist_ok=True); path.touch()
            else:
                path.mkdir(parents=True, exist_ok=True)

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

    def test_kernel_uses_custom_config_and_configure_only_never_builds_images(self):
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'kernel', configure_only=True)
        argv = run.call_args.args[0]
        self.assertIn('--custom', argv)
        self.assertIn('--configure-only', argv)
        self.assertIn('--config-fragment', argv)
        self.assertNotIn('images', argv)

    def test_only_rootfs_step_is_elevated(self):
        (self.root / self.data['root_out']).rmdir()
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'rootfs')
        self.assertTrue(run.call_args.kwargs['root'])
        self.assertIn('build/scripts/build-liuqin-custom-root.py', run.call_args.args[0])
        (self.root / self.data['root_out']).mkdir()
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'images')
        self.assertNotIn('root', run.call_args.kwargs)

    def test_target_images_reuse_installer_without_its_kernel_or_runtime(self):
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'images')
        env = run.call_args.kwargs['env']
        self.assertEqual(env['BOOT_IMAGE'], str(self.root / self.data['boot_out'] / 'boot-liuqin-native.img'))
        self.assertNotIn('KERNEL_SOURCE', env)
        self.assertEqual(env['CUSTOM_ROOT_OUT'], str(self.root / self.data['root_out']))
        self.assertEqual(env['INSTALLER_IMAGE'], str(self.root / self.data['installer_image']))
        self.assertNotIn('INSTALLER_RUNTIME', env)
        self.assertNotIn('INSTALLER_KERNEL_OUT', env)
        self.assertNotIn('root', run.call_args.kwargs)

    def test_installer_kernel_does_not_receive_target_zram_fragment_or_suffix(self):
        self.data['kernel_source_lock']='custom/target.json'
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'installer-kernel', configure_only=True)
        argv = run.call_args.args[0]
        self.assertIn(str(self.root / self.data['installer_kernel_out']), argv)
        self.assertIn('--localversion=-liuqin-installer', argv)
        self.assertNotIn('--config-fragment', argv)
        self.assertNotIn(str(self.root / self.data['kernel_out']), argv)
        self.assertEqual(argv[argv.index('--source-lock')+1],str(self.root/'build/kernel/source-6.17.json'))

    def test_target_build_needs_no_runtime_root_and_never_rebuilds_installer(self):
        self.data['runtime_root'] = 'not-prepared'
        (self.root / self.data['root_out']).rmdir()
        (self.root / self.data['boot_out']).rmdir()
        config = self.root / 'build.json'; config.write_text(json.dumps(self.data))
        with patch.object(sys, 'argv', ['build.py', 'all', '--config', str(config)]), patch.object(builder, 'stage') as stage:
            builder.main()
        self.assertEqual([call.args[1] for call in stage.call_args_list], ['kernel', 'modules', 'rootfs', 'boot', 'images'])

    def test_kernel_update_build_does_not_require_rootfs_or_installer(self):
        self.data.update(usb_manufacturer='测试厂商', usb_product='自定义平板')
        self.data['root_source'] = self.data['root_out'] = self.data['runtime_root'] = self.data['installer_image'] = 'missing'
        (self.root / self.data['boot_out']).rmdir()
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'boot')
        env = run.call_args.kwargs['env']
        self.assertEqual(env['LIUQIN_ROOT_PROFILE'], 'native-updatable')
        self.assertEqual(env['LIUQIN_USB_MANUFACTURER'], '测试厂商')
        self.assertEqual(env['LIUQIN_USB_PRODUCT'], '自定义平板')
        self.assertNotIn('NATIVE_ROOT_HASHES', env)
        (self.root / self.data['boot_out']).mkdir()
        with patch.object(builder, 'run') as run:
            builder.stage(self.data, 'kernel-debs')
        self.assertIn('build/scripts/build-liuqin-kernel-debs.py', run.call_args.args[0])
        self.assertNotIn(str(self.root / self.data['bundle_out']), run.call_args.args[0])

    def test_installer_can_be_built_without_target_root_or_kernel_outputs(self):
        self.data.update(usb_manufacturer='测试厂商', usb_product='自定义平板')
        (self.root / self.data['installer_image']).unlink()
        self.data['root_source'] = 'absent-target-root'
        self.data['kernel_out'] = 'absent-target-kernel'
        records = {'etc/liuqin-storage-mode': b'readonly\n', 'etc/liuqin-installer': b'liuqin\n',
                   'etc/liuqin-installer-interface': b'liuqin-installer-v2\n',
                   'etc/liuqin-installer-kernel.release': b'6.17-install\n'}
        def build(_argv, **kwargs):
            env = kwargs['env']
            self.assertEqual(env['INSTALLER_KERNEL_OUT'], str(self.root / self.data['installer_kernel_out']))
            self.assertEqual(env['LIUQIN_USB_MANUFACTURER'], '测试厂商')
            self.assertEqual(env['LIUQIN_USB_PRODUCT'], '自定义平板')
            self.assertNotIn('CUSTOM_ROOT_OUT', env)
            work = Path(env['OUT_DIR']); work.mkdir(parents=True)
            (work / 'installer.img').write_bytes(demo_image(records))
        with patch.object(builder, 'run', side_effect=build):
            builder.stage(self.data, 'installer')
        self.assertEqual((self.root / self.data['installer_image']).read_bytes(), demo_image(records))

    def test_full_installer_workflow_uses_only_its_own_inputs(self):
        (self.root / self.data['installer_image']).unlink()
        (self.root / self.data['runtime_out']).rmdir()
        self.data['root_source'] = 'missing-target-root'
        self.data['kernel_source'] = 'missing-target-source'
        config = self.root / 'build.json'; config.write_text(json.dumps(self.data))
        with patch.object(sys, 'argv', ['build.py', 'build-installer', '--config', str(config)]), patch.object(builder, 'stage') as stage:
            builder.main()
        self.assertEqual([call.args[1] for call in stage.call_args_list], ['installer-kernel', 'runtime', 'installer'])

    def test_missing_inputs_stop_before_any_stage_runs(self):
        self.data['inputs']['BUSYBOX'] = 'missing-file'
        with patch.object(builder, 'run') as run, self.assertRaisesRegex(RuntimeError, 'BUSYBOX'):
            builder.require_inputs(self.data, 'check-inputs')
        run.assert_not_called()

    def test_existing_output_is_not_deleted_or_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, '输出已存在'):
            builder.require_inputs(self.data, 'all')
        self.assertTrue((self.root / self.data['root_out']).exists())

    def test_init_does_not_overwrite_local_config(self):
        content = '{"rootfs_config": "my-rootfs.json", "rom_images": "/my/rom", "mine": true}'
        config = self.root / 'build.json'; config.write_text(content)
        with patch.object(sys, 'argv', ['build.py', 'init', '--config', str(config)]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            builder.main()
        self.assertEqual(config.read_text(), content)
        self.assertIn('已保留全部设置', output.getvalue())
        self.assertIn('无需重复初始化', output.getvalue())

    def test_init_preserves_legacy_config_and_explains_new_entry(self):
        content = '{"root_source": "my-root"}'
        config = self.root / 'build.json'; config.write_text(content)
        with patch.object(sys, 'argv', ['build.py', 'init', '--config', str(config)]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            builder.main()
        self.assertEqual(config.read_text(), content)
        self.assertIn('build.new.json', output.getvalue())

    def test_default_init_selects_official_root_and_preserves_old_flag(self):
        for index, flags in enumerate(([], ['--from-source'])):
            with self.subTest(flags=flags):
                config = self.root / ('new-' + str(index) + '.json')
                with patch.object(sys, 'argv', ['build.py', 'init', '--config', str(config), *flags]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    builder.main()
                values = json.loads(config.read_text())
                self.assertEqual(values['rootfs_config'], 'build/rootfs/fedora-plasma.json')
                self.assertIn('rom_images', values)
                self.assertNotIn('root_source', values)

    def test_kernel_command_does_not_read_unrelated_board_manifest(self):
        manifest = self.root / 'out/image-inputs.local.json'
        manifest.parent.mkdir(parents=True, exist_ok=True); manifest.write_text('incomplete json')
        config = self.root / 'minimal.json'; config.write_text(json.dumps({'kernel_source': self.data['kernel_source']}))
        with patch.object(sys, 'argv', ['build.py', 'kernel', '--config', str(config)]), patch.object(builder, 'run') as run:
            builder.main()
        self.assertIn('build/scripts/build-liuqin-kernel.py', run.call_args.args[0])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = patch.object(builder, 'PROJECT', self.root); self.project.start()
        self.file = self.root / 'build.json'

    def tearDown(self):
        self.project.stop(); self.temporary.cleanup()

    def load(self, values):
        self.file.write_text(json.dumps(values)); return builder.config(self.file)

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

    def test_minimal_configuration_keeps_explicit_kernel_choices_and_separate_installer(self):
        data = self.load({'kernel_source': 'my-kernel', 'root_source': 'my-root',
                          'kernel_fragments': [], 'kernel_localversion': '-liuqin2'})
        self.assertEqual(data['kernel_source'], 'my-kernel')
        self.assertEqual(data['root_source'], 'my-root')
        self.assertEqual(data['kernel_fragments'], [])
        self.assertEqual(data['kernel_localversion'], '-liuqin2')
        self.assertNotEqual(data['installer_kernel_source'], 'my-kernel')
        self.assertNotEqual(data['kernel_out'], data['installer_kernel_out'])
        self.assertEqual(data['installer_kernel_fragments'], [])

    def test_prepared_paths_are_reused_and_partial_explicit_paths_take_priority(self):
        manifest = self.root / 'out/image-inputs.local.json'; manifest.parent.mkdir()
        manifest.write_text(json.dumps({'FIRMWARE_POOL': '/prepared/firmware',
                                       'AUDIO_TOPOLOGY': '/prepared/topology.bin',
                                       'UBUNTU_DESKTOP_ROOT': '/unrelated/root'}))
        data = self.load({'inputs': {'AUDIO_TOPOLOGY': '/custom/topology.bin'}})
        self.assertEqual(data['inputs']['FIRMWARE_POOL'], '/prepared/firmware')
        self.assertEqual(data['inputs']['AUDIO_TOPOLOGY'], '/custom/topology.bin')
        self.assertIn('BUSYBOX', data['inputs'])
        self.assertNotIn('UBUNTU_DESKTOP_ROOT', data['inputs'])
        self.assertNotEqual(data['root_source'], '/unrelated/root')

    def test_old_full_configuration_retains_every_override(self):
        original = copy.deepcopy(builder.DEFAULTS)
        original.pop('inputs_file')
        original.update(kernel_out='out/build/private/kernel', package_version='2.0',
                        runtime_root='/custom/runtime', jobs=3)
        original['inputs']['BUSYBOX'] = '/custom/busybox'
        manifest = self.root / 'out/image-inputs.local.json'; manifest.parent.mkdir()
        manifest.write_text('unrelated incomplete json')
        loaded = self.load(original)
        for key, value in original.items(): self.assertEqual(loaded[key], value, key)

    def test_explicit_missing_manifest_is_reported_and_can_be_disabled(self):
        with self.assertRaisesRegex(RuntimeError, '素材清单不存在'):
            self.load({'inputs_file': 'missing.json'})
        loaded = self.load({'inputs_file': None})
        self.assertIn('FIRMWARE_POOL', loaded['inputs'])

    def test_old_builtin_fragment_path_is_migrated_without_changing_custom_paths(self):
        data = self.load({'kernel_fragments': ['kernel/configs/custom-zram.config', '/custom/my.config']})
        self.assertEqual(data['kernel_fragments'], ['build/kernel/configs/custom-zram.config', '/custom/my.config'])

    def test_show_config_is_readonly_and_works_before_inputs_exist(self):
        self.file.write_text('{}')
        before = set(self.root.rglob('*'))
        with patch.object(sys, 'argv', ['build.py', 'show-config', '--config', str(self.file)]), \
             patch.object(builder, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            builder.main()
        data = json.loads(output.getvalue())
        self.assertTrue(data['bundle_out'].startswith('out/releases/'))
        self.assertEqual(set(self.root.rglob('*')), before)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
