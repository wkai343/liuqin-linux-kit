/* SPDX-License-Identifier: MIT
 * Only the separately built fixture binary maps regular files to fake block
 * identities. Production has no fixture paths, environment flags or bypass.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static const char *fixture_dev;
static int fixture_sector;
static void fake_block(const char *path, struct stat *s)
{
    if (!S_ISREG(s->st_mode)) return;
    assert(strncmp(path, fixture_dev, strlen(fixture_dev)) == 0);
    const char *name = strrchr(path, '/') + 1;
    assert(strncmp(name, "sd", 2) == 0 && name[2] >= 'a' && name[2] <= 'z');
    unsigned int number = name[3] ? (unsigned int)strtoul(name + 3, NULL, 10) : 0;
    s->st_mode = S_IFBLK | 0600;
    s->st_rdev = makedev(240, (unsigned int)(name[2] - 'a') * 128 + number);
}
static int fixture_lstat(const char *path, struct stat *s)
{
    int result = lstat(path, s);
    if (!result) fake_block(path, s);
    return result;
}
static int fixture_fstat(int fd, struct stat *s)
{
    char link[64], path[4096];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path) - 1); assert(n > 0); path[n] = '\0';
    int result = fstat(fd, s);
    if (!result) fake_block(path, s);
    return result;
}
static int fixture_ioctl(int fd, unsigned long request, void *value)
{
    struct stat s;
    assert(fstat(fd, &s) == 0 && S_ISREG(s.st_mode));
    /* In particular, any write ioctl aborts this test binary. */
    if (request == BLKSSZGET) { *(int *)value = fixture_sector; return 0; }
    assert(request == BLKGETSIZE64);
    *(unsigned long long *)value = (unsigned long long)s.st_size;
    return 0;
}
#define lstat(path, s) fixture_lstat(path, s)
#define fstat fixture_fstat
#define ioctl fixture_ioctl
#include "../device/boot/liuqin-storage-ready.c"

int main(int argc, char **argv)
{
    assert(argc == 5);
    fixture_dev = argv[1]; fixture_sector = atoi(argv[4]);
    return ready_at(argv[1], argv[2], argv[3]);
}
