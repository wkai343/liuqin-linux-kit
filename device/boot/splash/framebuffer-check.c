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
static uint32_t crc_byte(uint32_t crc,uint8_t v) {
    crc^=v;for(unsigned int i=0;i<8;i++)crc=(crc>>1)^((0U-(crc&1U))&0xedb88320U);return crc;
}
int main(int argc,char **argv) {
    (void)argv;
    if(argc!=1)return 2;
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
    uint32_t crc=~0U;unsigned int nonblack=0;
    for(unsigned int row=0;row<64;row++)for(unsigned int col=0;col<256;col++) {
        size_t y=(size_t)row*(HEIGHT-1)/63,x=(size_t)col*(WIDTH-1)/255;
        uint32_t pixel=pixels[(y*STRIDE)/4+x];
        if(pixel&0xffffffU)nonblack++;
        for(unsigned int n=0;n<4;n++)crc=crc_byte(crc,(uint8_t)(pixel>>(8*n)));
    }
    munmap((void *)pixels,ACTIVE_SIZE);close(fd);
    printf("crc32=%08x samples=16384 nonblack=%u\n",~crc,nonblack);
    return nonblack?0:9;
}
