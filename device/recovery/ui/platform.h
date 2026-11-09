/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdbool.h>
#include "lvgl.h"
bool platform_display(bool preview);
void platform_inputs(lv_group_t *group);
void platform_focus(lv_group_t *group);
void platform_poll(void);
void platform_close(void);
bool platform_save(const char *path);
const char *platform_input_label(void);
const char *platform_usb_state(void);
void platform_test_key(uint32_t key);
