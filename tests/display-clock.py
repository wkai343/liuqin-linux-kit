#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the shipped PLL patch against hardware arithmetic and CCF constraints."""
from pathlib import Path
import argparse
import ctypes
import re
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


def new_side(patch):
    lines = []
    in_hunk = False
    for line in patch.read_text().splitlines():
        if line.startswith('@@ '):
            in_hunk = True
        elif line.startswith('diff --git'):
            in_hunk = False
        elif in_hunk and line[:1] in (' ', '+'):
            lines.append(line[1:])
    return '\n'.join(lines)


def round_function(source):
    start = source.index('static long dsi_pll_7nm_clk_round_rate(')
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


STUBS = r'''
#include <assert.h>
#include <stdint.h>
#include <limits.h>
typedef uint64_t u64;
#define VCO_REF_CLK_RATE 19200000
#define FRAC_BITS 18
#define DIV_ROUND_CLOSEST_ULL(x,y) (((x)+(y)/2)/(y))
#define DIV_ROUND_UP_ULL(x,y) (((x)+(y)-1)/(y))
#define clamp(x,l,h) ((x)<(l)?(l):((x)>(h)?(h):(x)))
static u64 div64_u64(u64 x,u64 y){return x/y;}
struct cfg {unsigned long min_pll_rate,max_pll_rate;};
struct phy {struct cfg *cfg;};
struct clk_hw {struct phy *phy;};
#define dsi_pll_7nm clk_hw
#define to_pll_7nm(x) (x)
static struct cfg config={600000000UL,5000000000UL};
static struct phy phy={&config};
static struct clk_hw pll={&phy};
'''
WRAPPER = r'''
unsigned long tested_round(unsigned long rate) {
    return dsi_pll_7nm_clk_round_rate(&pll,rate,0);
}
static unsigned long hardware_readback(unsigned long rate) {
    u64 pll_freq=rate, multiplier=1ULL<<FRAC_BITS, divider=38400000;
    u64 code=SET_EXPRESSION;
    u64 decimal=code>>FRAC_BITS, fraction=code&((1U<<FRAC_BITS)-1);
    assert(decimal<=255);
    return decimal*38400000+fraction*38400000/(1U<<FRAC_BITS);
}
static void check(unsigned long input) {
    unsigned long rate=tested_round(input);
    assert(rate>=config.min_pll_rate && rate<=config.max_pll_rate);
    assert(hardware_readback(rate)==rate);
    assert(tested_round(rate)==rate);
    unsigned long bound=clamp(input,config.min_pll_rate,config.max_pll_rate);
    if(bound>=tested_round(config.min_pll_rate))assert(rate<=bound);
    assert((rate>bound?rate-bound:bound-rate)<=147);
    for(int i=0;i<4;i++)assert(hardware_readback(rate)==rate);
}
int main(void) {
    unsigned long ranges[][2]={{600000000UL,5000000000UL},
        {1000000000UL,3500000000UL},{600000000UL,4294967295UL}};
    uint64_t seed=0x5f6b3cea;
    for(int j=0;j<3;j++) {
        config.min_pll_rate=ranges[j][0];config.max_pll_rate=ranges[j][1];
        unsigned long edges[]={0,1,ULONG_MAX,config.min_pll_rate-1,
            config.min_pll_rate,config.min_pll_rate+1,config.max_pll_rate-1,
            config.max_pll_rate,config.max_pll_rate+1,1007032320UL,2014064640UL};
        for(unsigned i=0;i<sizeof(edges)/sizeof(edges[0]);i++)check(edges[i]);
        for(int i=0;i<1000000;i++) {
            seed=seed*6364136223846793005ULL+1;
            check(seed%6000000001ULL);
        }
    }
    return 0;
}
'''


class DisplayClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = new_side(PATCH)
        expression = re.search(r'^\s*dec_multiple = (.+);$', source, re.M)[1]
        code = STUBS + round_function(source) + WRAPPER.replace('SET_EXPRESSION', expression)
        cls.work = tempfile.TemporaryDirectory()
        root = Path(cls.work.name)
        path = root/'clock.c'; path.write_text(code)
        compiler = shutil.which('cc') or shutil.which('gcc')
        if not compiler: raise RuntimeError('C compiler is required')
        flags = [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter']
        cls.binary = root/'clock'
        subprocess.run([*flags, str(path), '-o', str(cls.binary)], check=True)
        library = root/'clock.so'
        subprocess.run([*flags, '-shared', '-fPIC', str(path), '-o', str(library)], check=True)
        cls.library = ctypes.CDLL(str(library))
        cls.library.tested_round.argtypes = [ctypes.c_ulong]
        cls.library.tested_round.restype = ctypes.c_ulong

    @classmethod
    def tearDownClass(cls): cls.work.cleanup()

    def test_roundtrip_bounds_and_three_million_rates(self):
        subprocess.run([str(self.binary)], check=True)

    @staticmethod
    def divider(request, dividers, parent_round, current_parent):
        # clk_divider_bestdiv() selects the highest rate <= the request.
        best = None
        for divisor in dividers:
            if request*divisor == current_parent:
                return request
            rate = (parent_round(request*divisor)+divisor-1)//divisor
            if rate <= request and (best is None or rate > best): best = rate
        return best if best is not None else (parent_round(1)+max(dividers)-1)//max(dividers)

    @classmethod
    def byte_clock(cls, parent_round, current):
        def output(rate): return cls.divider(rate, (1,2,4,8), parent_round, current)
        bit = cls.divider(125879040*8, range(1,16), output, current)
        return bit

    @staticmethod
    def pixel_clock(bit):
        # The PHY pixel divider does not propagate rates to its parent.
        for numerator, denominator in [(3,8),(2,9),(4,9),(1,1),(2,3)]:
            requested = 167838720*denominator//numerator
            divisor = max(1, min(15, (bit+requested-1)//requested))
            parent = (bit+divisor-1)//divisor
            if requested-100000 <= parent <= requested+100000:
                return parent*numerator//denominator
        return None

    def test_existing_120hz_mode_with_different_initial_pll_rates(self):
        for initial in [600000000,1000000000,1007031738,1007032320,2014064640,4999999951]:
            with self.subTest(initial=initial):
                bit = self.byte_clock(self.library.tested_round, initial)
                pixel = self.pixel_clock(bit)
                self.assertIsNotNone(pixel)
                self.assertLess(abs(pixel-167838720), 30)
                self.assertLess(abs(bit//8-125879040), 30)

    def test_nearest_rate_prototype_cannot_select_pixel_clock(self):
        def nearest(rate):
            rate = max(600000000, min(5000000000, rate))
            coefficient = (rate*(1<<18)+19200000)//38400000
            coefficient = max((600000000*(1<<18)+38399999)//38400000,
                              min(5000000000*(1<<18)//38400000, coefficient))
            return coefficient*38400000//(1<<18)
        self.assertIsNone(self.pixel_clock(self.byte_clock(nearest, 1000000000)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--patch', type=Path, default=PROJECT/'build/kernel/patches/0008-dsi7nm-clock-precision.patch')
    args, remaining = parser.parse_known_args()
    PATCH = args.patch
    unittest.main(argv=[__file__, *remaining])
