/* SPDX-License-Identifier: MIT
 * Read-only enumeration proof for the optional native-boot early exit.
 * Unsupported/missing/inconsistent metadata means keep the legacy wait.
 * Root admission and the independent final BusyBox seal are unchanged.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <limits.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static uint32_t le32(const unsigned char *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t le64(const unsigned char *p)
{
    return le32(p) | (uint64_t)le32(p + 4) << 32;
}
static uint32_t crc32_bytes(const unsigned char *p, size_t size)
{
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < size; ++i) {
        crc ^= p[i];
        for (unsigned int bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}
static int ready_error(const char *path)
{
    fprintf(stderr, "liuqin-storage-ready: incomplete or inconsistent: %s\n", path);
    return -1;
}
static int make_path(char *out, size_t size, const char *root, const char *name, const char *suffix)
{
    int n = snprintf(out, size, "%s/%s%s", root, name, suffix);
    return n < 0 || (size_t)n >= size ? -1 : 0;
}
static int read_number(const char *path, uint64_t *value)
{
    char data[64], *end;
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    ssize_t n = read(fd, data, sizeof(data) - 1);
    close(fd);
    if (n <= 0 || data[0] < '0' || data[0] > '9') return -1;
    data[n] = '\0'; errno = 0;
    unsigned long long result = strtoull(data, &end, 0);
    if (errno || strcmp(end, "\n") != 0) return -1;
    *value = result;
    return 0;
}
static int block_identity(const char *devroot, const char *sysroot, const char *name, struct stat *s)
{
    char path[PATH_MAX], value[64], newline, extra;
    unsigned int maj, min;
    if (make_path(path, sizeof(path), devroot, name, "") || lstat(path, s) || !S_ISBLK(s->st_mode))
        return -1;
    if (make_path(path, sizeof(path), sysroot, name, "/dev")) return -1;
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    ssize_t n = read(fd, value, sizeof(value) - 1);
    close(fd);
    if (n <= 0) return -1;
    value[n] = '\0';
    if (sscanf(value, "%u:%u%c%c", &maj, &min, &newline, &extra) != 3 || newline != '\n') return -1;
    return maj != major(s->st_rdev) || min != minor(s->st_rdev) ? -1 : 0;
}
static int same_identity(const struct stat *a, const struct stat *b)
{
    return S_ISBLK(b->st_mode) && a->st_dev == b->st_dev &&
           a->st_ino == b->st_ino && a->st_rdev == b->st_rdev;
}
static int read_at(int fd, void *buffer, size_t size, uint64_t offset)
{
    unsigned char *out = buffer;
    if (offset > INT64_MAX || size > (uint64_t)INT64_MAX - offset) return -1;
    size_t done = 0;
    while (done < size) {
        ssize_t n = pread(fd, out + done, size - done, (off_t)(offset + done));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        done += (size_t)n;
    }
    return 0;
}
static int header_ok(unsigned char *h, unsigned int sector, uint64_t current, uint64_t other)
{
    uint32_t length = le32(h + 12), stored = le32(h + 16);
    if (memcmp(h, "EFI PART", 8) || le32(h + 8) != 0x10000 || length < 92 || length > sector ||
        le32(h + 20) || le64(h + 24) != current || le64(h + 32) != other) return -1;
    memset(h + 16, 0, 4);
    uint32_t actual = crc32_bytes(h, length);
    h[16] = stored; h[17] = stored >> 8; h[18] = stored >> 16; h[19] = stored >> 24;
    return actual == stored ? 0 : -1;
}
static int partition_ok(const char *devroot, const char *sysroot, const char *disk,
                        unsigned int number, uint64_t start, uint64_t count, const char *parent)
{
    char name[64], path[PATH_MAX], real[PATH_MAX];
    uint64_t value;
    struct stat s;
    int n = snprintf(name, sizeof(name), "%s%u", disk, number);
    if (n < 0 || (size_t)n >= sizeof(name) || block_identity(devroot, sysroot, name, &s)) return -1;
    if (make_path(path, sizeof(path), sysroot, name, "") || !realpath(path, real) ||
        strncmp(real, parent, strlen(parent)) || real[strlen(parent)] != '/') return -1;
    if (make_path(path, sizeof(path), sysroot, name, "/partition") || read_number(path, &value) || value != number)
        return -1;
    if (make_path(path, sizeof(path), sysroot, name, "/start") || read_number(path, &value) || value != start)
        return -1;
    if (make_path(path, sizeof(path), sysroot, name, "/size") || read_number(path, &value) || value != count)
        return -1;
    return 0;
}
static int disk_gpt(const char *devroot, const char *sysroot, const char *name, size_t *partitions)
{
    char path[PATH_MAX], parent[PATH_MAX];
    unsigned char primary[4096], backup[4096], zero[16] = {0};
    unsigned char *entries = NULL, *mirror = NULL;
    struct stat before, opened, after;
    uint64_t bytes = 0, value;
    int sector = 0, fd = -1, result = -1;
    if (block_identity(devroot, sysroot, name, &before) ||
        make_path(path, sizeof(path), devroot, name, "")) goto out;
    fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0 || fstat(fd, &opened) || !same_identity(&before, &opened) ||
        ioctl(fd, BLKSSZGET, &sector) || ioctl(fd, BLKGETSIZE64, &bytes) ||
        sector < 512 || sector > 4096 || (sector & (sector - 1)) ||
        bytes > INT64_MAX || bytes % sector || bytes / sector < 6) goto out;
    if (make_path(path, sizeof(path), sysroot, name, "/size") || read_number(path, &value) || value != bytes / 512)
        goto out;
    uint64_t last = bytes / sector - 1;
    if (read_at(fd, primary, sector, sector) || read_at(fd, backup, sector, last * sector) ||
        header_ok(primary, sector, 1, last) || header_ok(backup, sector, last, 1)) goto out;
    uint64_t first = le64(primary + 40), end = le64(primary + 48);
    uint32_t count = le32(primary + 80), stride = le32(primary + 84);
    if (first < 3 || end < first || end >= last || !memcmp(primary + 56, zero, 16) ||
        !count || count > 4096 || stride < 128 || stride > 1024 || (stride & (stride - 1)) ||
        (uint64_t)count * stride > 4 * 1024 * 1024 ||
        memcmp(primary + 40, backup + 40, 32) || memcmp(primary + 80, backup + 80, 8)) goto out;
    size_t length = (size_t)count * stride;
    uint64_t table_sectors = (length + sector - 1) / sector;
    uint64_t table = le64(primary + 72), tail = le64(backup + 72);
    if (table < 2 || table >= first || table_sectors > first - table ||
        tail <= end || tail >= last || table_sectors > last - tail) goto out;
    entries = malloc(length); mirror = malloc(length);
    /* ABL/Fastboot can leave the backup header's entries CRC stale. As in
     * liuqin-mark-slot-successful, the valid primary is authoritative. Here
     * we do not repair anything: the backup bytes must still match exactly,
     * both headers must validate, and every kernel partition must match.
     */
    if (!entries || !mirror || read_at(fd, entries, length, table * sector) ||
        read_at(fd, mirror, length, tail * sector) || memcmp(entries, mirror, length) ||
        crc32_bytes(entries, length) != le32(primary + 88)) goto out;
    if (make_path(path, sizeof(path), sysroot, name, "") || !realpath(path, parent)) goto out;
    size_t used = 0;
    for (uint32_t i = 0; i < count; ++i) {
        unsigned char *e = entries + (size_t)i * stride;
        if (!memcmp(e, zero, 16)) continue;
        uint64_t begin = le64(e + 32), finish = le64(e + 40);
        if (begin < first || finish < begin || finish > end) goto out;
        for (uint32_t j = 0; j < i; ++j) {
            unsigned char *p = entries + (size_t)j * stride;
            if (memcmp(p, zero, 16) && begin <= le64(p + 40) && finish >= le64(p + 32)) goto out;
        }
        uint64_t scale = (unsigned int)sector / 512;
        if (partition_ok(devroot, sysroot, name, i + 1, begin * scale,
                         (finish - begin + 1) * scale, parent)) goto out;
        ++used;
    }
    if (!used || make_path(path, sizeof(path), devroot, name, "") || lstat(path, &after) ||
        !same_identity(&before, &after)) goto out;
    *partitions = used;
    result = 0;
