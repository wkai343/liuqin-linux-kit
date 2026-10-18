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

struct node {
    int fd;
    struct stat identity;
};

int liuqin_storage_ready(void);

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

static int sysfs_matches(const char *path, dev_t device)
{
    char sysfs[256], value[64], newline, extra;
    unsigned int maj, min;
    ssize_t size;
    const char *name = strrchr(path, '/');
    int fd;

    if (!name || snprintf(sysfs, sizeof(sysfs), "/sys/class/block/%s/dev", name + 1) >= (int)sizeof(sysfs))
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

static int open_node(const char *path, struct node *node)
{
    struct stat opened;

    if (lstat(path, &node->identity) != 0 || !S_ISBLK(node->identity.st_mode))
        return reject(path, "not a canonical block node");
    node->fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (node->fd < 0)
        return reject(path, "cannot open block node");
    if (fstat(node->fd, &opened) != 0 || !same_node(&node->identity, &opened) ||
        sysfs_matches(path, opened.st_rdev) != 0) {
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
    int seal, failed = 0;

    if (argc == 2 && strcmp(argv[1], "--ready") == 0)
        return liuqin_storage_ready();
    if (argc != 2 || (strcmp(argv[1], "--seal") != 0 && strcmp(argv[1], "--check") != 0)) {
        fprintf(stderr, "usage: liuqin-storage-seal --seal|--check|--ready\n");
        return 2;
    }
    seal = strcmp(argv[1], "--seal") == 0;
    if (seal && ram_init_only() != 0)
        return 1;
    if (glob("/dev/sd*", GLOB_ERR, NULL, &before) != 0 || before.gl_pathc == 0) {
        globfree(&before);
        reject("/dev/sd*", "cannot enumerate a nonempty block namespace");
        return 1;
    }
    nodes = calloc(before.gl_pathc, sizeof(*nodes));
    if (!nodes) {
        globfree(&before);
        reject("/dev/sd*", "out of memory");
        return 1;
    }
    for (size_t i = 0; i < before.gl_pathc; ++i) {
        nodes[i].fd = -1;
        if (open_node(before.gl_pathv[i], &nodes[i]) != 0) {
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
        if (nodes[i].fd < 0)
            continue;
        if (ioctl(nodes[i].fd, BLKROGET, &readonly) != 0 || readonly != 1) {
            reject(before.gl_pathv[i], "read-only flag is unproven");
            failed = 1;
        }
        if (lstat(before.gl_pathv[i], &current) != 0 || !same_node(&nodes[i].identity, &current) ||
            sysfs_matches(before.gl_pathv[i], current.st_rdev) != 0) {
            reject(before.gl_pathv[i], "block identity changed during verification");
            failed = 1;
        }
        close(nodes[i].fd);
    }
    if (glob("/dev/sd*", GLOB_ERR, NULL, &after) != 0 || before.gl_pathc != after.gl_pathc) {
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
    if (!failed)
        printf("%zu\n", before.gl_pathc);
    free(nodes);
    globfree(&after);
    globfree(&before);
    return failed ? 1 : 0;
}
