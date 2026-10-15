#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run gadget setup against disposable configfs stand-ins, never USB hardware."""
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]

class Tests(unittest.TestCase):
    def test_installer_uses_rndis_os_descriptors_and_desktop_retains_ncm(self):
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
