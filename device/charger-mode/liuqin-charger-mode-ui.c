// SPDX-License-Identifier: MIT
/* Small CPU-rendered KMS charging screen. No rootfs, fonts, GPU or fbdev needed. */
#define _GNU_SOURCE
#if __has_include(<drm/drm.h>)
#include <drm/drm.h>
#include <drm/drm_mode.h>
#else
/* Debian-family host tests package these UAPI headers with libdrm-dev. */
#include <libdrm/drm.h>
#include <libdrm/drm_mode.h>
#endif
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define W 450
#define H 720
#define LIMIT 64
#define WHITE 0xe6edf3U
#define GREY 0x718096U
#define GREEN 0x5be4adU
static uint32_t canvas[W * H];
static volatile sig_atomic_t stopping;

static void stop(int signal_number) { (void)signal_number; stopping = 1; }

static void rect(int x, int y, int w, int h, uint32_t color)
{
	for (int row = y; row < y + h; row++)
		for (int col = x; col < x + w; col++)
			if (row >= 0 && row < H && col >= 0 && col < W)
				canvas[row * W + col] = color;
}

static void rounded(int x, int y, int w, int h, int r, uint32_t color)
{
	for (int row = 0; row < h; row++) {
		for (int col = 0; col < w; col++) {
			int dx = col < r ? r - col : col >= w - r ? col - (w - r - 1) : 0;
			int dy = row < r ? r - row : row >= h - r ? row - (h - r - 1) : 0;
			if (dx * dx + dy * dy <= r * r)
				rect(x + col, y + row, 1, 1, color);
		}
	}
}

/* Five-column, seven-row glyphs; all artwork is generated from these primitives. */
static const char alphabet[] = "0123456789%-ACDEGHIKNOPRSTW";
static const uint8_t glyphs[][7] = {
	{14,17,19,21,25,17,14},{4,12,4,4,4,4,14},{14,17,1,2,4,8,31},
	{30,1,1,14,1,1,30},{2,6,10,18,31,2,2},{31,16,16,30,1,1,30},
	{14,16,16,30,17,17,14},{31,1,2,4,8,8,8},{14,17,17,14,17,17,14},
	{14,17,17,15,1,1,14},{25,25,2,4,8,19,19},{0,0,0,31,0,0,0},
	{14,17,17,31,17,17,17},{14,17,16,16,16,17,14},{30,17,17,17,17,17,30},
	{31,16,16,30,16,16,31},{14,17,16,23,17,17,14},{17,17,17,31,17,17,17},
	{14,4,4,4,4,4,14},{17,18,20,24,20,18,17},{17,25,21,19,17,17,17},
	{14,17,17,17,17,17,14},{30,17,17,30,16,16,16},{30,17,17,30,20,18,17},
	{15,16,16,14,1,1,30},{31,4,4,4,4,4,4},{17,17,17,21,21,21,10}
};

static void label(const char *text, int y, int scale, uint32_t color)
{
	int x = (W - ((int)strlen(text) * 6 - 1) * scale) / 2;
	for (; *text; text++, x += 6 * scale) {
		const char *found = strchr(alphabet, *text);
		if (!found) continue;
		const uint8_t *glyph = glyphs[found - alphabet];
		for (int row = 0; row < 7; row++)
			for (int col = 0; col < 5; col++)
				if (glyph[row] & (1U << (4 - col)))
					rect(x + col * scale, y + row * scale, scale, scale, color);
	}
}

static void draw(int capacity, const char *status)
{
	bool charging = !strcmp(status, "Charging"), full = !strcmp(status, "Full");
	uint32_t accent = capacity >= 0 && capacity <= 15 ? 0xffac69U : GREEN;
	char number[16];
	memset(canvas, 0, sizeof(canvas));
	rounded(208, 221, 34, 10, 4, GREY);
	rounded(169, 236, 112, 164, 14, GREY);
	rounded(173, 240, 104, 156, 11, 0);
	if (capacity >= 0) {
		int fill = 140 * capacity / 100;
		if (fill) rounded(181, 388 - fill, 88, fill, fill < 8 ? 0 : 4, accent);
		snprintf(number, sizeof(number), "%d%%", capacity);
	} else {
		strcpy(number, "--%");
	}
	if (charging) {
		/* Lightning mark, visible even at an empty battery. */
		for (int y = 0; y < 37; y++) {
			int start = 229 - y / 2;
			rect(start, 277 + y, 12 + y / 3, 1, WHITE);
			rect(218 + y / 6, 313 + y, 27 - y * 2 / 3, 1, WHITE);
		}
	}
	label(number, 433, 5, WHITE);
	label(full ? "CHARGED" : charging ? "CHARGING" :
	      !strcmp(status, "Discharging") ? "CHECK POWER" : "WAITING", 492, 2,
	      charging || full ? accent : GREY);
}

