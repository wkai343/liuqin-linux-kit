/* SPDX-License-Identifier: MIT
 * Only this fixture maps temporary regular files to block identities.
 * All write ioctls update fixture flags, never a real disk.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/fs.h>
#include <linux/magic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/vfs.h>
#include <unistd.h>
static const char *fixture_dev, *fixture_sys, *fixture_state, *mutation;
static int glob_calls;
static int below(const char *p, const char *root) { return !strncmp(p,root,strlen(root)) && p[strlen(root)]=='/'; }
static void block_identity(const char *path, struct stat *s)
{
    if (!below(path,fixture_dev) || !S_ISREG(s->st_mode)) return;
    char file[4096]; unsigned int maj,min;
    snprintf(file,sizeof(file),"%s/class/block/%s/dev",fixture_sys,strrchr(path,'/')+1);
    FILE *f=fopen(file,"r"); assert(f && fscanf(f,"%u:%u",&maj,&min)==2);fclose(f);
    s->st_mode=S_IFBLK|0600;s->st_rdev=makedev(maj,min);
}
static int fixture_lstat(const char *p,struct stat *s) { int r=lstat(p,s);if(!r)block_identity(p,s);return r; }
static int fixture_fstat(int fd,struct stat *s)
{
    char link[64],path[4096];snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);
    ssize_t n=readlink(link,path,sizeof(path)-1);assert(n>0);path[n]=0;
    int r=fstat(fd,s);if(!r)block_identity(path,s);return r;
}
static int fixture_open(const char *p,int flags,...)
{
    assert((flags&O_ACCMODE)==O_RDONLY);
    if(below(p,fixture_dev))fprintf(stderr,"OPEN %s\n",strrchr(p,'/')+1);
    return open(p,flags);
}
static int fixture_ioctl(int fd,unsigned long request,void *arg)
{
    char link[64],path[4096],state[4096];snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);
    ssize_t n=readlink(link,path,sizeof(path)-1);assert(n>0);path[n]=0;
    assert(below(path,fixture_dev));const char *name=strrchr(path,'/')+1;
    snprintf(state,sizeof(state),"%s/%s",fixture_state,name);
    if(request==BLKROSET){
        assert(*(int*)arg==1);fprintf(stderr,"SET %s\n",name);
        FILE *f=fopen(state,"w");assert(f);fputs("1\n",f);fclose(f);return 0;
    }
    assert(request==BLKROGET);FILE *f=fopen(state,"r");assert(f&&fscanf(f,"%d",(int*)arg)==1);fclose(f);return 0;
}
static int fixture_stat(const char *p,struct stat *s)
{
    assert(!strcmp(p,"/bin/busybox")||!strcmp(p,"/proc/1/exe"));memset(s,0,sizeof(*s));s->st_mode=S_IFREG|0755;s->st_ino=42;return 0;
}
static int fixture_statfs(const char *p,struct statfs *s) { assert(!strcmp(p,"/"));memset(s,0,sizeof(*s));s->f_type=TMPFS_MAGIC;return 0; }
static int fixture_glob(const char *p,int flags,int(*err)(const char*,int),glob_t *g)
{
    if(++glob_calls==2 && strcmp(mutation,"none")){
        char a[4096],b[4096];snprintf(a,sizeof(a),"%s/.pending",fixture_dev);
        snprintf(b,sizeof(b),"%s/%s",fixture_dev,!strcmp(mutation,"usb")?"sdg1":"sda2");assert(!rename(a,b));
    }
    return glob(p,flags,err,g);
}
#define LIUQIN_STORAGE_DEVROOT fixture_dev
#define LIUQIN_STORAGE_SYSROOT fixture_sys
#define main storage_scope_main
int liuqin_storage_ready(int recovery) { (void)recovery;return 1; }
#define lstat fixture_lstat
#define fstat fixture_fstat
#define open fixture_open
#define ioctl fixture_ioctl
#define stat(p,s) fixture_stat(p,s)
#define statfs(p,s) fixture_statfs(p,s)
#define geteuid() 0
#define getppid() 1
#define glob fixture_glob
#include "../device/boot/liuqin-storage-seal.c"
#undef main
int main(int argc,char **argv)
{
    assert(argc==6||argc==8);fixture_dev=argv[1];fixture_sys=argv[2];fixture_state=argv[3];mutation=argv[5];
    if(argc==8){fixture_dev=argv[6];fixture_sys=argv[7];}
    char *args[]={argv[0],argv[4],argc==8?argv[6]:NULL,argc==8?argv[7]:NULL,NULL};
    return storage_scope_main(argc==8?4:2,args);
}
