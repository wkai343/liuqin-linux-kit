#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the shipped backlight C against the minimum-level blackout regression."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
PATCH = PROJECT / 'build/kernel/patches/0034-liuqin-minimum-brightness.patch'
DRIVER = Path('drivers/video/backlight/ktz8866.c')
HEADER = DRIVER.with_name('ktz8866-liuqin-curve.h')
CURVE_SHA256 = 'c24e55fd830fc5774abae7d572a939773eed3b1b706a7eb2b66b1a254a1224cf'


def new_side(path):
    lines = []
    in_hunk = False
    for line in path.read_text().splitlines():
        if line.startswith('@@ '):
            in_hunk = True
        elif line.startswith('diff --git'):
            in_hunk = False
        elif in_hunk and line[:1] in (' ', '+'):
            lines.append(line[1:])
    return '\n'.join(lines)


def function(source, name):
    match = re.search(r'^static (?:int|unsigned int)\s+' + name + r'\([^;{]*\)\s*\{', source, re.M)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


STUBS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <limits.h>
typedef uint16_t u16;
typedef uint8_t u8;
typedef uint32_t u32;
#define DEFAULT_BRIGHTNESS 1500
#define MAX_BRIGHTNESS 2047
#define BL_EN 8
#define BL_EN_BIT (1U << 6)
#define BL_BRT_LSB 4
#define BL_BRT_MSB 5
struct regmap {unsigned int values[0x16];};
struct ktz8866 {
    struct regmap *regmap;
    bool oem_curve, led_on, enable_state_valid;
    u8 active_sink_mask, blanked_sink_mask;
    u32 disable_delay_ms;
};
static struct ktz8866 *ktz_b;
struct backlight_device {struct ktz8866 *ktz; unsigned int level; bool off;};
static unsigned int writes;
static int regmap_write(struct regmap *map, unsigned int reg, unsigned int value) {
    assert(reg == BL_EN || reg == BL_BRT_LSB || reg == BL_BRT_MSB);
    map->values[reg] = value; writes++; return 0;
}
static void usleep_range(unsigned int lo, unsigned int hi) {assert(lo == 10000 && hi == 10010);}
static struct ktz8866 *bl_get_data(struct backlight_device *bl) {return bl->ktz;}
static unsigned int backlight_get_brightness(struct backlight_device *bl) {return bl->off ? 0 : bl->level;}
'''
CASES = r'''
static void check_update(struct backlight_device *bl, unsigned int expected) {
    assert(!ktz8866_backlight_update_status(bl));
    struct ktz8866 *ktz = bl->ktz;
    assert((ktz->regmap->values[4] | (ktz->regmap->values[5] << 3)) == expected);
    assert(ktz->regmap->values[8] == (expected ? 0x7fU : 0x1fU));
    assert(ktz->enable_state_valid && ktz->led_on == (expected > 0));
}
int main(int argc, char **argv) {
    (void)argv;
    struct regmap regs = {0};
    struct ktz8866 ktz = {.regmap = &regs, .oem_curve = true,
        .active_sink_mask = 63, .blanked_sink_mask = 31, .disable_delay_ms = 10};
    struct backlight_device bl = {.ktz = &ktz};
    if (argc > 1) {
        /* The unmodified shipped driver must reproduce the reported blackout. */
        bl.level = 1; assert(!ktz8866_map_brightness(&ktz, 1)); check_update(&bl, 0);
        return 0;
    }
    int previous = 0;
    for (unsigned int level = 0; level < 4096; level++) {
        unsigned int expected = level == 1 ? 17 : rom_expected[level];
        int mapped = ktz8866_map_brightness(&ktz, level);
        assert(mapped == (int)expected && mapped >= previous);
        assert((mapped > 0) == (level > 0)); previous = mapped;
        bl.level = level; check_update(&bl, expected);
    }
    assert(ktz8866_default_brightness(&ktz) == 510);
    assert(ktz8866_map_brightness(&ktz, 510) == 1500);
    assert(ktz8866_map_brightness(&ktz, 4095) == 2047);
    for (int cycle = 0; cycle < 16; cycle++) {
        bl.level = 1; check_update(&bl, 17);  /* Desktop slider minimum. */
        bl.level = 0; check_update(&bl, 0);   /* Explicit sysfs zero. */
        bl.level = 1; check_update(&bl, 17);
        bl.off = true; check_update(&bl, 0); /* Panel blank/suspend. */
        bl.off = false; check_update(&bl, 17);
    }
    unsigned int invalid[] = {4096, 4097, UINT_MAX};
    for (unsigned int i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        unsigned int before = writes; bl.level = invalid[i];
        assert(ktz8866_backlight_update_status(&bl) == -EINVAL && writes == before);
    }
    ktz.oem_curve = false;
    for (unsigned int level = 0; level < 2048; level++) {
        assert(ktz8866_map_brightness(&ktz, level) == (int)level);
        bl.level = level; check_update(&bl, level);
    }
    assert(ktz8866_default_brightness(&ktz) == 1500);
    bl.level = 2048; unsigned int before = writes;
    assert(ktz8866_backlight_update_status(&bl) == -EINVAL && writes == before);
    return 0;
}
'''


def compile_driver(source, header, output):
    table = [int(value) for value in re.findall(r'\d+', header.split('= {', 1)[1].split('};', 1)[0])]
    assert len(table) == 2048
    assert hashlib.sha256(struct.pack('<2048I', *table)).hexdigest() == CURVE_SHA256
    expected = [table[1700 * v // 2047 if v <= 2047 else 1700 + 347 * (v - 2048) // 2047] for v in range(4096)]
    functions = '\n'.join(function(source, name) for name in (
        'ktz8866_write', 'ktz8866_set_brightness', 'ktz8866_map_brightness',
        'ktz8866_default_brightness', 'ktz8866_backlight_update_status'))
    code = STUBS + header + '\n' + functions
    code += '\nstatic const unsigned int rom_expected[4096] = {' + ','.join(map(str, expected)) + '};\n' + CASES
    path = output.with_suffix('.c'); path.write_text(code)
    subprocess.run([CC, '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined',
                    '-fno-sanitize-recover=all', str(path), '-o', str(output)], check=True)


class MinimumBrightnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='liuqin-brightness-minimum-')
        cls.addClassCleanup(cls.work.cleanup)
        cls.before, cls.after = {}, {}
        for version, manifest, curve_patch, backlight_patch in (
            ('6.17', 'source-6.17.json', '0024-liuqin-oem-brightness-curve.patch', '0020-liuqin-oem-backlight-control.patch'),
            ('7.3', 'source.json', 'linux-7.3/0001-preserve-liuqin-adaptation.patch', 'linux-7.3/0001-preserve-liuqin-adaptation.patch'),
        ):
            config = json.loads((PROJECT / 'build/kernel' / manifest).read_text())
            assert str(PATCH.relative_to(PROJECT)) in config['target_patches']
            curve = new_side(PROJECT / 'build/kernel/patches' / curve_patch)
            backlight = new_side(PROJECT / 'build/kernel/patches' / backlight_patch)
            header = re.search(r'static const u16 liuqin_bl_curve\[2048\] = \{.*?\n\};', curve, re.S).group()
            # Only complete functions are needed; the fix is applied to the real mapping C.
            source = '\n\n'.join(function(backlight, name) for name in (
                'ktz8866_write', 'ktz8866_set_brightness'))
            source += '\n\n' + '\n\n'.join(function(curve, name) for name in (
                'ktz8866_map_brightness', 'ktz8866_default_brightness',
                'ktz8866_backlight_update_status')) + '\n\n'
            root = Path(cls.work.name) / version; path = root / DRIVER
            path.parent.mkdir(parents=True)
            path.write_text(source)
            cls.before[version] = root / 'before'
            compile_driver(source, header, cls.before[version])
            subprocess.run(['git', 'apply', '--check', str(PATCH)], cwd=root, check=True, capture_output=True)
            subprocess.run(['git', 'apply', str(PATCH)], cwd=root, check=True, capture_output=True)
            cls.after[version] = root / 'after'
            compile_driver(path.read_text(), header, cls.after[version])
        for i, source in enumerate(SOURCES):
            output = Path(cls.work.name) / f'assembled-{i}'
            compile_driver((source / DRIVER).read_text(), (source / HEADER).read_text(), output)
            cls.after[str(source)] = output

    def test_original_positive_minimum_reproduces_blackout(self):
        for version, binary in self.before.items():
            with self.subTest(version=version):
                subprocess.run([str(binary), '--expect-regression'], check=True, timeout=15)

    def test_fix_preserves_other_levels_zero_and_blank_resume(self):
        for version, binary in self.after.items():
            with self.subTest(version=version):
                subprocess.run([str(binary)], check=True, timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', default=shutil.which('cc') or 'gcc')
    parser.add_argument('--source', type=Path, action='append', default=[], help='Also execute an assembled kernel driver')
    args, remaining = parser.parse_known_args()
    CC, SOURCES = args.cc, args.source
    unittest.main(argv=[__file__, *remaining], verbosity=2)
