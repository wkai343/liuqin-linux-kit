#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Rebuild a pinned kernel fixture and reject source, config and bundle drift."""
import copy
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'build/recovery'))
import kernel_recipe as recipe
spec=importlib.util.spec_from_file_location('source_installer_builder',PROJECT/'build/recovery/build-native-installer.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
spec=importlib.util.spec_from_file_location('recovery_source_packager',PROJECT/'installer/package.py')
packager=importlib.util.module_from_spec(spec);spec.loader.exec_module(packager)


def dtb_snapshot(path):
    data=Path(path).read_bytes();header=struct.unpack_from('>10I',data)
    if header[0]!=0xd00dfeed or header[1]!=len(data):raise ValueError('invalid complete DTB')
    offset=header[2];strings=data[header[3]:header[3]+header[8]]
    nodes={};stack=[];reserved=[];cursor=header[4]
    while True:
        address,size=struct.unpack_from('>QQ',data,cursor);cursor+=16
        if not (address or size):break
        reserved.append((address,size))
    while True:
        token=struct.unpack_from('>I',data,offset)[0];offset+=4
        if token==1:
            end=data.index(0,offset);stack.append(data[offset:end].decode());offset=(end+4)&~3
            nodes['/'.join(stack) or '/']={}
        elif token==2:stack.pop()
        elif token==3:
            size,name=struct.unpack_from('>II',data,offset);offset+=8
            key=strings[name:strings.index(0,name)].decode()
            nodes['/'.join(stack) or '/'][key]=data[offset:offset+size];offset=(offset+size+3)&~3
        elif token==4:pass
        elif token==9:break
        else:raise ValueError('invalid FDT token')
    return nodes,reserved


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = patch.object(recipe, 'PROJECT', self.root)
        self.project.start(); self.addCleanup(self.project.stop)
        self.cache = self.root/'cache'
        self.env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
                        GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        self.run_git('init', '-q', str(self.cache), directory=self.root)
        self.run_git('remote', 'add', 'origin', 'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git')
        (self.cache/'driver.c').write_text('int maintenance = 0;\n')
        self.run_git('add', 'driver.c'); self.run_git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'Base')
        base = self.run_git('rev-parse', 'HEAD')
        (self.cache/'driver.c').write_text('int maintenance = 1;\n')
        source_patch = self.root/'build/recovery/kernel/driver.patch'
        source_patch.parent.mkdir(parents=True)
        source_patch.write_text(self.run_git('diff')+'\n')
        self.run_git('add', 'driver.c'); expected = self.run_git('write-tree')
        self.run_git('reset', '--hard', '-q', 'HEAD')
        config = self.root/'build/recovery/kernel/fixture.config'
        config.write_text('CONFIG_USB_GADGET=y\nCONFIG_RUSTC_VERSION=1\n# CONFIG_SND is not set\n')
        self.lock = self.root/'build/recovery/kernel/source.json'
        self.record = dict(schema=1, device='liuqin', repo=self.run_git('remote','get-url','origin'),
                           commit=base, expected_source_tree=expected,
                           config=self.entry(config), patches=[self.entry(source_patch)], dtb_inputs=[],
                           dtb='qcom/fixture.dtb')
        self.save_lock()

    def run_git(self, *args, directory=None):
        return subprocess.check_output(['git','-C',str(directory or self.cache),*args],
                                       env=self.env, text=True).strip()

    def entry(self, path):
        return dict(path=str(path.relative_to(self.root)), sha256=recipe.sha(path))

    def save_lock(self):
        self.lock.write_text(json.dumps(self.record)+'\n')

    def shared_lock(self):
        path=self.root/'build/kernel/source.json';path.parent.mkdir(parents=True)
        shared={key:self.record[key] for key in ('repo','commit','dtb')}
        shared['target_patches']=[entry['path'] for entry in self.record['patches']]
        path.write_text(json.dumps(shared)+'\n')
        self.record['shared_kernel']=self.entry(path);self.save_lock()
        return path

    def assembled(self):
        return recipe.prepare_source(self.cache, self.root/'assembled', self.lock)

    def components(self):
        directory=self.root/'components'; directory.mkdir()
        files={'Image.gz':gzip.compress(b'kernel'), 'recovery.dtb':b'dtb',
               'liuqin_bootloader_panel.ko':b'module',
               'kernel.config':recipe.project_file(self.record['config']['path']).read_bytes(),
               'source-lock.json':self.lock.read_bytes(),
               'driver.patch':recipe.project_file(self.record['patches'][0]['path']).read_bytes()}
        for name, data in files.items(): (directory/name).write_bytes(data)
        manifest=dict(schema=2, device='liuqin', source=recipe.identity(self.lock),
                      files={name:recipe.sha(directory/name) for name in files},
                      source_files={self.record['config']['path']:self.record['config']['sha256']},
                      config_sha256=recipe.sha(directory/'kernel.config'),
                      dtb_sha256=recipe.sha(directory/'recovery.dtb'))
        (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
        return directory, manifest

    def test_patch_reconstructs_exact_tree_and_cache_needs_no_device_remote(self):
        source=self.assembled()
        self.assertEqual((source/'driver.c').read_text(), 'int maintenance = 1;\n')
        self.assertEqual(recipe.git(source,'rev-parse','HEAD^{tree}'), self.record['expected_source_tree'])
        self.assertNotEqual(recipe.git(source,'rev-parse','HEAD'), self.record['commit'])
        self.assertEqual(recipe.prepare_source(self.cache,self.root/'assembled',self.lock),source)

    def test_recipe_rejects_changed_patch_before_preparing_sources(self):
        recipe.project_file(self.record['patches'][0]['path']).write_text('changed\n')
        with self.assertRaisesRegex(ValueError,'input changed'):self.assembled()
        self.assertFalse((self.root/'assembled').exists())

    def test_recipe_rejects_changed_configuration(self):
        recipe.project_file(self.record['config']['path']).write_text('CONFIG_SND=y\n')
        with self.assertRaisesRegex(ValueError,'input changed'):recipe.load(self.lock)

    def test_shared_source_is_pinned_and_order_cannot_diverge(self):
        path=self.shared_lock()
        recipe.load(self.lock)
        path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'input changed'):recipe.load(self.lock)
        self.record['shared_kernel']=self.entry(path)
        self.record['commit']='1'*40;self.save_lock()
        with self.assertRaisesRegex(ValueError,'shared kernel'):recipe.load(self.lock)
        self.record['commit']=json.loads(path.read_text())['commit']
        self.record['patches']*=2
        # Use a distinct local patch before the shared sequence.
        extra=self.root/'build/recovery/kernel/extra.patch';extra.write_text('extra')
        self.record['patches'][0]=self.entry(extra);self.save_lock()
        with self.assertRaisesRegex(ValueError,'patch order'):recipe.load(self.lock)

    def test_touch_patch_only_changes_the_selected_build_copy(self):
        source=self.root/'device/recovery/touch';source.mkdir(parents=True)
        (source/'shim.c').write_text('prepared\n')
        unpatched=recipe.prepare_touch(self.root/'touch-old',self.lock)
        change=self.root/'build/recovery/kernel/touch.patch'
        change.write_text('--- a/shim.c\n+++ b/shim.c\n@@ -1 +1 @@\n-prepared\n+enabled\n')
        self.record['touch_patches']=[self.entry(change)];self.save_lock()
        patched=recipe.prepare_touch(self.root/'touch-new',self.lock)
        self.assertEqual((patched/'shim.c').read_text(),'enabled\n')
        self.assertEqual((source/'shim.c').read_text(),'prepared\n')
        self.assertEqual((unpatched/'shim.c').read_text(),'prepared\n')
        change.write_text('changed')
        with self.assertRaisesRegex(ValueError,'input changed'):recipe.load(self.lock)

    def test_bundle_requires_exact_shared_recipe_and_touch_patch(self):
        path=self.shared_lock()
        change=self.root/'build/recovery/kernel/touch.patch';change.write_text('touch patch')
        self.record['touch_patches']=[self.entry(change)];self.save_lock()
        directory,manifest=self.components()
        with self.assertRaisesRegex(ValueError,'incomplete'):recipe.verify_components(directory)
        for name,body in [('shared-source-lock.json',path.read_bytes()),('touch.patch',change.read_bytes())]:
            (directory/name).write_bytes(body);manifest['files'][name]=recipe.sha(directory/name)

        (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
        recipe.verify_components(directory)
        for name in ('shared-source-lock.json','touch.patch'):
            body=(directory/name).read_bytes();(directory/name).write_text('changed')
            manifest['files'][name]=recipe.sha(directory/name)
            (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'embedded'):recipe.verify_components(directory)
            (directory/name).write_bytes(body);manifest['files'][name]=recipe.sha(directory/name)

    def test_distribution_exports_shared_and_rec_sources_and_rejects_tampering(self):
        directory,manifest=self.components()
        extra={'shared-source-lock.json':b'{}','touch.patch':b'touch',
               'large-shared.patch':b'p'*(2*1024**2)}
        for name,body in extra.items():
            (directory/name).write_bytes(body);manifest['files'][name]=recipe.sha(directory/name)
        files={name:(directory/name).read_bytes() for name in manifest['files']}
        files.update({'kernel-manifest.json':json.dumps(manifest).encode(),'aligned-entry.S':b'entry'})
        prefix='usr/share/licenses/liuqin-recovery-kernel/'
        entries={prefix+name:([0,0o100644],body) for name,body in files.items()}
        exported=packager.kernel_build_records(entries,manifest)
        for name in ('driver.patch','touch.patch','large-shared.patch','source-lock.json',
                     'shared-source-lock.json','kernel.config','kernel-manifest.json','aligned-entry.S'):
            self.assertEqual(exported['installer/runtime/kernel-build/'+name],files[name])
        self.assertNotIn('installer/runtime/kernel-build/Image.gz',exported)
        entries[prefix+'touch.patch']=([0,0o100644],b'changed')
        with self.assertRaisesRegex(RuntimeError,'摘要'):packager.kernel_build_records(entries,manifest)
        del entries[prefix+'touch.patch']
        with self.assertRaisesRegex(RuntimeError,'缺失'):packager.kernel_build_records(entries,manifest)

    def test_device_fork_and_wrong_cache_remote_are_rejected(self):
        self.record['repo']='https://example.invalid/device/linux.git';self.save_lock()
        with self.assertRaisesRegex(ValueError,'source lock'):recipe.load(self.lock)
        self.record['repo']='https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git';self.save_lock()
        self.run_git('remote','set-url','origin','https://example.invalid/device/linux.git')
        with self.assertRaisesRegex(ValueError,'kernel.org'):self.assembled()

    def test_wrong_expected_tree_is_rejected(self):
        self.record['expected_source_tree']='1'*40;self.save_lock()
        with self.assertRaisesRegex(ValueError,'source tree'):self.assembled()

    def test_modified_or_untracked_source_is_rejected(self):
        source=self.assembled()
        (source/'unexpected').write_text('untracked\n')
        with self.assertRaisesRegex(ValueError,'clean'):recipe.verify_source(source,self.lock)
        (source/'unexpected').unlink();(source/'driver.c').write_text('changed\n')
        with self.assertRaisesRegex(ValueError,'clean'):recipe.verify_source(source,self.lock)

    def test_runtime_config_changes_are_rejected_but_disabled_tool_metadata_is_allowed(self):
        before='CONFIG_USB_GADGET=y\nCONFIG_RUSTC_VERSION=1\n'
        recipe.check_configuration(before, before.replace('VERSION=1','VERSION=0'))
        for after in (before.replace('GADGET=y','GADGET=m'),before+'CONFIG_SND=y\n',before+'CONFIG_RUST=y\n'):
            with self.assertRaisesRegex(ValueError,'capabilities'):recipe.check_configuration(before,after)

    def test_bundle_accepts_exact_inputs_and_rejects_modified_module(self):
        directory,manifest=self.components()
        self.assertEqual(recipe.verify_components(directory),manifest)
        (directory/'liuqin_bootloader_panel.ko').write_bytes(b'wrong module')
        with self.assertRaisesRegex(ValueError,'component changed'):recipe.verify_components(directory)

    def test_bundle_requires_corresponding_patch_and_exact_embedded_lock(self):
        directory,manifest=self.components()
        del manifest['files']['driver.patch']
        (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'incomplete'):recipe.verify_components(directory)
        manifest['files']['driver.patch']=recipe.sha(directory/'driver.patch')
        (directory/'source-lock.json').write_text('{}')
        manifest['files']['source-lock.json']=recipe.sha(directory/'source-lock.json')
        (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'embedded'):recipe.verify_components(directory)

    def test_bundle_rejects_parent_traversal_even_with_a_matching_digest(self):
        directory,manifest=self.components()
        manifest['files']['../outside']=recipe.sha(directory/'kernel.config')
        (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'component changed'):recipe.verify_components(directory)

    def test_bundle_rejects_source_identity_from_another_recipe(self):
        directory,manifest=self.components();manifest['source']['tree']='0'*40
        (directory/'kernel-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'recipe changed'):recipe.verify_components(directory)

    def test_same_tree_repackaged_patch_does_not_requalify_old_components(self):
        source=self.assembled();directory,_=self.components()
        before=recipe.identity(self.lock);recipe.verify_components(directory)
        original=recipe.project_file(self.record['patches'][0]['path'])
        combined=original.with_name('combined.patch');combined.write_bytes(original.read_bytes())
        self.record['patches']=[self.entry(combined)];self.save_lock()
        after=recipe.verify_source(source,self.lock)
        self.assertEqual(before['tree'],after['tree'])
        self.assertEqual(before['config_sha256'],after['config_sha256'])
        self.assertNotEqual(before['lock_sha256'],after['lock_sha256'])
        with self.assertRaisesRegex(ValueError,'recipe changed'):recipe.verify_components(directory)

    def test_new_base_requires_matching_source_and_kernel_components(self):
        identity=recipe.identity(self.lock)
        manifest=dict(identity=dict(recovery_source=identity))
        self.assertEqual(installer.check_source_base(manifest,'a'*64,True),identity)
        with self.assertRaises(ValueError):installer.check_source_base(manifest,'a'*64,False)
        with self.assertRaises(ValueError):installer.check_source_base({},'a'*64,True)
        components=dict(source=identity,raw_kernel_sha256='b'*64)
        installer.check_component_base(components,identity,'b'*64)
        with self.assertRaises(ValueError):installer.check_component_base(components,identity,'c'*64)
        components['source']=dict(identity,tree='0'*40)
        with self.assertRaises(ValueError):installer.check_component_base(components,identity,'b'*64)
        self.assertIsNone(installer.check_source_base({},installer.BASE_SHA,False))


class ProjectRecipeTests(unittest.TestCase):
    def test_current_recipe_pins_shared_patch_order_hashes_and_all_patch_files(self):
        record=recipe.load()
        shared=json.loads(recipe.project_file(record['shared_kernel']['path']).read_text())
        paths=shared['target_patches']
        self.assertEqual(len(paths),len(set(paths)))
        self.assertEqual(set(paths),{str(p.relative_to(PROJECT)) for p in
                                     (PROJECT/'build/kernel/patches/linux-7.3').glob('*.patch')})
        self.assertEqual([p['path'] for p in record['patches'][:len(paths)]],paths)


@unittest.skipUnless(os.environ.get('LIUQIN_RECOVERY_COMPONENTS'), 'set LIUQIN_RECOVERY_COMPONENTS for a real build')
class BuiltKernelTests(unittest.TestCase):
    def setUp(self):
        self.components=Path(os.environ['LIUQIN_RECOVERY_COMPONENTS'])

    def test_component_identity_and_corresponding_sources(self):
        recipe.verify_components(self.components)

    def test_maintenance_capabilities_are_builtin(self):
        config=(self.components/'kernel.config').read_text()
        for key in ('BLK_DEV_SD','SCSI_UFSHCD','SCSI_UFS_QCOM','USB_DWC3',
                    'USB_GADGET','USB_CONFIGFS_F_FS','USB_CONFIGFS_RNDIS',
                    'EXT4_FS','BTRFS_FS','DEVTMPFS','FB_SIMPLE','FRAMEBUFFER_CONSOLE',
                    'BACKLIGHT_KTZ8866','TOUCHSCREEN_NT36523_SPI','HID_NANOSIC',
                    'QCOM_Q6V5_PAS','QCOM_PMIC_GLINK','BATTERY_QCOM_BATTMGR'):
            self.assertIn('CONFIG_'+key+'=y\n',config,key)
        self.assertIn('# CONFIG_SND is not set\n',config)

    def test_dtb_retains_scanout_and_touch_with_fixed_usb_and_adsp_battery_transport(self):
        nodes,_=dtb_snapshot(self.components/'recovery.dtb')
        for node in ('/soc@0/display-subsystem@ae00000','/soc@0/gpu@3d00000',
                     '/soc@0/clock-controller@af00000','/sound','/soc@0/remoteproc@3000000/glink-edge/gpr'):
            self.assertEqual(nodes[node]['status'],b'disabled\0',node)
        self.assertEqual(nodes['/soc@0/remoteproc@3000000']['status'],b'okay\0')
        usb=('/soc@0/usb@a600000' if '/soc@0/usb@a600000' in nodes else
             '/soc@0/usb@a6f8800/usb@a600000')
        self.assertEqual(nodes[usb]['dr_mode'],b'peripheral\0')
        self.assertNotIn('usb-role-switch',nodes[usb]);self.assertNotIn(usb+'/ports',nodes)
        if usb == '/soc@0/usb@a600000':
            self.assertNotIn('role-switch-default-mode',nodes[usb])
        touch='/soc@0/geniqup@9c0000/spi@990000/touchscreen@0'
        self.assertNotIn('novatek,pen-support',nodes[touch])
        panel='/soc@0/display-subsystem@ae00000/dsi@ae94000/panel@0'
        self.assertIn(nodes[panel].get('status'),(None,b'okay\0'))
        backlight='/soc@0/geniqup@9c0000/i2c@984000/backlight@11'
        self.assertNotIn('kinetic,backlight-initially-off',nodes[backlight],
                         'Retained scanout has no real DRM panel to enable its backlight')
        self.assertIn('liuqin,recovery',nodes['/']);self.assertIn('liuqin,recovery',nodes['/pmic-glink'])
        args=nodes['/chosen']['bootargs']
        self.assertNotIn(b'hung_task_panic=',args);self.assertNotIn(b'softlockup_panic=',args)

    @unittest.skipUnless(os.environ.get('LIUQIN_RECOVERY_BASELINE_DTB'), 'set baseline DTB for migration equivalence')
    def test_every_dtb_property_and_memory_reservation_matches_the_qualified_baseline(self):
        self.assertEqual(dtb_snapshot(self.components/'recovery.dtb'),
                         dtb_snapshot(os.environ['LIUQIN_RECOVERY_BASELINE_DTB']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
