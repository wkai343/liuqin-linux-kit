/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include <linux/reboot.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
int main(int argc,char **argv) {
    if (argc!=2 || geteuid()!=0 || access("/etc/liuqin-native-recovery.json",R_OK)) return 64;
    if (strcmp(argv[1],"normal") && strcmp(argv[1],"bootloader") && strcmp(argv[1],"poweroff")) return 64;
    sync();
    long ret=!strcmp(argv[1],"poweroff") ?
        syscall(SYS_reboot,LINUX_REBOOT_MAGIC1,LINUX_REBOOT_MAGIC2,LINUX_REBOOT_CMD_POWER_OFF,NULL) :
        syscall(SYS_reboot,LINUX_REBOOT_MAGIC1,LINUX_REBOOT_MAGIC2,LINUX_REBOOT_CMD_RESTART2,argv[1]);
    if(ret<0){perror("Recovery power action");return 1;}
    return 0;
}
