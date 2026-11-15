#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the USB PHY tables and compare every write with the pinned OEM DTBO."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path)
args, rest = parser.parse_known_args()
FIXTURE = Path(__file__).with_name('fixtures') / 'usb-phy-liuqin-oem.json'


@unittest.skipUnless(args.source, '--source enables actual USB PHY table execution')
class Tests(unittest.TestCase):
    def test_complete_oem_register_image_for_both_lanes(self):
        phy = args.source / 'drivers/phy/qualcomm'
        source = (phy / 'phy-qcom-qmp-combo.c').read_text()
        tables = ('sm8475_usb3_serdes', 'sm8475_usb3_tx', 'sm8475_usb3_rx',
                  'sm8475_usb3_pcs', 'sm8350_usb3_pcs_usb')
        headers = ('qserdes-com-v5', 'qserdes-com-v6', 'qserdes-txrx-v5',
                   'qserdes-txrx-v5_5nm', 'pcs-v4', 'pcs-usb-v5')
        expected = json.loads(FIXTURE.read_text())['registers']
        prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
#define __iomem
#define dev_dbg(...) ((void)0)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
struct device {int unused;};
static unsigned char registers[0x2000];
static unsigned int values[0x800], writes[0x800];
static void writel(unsigned int value, void *address) {
 size_t offset=(unsigned char *)address-registers;
 assert(offset<sizeof(registers) && !(offset%4));
 values[offset/4]=value; writes[offset/4]++;
}
'''
        code = prefix + '\n'.join('#include '+json.dumps(str((phy/('phy-qcom-qmp-'+h+'.h')).resolve())) for h in headers)
        code += '\n'+(phy/'phy-qcom-qmp-common.h').read_text()
        for name in tables:
            code += '\n'+re.search(r'static const struct qmp_phy_init_tbl '+name+r'_tbl\[\] = \{.*?\n\};', source, re.S).group()
        code += '\nint main(void) {\n'
        for name, offset, lane in ((tables[0],0x1000,255), (tables[1],0x1200,1),
                                  (tables[1],0x1600,2), (tables[2],0x1400,1),
                                  (tables[2],0x1800,2), (tables[3],0x1c00,255),
                                  (tables[4],0x1f00,255)):
            code += f'qmp_configure_lane(NULL,registers+{offset},{name}_tbl,ARRAY_SIZE({name}_tbl),{lane});\n'
        for offset in range(0, 0x2000, 4):
            value = expected.get(f'{offset:#06x}')
            if value is None:
                code += f'assert(writes[{offset//4}]==0);\n'
            else:
                code += f'if(writes[{offset//4}]!=1 || values[{offset//4}]!={value}) {{fprintf(stderr,"OEM mismatch at 0x{offset:x}: writes=%u value=0x%x expected=0x{value:x}\\n",writes[{offset//4}],values[{offset//4}]);return 1;}}\n'
        code += 'return 0;\n}\n'
        with tempfile.TemporaryDirectory(prefix='liuqin-usb-phy-') as work:
            c = Path(work)/'test.c'; binary=Path(work)/'test'; c.write_text(code)
            subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror',
                            '-Wno-unused-parameter','-Wno-unused-function',str(c),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True,timeout=10)


if __name__ == '__main__':
    unittest.main(argv=[__file__, *rest])
