// SPDX-License-Identifier: MIT
/* Check and mark the running liuqin A/B slot successful in the sde GPT, fail closed.
 *
 * Qualcomm keeps the A/B state in bits 48-55 of the GPT entry attributes of
 * each slot-suffixed partition:
 *
 *   b48-b49 priority   b50 active   b51-b53 retry count
 *   b54 successful     b55 unbootable
 *
 * This tool only reads those bits and marks the slot it is running from
 * successful (--mark/--check).  It must never select a slot.  Verified on the
 * device on 2026-09-25: rewriting only the boot_a/boot_b attribute bytes left
 * the tablet unable to boot.  Switching slots on this ABL means swapping the
 * partition type GUIDs of every _a/_b pair and moving the UFS boot LUN, which
 * only the bootloader's own `fastboot --set-active` does.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define SECTOR_SIZE 4096ULL
#define DISK_BYTES 2923429888ULL
#define ENTRY_COUNT 96U
#define ENTRY_SIZE 128U
#define HEADER_SIZE 92U
#define ENTRIES_SECTORS ((ENTRY_COUNT * ENTRY_SIZE + SECTOR_SIZE - 1) / SECTOR_SIZE)
#define FIRST_USABLE 6ULL
#define BOOT_A_FIRST 75014ULL
#define BOOT_A_LAST 124165ULL
#define BOOT_B_FIRST 344107ULL
#define BOOT_B_LAST 393258ULL
#define ATTR_PRIORITY_MASK (3ULL << 48)
#define ATTR_ACTIVE (1ULL << 50)
#define ATTR_TRIES_MASK (7ULL << 51)
#define ATTR_SUCCESSFUL (1ULL << 54)
#define ATTR_UNBOOTABLE (1ULL << 55)

enum op { OP_NONE, OP_MARK, OP_CHECK };

static uint32_t le32(const unsigned char *p)
{
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
	       (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t le64(const unsigned char *p)
{
	return (uint64_t)le32(p) | (uint64_t)le32(p + 4) << 32;
}

static void put_le32(unsigned char *p, uint32_t v)
{
	p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24;
}

static void put_le64(unsigned char *p, uint64_t v)
{
	put_le32(p, (uint32_t)v);
	put_le32(p + 4, (uint32_t)(v >> 32));
}

static uint32_t crc32_bytes(const unsigned char *buf, size_t len)
{
	uint32_t crc = ~0U;
	size_t i;
	int bit;

	for (i = 0; i < len; i++) {
		crc ^= buf[i];
		for (bit = 0; bit < 8; bit++)
			crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
	}
	return ~crc;
}

static int full_pread(int fd, void *buf, size_t len, off_t off)
{
	unsigned char *p = buf;
	while (len) {
		ssize_t n = pread(fd, p, len, off);
		if (n <= 0)
			return -1;
		p += n; off += n; len -= (size_t)n;
	}
	return 0;
}

static int full_pwrite(int fd, const void *buf, size_t len, off_t off)
{
	const unsigned char *p = buf;
	while (len) {
		ssize_t n = pwrite(fd, p, len, off);
		if (n <= 0)
			return -1;
		p += n; off += n; len -= (size_t)n;
	}
	return 0;
}

static int name_is(const unsigned char *entry, const char *name)
{
	size_t i, len = strlen(name);
	if (len > 35)
		return 0;
	for (i = 0; i < len; i++)
		if (entry[56 + i * 2] != (unsigned char)name[i] || entry[57 + i * 2])
			return 0;
	return entry[56 + len * 2] == 0 && entry[57 + len * 2] == 0;
}

struct gpt_copy {
	unsigned char header[SECTOR_SIZE];
	unsigned char entries[ENTRY_COUNT * ENTRY_SIZE];
	uint64_t header_lba;
	uint64_t entries_lba;
};

/* The entry array location is read from each header and validated rather
 * than assumed.  The stock Xiaomi ABL writes the backup array at
 * disk_lbas - 7 (LastUsableLBA = disk_lbas - 8), while a generic GPT tool puts
 * it at disk_lbas - 5; both are legal.  What must hold is that the primary
 * array sits between the primary header and FirstUsableLBA, the backup array
 * between LastUsableLBA and the backup header, and that neither copy overlaps
 * the usable area or the boot_a/boot_b ranges pinned below.  Writes go to the
 * entries_lba that each header declares. */
