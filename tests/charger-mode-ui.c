// SPDX-License-Identifier: MIT
/* Run the actual renderer against a fake KMS device, never a host display. */
#define main charger_ui_main
#include "../device/charger-mode/liuqin-charger-mode-ui.c"
#undef main
#include <assert.h>
#include <stdarg.h>

static int fault, creates, destroys, removes, closes, disabled;
static size_t mapped;
static bool run_loop;
static int64_t fake_ms, available_ms, first_value_ms, previous_read_ms;
static int initial_percent, reads, steady_reads;
enum { BAD_COUNT = 1, EXTERNAL, BAD_MODE, BUSY, BAD_PITCH, BAD_SIZE, MAP_FAIL, ADD_FAIL };

uid_t __wrap_geteuid(void) { return 0; }
int __wrap_access(const char *path, int mode)
{
	assert(!strcmp(path, "/etc/liuqin-charger-mode") && mode == R_OK); return 0;
}
int __wrap_clock_gettime(clockid_t clock, struct timespec *now)
{
	assert(clock == CLOCK_MONOTONIC);
	now->tv_sec = fake_ms / 1000; now->tv_nsec = fake_ms % 1000 * 1000000; return 0;
}
int __wrap_nanosleep(const struct timespec *delay, struct timespec *remaining)
{
	(void)remaining;
	int64_t ms = delay->tv_sec * 1000 + delay->tv_nsec / 1000000;
	assert(ms > 0 && ms <= 2000); fake_ms += ms; return 0;
}
FILE *__wrap_fopen(const char *path, const char *mode)
{
	FILE *file = tmpfile(); assert(file);
	if (!strcmp(mode, "w")) return file;
	if (!strcmp(path, "/test-battery/capacity")) {
		reads++;
		if (first_value_ms >= 0) {
			assert(fake_ms - previous_read_ms == 2000); steady_reads++;
		}
		previous_read_ms = fake_ms;
		if (fake_ms >= available_ms) {
			if (first_value_ms < 0) first_value_ms = fake_ms;
			fprintf(file, "%d\n", initial_percent);
		}
	} else if (!strcmp(path, "/test-battery/status")) fputs("Charging\n", file);
	else if (!strcmp(path, "/test-backlight/max_brightness")) fputs("2047\n", file);
	else assert(!"unexpected sysfs read");
	rewind(file); return file;
}

