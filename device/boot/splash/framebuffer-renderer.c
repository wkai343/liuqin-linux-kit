/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mount.h>
#include <unistd.h>

/* Give only this direct child a read-only FB renderer view. Never move or
 * replace the native renderer files in the parent's initramfs namespace. */
int main(int argc,char **argv) {
    (void)argv;
    if(argc!=1 || geteuid()!=0)return 2;
    if(unshare(CLONE_NEWNS)) {perror("firmware renderer unshare");return 3;}
    if(mount(NULL,"/",NULL,MS_REC|MS_PRIVATE,NULL)) {
        perror("firmware renderer private propagation");return 4;
    }
    const char *target[]={"/usr/lib64/plymouth/renderers","/lib64/plymouth/renderers"};
    for(unsigned int i=0;i<2;i++) {
        if(mount("/usr/lib/liuqin-firmware-renderers",target[i],NULL,MS_BIND,NULL) ||
           mount(NULL,target[i],NULL,MS_BIND|MS_REMOUNT|MS_RDONLY|MS_NOSUID|MS_NODEV,NULL)) {
            perror("firmware renderer private read-only view");return 4;
        }
    }
    if(setenv("FRAMEBUFFER","/dev/fb0",1) || unsetenv("DISPLAY"))return 5;
    char *const command[]={
        "/usr/bin/plymouthd","--no-daemon","--mode=boot",
        "--pid-file=/run/plymouth/pid","--tty=/dev/tty1",
        "--ignore-serial-consoles","--graphical-boot","--no-boot-log","--attach-to-session",
        "--kernel-command-line=rhgb quiet plymouth.ignore-udev plymouth.ignore-serial-consoles",
        NULL
    };
    puts("firmware renderer: private read-only FB-only view ready; native renderer files unchanged");
    fflush(stdout);
    execv(command[0],command);
    perror("firmware renderer exec");return 6;
}
