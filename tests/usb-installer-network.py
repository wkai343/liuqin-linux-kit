#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run gadget setup against disposable configfs stand-ins, never USB hardware."""
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]

class Tests(unittest.TestCase):
    def test_persistent_handoff_precedes_usb_role_override_and_failed_roots_stay_sealed(self):
        init=(PROJECT/'device/boot/initramfs/init').read_text()
        start=init.index('# Admit a persistent system before any diagnostic gadget or role override.')
        end=init.index('# USB rescue is reached only for RAM images or a safely rejected root.',start)
        handoff=init[start:end]
        self.assertLess(end,init.index('gadget=/sys/kernel/config/usb_gadget/liuqin\n'))
        self.assertLess(init.index('native-adb-start.py >/run/liuqin-native-adb-start.log'),start)
        self.assertLess(init.index('exec /bin/liuqin-charger-mode'),start)
        self.assertEqual(init.count('if storage_prepare_persistent_root; then\n'),2)  # storage-only test entry + normal entry
        self.assertNotIn('start_stage1_telnet',handoff)
        handoff=handoff.replace('/bin/liuqin-mark-slot-successful','slot_tool')
        for mode,case,expected in [('persistent','success','prepare\nslot\nhandoff\n'),
                                   ('persistent','reject','prepare\nseal\nrescue:true\n'),
                                   ('persistent','slot-fail','prepare\nslot\nslot\nabort\nrescue:true\n'),
                                   ('persistent','handoff-fail','prepare\nslot\nhandoff\nseal\nrescue:true\n'),
                                   ('persistent','unsafe','prepare\nrescue:false\n'),
                                   ('readonly','success','rescue:true\n')]:
            with self.subTest(mode=mode,case=case):
                script='''
storage_mode=$1; scenario=$2; ufs_safe=true; storage_fallback_safe=true
BB=busybox
log() { :; }
storage_prepare_persistent_root() {
 printf 'prepare\\n'
 case $scenario in reject) return 1;; unsafe) storage_fallback_safe=false; return 1;; esac
}
slot_from_cmdline() { slot_suffix=a; return 0; }
slot_tool() { printf 'slot\\n'; [ "$scenario" != slot-fail ]; }
storage_abort() { printf 'abort\\n'; return 0; }
storage_lock_all() { printf 'seal\\n'; return 0; }
storage_switch_persistent_root() {
 printf 'handoff\\n'
 [ "$scenario" != handoff-fail ] || return 1
 exit 0
}
'''+handoff+'''printf 'rescue:%s\\n' "$ufs_safe"\n'''
                result=subprocess.run(['sh','-c',script,'test',mode,case],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout,expected)

    def test_installer_uses_rndis_os_descriptors_and_ram_rescue_retains_ncm(self):
        init=(PROJECT/'device/boot/initramfs/init').read_text()
        start=init.index('gadget=/sys/kernel/config/usb_gadget/liuqin\n')
        end=init.index('udc_name=\n',start)
        block=init[start:end]
        for installer in (False,True):
            with self.subTest(installer=installer),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);gadget=root/'gadget'
                (gadget/'functions').mkdir(parents=True);(gadget/'os_desc').mkdir()
                marker=root/'installer'
                if installer:marker.write_text('liuqin\n')
                script='''set -eu
log() { printf '%s\\n' "$*" >&2; }
mkdir() {
 command mkdir "$@" || return
 for path in "$@"; do
  case "$path" in */functions/rndis.usb0) command mkdir -p "$path/os_desc/interface.rndis" ;; esac
 done
}
storage_installer_marker="'''+str(marker)+'''"
'''+block.replace('/sys/kernel/config/usb_gadget/liuqin',str(gadget)).replace('/etc/liuqin-usb-',str(root/'absent-usb-'))
                result=subprocess.run(['sh','-c',script],capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                if installer:
                    self.assertEqual((gadget/'idProduct').read_text().strip(),'0x1041')
                    self.assertEqual((gadget/'bDeviceClass').read_text().strip(),'0xef')
                    self.assertEqual((gadget/'os_desc/use').read_text().strip(),'1')
                    self.assertEqual((gadget/'os_desc/qw_sign').read_text().strip(),'MSFT100')
                    self.assertEqual((gadget/'functions/rndis.usb0/os_desc/interface.rndis/compatible_id').read_text().strip(),'RNDIS')
                    self.assertEqual((gadget/'functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id').read_text().strip(),'5162001')
                    self.assertEqual((gadget/'os_desc/c.1').resolve(),(gadget/'configs/c.1').resolve())
                else:
                    self.assertEqual((gadget/'idProduct').read_text().strip(),'0x1040')
                    self.assertTrue((gadget/'functions/ncm.usb0').is_dir())
                    self.assertFalse((gadget/'os_desc/c.1').exists())
                name='rndis.usb0' if installer else 'ncm.usb0'
                self.assertEqual((gadget/'functions'/name/'host_addr').read_text().strip(),'02:00:00:00:07:01')

if __name__=='__main__':unittest.main(verbosity=2)
