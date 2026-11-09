// SPDX-License-Identifier: GPL-2.0-only
/*
 * Preserve the bootloader-owned panel while its NVT touchscreen follows it.
 * Recovery only: no clocks, regulators, pinctrl, GPIO, DSI or reset operations.
 * The NVT follower uses its existing no-engineering-reset RAM firmware path.
 */
#include <linux/device.h>
#include <linux/err.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <drm/drm_connector.h>
#include <drm/drm_panel.h>

static bool bootloader_handoff;
module_param(bootloader_handoff, bool, 0400);
MODULE_PARM_DESC(bootloader_handoff, "Opt in after native Recovery checks the active framebuffer");

struct handoff_panel { struct drm_panel panel; };
static const struct drm_panel_funcs handoff_funcs = { };

static bool disabled(const char *path)
{
	struct device_node *np = of_find_node_by_path(path);
	bool ok = np && !of_device_is_available(np);
	of_node_put(np);
	return ok;
}

static bool liuqin_board(void)
{
	struct device_node *root = of_find_node_by_path("/");
	const char *model;
	u32 id[2];
	bool ok = root && !of_property_read_string(root, "model", &model) &&
		!strcmp(model, "Liuqin based on Qualcomm Technologies, Inc SM8475") &&
		!of_property_read_u32_array(root, "xiaomi,miboard-id", id, 2) &&
		id[0] == 0x10 && id[1] == 0;
	of_node_put(root);
	return ok;
}

static bool active_simplefb(void)
{
	struct device_node *np;
	bool ok = false;
	for_each_compatible_node(np, NULL, "simple-framebuffer") {
		struct platform_device *pdev = of_find_device_by_node(np);
		u32 w, h;
		if (pdev) {
			device_lock(&pdev->dev);
			ok = of_device_is_available(np) && pdev->dev.driver &&
				!strcmp(pdev->dev.driver->name, "simple-framebuffer") &&
				!of_property_read_u32(np, "width", &w) && w == 1800 &&
				!of_property_read_u32(np, "height", &h) && h == 2880;
			device_unlock(&pdev->dev);
			put_device(&pdev->dev);
		}
		if (ok) { of_node_put(np); break; }
	}
	return ok;
}

static int __init handoff_init(void)
{
	struct device_node *touch, *node;
	struct drm_panel *existing;
	struct handoff_panel *ctx;
	struct device *dev;
	int ret;

	if (!bootloader_handoff || !liuqin_board() ||
	    !disabled("/soc@0/display-subsystem@ae00000") ||
	    !disabled("/soc@0/clock-controller@af00000") || !active_simplefb())
		return -ENODEV;
	touch = of_find_node_by_path("/soc@0/geniqup@9c0000/spi@990000/touchscreen@0");
	/* Kernel DTs may name the QUP node after its unit address instead. */
	if (!touch)
		touch = of_find_compatible_node(NULL, NULL, "novatek,NVT-ts-spi");
	if (!touch || !of_device_is_available(touch)) {
		of_node_put(touch);
		return -ENODEV;
	}
	node = of_parse_phandle(touch, "panel", 0);
	of_node_put(touch);
	if (!node || !of_device_is_compatible(node, "xiaomi,pipa-nt36532")) {
		of_node_put(node);
		return -ENODEV;
	}
	existing = of_drm_find_panel(node);
	if (!IS_ERR(existing) || PTR_ERR(existing) != -EPROBE_DEFER) {
		of_node_put(node);
		return -EBUSY;
	}

	/* A root device has no bus driver or pinctrl binding. Assigning the
	 * real panel node to a platform driver would apply its reset pin state.
	 */
	dev = root_device_register("liuqin-bootloader-panel");
	if (IS_ERR(dev)) { of_node_put(node); return PTR_ERR(dev); }
	dev->of_node = node;
	ctx = devm_drm_panel_alloc(dev, struct handoff_panel, panel,
				   &handoff_funcs, DRM_MODE_CONNECTOR_DSI);
	if (IS_ERR(ctx)) {
		ret = PTR_ERR(ctx);
		dev->of_node = NULL;
		root_device_unregister(dev);
		of_node_put(node);
		return ret;
	}
	drm_panel_add(&ctx->panel);
	drm_panel_prepare(&ctx->panel);
	pr_info("liuqin-bootloader-panel: preserved scanout; panel follower ready\n");
	return 0;
}
module_init(handoff_init);
/* Deliberately resident until reboot: followers retain the panel and device.
 * There is no unload or panel power-down path in this RAM-only environment.
 */
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Liuqin RAM Recovery bootloader panel follower handoff");