out:
    free(entries); free(mirror);
    if (fd >= 0) close(fd);
    return result;
}

static int ready_at(const char *devroot, const char *sysroot, const char *controller)
{
    char path[PATH_MAX], pattern[PATH_MAX], real[PATH_MAX], controller_real[PATH_MAX];
    glob_t before = {0}, after = {0};
    uint64_t expected, value;
    unsigned char seen[128] = {0};
    size_t disks = 0, nodes = 0;
    int result = 1;
    if (!realpath(controller, controller_real) ||
        make_path(path, sizeof(path), controller, "device_descriptor", "/number_of_luns") ||
        read_number(path, &expected) || !expected || expected > sizeof(seen)) return 1;
    if (make_path(pattern, sizeof(pattern), devroot, "sd*", "") ||
        glob(pattern, GLOB_ERR, NULL, &before) || !before.gl_pathc) goto out;
    for (size_t i = 0; i < before.gl_pathc; ++i) {
        const char *name = strrchr(before.gl_pathv[i], '/') + 1;
        const char *p = name + 2;
        while (*p >= 'a' && *p <= 'z') ++p;
        if (*p) continue; /* partitions are checked against their GPT slot */
        if (p == name + 2 || make_path(path, sizeof(path), sysroot, name, "/device") ||
            !realpath(path, real) || strncmp(real, controller_real, strlen(controller_real)) ||
            real[strlen(controller_real)] != '/') goto out;
        unsigned int host, bus, target, lun;
        char extra;
        const char *address = strrchr(real, '/') + 1;
        if (sscanf(address, "%u:%u:%u:%u%c", &host, &bus, &target, &lun, &extra) != 4 ||
            lun >= sizeof(seen) || seen[lun]) goto out;
        seen[lun] = 1;
        if (make_path(path, sizeof(path), sysroot, name, "/device/unit_descriptor/lu_enable") ||
            read_number(path, &value) || !value) goto out;
        size_t partitions;
        if (disk_gpt(devroot, sysroot, name, &partitions)) goto out;
        ++disks; nodes += 1 + partitions;
    }
    if (make_path(path, sizeof(path), controller, "device_descriptor", "/number_of_luns") ||
        read_number(path, &value) || value != expected || disks != expected || nodes != before.gl_pathc ||
        glob(pattern, GLOB_ERR, NULL, &after) || before.gl_pathc != after.gl_pathc) goto out;
    for (size_t i = 0; i < before.gl_pathc; ++i)
        if (strcmp(before.gl_pathv[i], after.gl_pathv[i])) goto out;
    printf("%zu UFS LUNs; %zu block nodes\n", disks, nodes);
    result = 0;
out:
    if (result) ready_error("UFS LUN/GPT/sysfs enumeration");
    globfree(&before); globfree(&after);
    return result;
}

int liuqin_storage_ready(void)
{
    return ready_at("/dev", "/sys/class/block", "/sys/devices/platform/soc@0/1d84000.ufshc");
}
