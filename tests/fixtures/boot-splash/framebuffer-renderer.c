/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>
static const char *scenario;static unsigned int shares,mounts,execs;static jmp_buf done;
int launcher_main(int,char **);
uid_t __wrap_geteuid(void) {return !strcmp(scenario,"nonroot")?1000:0;}
int __wrap_unshare(int flags) {
    if(flags!=CLONE_NEWNS)abort();shares++;
    if(!strcmp(scenario,"unshare-error")){errno=EPERM;return -1;}return 0;
}
int __wrap_mount(const char *src,const char *dst,const char *type,unsigned long flags,const void *data) {
    if(shares!=1 || type || data)abort();
    if(mounts==0) {if(src || strcmp(dst,"/") || flags!=(MS_REC|MS_PRIVATE))abort();}
    else {
        const char *want=mounts<3?"/usr/lib64/plymouth/renderers":"/lib64/plymouth/renderers";
        if(strcmp(dst,want))abort();
        if(mounts%2) {if(!src || strcmp(src,"/usr/lib/liuqin-firmware-renderers") || flags!=MS_BIND)abort();}
        else if(src || flags!=(MS_BIND|MS_REMOUNT|MS_RDONLY|MS_NOSUID|MS_NODEV))abort();
    }
    mounts++;char bad[32];snprintf(bad,sizeof(bad),"mount-error-%u",mounts);
    if(!strcmp(scenario,bad)){errno=EPERM;return -1;}return 0;
}
int __wrap_setenv(const char *n,const char *v,int overwrite) {
    if(mounts!=5 || strcmp(n,"FRAMEBUFFER") || strcmp(v,"/dev/fb0") || overwrite!=1)abort();
    return !strcmp(scenario,"env-error")?-1:0;
}
int __wrap_unsetenv(const char *n) {if(strcmp(n,"DISPLAY"))abort();return 0;}
int __wrap_execv(const char *path,char *const args[]) {
    if(mounts!=5 || strcmp(path,"/usr/bin/plymouthd") || strcmp(args[0],path))abort();
    if(strcmp(args[1],"--no-daemon") || strcmp(args[2],"--mode=boot") || !strstr(args[9],"plymouth.ignore-udev") || args[10])abort();
    execs++;if(!strcmp(scenario,"exec-error")){errno=ENOENT;return -1;}longjmp(done,1);
}
int main(int argc,char **argv) {
    if(argc!=2)return 100;scenario=argv[1];volatile int result=0;
    if(!setjmp(done))result=launcher_main(!strcmp(scenario,"arguments")?2:1,argv);
    fprintf(stderr,"shares=%u mounts=%u execs=%u\n",shares,mounts,execs);return result;
}
