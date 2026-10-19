/* SPDX-License-Identifier: MIT */
/* Bounded, read-only summary of the fixed Liuqin boot framebuffer. */
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#define WIDTH 1800U
#define HEIGHT 2880U
#define STRIDE 7200U
#define ACTIVE_SIZE ((size_t)STRIDE * HEIGHT)
#ifndef LOGO_FIXTURE_TEST
static int dt_string(const char *path,const char *value) {
    char data[256];int fd=open(path,O_RDONLY|O_CLOEXEC);
    if(fd<0)return 0;
    ssize_t n=read(fd,data,sizeof(data));close(fd);
    if(n<=0 || n==(ssize_t)sizeof(data) || data[n-1])return 0;
    for(size_t off=0;off<(size_t)n;off+=strlen(data+off)+1)
        if(!strcmp(data+off,value))return 1;
    return 0;
}
static int absent(const char *path) {
    struct stat st;errno=0;return lstat(path,&st)<0 && errno==ENOENT;
}
#endif
#include "logo-points.h"
#define POINTS (sizeof(logo_points)/sizeof(logo_points[0]))
static int classify(const unsigned char values[]) {
    unsigned int score[4]={0},uncertain=0;
    for(unsigned int n=0;n<POINTS;n++) {
        unsigned int bright=values[n]>=128;
        if(values[n]>=32 && values[n]<=223)uncertain++;
        for(unsigned int k=0;k<4;k++)score[k]+=(bright!=((logo_points[n].mask>>k)&1U));
    }
    unsigned int best=0,second=~0U;
    for(unsigned int k=1;k<4;k++)if(score[k]<score[best])best=k;
    for(unsigned int k=0;k<4;k++)if(k!=best && score[k]<second)second=score[k];
    fprintf(stderr,"logo direction: samples=%zu score=%u,%u,%u,%u uncertain=%u\n",POINTS,score[0],score[1],score[2],score[3],uncertain);
    if(uncertain>8 || score[best]>8 || second<score[best]+40)return 12;
    printf("%u\n",logo_angles[best]);return 0;
}
#ifndef LOGO_FIXTURE_TEST
static int read_framebuffer(unsigned char values[]) {
    if(!dt_string("/proc/device-tree/model","Liuqin based on Qualcomm Technologies, Inc SM8475") ||
       !dt_string("/proc/device-tree/compatible","qcom,cape-mtp"))return 3;
    if(!absent("/sys/module/ktz8866") || !absent("/sys/module/dispcc_sm8450"))return 4;
    int fd=open("/dev/fb0",O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
    if(fd<0)return 5;
    struct stat st;struct fb_fix_screeninfo fix={0};struct fb_var_screeninfo var={0};
    if(fstat(fd,&st) || !S_ISCHR(st.st_mode) || major(st.st_rdev)!=29 || minor(st.st_rdev)!=0 ||
       ioctl(fd,FBIOGET_FSCREENINFO,&fix) || ioctl(fd,FBIOGET_VSCREENINFO,&var)) {close(fd);return 6;}
    if(memcmp(fix.id,"simple\0",7) || fix.smem_start!=0xb8000000UL || fix.smem_len!=0x2b00000 ||
       fix.type!=FB_TYPE_PACKED_PIXELS || fix.visual!=FB_VISUAL_TRUECOLOR || fix.line_length!=STRIDE ||
       var.xres!=WIDTH || var.yres!=HEIGHT || var.xres_virtual!=WIDTH || var.yres_virtual!=HEIGHT ||
       var.xoffset || var.yoffset || var.bits_per_pixel!=32 ||
       var.red.offset!=16 || var.red.length!=8 || var.green.offset!=8 || var.green.length!=8 ||
       var.blue.offset!=0 || var.blue.length!=8 || var.transp.offset!=24 || var.transp.length!=8 ||
       var.red.msb_right || var.green.msb_right || var.blue.msb_right || var.transp.msb_right) {close(fd);return 7;}
    const volatile uint32_t *pixels=mmap(NULL,ACTIVE_SIZE,PROT_READ,MAP_SHARED,fd,0);
    if(pixels==MAP_FAILED) {close(fd);return 8;}
    for(unsigned int n=0;n<POINTS;n++) {
        uint32_t v=pixels[(logo_points[n].y*STRIDE)/4+logo_points[n].x];
        unsigned int red=(v>>16)&255U,green=(v>>8)&255U,blue=v&255U;
        values[n]=(unsigned char)(red>green?(red>blue?red:blue):(green>blue?green:blue));
    }
    munmap((void *)pixels,ACTIVE_SIZE);close(fd);return 0;
}
#endif
int main(int argc,char **argv) {
    unsigned char values[POINTS];
#ifdef LOGO_FIXTURE_TEST
    if(argc!=2)return 2;
    FILE *f=fopen(argv[1],"rb");if(!f)return 5;
    size_t n=fread(values,1,POINTS,f);int extra=fgetc(f);fclose(f);
    if(n!=POINTS || extra!=EOF)return 6;
#else
    (void)argv;if(argc!=1)return 2;
    int rc=read_framebuffer(values);if(rc)return rc;
#endif
    return classify(values);
}
