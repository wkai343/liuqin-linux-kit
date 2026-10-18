/* SPDX-License-Identifier: MIT
 * Compile-time syscall substitution: no real device is opened or changed.
 */
#define _GNU_SOURCE
#include <assert.h>
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

static const char *scenario;
static const char *names[] = {"sda", "sda1", "sdb", "sdc"};
static int flags[4], stats[4], reads[4], glob_calls;
static int is(const char *value) { return strcmp(scenario, value) == 0; }
static int index_of(const char *name)
{
    for (int i = 0; i < 4; ++i)
        if (strcmp(name, names[i]) == 0) return i;
    abort();
}
static void identity(int i, struct stat *s)
{
    memset(s, 0, sizeof(*s));
    s->st_mode = S_IFBLK | 0600; s->st_dev = 4; s->st_ino = 1000 + i;
    s->st_rdev = makedev(8, i == 2 ? 16 : i);
}
static int fake_glob(const char *pattern, int options,
                     int (*errfunc)(const char *, int), glob_t *g)
{
    assert(strcmp(pattern, "/dev/sd*") == 0 && options == GLOB_ERR && !errfunc);
    ++glob_calls;
    if (is("empty")) return GLOB_NOMATCH;
    if (is("glob-error")) return GLOB_ABORTED;
    int count = glob_calls == 2 && is("added") ? 4 :
                glob_calls == 2 && is("removed") ? 2 : 3;
    g->gl_pathc = count;
    g->gl_pathv = calloc(count + 1, sizeof(char *));
    for (int i = 0; i < count; ++i) {
        const char *name = glob_calls == 2 && i == 2 && is("renamed") ? "sdc" : names[i];
        assert(asprintf(&g->gl_pathv[i], "/dev/%s", name) > 0);
    }
    return 0;
}
static void fake_globfree(glob_t *g)
{
    for (size_t i = 0; i < g->gl_pathc; ++i) free(g->gl_pathv[i]);
    free(g->gl_pathv);
}
static int fake_lstat(const char *path, struct stat *s)
{
    int i = index_of(path + 5); ++stats[i]; identity(i, s);
    if (i == 1 && is("symlink")) s->st_mode = S_IFLNK | 0777;
    if (i == 1 && is("regular")) s->st_mode = S_IFREG | 0600;
    if (i == 1 && stats[i] > 1 && is("replaced")) s->st_ino++;
    if (i == 1 && stats[i] > 1 && is("disappeared")) return -1;
    return 0;
}
static int fake_open(const char *path, int options, ...)
{
    assert((options & O_ACCMODE) == O_RDONLY);
    assert((options & (O_CLOEXEC | O_NOFOLLOW)) == (O_CLOEXEC | O_NOFOLLOW));
    if (strncmp(path, "/dev/", 5) == 0) {
        assert(options & O_NONBLOCK);
        int i = index_of(path + 5);
        fprintf(stderr, "OPEN %s\n", names[i]);
        return i == 1 && is("open-error") ? -1 : 100 + i;
    }
    char name[32]; assert(sscanf(path, "/sys/class/block/%31[^/]/dev", name) == 1);
    int i = index_of(name);
    return i == 1 && is("sysfs-missing") ? -1 : 200 + i;
}
static int fake_fstat(int fd, struct stat *s)
{
    int i = fd - 100; assert(i >= 0 && i < 4); identity(i, s);
    if (i == 1 && is("replace-open")) s->st_ino++;
    return 0;
}
static ssize_t fake_read(int fd, void *buffer, size_t size)
{
    int i = fd - 200; assert(i >= 0 && i < 4); ++reads[i];
    if (i == 1 && is("read-error")) return -1;
    const char *bad = i == 1 && is("sysfs-extra") ? "8:1\njunk" :
                      i == 1 && is("sysfs-truncated") ? "8:" : NULL;
    if (bad) { assert(strlen(bad) < size); memcpy(buffer, bad, strlen(bad)); return strlen(bad); }
    unsigned int min = i == 2 ? 16 : (unsigned int)i;
    if (i == 1 && (is("sysfs-mismatch") || (reads[i] > 1 && is("sysfs-changed")))) min = 99;
    return snprintf(buffer, size, "8:%u\n", min);
}
static int fake_close(int fd) { assert(fd >= 100 && fd < 204); return 0; }
static int fake_ioctl(int fd, unsigned long request, void *arg)
{
    int i = fd - 100; assert(i >= 0 && i < 4);
    if (request == BLKROSET) {
        assert(*(int *)arg == 1);
        fprintf(stderr, "SET %s\n", names[i]);
        if (i == 1 && is("set-error")) return -1;
        flags[i] = 1; return 0;
    }
    assert(request == BLKROGET);
    fprintf(stderr, "GET %s\n", names[i]);
    if (i == 1 && is("get-error")) return -1;
    *(int *)arg = i == 1 && is("still-writable") ? 0 : flags[i];
    return 0;
}
static uid_t fake_geteuid(void) { return is("not-root") ? 1000 : 0; }
static pid_t fake_getppid(void) { return is("not-init-child") ? 123 : 1; }
static int fake_statfs(const char *path, struct statfs *s)
{
    assert(strcmp(path, "/") == 0);
    memset(s, 0, sizeof(*s)); s->f_type = is("desktop-root") ? 0x9123683e : TMPFS_MAGIC;
    return 0;
}
static int fake_stat(const char *path, struct stat *s)
{
    assert(strcmp(path, "/bin/busybox") == 0 || strcmp(path, "/proc/1/exe") == 0);
    memset(s, 0, sizeof(*s)); s->st_mode = S_IFREG | 0755; s->st_ino = 42;
    if (is("wrong-init") && strcmp(path, "/proc/1/exe") == 0) ++s->st_ino;
    return 0;
}

#define main storage_seal_main
int liuqin_storage_ready(void) { return 1; }
#define glob fake_glob
#define globfree fake_globfree
#define lstat(path, out) fake_lstat(path, out)
#define stat(path, out) fake_stat(path, out)
#define fstat fake_fstat
#define statfs(path, out) fake_statfs(path, out)
#define open fake_open
#define read fake_read
#define close fake_close
#define ioctl fake_ioctl
#define geteuid fake_geteuid
#define getppid fake_getppid
#include "../device/boot/liuqin-storage-seal.c"
#undef main

int main(int argc, char **argv)
{
    assert(argc >= 3); scenario = argv[1];
    if (strcmp(argv[2], "--check") == 0 && !is("check-writable"))
        for (int i = 0; i < 4; ++i) flags[i] = 1;
    return storage_seal_main(argc - 1, argv + 1);
}