static int value(const char *root, const char *field, char *out, size_t size)
{
	char path[512];
	out[0] = 0;
	if (snprintf(path, sizeof(path), "%s/%s", root, field) >= (int)sizeof(path)) return -1;
	FILE *file = fopen(path, "r");
	if (!file) return -1;
	bool ok = fgets(out, size, file) != NULL;
	if (ferror(file)) ok = false;
	fclose(file);
	if (!ok) { out[0] = 0; return -1; }
	out[strcspn(out, "\r\n")] = 0;
	return 0;
}

static int capacity_value(const char *text)
{
	if (!*text || strspn(text, "0123456789") != strlen(text) || strlen(text) > 3) return -1;
	int n = atoi(text);
	return n <= 100 ? n : -1;
}

static int64_t monotonic_ms(void)
{
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
	return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int set_value(const char *root, const char *field, int n)
{
	char path[512];
	if (snprintf(path, sizeof(path), "%s/%s", root, field) >= (int)sizeof(path)) return -1;
	FILE *file = fopen(path, "w");
	if (!file) return -1;
	int rc = fprintf(file, "%d\n", n) < 0 ? -1 : 0;
	return fclose(file) ? -1 : rc;
}

static int dim_brightness(const char *backlight)
{
	char maximum[32], curve[32];
	if (value(backlight, "max_brightness", maximum, sizeof(maximum))) return -1;
	if (!strcmp(maximum, "2047")) return 150;
	if (!strcmp(maximum, "4095") && !value(backlight, "brightness_curve", curve, sizeof(curve)) &&
	    !strcmp(curve, "liuqin-oem")) return 128;
	return -1;
}

struct display {
	int fd;
	uint32_t connector, crtc, fb, handle, pitch;
	size_t size;
	uint8_t *pixels;
	struct drm_mode_modeinfo mode;
	bool active;
};

static void close_display(struct display *d)
{
	if (d->fd < 0) return;
	if (d->active) {
		struct drm_mode_crtc off = { .crtc_id = d->crtc };
		ioctl(d->fd, DRM_IOCTL_MODE_SETCRTC, &off);
	}
	if (d->pixels) munmap(d->pixels, d->size);
	if (d->fb) ioctl(d->fd, DRM_IOCTL_MODE_RMFB, &d->fb);
	if (d->handle) {
		struct drm_mode_destroy_dumb destroy = { .handle = d->handle };
		ioctl(d->fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
	}
	close(d->fd);
	*d = (struct display){ .fd = -1 };
}

static bool find_panel(struct display *d)
{
	uint32_t connectors[LIMIT], crtcs[32];
	struct drm_mode_card_res res = { .connector_id_ptr = (uintptr_t)connectors,
		.crtc_id_ptr = (uintptr_t)crtcs, .count_connectors = LIMIT, .count_crtcs = 32 };
	if (ioctl(d->fd, DRM_IOCTL_MODE_GETRESOURCES, &res) ||
	    res.count_connectors > LIMIT || res.count_crtcs > 32) return false;
	for (unsigned i = 0; i < res.count_connectors; i++) {
		struct drm_mode_modeinfo modes[LIMIT];
		uint32_t encoders[LIMIT];
		struct drm_mode_get_connector c = { .connector_id = connectors[i] };
		/* A zero-count master query probes the connector before fetching modes. */
		if (ioctl(d->fd, DRM_IOCTL_MODE_GETCONNECTOR, &c) ||
		    c.connector_type != DRM_MODE_CONNECTOR_DSI || c.connection != 1) continue;
		c.modes_ptr = (uintptr_t)modes; c.count_modes = LIMIT;
		c.encoders_ptr = (uintptr_t)encoders; c.count_encoders = LIMIT;
		c.count_props = 0; c.props_ptr = 0; c.prop_values_ptr = 0;
		if (ioctl(d->fd, DRM_IOCTL_MODE_GETCONNECTOR, &c) || c.count_modes > LIMIT ||
		    c.count_encoders > LIMIT || c.connection != 1) continue;
		int selected = -1;
		for (unsigned m = 0; m < c.count_modes; m++) {
			if (modes[m].hdisplay != W * 4 || modes[m].vdisplay != H * 4 ||
			    !modes[m].vrefresh || modes[m].flags & DRM_MODE_FLAG_INTERLACE) continue;
			if (selected < 0 || modes[m].vrefresh < modes[selected].vrefresh) selected = (int)m;
		}
		if (selected < 0) continue;
		for (unsigned e = 0; e < c.count_encoders; e++) {
			struct drm_mode_get_encoder enc = { .encoder_id = encoders[e] };
			if (ioctl(d->fd, DRM_IOCTL_MODE_GETENCODER, &enc)) continue;
			for (unsigned cr = 0; cr < res.count_crtcs; cr++) {
				if (!(enc.possible_crtcs & (1U << cr))) continue;
				struct drm_mode_crtc current = { .crtc_id = crtcs[cr] };
				if (ioctl(d->fd, DRM_IOCTL_MODE_GETCRTC, &current) || current.mode_valid) continue;
				d->connector = c.connector_id; d->crtc = crtcs[cr]; d->mode = modes[selected];
				return true;
			}
		}
	}
	return false;
}

static bool open_display(struct display *d)
{
	for (int card = 0; card < 8; card++) {
		char path[64], name[32] = {0}; struct stat st;
		snprintf(path, sizeof(path), "/dev/dri/card%d", card);
		d->fd = open(path, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
		if (d->fd < 0) continue;
		struct drm_version version = { .name = name, .name_len = sizeof(name) - 1 };
		if (!fstat(d->fd, &st) && S_ISCHR(st.st_mode) &&
		    !ioctl(d->fd, DRM_IOCTL_VERSION, &version) && !strcmp(name, "msm") &&
		    !ioctl(d->fd, DRM_IOCTL_SET_MASTER, NULL) && find_panel(d)) break;
		close_display(d);
	}
	if (d->fd < 0) return false;
	struct drm_mode_create_dumb create = { .width = W * 4, .height = H * 4, .bpp = 32 };
	if (ioctl(d->fd, DRM_IOCTL_MODE_CREATE_DUMB, &create)) return false;
	d->handle = create.handle;
	if (create.pitch < W * 16 || create.pitch % 4 || create.size < (uint64_t)create.pitch * H * 4 ||
	    create.size > 64U * 1024 * 1024) return false;
	d->pitch = create.pitch; d->size = create.size;
	struct drm_mode_fb_cmd fb = { .width = W * 4, .height = H * 4, .pitch = d->pitch,
		.bpp = 32, .depth = 24, .handle = d->handle };
	if (ioctl(d->fd, DRM_IOCTL_MODE_ADDFB, &fb)) return false;
	d->fb = fb.fb_id;
	struct drm_mode_map_dumb map = { .handle = d->handle };
	if (ioctl(d->fd, DRM_IOCTL_MODE_MAP_DUMB, &map)) return false;
	d->pixels = mmap(NULL, d->size, PROT_READ | PROT_WRITE, MAP_SHARED, d->fd, map.offset);
	if (d->pixels == MAP_FAILED) { d->pixels = NULL; return false; }
	return true;
}

static void copy_frame(struct display *d)
{
	for (int y = 0; y < H * 4; y++) {
		uint32_t *row = (uint32_t *)(d->pixels + (size_t)y * d->pitch);
		for (int x = 0; x < W * 4; x++) row[x] = canvas[(y / 4) * W + x / 4] | 0xff000000U;
	}
}

static int preview(const char *path, const char *capacity, const char *status)
{
	draw(capacity_value(capacity), status);
	FILE *file = fopen(path, "wb");
	if (!file) return 1;
	fprintf(file, "P6\n%d %d\n255\n", W, H);
	for (int i = 0; i < W * H; i++) {
		uint8_t rgb[3] = { canvas[i] >> 16, canvas[i] >> 8, canvas[i] };
		if (fwrite(rgb, 1, 3, file) != 3) { fclose(file); return 1; }
	}
	return fclose(file) ? 1 : 0;
}

int main(int argc, char **argv)
{
	if (argc == 5 && !strcmp(argv[1], "--preview")) return preview(argv[2], argv[3], argv[4]);
	if (argc != 3) {
		fprintf(stderr, "usage: %s BATTERY_SYSFS BACKLIGHT_SYSFS | --preview FILE CAPACITY STATUS\n", argv[0]);
		return 64;
	}
	/* This program is installed only in stage 1 and must not take over a desktop. */
	if (getpid() == 1 || geteuid() != 0 || access("/etc/liuqin-charger-mode", R_OK)) return 1;
	const char *battery = argv[1], *backlight = argv[2];
	int brightness = dim_brightness(backlight), rc = 1;
	struct display d = { .fd = -1 };
	struct sigaction action = { .sa_handler = stop };
	sigemptyset(&action.sa_mask); sigaction(SIGTERM, &action, NULL); sigaction(SIGINT, &action, NULL);
	if (brightness < 0) return 1;
	for (int attempt = 0; attempt < 8 && !stopping; attempt++) {
		if (open_display(&d)) break;
		close_display(&d); sleep(1);
	}
	if (!d.pixels || stopping) {
		fprintf(stderr, "internal DSI display unavailable\n");
		goto done;
	}
	fprintf(stderr, "mode=%ux%u@%u connector=%u crtc=%u\n",
		d.mode.hdisplay, d.mode.vdisplay, d.mode.vrefresh, d.connector, d.crtc);
	int64_t deadline = 0, next_read = 0;
	int previous_percent = -2;
	char previous_status[32] = "";
	while (!stopping) {
		int64_t now = monotonic_ms();
		if (now < 0) goto done;
		if (deadline && now >= deadline) break;
		if (now >= next_read) {
			char capacity[32] = "", status[32] = "";
			value(battery, "capacity", capacity, sizeof(capacity));
			value(battery, "status", status, sizeof(status));
			int percent = capacity_value(capacity);
			if (percent != previous_percent || strcmp(status, previous_status)) {
				draw(percent, status); copy_frame(&d);
				previous_percent = percent;
				memcpy(previous_status, status, sizeof(previous_status));
			}
			/* The sysfs node can precede the ADSP service. Show the icon
			 * immediately, then acquire the first valid reading promptly.
			 * A real 0% is valid and uses the normal low-rate refresh. */
			now = monotonic_ms();
			if (now < 0) goto done;
			next_read = now + (percent < 0 ? 200 : 2000);
		}
		if (!d.active) {
			/* Keep power blanked until DRM's first-frame backlight sequence. */
			if (set_value(backlight, "brightness", brightness)) goto done;
			struct drm_mode_crtc on = { .crtc_id = d.crtc, .fb_id = d.fb,
				.set_connectors_ptr = (uintptr_t)&d.connector, .count_connectors = 1,
				.mode = d.mode, .mode_valid = 1 };
			if (ioctl(d.fd, DRM_IOCTL_MODE_SETCRTC, &on)) {
				perror("DRM set charging mode"); goto done;
			}
			d.active = true;
			fprintf(stderr, "charging frame displayed\n");
			now = monotonic_ms();
			if (now < 0) goto done;
			deadline = now + 15000;
		}
		now = monotonic_ms();
		if (now < 0) goto done;
		int64_t wake = next_read < deadline ? next_read : deadline;
		if (wake > now) {
			struct timespec delay = { .tv_sec = (wake - now) / 1000,
				.tv_nsec = (wake - now) % 1000 * 1000000 };
			nanosleep(&delay, NULL);
		}
	}
	rc = 0;
done:
	set_value(backlight, "bl_power", 4); set_value(backlight, "brightness", 0);
	close_display(&d);
	fprintf(stderr, "screen off, result=%d\n", rc);
	return rc;
}