static int load_gpt_copy(int fd, uint64_t disk_lbas, uint64_t header_lba,
			 struct gpt_copy *g, int allow_stale_entries_crc)
{
	uint32_t saved_header_crc, saved_entries_crc;
	uint64_t first_usable, last_usable;
	unsigned char check[SECTOR_SIZE];

	memset(g, 0, sizeof(*g));
	g->header_lba = header_lba;
	if (disk_lbas < 2 * (1 + ENTRIES_SECTORS) + FIRST_USABLE ||
	    (header_lba != 1 && header_lba != disk_lbas - 1))
		return -1;
	if (full_pread(fd, g->header, sizeof(g->header), header_lba * SECTOR_SIZE))
		return -1;
	first_usable = le64(g->header + 40);
	last_usable = le64(g->header + 48);
	if (memcmp(g->header, "EFI PART", 8) || le32(g->header + 12) != HEADER_SIZE ||
	    le64(g->header + 24) != header_lba ||
	    le64(g->header + 32) != (header_lba == 1 ? disk_lbas - 1 : 1) ||
	    first_usable != FIRST_USABLE ||
	    last_usable < BOOT_B_LAST ||
	    last_usable + ENTRIES_SECTORS + 1 >= disk_lbas ||
	    le32(g->header + 80) != ENTRY_COUNT || le32(g->header + 84) != ENTRY_SIZE)
		return -1;
	saved_header_crc = le32(g->header + 16);
	memcpy(check, g->header, sizeof(check));
	memset(check + 16, 0, 4);
	if (crc32_bytes(check, HEADER_SIZE) != saved_header_crc)
		return -1;
	g->entries_lba = le64(g->header + 72);
	if (header_lba == 1) {
		if (g->entries_lba < 2 ||
		    g->entries_lba > first_usable - ENTRIES_SECTORS)
			return -1;
	} else {
		if (g->entries_lba <= last_usable ||
		    g->entries_lba > header_lba - ENTRIES_SECTORS)
			return -1;
	}
	if (full_pread(fd, g->entries, sizeof(g->entries), g->entries_lba * SECTOR_SIZE))
		return -1;
	saved_entries_crc = le32(g->header + 88);
	if (crc32_bytes(g->entries, sizeof(g->entries)) != saved_entries_crc &&
	    !allow_stale_entries_crc)
		return -1;
	return 0;
}

/* slot is 0 for a, 1 for b.  Each boot_<x> entry is pinned to the sector range
 * recorded by the read-only P0 inventory of this device family. */
static unsigned char *find_boot(struct gpt_copy *g, int slot)
{
	static const char *const names[2] = { "boot_a", "boot_b" };
	static const uint64_t first[2] = { BOOT_A_FIRST, BOOT_B_FIRST };
	static const uint64_t last[2] = { BOOT_A_LAST, BOOT_B_LAST };
	unsigned int i;

	for (i = 0; i < ENTRY_COUNT; i++) {
		unsigned char *e = g->entries + i * ENTRY_SIZE;
		if (!name_is(e, names[slot]))
			continue;
		if (le64(e + 32) != first[slot] || le64(e + 40) != last[slot])
			return NULL;
		return e;
	}
	return NULL;
}

static void refresh_crcs(struct gpt_copy *g)
{
	put_le32(g->header + 88, crc32_bytes(g->entries, sizeof(g->entries)));
	put_le32(g->header + 16, 0);
	put_le32(g->header + 16, crc32_bytes(g->header, HEADER_SIZE));
}

/* Returns 0 for slot a, 1 for slot b, -1 when the kernel command line carries
 * no usable androidboot.slot_suffix. */
