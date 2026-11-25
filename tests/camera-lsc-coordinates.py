#!/usr/bin/env python3
"""Execute the real LSC mesh sampler and catch signed edge-offset wraparound.

A smooth, bounded synthetic calibration must stay bounded after the small
outside-image extrapolation used by the front/rear hardware grids.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--libcamera', type=Path, required=True)
    a = p.parse_args()
    source = (a.libcamera / 'src/libcamera/pipeline/simple/liuqin_isp.cpp').read_text()
    sampler = source[source.index('float LiuqinIsp::shading('):].split('\n}\n', 1)[0] + '\n}\n'
    mesh = source[source.index('\t\tunsigned int h = info_.outputSize.height;'):].split('\t\tControlList ispControls', 1)[0]
    code = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cerrno>
#include <cmath>
#include <cstdio>
struct Size { unsigned width, height; Size(unsigned w,unsigned h):width(w),height(h){} bool operator!=(Size s)const{return width!=s.width||height!=s.height;} };
enum Transform { HFlip=1, VFlip=2 };
struct LiuqinIsp {
 bool rear_;int transform_;struct {Size outputSize{0,0};} info_;
 struct {std::array<float,884> lsc;float grgb=1;} calibration_;
 std::array<uint16_t,884> result;
 float shading(unsigned int,float,float) const;
 int make(){
''' + mesh + r'''
 result=mesh;return 0;
 }
};
''' + sampler + r'''
int main(){
 for(auto mode:{Size(4080,3060),Size(4080,2296),Size(3840,2160),Size(2592,1952),Size(2592,1472)})for(int flip=0;flip<4;flip++){
  LiuqinIsp isp;isp.rear_=mode.width>=3840;isp.transform_=flip;isp.info_.outputSize=mode;
  for(unsigned c=0;c<4;c++)for(unsigned y=0;y<13;y++)for(unsigned x=0;x<17;x++){
   float xx=(int(x)-8)/8.0f,yy=(int(y)-6)/6.0f;
   isp.calibration_.lsc[c*221+y*17+x]=1+0.4f*xx*xx+0.3f*yy*yy;
  }
  assert(isp.make()==0);
  if(isp.rear_ && mode.height!=3060){
   // OEM crop offsets in active-array pixels: (0,764) and (240,900).
   // Each grid coordinate describes a pair of binned Bayer pixels.
   const int left=mode.width==3840 ? 240 : 0;
   const int top=mode.height==2160 ? 900 : 764;
   for(int y=1;y*128-3<int(mode.height/2);y++)for(int x=1;x*128-4<int(mode.width/2);x++){
    float px=(4*(x*128-4)+left)/(4.f*2039);
    float py=(4*(y*128-3)+top)/(4.f*1529);
    assert(isp.result[y*17+x]==std::lround(1024*isp.shading(0,px,py)));
   }
   continue;
  }
  for(auto gain:isp.result)assert(gain>=1000 && gain<=1900);
  // The deliberately smooth symmetric calibration has no clipped edge row.
  for(unsigned x=0;x<17;x++)assert(std::abs(int(isp.result[x])-int(isp.result[12*17+x]))<60);
  assert(isp.result[6*17+8]<1040);
 }
 puts("PASS: signed top-edge sampling at five sensor geometries and all four flips; smooth mesh stays bounded");
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'mesh.cpp', Path(tmp) / 'mesh'
        c.write_text('#include <cstdint>\n' + code)
        subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
