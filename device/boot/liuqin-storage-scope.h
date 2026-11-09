/* SPDX-License-Identifier: MIT
 * Identify the tablet's internal UFS controller through canonical sysfs paths.
 * A SCSI name alone never grants a device membership in this scope.
 */
#ifndef LIUQIN_STORAGE_SCOPE_H
#define LIUQIN_STORAGE_SCOPE_H
#include <limits.h>
#include <glob.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct liuqin_ufs_scope {
    char controller[PATH_MAX];
    char devices[PATH_MAX];
};

static inline int liuqin_path_below(const char *path, const char *parent)
{
    size_t n = strlen(parent);
    return strncmp(path, parent, n) == 0 && path[n] == '/';
}

static inline int liuqin_ufs_scope_init(struct liuqin_ufs_scope *scope, const char *sysroot)
{
    char path[PATH_MAX], driver[PATH_MAX], expected[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/devices", sysroot) >= (int)sizeof(path) ||
        !realpath(path, scope->devices) ||
        snprintf(path, sizeof(path), "%s/devices/platform/soc@0/1d84000.ufshc", sysroot) >= (int)sizeof(path) ||
        !realpath(path, scope->controller) || !liuqin_path_below(scope->controller, scope->devices) ||
        snprintf(path, sizeof(path), "%s/driver", scope->controller) >= (int)sizeof(path) ||
        !realpath(path, driver) ||
        snprintf(path, sizeof(path), "%s/bus/platform/drivers/ufshcd-qcom", sysroot) >= (int)sizeof(path) ||
        !realpath(path, expected) || strcmp(driver, expected))
        return -1;
    return 0;
}

/* 1: this internal controller, 0: another controller, -1: unproven identity. */
static inline int liuqin_ufs_node(const struct liuqin_ufs_scope *scope,
                                  const char *sysroot, const char *name, char *real)
{
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/class/block/%s", sysroot, name) >= (int)sizeof(path) ||
        !realpath(path, real) || !liuqin_path_below(real, scope->devices) ||
        strcmp(strrchr(real, '/') + 1, name))
        return -1;
    return liuqin_path_below(real, scope->controller);
}

/* glob(3) owns its result, including libc-specific string allocations.
 * Never compact/free individual entries in gl_pathv. Copy selected paths to
 * our own vector and release the original with its matching globfree(). */
static inline void liuqin_storage_globfree(glob_t *paths, int ufs_only)
{
    if (!ufs_only) { globfree(paths); return; }
    for (size_t i = 0; i < paths->gl_pathc; ++i) free(paths->gl_pathv[i]);
    free(paths->gl_pathv);
    memset(paths, 0, sizeof(*paths));
}

static inline int liuqin_storage_glob(const char *pattern, glob_t *paths,
                                      const char *sysroot, int ufs_only)
{
    glob_t raw = {0};
    struct liuqin_ufs_scope scope;
    char real[PATH_MAX];
    int result = -1;
    if (glob(pattern, GLOB_ERR, NULL, &raw) != 0 || !raw.gl_pathc) goto out;
    if (!ufs_only) { *paths = raw; return 0; }
    if (liuqin_ufs_scope_init(&scope, sysroot)) goto out;
    paths->gl_pathv = calloc(raw.gl_pathc + 1, sizeof(char *));
    if (!paths->gl_pathv) goto out;
    for (size_t i = 0; i < raw.gl_pathc; ++i) {
        const char *name = strrchr(raw.gl_pathv[i], '/') + 1;
        int selected = liuqin_ufs_node(&scope, sysroot, name, real);
        if (selected < 0) goto out;
        if (selected) {
            char *copy = strdup(raw.gl_pathv[i]);
            if (!copy) goto out;
            paths->gl_pathv[paths->gl_pathc++] = copy;
        }
    }
    if (paths->gl_pathc) result = 0;
out:
    globfree(&raw);
    if (result && ufs_only) liuqin_storage_globfree(paths, 1);
    return result;
}
#endif