static int cmdline_slot(void)
{
	char buf[8192];
	int fd = open("/proc/cmdline", O_RDONLY | O_CLOEXEC);
	ssize_t n;
	int slot = -1;

	if (fd < 0)
		return -1;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return -1;
	buf[n] = 0;
	if (strstr(buf, "androidboot.slot_suffix=_a"))
		slot = 0;
	if (strstr(buf, "androidboot.slot_suffix=_b"))
		slot = slot == 0 ? -1 : 1;
	return slot;
}

static int sde_is_unmounted(void)
{
	char line[1024];
	FILE *f = fopen("/proc/mounts", "re");
	if (!f)
		return 0;
	while (fgets(line, sizeof(line), f)) {
		if (!strncmp(line, "/dev/sde", 8)) {
			fclose(f);
			return 0;
		}
	}
	fclose(f);
	return 1;
}

static void usage(void)
{
	fputs("usage: liuqin-mark-slot-successful --mark <a|b>\n"
	      "       liuqin-mark-slot-successful --check <a|b>\n"
	      "\n"
	      "--mark   set successful and clear unbootable on the running slot\n"
	      "--check  report whether that slot is already successful\n"
	      "\n"
	      "Slot selection is the bootloader's job (fastboot --set-active).\n",
	      stderr);
}

static int parse_slot(const char *s, int *slot)
{
	if (!strcmp(s, "a") || !strcmp(s, "_a") || !strcmp(s, "A"))
		*slot = 0;
	else if (!strcmp(s, "b") || !strcmp(s, "_b") || !strcmp(s, "B"))
		*slot = 1;
	else
		return -1;
	return 0;
}

