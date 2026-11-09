/* SPDX-License-Identifier: MIT
 * Private, versioned host/TEE-client wire formats. Never journal these bytes.
 */
#ifndef LIUQIN_FPC_GALLERY_H
#define LIUQIN_FPC_GALLERY_H
#include <stdint.h>
#include <string.h>

#define FPC_GALLERY_MAX 5u
#define FPC_REFERENCE_SIZE 48u
#define FPC_METADATA_SIZE 64u
#define FPC_RESULT_SIZE 16u
#define FPC_DATABASE_MAX (16u * 1024u * 1024u)
static const char liuqin_fpc_gallery_protocol[] __attribute__((used)) = "LIUQIN_FPC_GALLERY_V1";

struct fpc_inventory { uint32_t count, ids[FPC_GALLERY_MAX]; };

static inline uint32_t fpc_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static inline void fpc_put32(uint8_t *p, uint32_t value)
{
    for (unsigned int i = 0; i < 4; i++) p[i] = (uint8_t)(value >> (i * 8));
}
static inline int fpc_has_id(const struct fpc_inventory *set, uint32_t id)
{
    for (uint32_t i = 0; i < set->count; i++) if (set->ids[i] == id) return 1;
    return 0;
}
static inline int fpc_valid_ids(const struct fpc_inventory *set)
{
    if (set->count > FPC_GALLERY_MAX) return 0;
    for (uint32_t i = 0; i < set->count; i++) {
        if (!set->ids[i]) return 0;
        for (uint32_t j = 0; j < i; j++) if (set->ids[i] == set->ids[j]) return 0;
    }
    return 1;
}
static inline int fpc_same_ids(const struct fpc_inventory *a, const struct fpc_inventory *b)
{
    if (!fpc_valid_ids(a) || !fpc_valid_ids(b) || a->count != b->count) return 0;
    for (uint32_t i = 0; i < a->count; i++) if (!fpc_has_id(b, a->ids[i])) return 0;
    return 1;
}
#endif
