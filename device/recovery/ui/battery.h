/* SPDX-License-Identifier: MIT */
#pragma once
int recovery_battery_read(const char *directory);
unsigned recovery_battery_interval(int percent);
void recovery_battery_start(void);
int recovery_battery_percent(void);
void recovery_battery_stop(void);
