/* SPDX-License-Identifier: MIT */
/* Reproduce the 32/64-bit mount ID ABI used by systemd >= 260. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#ifndef AT_HANDLE_MNT_ID_UNIQUE
#define AT_HANDLE_MNT_ID_UNIQUE 0x001
#endif
#ifndef AT_HANDLE_FID
#define AT_HANDLE_FID 0x200
#endif
int main(void) {
    struct file_handle *handle=calloc(1,sizeof(*handle)+128);
    uint64_t mount_id=0;
    if(!handle) return 2;
    handle->handle_bytes=128;
    int result=name_to_handle_at(AT_FDCWD,".",handle,(int *)&mount_id,
                                 AT_HANDLE_FID|AT_HANDLE_MNT_ID_UNIQUE);
    int error=result<0?errno:0;
    printf("return=%d errno=%d unique_id_nonzero=%d\n",result,error,mount_id!=0);
    free(handle);
    if (result < 0 && (error == EINVAL || error == EOPNOTSUPP || error == ENOSYS || error == EPERM)) return 0;
    return result<0?1:0;
}
