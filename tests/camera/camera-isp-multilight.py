#!/usr/bin/env python3
"""Check front/rear illuminant data and the compiled hardware-ISP colour estimator.

Requires a host C++ compiler and the user's extracted shipping vendor image.
Does not operate a camera. Output captures and light-box validation remain needed.
"""
import argparse
import json
from pathlib import Path
import runpy
import struct
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--libcamera', type=Path, required=True)
    p.add_argument('--vendor', type=Path, required=True)
    p.add_argument('--sensor', choices=('imx596', 's5kjn1'), default='imx596')
    a = p.parse_args()
    decode = runpy.run_path(str(Path(__file__).with_name('camera-oem.py')))['decode']
    rear = a.sensor == 's5kjn1'
    filename, digest, table = (
        ('com.qti.tuned.liuqin_qtech_s5kjn1_wide.bin',
         'ab4c7b74b4f4d88c65b2c545994e4c90314be3fee20b2b05042ca7e9114147ab', 2053)
        if rear else ('com.qti.tuned.liuqin_aac_imx596_front.bin',
         'd7f013bef1c515e57f8c8d6d12488e0dc8018794792223d8ace2bf6e83b5bd08', 1574))
    rows = decode(a.vendor / 'lib64/camera' / filename, digest)
    header = a.libcamera / 'src/libcamera/pipeline/simple' / (
        'liuqin_rear_colour.h' if rear else 'liuqin_colour.h')
    source = '#include ' + json.dumps(str(header.resolve())) + r'''
#include <iostream>
#include <iomanip>
#include <cassert>
int main() {
 using namespace libcamera;
 const auto &matrices = COLOUR_NAMESPACE::matrices;
 const auto &whites = COLOUR_NAMESPACE::whites;
 auto matrix = [&](double t) { return liuqinColour::matrix(t, matrices); };
 auto estimate = [&](double r, double b, double ur, double ub) { return liuqinColour::estimate(r, b, ur, ub, whites); };
 auto gains = [&](double t, double ur, double ub) { return liuqinColour::gains(t, ur, ub, whites); };
 std::cout << std::setprecision(10);
 for (const auto &m : matrices) {
  std::cout << "M " << m.start << ' ' << m.end;
  for (float v : m.matrix) std::cout << ' ' << v;
  std::cout << '\n';
 }
 for (const auto &w : whites)
  std::cout << "W " << w.kelvin << ' ' << w.rg << ' ' << w.bg << '\n';
 // Unit calibration cancels multiplicatively, including another module's
 // simulated response. All daylight/warm-light segments must round-trip.
 for (auto unit : {std::pair{.59375, .586914}, std::pair{.53, .64}}) {
  for (unsigned i = 0; i + 1 < std::size(whites); i++) {
   for (int k = 0; k <= 100; k++) {
    double t = k / 100.0;
    double rg = std::exp((1-t)*std::log(whites[i].rg)+t*std::log(whites[i+1].rg));
    double bg = std::exp((1-t)*std::log(whites[i].bg)+t*std::log(whites[i+1].bg));
    auto e = estimate(rg/whites[2].rg*unit.first, bg/whites[2].bg*unit.second, unit.first, unit.second);
    double kelvin = 1 / ((1-t)/whites[i].kelvin+t/whites[i+1].kelvin);
    auto g = gains(kelvin, unit.first, unit.second);
    assert(std::abs(g.first - std::clamp(whites[2].rg / (rg * unit.first), 1.0, 16.0)) < 1e-9);
    assert(std::abs(g.second - std::clamp(whites[2].bg / (bg * unit.second), 1.0, 16.0)) < 1e-9);
    assert(e.distance < 1e-9 && std::abs(e.kelvin-kelvin) < .001);
   }
  }
  assert(estimate(unit.first, unit.second, unit.first, unit.second).kelvin == 5000);
  assert(estimate(unit.first*2, unit.second*2, unit.first, unit.second).distance > .10);
  assert(estimate(unit.first*.2, unit.second*.2, unit.first, unit.second).distance > .10);
 }
 for (int ct = 1900; ct < 9000; ct++) {
  auto m = matrix(ct), next = matrix(ct+1);
  for (unsigned i=0;i<9;i++) {
   assert(m[i] >= MIN_COEFF && m[i] <= MAX_COEFF);
   assert(std::abs(m[i]-next[i]) < .003);
  }
  for (unsigned i=0;i<9;i+=3) assert(std::abs(m[i]+m[i+1]+m[i+2]-1) < .00001);
 }
 assert(!std::isfinite(estimate(0,1,1,1).distance));
 assert(!std::isfinite(estimate(NAN,1,1,1).distance));
 assert(!std::isfinite(estimate(INFINITY,1,1,1).distance));
 assert(matrix(NAN)==matrix(5000));
}
'''
    source = source.replace('COLOUR_NAMESPACE', 'liuqinRearColour' if rear else 'liuqinColour')
    source = source.replace('MIN_COEFF', '-4' if rear else '-2').replace('MAX_COEFF', '4095.0/1024' if rear else '2047.0/1024')
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'colour.cpp', Path(tmp) / 'colour'
        c.write_text(source)
        subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(c), '-o', str(exe)], check=True)
        output = subprocess.check_output([str(exe)], text=True).splitlines()
    assert len(output) == 10
    for i, line in enumerate(output[:5]):
        actual = list(map(float, line.split()[1:]))
        low, high, *refs = struct.unpack_from('<ff4I', rows[table][1], i*24)
        coeffs = struct.unpack('<9f3i', rows[refs[-1]][1])
        assert coeffs[9:] == (0, 0, 0)
        assert all(abs(x-y) < 1e-6 for x, y in zip(actual, (low, high, *coeffs[:9])))
    for index, line in zip((7, 6, 3, 2, 1), output[5:]):
        _, rg, bg = map(float, line.split()[1:])
        expected = struct.unpack_from('<2f', rows[197][1], 24 + index*8)
        assert all(abs(x-y) < 1e-6 for x, y in zip((rg, bg), expected))
    print(a.sensor + ' PASS: five OEM CCM intervals, five white references, 808 calibrated locus cases, continuous bounded matrices')


if __name__ == '__main__':
    main()
