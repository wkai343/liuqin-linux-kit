// SPDX-License-Identifier: GPL-2.0-only
#include <linux/module.h>
#include <linux/of.h>
#include "overlay-blob.h"
static int overlay_id;
static int __init start(void)
{
 return of_overlay_fdt_apply(overlay_blob, sizeof(overlay_blob), &overlay_id, NULL);
}
static void __exit stop(void)
{
 of_overlay_remove(&overlay_id);
}
module_init(start);
module_exit(stop);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Temporary ICP device resources for bringup");
