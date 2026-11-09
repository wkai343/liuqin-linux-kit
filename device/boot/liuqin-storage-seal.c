/* SPDX-License-Identifier: MIT
 * Batch the initial initramfs BLKROSET pass without spawning per-node tools.
 * This does not admit a root: the independent BusyBox storage gate still does
 * the final seal, namespace snapshots, mount checks and rollback.
 */
#define _GNU_SOURCE
#include <errno.h>
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
#include "liuqin-storage-scope.h"
#ifndef LIUQIN_STORAGE_DEVROOT
#define LIUQIN_STORAGE_DEVROOT "/dev"
#define LIUQIN_STORAGE_SYSROOT "/sys"
#endif

struct node {
    int fd;
    struct stat identity;
    char sysfs[PATH_MAX];
};

int liuqin_storage_ready(int recovery);

static int reject(const char *path, const char *reason)
{
    fprintf(stderr, "liuqin-storage-seal: %s: %s\n", path, reason);
    return -1;
}

static int ram_init_only(void)
{
    struct statfs root;
    struct stat busybox, init;

    /* Never lock disks from a running desktop, chroot or service by mistake.
     * There is no environment-variable or command-line override for this.
     */
    if (geteuid() != 0 || getppid() != 1 || statfs("/", &root) != 0 ||
        (root.f_type != TMPFS_MAGIC && root.f_type != RAMFS_MAGIC) ||
        stat("/bin/busybox", &busybox) != 0 || !S_ISREG(busybox.st_mode) ||
        stat("/proc/1/exe", &init) != 0 ||
        init.st_dev != busybox.st_dev || init.st_ino != busybox.st_ino)
        return reject("--seal", "requires the BusyBox PID 1 RAM environment");
    return 0;
}

static int sysfs_matches(const char *path, dev_t device, const char *sysroot)
{
    char sysfs[256], value[64], newline, extra;
    unsigned int maj, min;
    ssize_t size;
    const char *name = strrchr(path, '/');
    int fd;

    if (!name || snprintf(sysfs, sizeof(sysfs), "%s/class/block/%s/dev", sysroot, name + 1) >= (int)sizeof(sysfs))
        return reject(path, "invalid sysfs path");
    fd = open(sysfs, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0)
        return reject(path, "cannot open sysfs identity");
    size = read(fd, value, sizeof(value) - 1);
    close(fd);
    if (size <= 0)
        return reject(path, "cannot read sysfs identity");
    value[size] = '\0';
    if (sscanf(value, "%u:%u%c%c", &maj, &min, &newline, &extra) != 3 || newline != '\n' ||
        maj != major(device) || min != minor(device))
        return reject(path, "sysfs device number mismatch");
    return 0;
}

static int same_node(const struct stat *a, const struct stat *b)
{
    return S_ISBLK(b->st_mode) && a->st_dev == b->st_dev &&
           a->st_ino == b->st_ino && a->st_rdev == b->st_rdev;
}

static int open_node(const char *path, struct node *node, const char *sysroot)
{
    struct stat opened;

    if (lstat(path, &node->identity) != 0 || !S_ISBLK(node->identity.st_mode))
        return reject(path, "not a canonical block node");
    node->fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (node->fd < 0)
        return reject(path, "cannot open block node");
    if (fstat(node->fd, &opened) != 0 || !same_node(&node->identity, &opened) ||
        sysfs_matches(path, opened.st_rdev, sysroot) != 0) {
        close(node->fd);
        node->fd = -1;
        return reject(path, "block identity changed or is unproven");
    }
    return 0;
}


