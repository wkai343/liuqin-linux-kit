# Liuqin native Recovery — roles, provenance and validation

These images contain their own Linux kernel, initramfs, device tree, board
firmware and tools. They do not boot the installed Android userspace or load
its vendor kernel modules, and the native installer does not require the old
installer.img. Only Xiaomi Pad 6 Pro (liuqin) is supported.

Temporary and fixed images have distinct roles. The default temporary image
is loaded by Bootloader Fastboot; it must not be flashed into recovery_a,
recovery_b or boot. Fixed images use a dedicated boot/DTBO/AVB format and need
their own device/firmware qualification. Bootloader and firmware dependencies
remain even when the installed OS is Linux.

Storage starts read-only. Declared installer and maintenance capabilities
require a confirmed, boot-bound scope and verified inputs/backups before any
write. Full-ROM v2 may update ROM-declared A-slot firmware and Android Recovery;
qualified fixed-Recovery protection follows its separate compatibility checks.
Rootfs, boot switching and boot-store maintenance have independent validation
boundaries. Android data decryption is not implemented.

Build-time DEVICE_UNTESTED describes the initial artifact, not a statement
that every current artifact is inspection-only. Exact image SHA256, build ID,
STATUS.txt and matching external hardware evidence determine the tested scope.
Historical RNDIS prototypes, current ADB installers and fixed images must not
inherit one another's hardware validation. A successful temporary boot does
not itself qualify a fixed image or a full OS reinstall.

The LVGL interface uses bootloader simplefb, preserved display clocks and a
dedicated touch handoff. UI, USB, storage, power actions, installation and
recovery are tested separately. Desktop Fedora retains its own full DRM,
audio and OTG configuration.

The UI includes LVGL 9.6.0 and cJSON 1.7.19 under MIT licenses, and a generated
Noto Sans SC font subset under SIL OFL 1.1. Their notices are included in the
image under /usr/share/licenses/liuqin-recovery-ui and in the distribution.

The bootloader-panel handoff module is GPL-2.0-only. Its source and GPLv2 text
are included under device/recovery/touch in the distribution. It keeps panel
GPIO, pinctrl, power and display clocks untouched, and uses the existing NVT
follower RAM-firmware path without an engineering reset.

Current ADB engineering images expose USB ADB only, with no USB network, DHCP,
or Telnet service. This root ADB endpoint must only be used with a trusted
directly attached computer. Historical network images remain build/research references and are
not supported by the current GUI.

Kernel provenance is retained in the build records and native manifest.
Dedicated Recovery kernel builds also embed their exact config, local kernel
patch, ARM64 aligned-entry source and component hashes under
/usr/share/licenses/liuqin-recovery-kernel; desktop packages copy these records
to installer/runtime/kernel-build. The kernel patch applies to the exact
upstream commit recorded by kernel-manifest.json. New image digests require
their own hardware validation; a qualification for an older kernel is not
inherited.
Firmware provenance and redistribution restrictions are described in
device/firmware/NOTICE and device/firmware/firmware.manifest in the source
repository. The ARM64 tool runtime retains its upstream licenses. A public
redistribution requires a corresponding-source and firmware-license audit.
