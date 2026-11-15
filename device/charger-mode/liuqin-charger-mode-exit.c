// SPDX-License-Identifier: MIT
/* Explicit stage-1 exits; no init/systemd process is running in charger mode. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/reboot.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	const char *reason;
	int command;

	if (argc != 2 || (strcmp(argv[1], "normal") && strcmp(argv[1], "bootloader") &&
			 strcmp(argv[1], "poweroff"))) {
		fprintf(stderr, "usage: %s normal|bootloader|poweroff\n", argv[0]);
		return 64;
	}
	if (geteuid() != 0) {
		fprintf(stderr, "liuqin-charger-mode-exit: root is required\n");
		return 2;
	}
	reason = argv[1];
	command = !strcmp(reason, "poweroff") ? LINUX_REBOOT_CMD_POWER_OFF :
		LINUX_REBOOT_CMD_RESTART2;
	sync();
	if (syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
		    command, command == LINUX_REBOOT_CMD_POWER_OFF ? NULL : reason) < 0) {
		fprintf(stderr, "liuqin-charger-mode-exit: %s failed: %s\n",
			reason, strerror(errno));
		return 1;
	}
	return 0;
}