int main(int argc, char **argv)
{
    glob_t before = {0}, after = {0};
    struct node *nodes;
    int seal, list, ufs_only, failed = 0;
    const char *devroot = LIUQIN_STORAGE_DEVROOT, *sysroot = LIUQIN_STORAGE_SYSROOT;
    char pattern[PATH_MAX];

    if (argc == 2 && strcmp(argv[1], "--ready") == 0)
        return liuqin_storage_ready(0);
    if (argc == 2 && strcmp(argv[1], "--ready-recovery") == 0)
        return liuqin_storage_ready(1);
    if (argc == 2 && strcmp(argv[1], "--ready-ufs") == 0)
        return liuqin_storage_ready(2);
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts("liuqin-storage-scope-v1"); return 0;
    }
    list = argc >= 2 && strcmp(argv[1], "--list-ufs") == 0;
    if (!((argc == 2 && (!strcmp(argv[1], "--seal") || !strcmp(argv[1], "--seal-ufs") ||
                        !strcmp(argv[1], "--check") || list)) || (argc == 4 && list))) {
        fprintf(stderr, "usage: liuqin-storage-seal --seal|--seal-ufs|--check|--list-ufs [devroot sysroot]|--ready|--ready-ufs|--ready-recovery|--version\n");
        return 2;
    }
    if (argc == 4) { devroot = argv[2]; sysroot = argv[3]; }
    ufs_only = list || strcmp(argv[1], "--seal-ufs") == 0;
    seal = !list && strcmp(argv[1], "--check") != 0;
    if (seal && ram_init_only() != 0)
        return 1;
    if (snprintf(pattern, sizeof(pattern), "%s/sd*", devroot) >= (int)sizeof(pattern) ||
        liuqin_storage_glob(pattern, &before, sysroot, ufs_only) != 0) {
        liuqin_storage_globfree(&before, ufs_only);
        reject("/dev/sd*", "cannot enumerate a nonempty block namespace");
        return 1;
    }
    nodes = calloc(before.gl_pathc, sizeof(*nodes));
    if (!nodes) {
        liuqin_storage_globfree(&before, ufs_only);
        reject("/dev/sd*", "out of memory");
        return 1;
    }
    for (size_t i = 0; i < before.gl_pathc; ++i) {
        nodes[i].fd = -1;
        if (list) {
            char real[PATH_MAX], sysfs[PATH_MAX];
            const char *path = before.gl_pathv[i], *name = strrchr(path, '/') + 1;
            if (lstat(path, &nodes[i].identity) || !S_ISBLK(nodes[i].identity.st_mode) ||
                !realpath(path, real) || strcmp(path, real) ||
                sysfs_matches(path, nodes[i].identity.st_rdev, sysroot) ||
                snprintf(sysfs, sizeof(sysfs), "%s/class/block/%s", sysroot, name) >= (int)sizeof(sysfs) ||
                !realpath(sysfs, nodes[i].sysfs)) {
                reject(path, "unproven UFS block identity"); failed = 1;
            }
            continue;
        }
        if (open_node(before.gl_pathv[i], &nodes[i], sysroot) != 0) {
            failed = 1;
            continue;
        }
        if (seal) {
            int readonly = 1;
            if (ioctl(nodes[i].fd, BLKROSET, &readonly) != 0) {
                reject(before.gl_pathv[i], "BLKROSET failed");
                failed = 1;
            }
        }
    }
    /* Finish setting every disk and partition before verifying any of them.
     * Retained descriptors plus a second namespace/identity pass reject paths
     * that disappear, get replaced, or are added during this operation.
     */
    for (size_t i = 0; i < before.gl_pathc; ++i) {
        int readonly = 0;
        struct stat current;
        if (!list && nodes[i].fd < 0)
            continue;
        if (!list && (ioctl(nodes[i].fd, BLKROGET, &readonly) != 0 || readonly != 1)) {
            reject(before.gl_pathv[i], "read-only flag is unproven");
            failed = 1;
        }
        if (lstat(before.gl_pathv[i], &current) != 0 || !same_node(&nodes[i].identity, &current) ||
            sysfs_matches(before.gl_pathv[i], current.st_rdev, sysroot) != 0) {
            reject(before.gl_pathv[i], "block identity changed during verification");
            failed = 1;
        }
        if (list) {
            struct liuqin_ufs_scope scope;
            char real[PATH_MAX];
            const char *name = strrchr(before.gl_pathv[i], '/') + 1;
            if (liuqin_ufs_scope_init(&scope, sysroot) ||
                liuqin_ufs_node(&scope, sysroot, name, real) != 1 || strcmp(real, nodes[i].sysfs)) {
                reject(before.gl_pathv[i], "UFS controller identity changed"); failed = 1;
            }
        } else close(nodes[i].fd);
    }
    if (liuqin_storage_glob(pattern, &after, sysroot, ufs_only) != 0 || before.gl_pathc != after.gl_pathc) {
        reject("/dev/sd*", "block namespace changed");
        failed = 1;
    } else {
        for (size_t i = 0; i < before.gl_pathc; ++i) {
            if (strcmp(before.gl_pathv[i], after.gl_pathv[i]) != 0) {
                reject("/dev/sd*", "block namespace changed");
                failed = 1;
                break;
            }
        }
    }
    if (!failed) {
        if (list) {
            for (size_t i = 0; i < before.gl_pathc; ++i)
                puts(strrchr(before.gl_pathv[i], '/') + 1);
        } else printf("%zu\n", before.gl_pathc);
    }
    free(nodes);
    liuqin_storage_globfree(&after, ufs_only);
    liuqin_storage_globfree(&before, ufs_only);
    return failed ? 1 : 0;
}
