#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check initramfs firmware identity and locale-independent ELF gates; no device access."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'build/lib'))
from usb_identity import environment
TOPOLOGY = 'usr/lib/firmware/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin'


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        script = self.root / 'build/lib/build-initramfs.sh'; script.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT / 'build/lib/build-initramfs.sh', script)
        shutil.copyfile(PROJECT / 'build/lib/usb_identity.py', script.parent / 'usb_identity.py')
        self.script = script
        self.manifest = self.root / 'device/firmware/firmware.manifest'
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_bytes((PROJECT / 'device/firmware/firmware.manifest').read_bytes())
        self.topology = self.root / 'topology.bin'
        self.topology.write_bytes((PROJECT / 'device/firmware' / TOPOLOGY).read_bytes())
        self.release = self.root / 'kernel.release'; self.release.write_text('fixture-liuqin-installer\n')

    def tearDown(self):
        self.temporary.cleanup()

    def run_gate(self, profile='installer', topology=None):
        env = dict(os.environ, ROOTFS='none', LIUQIN_ROOT_PROFILE=profile, LIUQIN_EMBED_ROOTFS='0',
                   LIUQIN_STORAGE_MODE='readonly' if profile=='installer' else 'persistent',
                   INSTALLER_RUNTIME=str(self.root/'runtime-fixture'), INSTALLER_KERNEL_RELEASE=str(self.release),
                   WLAN_HSP2_TUPLE=str(PROJECT/'device/firmware/usr/lib/firmware/updates/ath11k/WCN6855'),
                   AUDIO_TOPOLOGY=str(topology or self.topology), BUSYBOX=str(self.root/'absent-busybox'),
                   OUT_DIR=str(self.root/'output'))
        result = subprocess.run(['sh', str(self.script)], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'output').exists())
        return result.stderr

    def test_canonical_repository_topology_passes_both_input_roles(self):
        digest = hashlib.sha256(self.topology.read_bytes()).hexdigest()
        self.assertIn(digest+'  /'+TOPOLOGY, self.manifest.read_text())
        for profile in ('installer','native-updatable'):
            with self.subTest(profile=profile):
                # Stop deliberately at the next gate, without building an image.
                error = self.run_gate(profile)
                self.assertIn('static ARM64 BusyBox is unavailable', error)
                self.assertNotIn('topology identity mismatch', error)

    def test_modified_topology_is_rejected_before_later_inputs(self):
        self.topology.write_bytes(self.topology.read_bytes()+b'changed')
        for profile in ('installer','native-updatable'):
            with self.subTest(profile=profile):
                self.assertIn('topology identity mismatch', self.run_gate(profile))

    def test_topology_symlink_is_rejected(self):
        link = self.root / 'linked-topology'; link.symlink_to(self.topology)
        self.assertIn('topology identity mismatch', self.run_gate(topology=link))

    def test_missing_or_duplicate_manifest_identity_is_rejected(self):
        original = self.manifest.read_text()
        row = next(line for line in original.splitlines() if line.endswith('  /'+TOPOLOGY))
        for value in (original.replace(row+'\n',''), original+row+'\n'):
            with self.subTest(value=value[-80:]):
                self.manifest.write_text(value)
                self.assertIn('firmware manifest entry', self.run_gate())

    def test_obsolete_root_profiles_are_rejected(self):
        for profile in ('native', 'native-custom', 'gnome'):
            self.assertIn('unsupported LIUQIN_ROOT_PROFILE', self.run_gate(profile))


class LocaleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root/'build/lib/build-initramfs.sh';self.script.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT/'build/lib/build-initramfs.sh',self.script)
        shutil.copyfile(PROJECT/'build/lib/usb_identity.py',self.script.parent/'usb_identity.py')
        self.busybox = self.root/'busybox';self.busybox.write_text('synthetic ELF gate fixture\n');self.busybox.chmod(0o755)
        tools = self.root/'tools';tools.mkdir();self.tools=tools
        # Reproduce readelf's translated field name without requiring locale catalogs in CI.
        readelf = tools/'readelf'
        readelf.write_text('#!/bin/sh\n'
                          'case "$1" in\n'
                          ' -h) if [ "${LC_ALL:-}" = C ]; then label=Machine:; else label=系统架构:; fi\n'
                          '     printf "%s %s\\n" "$label" "${LIUQIN_TEST_MACHINE:-AArch64}" ;;\n'
                          ' -l) printf "%s\\n" "${LIUQIN_TEST_SEGMENT:-LOAD}" ;;\n'
                          ' *) exit 1 ;;\n'
                          'esac\n')
        readelf.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def run_gate(self, **overrides):
        env=dict(os.environ,LC_ALL='zh_CN.UTF-8',LANG='zh_CN.UTF-8',LANGUAGE='zh_CN',
                 PATH=str(self.tools)+os.pathsep+os.environ['PATH'],ROOTFS='none',LIUQIN_ROOT_PROFILE='installer',
                 LIUQIN_STORAGE_MODE='readonly',LIUQIN_EMBED_ROOTFS='0',BUSYBOX=str(self.busybox),
                 TOUCH_FIRMWARE_DIR=str(self.root/'absent-firmware'),OUT_DIR=str(self.root/'output'))
        release=self.root/'kernel.release'; release.write_text('fixture-liuqin-installer\n')
        env.update(INSTALLER_RUNTIME=str(self.root/'runtime'),INSTALLER_KERNEL_RELEASE=str(release),
                   WLAN_HSP2_TUPLE=str(PROJECT/'device/firmware/usr/lib/firmware/updates/ath11k/WCN6855'),
                   AUDIO_TOPOLOGY=str(PROJECT/'device/firmware'/TOPOLOGY))
        manifest=self.root/'device/firmware/firmware.manifest';manifest.parent.mkdir(parents=True,exist_ok=True)
        manifest.write_bytes((PROJECT/'device/firmware/firmware.manifest').read_bytes())
        env.update(overrides)
        result=subprocess.run(['sh',str(self.script)],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'output').exists())
        return result.stderr

    def test_chinese_environment_passes_valid_arm64_static_gate(self):
        error=self.run_gate()
        self.assertIn('touchscreen firmware does not match',error)
        self.assertNotIn('BusyBox is not an AArch64',error)

    def test_wrong_architecture_remains_rejected(self):
        self.assertIn('BusyBox is not an AArch64',self.run_gate(LIUQIN_TEST_MACHINE='X86-64'))

    def test_dynamic_executable_remains_rejected(self):
        self.assertIn('BusyBox is dynamically linked',self.run_gate(LIUQIN_TEST_SEGMENT='INTERP'))


class USBTests(unittest.TestCase):
    def test_packaged_names_are_applied_as_data_without_changing_usb_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);etc=root/'etc';etc.mkdir()
            product='小米 Pad "$HOME" $(touch sentinel) `touch sentinel` \\!'
            env=dict(os.environ,**environment({'usb_product':product,'usb_manufacturer':'我的设备'}))
            subprocess.run([sys.executable,PROJECT/'build/lib/usb_identity.py',
                            '--output-directory',etc],env=env,check=True)
            self.assertEqual((etc/'liuqin-usb-product').read_text(),product+'\n')
            self.assertEqual((etc/'liuqin-usb-product').stat().st_mode & 0o777,0o644)
            init=(PROJECT/'device/boot/initramfs/init').read_text()
            start=init.index('gadget=/sys/kernel/config/usb_gadget/liuqin\n')
            end=init.index('mkdir -p "$gadget/configs/c.1/strings/0x409"',start)
            script=init[start:end].replace('/sys/kernel/config/usb_gadget/liuqin',str(root/'gadget'))
            script=script.replace('/etc/liuqin-usb-',str(etc/'liuqin-usb-'))
            subprocess.run(['sh','-eu','-c',script],cwd=root,check=True)
            strings=root/'gadget/strings/0x409'
            self.assertEqual((strings/'product').read_text(),product)
            self.assertEqual((strings/'manufacturer').read_text(),'我的设备')
            self.assertFalse((root/'sentinel').exists())
            self.assertEqual((root/'gadget/idVendor').read_text(),'0x1d6b\n')
            self.assertEqual((root/'gadget/idProduct').read_text(),'0x1040\n')
            self.assertEqual((strings/'serialnumber').read_text(),'000000000001\n')

    def test_invalid_environment_stops_before_creating_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(os.environ,LIUQIN_USB_PRODUCT='invalid\nname')
            result=subprocess.run([sys.executable,PROJECT/'build/lib/usb_identity.py',
                                   '--output-directory',directory],env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('USB 名称配置错误',result.stderr)
            self.assertEqual(list(Path(directory).iterdir()),[])


if __name__ == '__main__':
    unittest.main(verbosity=2)
