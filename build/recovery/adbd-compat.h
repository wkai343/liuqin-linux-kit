/* SPDX-License-Identifier: MIT */
/* AOSP's remaining C-linkage macros are not provided by musl. */
#if __has_include(<sys/cdefs.h>)
#include <sys/cdefs.h>
#endif
#ifndef __BEGIN_DECLS
#ifdef __cplusplus
#define __BEGIN_DECLS extern "C" {
#define __END_DECLS }
#else
#define __BEGIN_DECLS
#define __END_DECLS
#endif
#endif
