#!/usr/bin/env python3
"""Execute CAMSS format negotiation and check complete NV12 DMA allocations.

Compiles the actual __video_try_fmt body with minimal userspace context.
No device access. In particular, 4080x3060 NV12 must exceed 16 MiB safely.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', type=Path, required=True)
    args = parser.parse_args()
    source = (args.kernel / 'drivers/media/platform/qcom/camss/camss-video.c').read_text()
    body = 'static int __video_try_fmt(' + source.split('static int __video_try_fmt(', 1)[1].split('\nstatic int video_try_fmt(', 1)[0]
    wrapper = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <linux/videodev2.h>
typedef uint32_t u32;
#define clamp_t(type, v, lo, hi) ((type)(v) < (type)(lo) ? (type)(lo) : ((type)(v) > (type)(hi) ? (type)(hi) : (type)(v)))
#define roundup(v, a) (((v) + (a) - 1) / (a) * (a))
#define CAMSS_FRAME_MAX_WIDTH 8191
#define CAMSS_FRAME_MAX_HEIGHT_RDI 8191
#define CAMSS_FRAME_MAX_HEIGHT_PIX 4096
struct fraction { unsigned int numerator, denominator; };
struct camss_format_info {
 u32 pixelformat; uint8_t planes;
 struct fraction hsub[3], vsub[3]; unsigned int bpp[3];
};
struct camss_video {
 unsigned int bpl_alignment; bool line_based;
 int nformats; const struct camss_format_info *formats;
};
static unsigned int camss_format_get_bpl_alignment(const struct camss_format_info *f) { (void)f; return 16; }
''' + body + r'''
int main(void) {
 const struct camss_format_info formats[] = {
  {V4L2_PIX_FMT_NV12, 1, {{1,1}}, {{2,3}}, {8}},
  {V4L2_PIX_FMT_NV12M, 2, {{1,1},{1,1}}, {{1,1},{2,1}}, {8,8}},
  {V4L2_PIX_FMT_SRGGB10, 1, {{1,1}}, {{1,1}}, {16}},
 };
 struct camss_video video = {16, true, 3, formats};
 const unsigned int sizes[][2] = {{2592,1472},{2592,1952},{4080,3060},{4096,4096}};
 for (unsigned int i=0;i<4;i++) {
  for (unsigned int j=0;j<3;j++) {
   for (unsigned int padding=0;padding<=64;padding+=64) {
    struct v4l2_format f = {0};
    struct v4l2_pix_format_mplane *p = &f.fmt.pix_mp;
    p->width=sizes[i][0]; p->height=sizes[i][1]; p->pixelformat=formats[j].pixelformat;
    p->num_planes=formats[j].planes;
    for (unsigned int k=0;k<p->num_planes;k++) p->plane_fmt[k].bytesperline=p->width*(j==2?2:1)+padding;
    assert(__video_try_fmt(&video,&f)==0);
    unsigned int total=0;
    for (unsigned int k=0;k<p->num_planes;k++) {
     unsigned int lines=p->height;
     if (j==0) lines=lines*3/2;
     if (j==1 && k==1) lines/=2;
     assert(p->plane_fmt[k].sizeimage>=p->plane_fmt[k].bytesperline*lines);
     total+=p->plane_fmt[k].sizeimage;
    }
    if (!padding) assert(total==p->width*p->height*(j==2?4:3)/2);
   }
  }
 }
 return 0;
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        c, exe = Path(tmp) / 'format.c', Path(tmp) / 'format'
        c.write_text(wrapper)
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS: complete front/rear NV12, NV12M and RAW DMA allocations, including padded strides')


if __name__ == '__main__':
    main()
