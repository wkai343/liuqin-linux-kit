/* SPDX-License-Identifier: MIT */
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
static const char *scenario;static unsigned int maps,gets;static void *memory;
int check_main(int,char **);
int __wrap_open(const char *p,int flags,...) {
    if(!strcmp(p,"/proc/device-tree/model") && flags==(O_RDONLY|O_CLOEXEC))return 10;
    if(!strcmp(p,"/proc/device-tree/compatible") && flags==(O_RDONLY|O_CLOEXEC))return 11;
    if(!strcmp(p,"/dev/fb0") && flags==(O_RDONLY|O_CLOEXEC|O_NOFOLLOW))return 12;
    abort();
}
ssize_t __wrap_read(int fd,void *out,size_t n) {
    const char *s=fd==10?"Liuqin based on Qualcomm Technologies, Inc SM8475":"qcom,cape-mtp";
    if(fd!=10 && fd!=11)abort();
    if(!strcmp(scenario,"wrong-board"))s="wrong";
    size_t len=strlen(s)+1;if(n<len)abort();memcpy(out,s,len);return len;
}
int __wrap_close(int fd) {if(fd<10 || fd>12)abort();return 0;}
int __wrap_lstat(const char *p,struct stat *s) {
    (void)s;if(strcmp(p,"/sys/module/ktz8866") && strcmp(p,"/sys/module/dispcc_sm8450"))abort();
    if(!strcmp(scenario,"owned"))return 0;
    errno=!strcmp(scenario,"unknown-owner")?EACCES:ENOENT;return -1;
}
int __wrap_fstat(int fd,struct stat *s) {
    if(fd!=12)abort();memset(s,0,sizeof(*s));s->st_mode=S_IFCHR;s->st_rdev=makedev(29,!strcmp(scenario,"wrong-device")?1:0);return 0;
}
int __wrap_ioctl(int fd,unsigned long request,...) {
    if(fd!=12)abort();va_list ap;va_start(ap,request);gets++;
    if(!strcmp(scenario,"ioctl-error")){va_end(ap);errno=EIO;return -1;}
    if(request==FBIOGET_FSCREENINFO) {
        struct fb_fix_screeninfo *p=va_arg(ap,struct fb_fix_screeninfo *);memset(p,0,sizeof(*p));
        memcpy(p->id,"simple",7);p->smem_start=0xb8000000;p->smem_len=0x2b00000;p->type=FB_TYPE_PACKED_PIXELS;p->visual=FB_VISUAL_TRUECOLOR;p->line_length=7200;
        if(!strcmp(scenario,"wrong-address"))p->smem_start++;
        if(!strcmp(scenario,"wrong-size"))p->smem_len--;
        if(!strcmp(scenario,"wrong-stride"))p->line_length--;
        if(!strcmp(scenario,"wrong-driver"))memcpy(p->id,"native",7);
    } else if(request==FBIOGET_VSCREENINFO) {
        struct fb_var_screeninfo *p=va_arg(ap,struct fb_var_screeninfo *);memset(p,0,sizeof(*p));
        p->xres=p->xres_virtual=1800;p->yres=p->yres_virtual=2880;p->bits_per_pixel=32;
        p->red.offset=16;p->green.offset=8;p->blue.offset=0;p->transp.offset=24;
        p->red.length=p->green.length=p->blue.length=p->transp.length=8;
        if(!strcmp(scenario,"wrong-width"))p->xres--;
        if(!strcmp(scenario,"wrong-format"))p->transp.offset=0;
        if(!strcmp(scenario,"offset"))p->yoffset=1;
    } else abort();
    va_end(ap);return 0;
}
void *__wrap_mmap(void *a,size_t n,int prot,int flags,int fd,off_t off) {
    if(a || n!=20736000 || prot!=PROT_READ || flags!=MAP_SHARED || fd!=12 || off)abort();maps++;
    if(!strcmp(scenario,"map-error")){errno=ENOMEM;return MAP_FAILED;}
    if(posix_memalign(&memory,4096,20738048))abort();memset(memory,!strcmp(scenario,"white")?255:0,20738048);
    if(!strcmp(scenario,"last-pixel"))((uint32_t *)memory)[20736000/4-1]=0xffffffff;
    if(mprotect(memory,20738048,PROT_READ))abort();return memory;
}
int __wrap_munmap(void *p,size_t n) {
    if(p!=memory || n!=20736000)abort();if(mprotect(memory,20738048,PROT_READ|PROT_WRITE))abort();free(memory);memory=NULL;return 0;
}
int main(int argc,char **argv) {
    if(argc!=2)return 100;scenario=argv[1];int ret=check_main(!strcmp(scenario,"arguments")?2:1,argv);
    if(memory)abort();fprintf(stderr,"maps=%u gets=%u\n",maps,gets);return ret;
}