int main(int argc, char **argv)
{
	static const char slot_name[2] = { 'A', 'B' };
	const char *path = "/dev/sde";
	int testing = 0, made_rw = 0, fd = -1, ro = 1, zero = 0, one = 1, rc = 1;
	int i, slot = -1, other, running;
	enum op op = OP_NONE;
	uint64_t bytes = 0, disk_lbas, attrs;
	struct stat st;
	struct gpt_copy primary, backup, verify_primary, verify_backup;
	unsigned char *pt, *bt, *po, *vp, *vb;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--test-image") && i + 1 < argc && !testing) {
			testing = 1;
			path = argv[++i];
		} else if (i + 1 < argc && op == OP_NONE &&
			   (!strcmp(argv[i], "--mark") || !strcmp(argv[i], "--check"))) {
			op = !strcmp(argv[i], "--mark") ? OP_MARK : OP_CHECK;
			if (parse_slot(argv[++i], &slot)) {
				usage();
				return 2;
			}
		} else {
			usage();
			return 2;
		}
	}
	if (op == OP_NONE) {
		usage();
		return 2;
	}
	other = !slot;

	if (testing) {
		const char *env = getenv("LIUQIN_SLOT_SUCCESS_TESTING");
		if (!env || strcmp(env, "unsafe-mock-only")) {
			fprintf(stderr, "liuqin-slot-success: --test-image is not enabled\n");
			return 2;
		}
	} else {
		running = cmdline_slot();
		if (running < 0 || !sde_is_unmounted()) {
			fprintf(stderr, "liuqin-slot-success: current slot/mount guard failed\n");
			return 1;
		}
		/* --mark/--check speak only about the slot we are running from. */
		if (running != slot) {
			fprintf(stderr, "liuqin-slot-success: refusing to touch slot %c "
				"while running slot %c\n", slot_name[slot], slot_name[running]);
			return 1;
		}
	}

	if (lstat(path, &st) || (!testing && !S_ISBLK(st.st_mode)) ||
	    (testing && !S_ISREG(st.st_mode)))
		goto out;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		goto out;
	if (testing) {
		bytes = (uint64_t)st.st_size;
	} else {
		int logical = 0;
		if (ioctl(fd, BLKGETSIZE64, &bytes) || ioctl(fd, BLKSSZGET, &logical) ||
		    ioctl(fd, BLKROGET, &ro) || bytes != DISK_BYTES ||
		    logical != (int)SECTOR_SIZE || ro != 1)
			goto out;
	}
	if (bytes != DISK_BYTES || bytes % SECTOR_SIZE)
		goto out;
	disk_lbas = bytes / SECTOR_SIZE;
	if (load_gpt_copy(fd, disk_lbas, 1, &primary, 0) ||
	    load_gpt_copy(fd, disk_lbas, disk_lbas - 1, &backup, 1) ||
	    memcmp(primary.header + 56, backup.header + 56, 16) ||
	    memcmp(primary.header + 40, backup.header + 40, 16))
		goto out;
	pt = find_boot(&primary, slot);
	po = find_boot(&primary, other);
	if (!pt || !po || !find_boot(&backup, slot) || !find_boot(&backup, other))
		goto out;
	/* Qualcomm fastboot/ABL updates backup entries but can leave the backup
	 * header's entry CRC stale.  A fully valid primary GPT is authoritative;
	 * rebuild the backup array from it rather than merging untrusted attrs. */
	memcpy(backup.entries, primary.entries, sizeof(primary.entries));
	bt = find_boot(&backup, slot);
	attrs = le64(pt + 48);

	/* The running slot has to look like the one ABL chose. */
	if ((attrs & ATTR_PRIORITY_MASK) != ATTR_PRIORITY_MASK ||
	    !(attrs & ATTR_ACTIVE) ||
	    (!(attrs & ATTR_TRIES_MASK) && !(attrs & ATTR_SUCCESSFUL)))
		goto out;
	if (op == OP_CHECK) {
		if (!(attrs & ATTR_SUCCESSFUL) || (attrs & ATTR_UNBOOTABLE))
			goto out;
		rc = 0;
		goto out;
	}
	attrs |= ATTR_SUCCESSFUL;
	attrs &= ~ATTR_UNBOOTABLE;
	put_le64(pt + 48, attrs);
	put_le64(bt + 48, attrs);
	refresh_crcs(&primary);
	refresh_crcs(&backup);

	if (!testing) {
		if (ioctl(fd, BLKROSET, &zero))
			goto out;
		made_rw = 1;
		close(fd);
		fd = -1;
		fd = open(path, O_RDWR | O_CLOEXEC | O_SYNC);
		if (fd < 0)
			goto out;
	} else {
		close(fd);
		fd = open(path, O_RDWR | O_CLOEXEC | O_SYNC);
		if (fd < 0)
			goto out;
	}
	/* Backup GPT first: a power loss always leaves at least one old/new copy. */
	if (full_pwrite(fd, backup.entries, sizeof(backup.entries), backup.entries_lba * SECTOR_SIZE) ||
	    full_pwrite(fd, backup.header, sizeof(backup.header), backup.header_lba * SECTOR_SIZE) || fsync(fd) ||
	    full_pwrite(fd, primary.entries, sizeof(primary.entries), primary.entries_lba * SECTOR_SIZE) ||
	    full_pwrite(fd, primary.header, sizeof(primary.header), primary.header_lba * SECTOR_SIZE) || fsync(fd))
		goto out;
	if (load_gpt_copy(fd, disk_lbas, 1, &verify_primary, 0) ||
	    load_gpt_copy(fd, disk_lbas, disk_lbas - 1, &verify_backup, 0) ||
	    memcmp(verify_primary.entries, verify_backup.entries, sizeof(verify_primary.entries)))
		goto out;
	vp = find_boot(&verify_primary, slot);
	vb = find_boot(&verify_backup, slot);
	if (!vp || !vb || le64(vp + 48) != attrs || le64(vb + 48) != attrs)
		goto out;
	rc = 0;

out:
	if (fd < 0 && made_rw)
		fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd >= 0) {
		if (!testing && made_rw && ioctl(fd, BLKROSET, &one))
			rc = 1;
		close(fd);
	}
	if (rc) {
		fprintf(stderr, "liuqin-slot-success: refused or failed; GPT not accepted\n");
	} else if (op == OP_MARK) {
		printf("liuqin-slot-success: slot %c is successful; "
		       "primary/backup GPT CRCs verified\n", slot_name[slot]);
	} else {
		printf("liuqin-slot-success: slot %c success is verified; "
		       "primary/backup GPT CRCs valid\n", slot_name[slot]);
	}
	return rc;
}
