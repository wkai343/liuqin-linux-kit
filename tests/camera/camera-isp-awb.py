#!/usr/bin/env python3
"""Exercise calibrated AWB selection with warm/daylight and shadow populations.

These deterministic scenes check selection and invalid-data handling; they do
not replace colour-chart or multi-illuminant capture validation.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--libcamera', type=Path, required=True)
    p.add_argument('--sanitize', action='store_true')
    a = p.parse_args()
    directory = a.libcamera.resolve() / 'src/libcamera/pipeline/simple'
    source = '\n'.join('#include ' + json.dumps(str(directory / n)) for n in
                       ('liuqin_colour.h', 'liuqin_awb.h')) + r'''
#include <cassert>
using namespace libcamera::liuqinColour;
Neutral sample(double k, double g, double spatial) {
 auto wb = gains(k, .59375, .5869140625);
 return {wb.first, wb.second, k, g, spatial};
}
void close(const Balance &b, const Neutral &n) {
 assert(b.selected && std::abs(b.red-n.red)<1e-9 && std::abs(b.blue-n.blue)<1e-9);
}
int main() {
 // Stable neutral surfaces under each calibrated illumination, independent
 // of position and brightness. Do not introduce a permanent warm correction.
 for (double k : {2856., 3500., 4000., 5000., 6500., 7500.})
  for (double g : {.04, .18, .60})
   for (double spatial : {1.,4.}) {
    Neutral n = sample(k,g,spatial);
    close(balance(std::vector<Neutral>(100,n)),n);
   }
 // An illuminated central target must not be overruled by many dim neutral
 // wall cells under a different light. Repeat with the light directions swapped.
 for (auto pair : {std::pair{4000.,6500.},std::pair{6500.,4000.}}) {
  auto target=sample(pair.first,.32,4);
  std::vector<Neutral> scene(500,sample(pair.second,.06,1));
  scene.insert(scene.end(),80,target);
  close(balance(scene),target);
  // Bright clipped window cells and zero/noise-level samples must not pull WB.
  scene.insert(scene.end(),1000,sample(7500.,.98,4));
  scene.insert(scene.end(),1000,sample(2856.,.005,4));
  scene.push_back({NAN,2,5000,.2,4});
  scene.push_back({2,INFINITY,5000,.2,4});
  close(balance(scene),target);
 }
 assert(!balance({}).selected);
 assert(!balance(std::vector<Neutral>(23,sample(5000,.2,4))).selected);
 assert(!balance(std::vector<Neutral>(768,sample(5000,.005,4))).selected);
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'awb.cpp', Path(tmp) / 'awb'
        c.write_text(source)
        subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                        *(['-fsanitize=address,undefined'] if a.sanitize else []),
                        str(c), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS: calibrated warm/daylight neutrals, mixed-light shadow rejection, clipping and dark-frame hold' +
          ('; ASan/UBSan' if a.sanitize else ''))


if __name__ == '__main__':
    main()
