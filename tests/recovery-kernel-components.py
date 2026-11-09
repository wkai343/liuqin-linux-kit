#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the ARM64 relocation entry and check Recovery DTB dependencies."""
import importlib.util
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
BASE_IMAGE = PROJECT/'out/releases/native-recovery/recovery.img'
def available(name):
    return bool(shutil.which(name) or (PROJECT/'.devbox/nix/profile/default/bin'/name).is_file())
spec = importlib.util.spec_from_file_location('recovery_kernel_builder',
    PROJECT/'build/recovery/prepare-native-kernel.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_PC, UC_ARM64_REG_PSTATE
    EMULATOR = True
except ImportError:
    EMULATOR = False


@unittest.skipUnless(EMULATOR and all(available('aarch64-unknown-linux-musl-'+name)
                                   for name in ('gcc','ld','objcopy')),
                     'Unicorn and the Recovery ARM64 toolchain are required')
class AlignedEntryTests(unittest.TestCase):
    def test_overlapping_relocation_preserves_kernel_and_boot_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = bytearray(bytes(range(256))*16384)
            struct.pack_into('<QQQ', data, 8, 0, len(data)+4096, 0xa)
            data[56:60] = b'ARM\x64'
            original = root/'Image'
            original.write_bytes(data)
            wrapped = builder.aligned_kernel(original, root/'AlignedImage')
            for base in (0x10000000, 0x10008000, 0x10080000, 0x101ff000):
                with self.subTest(load_address=hex(base)):
                    target = (base+4096+0x1fffff) & ~0x1fffff
                    machine = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
                    machine.mem_map(0x10000000, 16*1024**2)
                    machine.mem_write(base, wrapped)
                    machine.reg_write(UC_ARM64_REG_PSTATE, 0x3c5)
                    machine.reg_write(UC_ARM64_REG_X0, 0x400000)
                    for index in (1, 2, 3):
                        machine.reg_write(UC_ARM64_REG_X0+index, 0xdeadbeef)
                    machine.hook_add(UC_HOOK_CODE,
                        lambda emulator, address, size, state: emulator.emu_stop(),
                        begin=target, end=target)
                    machine.emu_start(base, 0x11000000, count=4000000)
                    self.assertEqual(machine.reg_read(UC_ARM64_REG_PC), target)
                    self.assertEqual(machine.reg_read(UC_ARM64_REG_X0), 0x400000)
                    for index in (1, 2, 3):
                        self.assertEqual(machine.reg_read(UC_ARM64_REG_X0+index), 0)
                    self.assertEqual(bytes(machine.mem_read(target,len(data))), data)
                    self.assertGreaterEqual(base+struct.unpack_from('<Q',wrapped,16)[0],
                                            target+len(data)+4096)


@unittest.skipUnless(BASE_IMAGE.is_file() and all(available(name) for name in ('fdtget','fdtput')),
                     'the qualified native base and device tree tools are required')
class DeviceTreeTests(unittest.TestCase):
    def test_recovery_retains_pmic_transport_and_selects_fixed_usb_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            dtb = Path(temporary)/'recovery.dtb'
            builder.recovery_dtb(BASE_IMAGE, dtb)
            import subprocess
            get = builder.tool('fdtget')
            def read(node, prop):
                return subprocess.check_output([get,'-t','s',str(dtb),node,prop],text=True).strip()
            self.assertEqual(read('/soc@0/remoteproc@3000000','status'), 'okay')
            self.assertEqual(read('/soc@0/remoteproc@3000000/glink-edge/gpr','status'), 'disabled')
            self.assertEqual(read('/sound','status'), 'disabled')
            self.assertEqual(read('/soc@0/usb@a6f8800/usb@a600000','dr_mode'), 'peripheral')
            self.assertEqual(read('/soc@0/display-subsystem@ae00000','status'), 'disabled')
            panel='/soc@0/display-subsystem@ae00000/dsi@ae94000/panel@0'
            props=subprocess.check_output([get,'-p',str(dtb),panel],text=True).splitlines()
            self.assertTrue('status' not in props or read(panel,'status')=='okay',
                            'the disabled display bus must retain the touch follower panel')
            # Do not prune the clock/power graph while relying on bootloader
            # scanout. GPU IOMMU, GMU and clock providers retain base status.
            base_blob=BASE_IMAGE.read_bytes()
            u=lambda n:struct.unpack_from('<I',base_blob,n)[0]
            align=lambda n:(n+4095)//4096*4096
            offset=4096+align(u(8))+align(u(16))
            base_dtb=Path(temporary)/'base.dtb';base_dtb.write_bytes(base_blob[offset:offset+u(1648)])
            for node in ('/soc@0/gmu@3d6a000','/soc@0/iommu@3da0000',
                         '/soc@0/clock-controller@3d90000','/soc@0/clock-controller@af00000',
                         '/soc@0/clock-controller@aaf0000','/soc@0/clock-controller@ade0000',
                         '/soc@0/remoteproc@32300000'):
                original=subprocess.run([get,'-t','s',str(base_dtb),node,'status'],capture_output=True)
                modified=subprocess.run([get,'-t','s',str(dtb),node,'status'],capture_output=True)
                self.assertEqual((modified.returncode,modified.stdout),
                                 (original.returncode,original.stdout),node)
            touch='/soc@0/geniqup@9c0000/spi@990000/touchscreen@0'
            props=subprocess.check_output([get,'-p',str(dtb),touch],text=True).splitlines()
            self.assertNotIn('novatek,pen-support',props)
            # ABL rewrites compatible to generic cape strings; its preserved
            # model and our Recovery marker keep the liuqin quirks scoped.
            self.assertEqual(read('/','model'), 'Xiaomi Pad 6 Pro')
            properties = subprocess.check_output([get,'-p',str(dtb),'/'],text=True).splitlines()
            self.assertIn('liuqin,recovery', properties)
            args = read('/chosen','bootargs')
            self.assertNotIn('hung_task_panic=', args)
            self.assertNotIn('softlockup_panic=', args)


class KernelQuirkTests(unittest.TestCase):
    def test_factory_gpt_exception_rejects_other_disks_and_mismatched_tables(self):
        patch = (PROJECT/'build/recovery/kernel-log-fixes.patch').read_text()
        match = re.search(r'^\+static bool liuqin_factory_backup\(.*?^\+}\n', patch, re.M | re.S)
        self.assertIsNotNone(match)
        # Compile the actual added kernel predicate with small host-side types.
        function = '\n'.join(line[1:] for line in match[0].splitlines())
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint64_t u64;
typedef uint8_t gpt_entry[128];
typedef struct {
    u64 my_lba, alternate_lba, first_usable_lba, last_usable_lba;
    uint32_t num_partition_entries, sizeof_partition_entry, partition_entry_array_crc32;
    uint8_t disk_guid[16];
} gpt_header;
struct disk { const char *disk_name; };
struct parsed_partitions { struct disk *disk; };
static bool compatible=true, recovery=true, abl_model=false;
static void *of_root;
#define le64_to_cpu(x) (x)
#define le32_to_cpu(x) (x)
#define efi_guidcmp(a,b) memcmp((a),(b),16)
static bool of_machine_is_compatible(const char *name) { return compatible; }
static int of_property_match_string(void *root,const char *key,const char *value) { return abl_model?0:-1; }
static bool of_property_read_bool(void *root,const char *name) { return recovery; }
'''+function+r'''
int main(void) {
    struct disk disk={"sdd"};
    struct parsed_partitions state={&disk};
    gpt_header primary={.my_lba=1,.alternate_lba=8191,.first_usable_lba=6,
        .last_usable_lba=8186,.num_partition_entries=4,.sizeof_partition_entry=128,
        .partition_entry_array_crc32=0x12345678};
    gpt_header backup=primary; backup.my_lba=8191;
    gpt_entry entries[4]={{1}}, mirror[4]; memcpy(mirror,entries,sizeof(entries));
#define ACCEPT() liuqin_factory_backup(&state,&primary,&backup,entries,mirror,8191)
    assert(ACCEPT()); disk.disk_name="sdf"; assert(ACCEPT());
    disk.disk_name="sda"; assert(!ACCEPT()); disk.disk_name="sdd";
    recovery=false; assert(!ACCEPT()); recovery=true;
    compatible=false; assert(!ACCEPT()); abl_model=true; assert(ACCEPT());
    abl_model=false; compatible=true;
    backup.alternate_lba=1; assert(!ACCEPT()); backup.alternate_lba=8191;
    backup.disk_guid[0]=1; assert(!ACCEPT()); backup.disk_guid[0]=0;
    backup.first_usable_lba++; assert(!ACCEPT()); backup.first_usable_lba--;
    backup.partition_entry_array_crc32++; assert(!ACCEPT()); backup.partition_entry_array_crc32--;
    mirror[3][100]=1; assert(!ACCEPT()); mirror[3][100]=0;
    assert(!liuqin_factory_backup(&state,&primary,&backup,entries,mirror,16383));
    assert(!liuqin_factory_backup(&state,&primary,NULL,entries,mirror,8191));
    assert(ACCEPT());
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/'gpt-fixture.c'; source.write_text(fixture)
            binary=root/'gpt-fixture'
            compiler=shutil.which('cc') or shutil.which('gcc')
            self.assertIsNotNone(compiler)
            subprocess.run([compiler,'-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                            str(source),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