int __wrap_open(const char *path, int flags, ...)
{
	(void)flags;
	if (!strcmp(path, "/dev/dri/card0")) return 101;
	errno = ENOENT; return -1;
}
int __wrap_fstat(int fd, struct stat *st)
{
	assert(fd == 101); memset(st, 0, sizeof(*st)); st->st_mode = S_IFCHR; return 0;
}
int __wrap_close(int fd) { assert(fd == 101); closes++; return 0; }
void *__wrap_mmap(void *address, size_t size, int prot, int flags, int fd, off_t offset)
{
	(void)address; (void)prot; (void)flags; (void)offset; assert(fd == 101);
	if (fault == MAP_FAIL) return MAP_FAILED;
	mapped = size; return calloc(1, size);
}
int __wrap_munmap(void *address, size_t size)
{
	assert(size == mapped); free(address); mapped = 0; return 0;
}
int __wrap_ioctl(int fd, unsigned long request, ...)
{
	assert(fd == 101);
	va_list args; va_start(args, request); void *arg = va_arg(args, void *); va_end(args);
	switch (request) {
	case DRM_IOCTL_VERSION: strcpy(((struct drm_version *)arg)->name, "msm"); break;
	case DRM_IOCTL_SET_MASTER: break;
	case DRM_IOCTL_MODE_GETRESOURCES: {
		struct drm_mode_card_res *r = arg;
		((uint32_t *)(uintptr_t)r->connector_id_ptr)[0] = 11;
		((uint32_t *)(uintptr_t)r->crtc_id_ptr)[0] = 22;
		r->count_connectors = fault == BAD_COUNT ? LIMIT + 1 : 1; r->count_crtcs = 1; break;
	}
	case DRM_IOCTL_MODE_GETCONNECTOR: {
		struct drm_mode_get_connector *c = arg;
		c->connector_type = fault == EXTERNAL ? DRM_MODE_CONNECTOR_DisplayPort : DRM_MODE_CONNECTOR_DSI;
		c->connection = 1;
		if (c->modes_ptr) {
			struct drm_mode_modeinfo *m = (void *)(uintptr_t)c->modes_ptr;
			for (int i = 0; i < 3; i++) m[i] = (struct drm_mode_modeinfo){
				.hdisplay = fault == BAD_MODE ? 1920 : W * 4, .vdisplay = H * 4,
				.vrefresh = i == 0 ? 120 : i == 1 ? 60 : 144 };
		}
		if (c->encoders_ptr) ((uint32_t *)(uintptr_t)c->encoders_ptr)[0] = 33;
		c->count_modes = 3; c->count_encoders = 1; break;
	}
	case DRM_IOCTL_MODE_GETENCODER: ((struct drm_mode_get_encoder *)arg)->possible_crtcs = 1; break;
	case DRM_IOCTL_MODE_GETCRTC: ((struct drm_mode_crtc *)arg)->mode_valid = fault == BUSY; break;
	case DRM_IOCTL_MODE_CREATE_DUMB: {
		struct drm_mode_create_dumb *c = arg;
		assert(c->width == W * 4 && c->height == H * 4 && c->bpp == 32);
		c->handle = 44; c->pitch = fault == BAD_PITCH ? 3 : W * 16 + 64;
		c->size = fault == BAD_SIZE ? 1 : (uint64_t)c->pitch * H * 4; creates++; break;
	}
	case DRM_IOCTL_MODE_ADDFB:
		if (fault == ADD_FAIL) return -1;
		((struct drm_mode_fb_cmd *)arg)->fb_id = 55; break;
	case DRM_IOCTL_MODE_MAP_DUMB: ((struct drm_mode_map_dumb *)arg)->offset = 0; break;
	case DRM_IOCTL_MODE_SETCRTC: {
		struct drm_mode_crtc *c = arg;
		assert(c->crtc_id == 22);
		if (c->mode_valid) assert(run_loop && fake_ms == 0);
		else disabled++;
		break;
	}
	case DRM_IOCTL_MODE_RMFB: assert(*(uint32_t *)arg == 55); removes++; break;
	case DRM_IOCTL_MODE_DESTROY_DUMB:
		assert(((struct drm_mode_destroy_dumb *)arg)->handle == 44); destroys++; break;
	default: assert(!"unexpected ioctl");
	}
	return 0;
}

int main(void)
{
	assert(sizeof(glyphs) / sizeof(glyphs[0]) == strlen(alphabet));
	assert(capacity_value("0") == 0 && capacity_value("100") == 100);
	for (const char **p = (const char *[]){"", "-1", "101", "unknown", "20%", "999999999999", NULL}; *p; p++)
		assert(capacity_value(*p) == -1);
	for (fault = 0; fault <= ADD_FAIL; fault++) {
		struct display d = { .fd = -1 };
		creates = destroys = removes = closes = disabled = 0;
		bool ok = open_display(&d);
		assert(ok == (fault == 0));
		if (ok) {
			assert(d.mode.vrefresh == 60 && d.connector == 11 && d.crtc == 22);
			draw(68, "Charging"); copy_frame(&d);
			for (int y = 0; y < H * 4; y++) {
				uint32_t *row = (void *)(d.pixels + (size_t)y * d.pitch);
				for (int x = 0; x < W * 4; x++) assert(row[x] == (canvas[y / 4 * W + x / 4] | 0xff000000U));
				assert(row[W * 4] == 0); /* Never write into row padding. */
			}
			d.active = true;
		}
		close_display(&d);
		assert(!mapped && creates == destroys && closes == 1 && disabled == (fault == 0));
		assert(removes == (fault == 0 || fault == MAP_FAIL));
	}
	/* The real display loop must show an unknown icon immediately, acquire a
	 * late first reading within 200 ms, then return to two-second polling.
	 * A valid empty battery must not remain on the fast retry path. */
	run_loop = true; fault = 0;
	for (int scenario = 0; scenario < 2; scenario++) {
		fake_ms = 0; first_value_ms = -1; previous_read_ms = -1;
		available_ms = scenario ? 0 : 750; initial_percent = scenario ? 0 : 68;
		reads = steady_reads = 0;
		char *args[] = {"ui", "/test-battery", "/test-backlight", NULL};
		assert(charger_ui_main(3, args) == 0);
		assert(first_value_ms >= available_ms && first_value_ms <= available_ms + 200);
		assert(fake_ms == 15000 && steady_reads >= 6 && reads <= 12);
		assert(!mapped);
	}
	puts("charger UI: KMS selection, bounds, stride, rendering and cleanup passed");
	return 0;
}
