# Historical paired TWRP Recovery — engineering build

This notice describes the retained stock-kernel/TWRP compatibility backend,
not the current native ADB installer. See NATIVE-NOTICE.md for native images.

This is a modified binary distribution of the liuqin TWRP release dated
2025-02-09, not a complete TWRP source build. Device tree and build workflow:
https://github.com/ymdzq/device_xiaomi_liuqin-TWRP/tree/3502543342b335a495fabe5efb6c583792cae12c
Release: https://github.com/ymdzq/device_xiaomi_liuqin-TWRP/releases/tag/13226064562
TWRP source: https://github.com/TeamWin/android_bootable_recovery

The kernel is extracted without modification from Xiaomi's pinned
OS2.0.6.0.VMYCNXM boot.img. Xiaomi kernel sources:
https://github.com/MiCode/Xiaomi_Kernel_OpenSource
The exact upstream source/binary correspondence and redistribution licenses
must be audited before a public release. This local engineering artifact is
not represented as a complete corresponding-source offer.

The private tool runtime is built from source with Buildroot and musl
(Python, GNU tar, e2fsprogs, gdisk, F2FS tools and dependencies),
under their respective licenses. This historical paired backend separately
retains its pinned distribution-provided static BusyBox; the current native
Rec uses the BusyBox built with its Buildroot runtime. Tool hashes and input provenance are in recovery-manifest.json and
the runtime build manifests. Keep original package copyrights and obtain
matching sources before redistributing this engineering artifact publicly.

The new gateway, runtime preparation, builder and desktop integration are
licensed under this project's MIT license. Magisk add-on packages have been
removed. The image inherits upstream root ADB; use a trusted USB host.

Status: DEVICE_UNTESTED. Boot, touch, USB, storage and recovery rollback require
validation on a bootloader-unlocked Xiaomi Pad 6 Pro (liuqin). Inserting a stock
kernel does not remove the dependency on compatible vendor_boot/DTBO firmware.

This is a temporary-boot-only image. Do not flash it to recovery_a or recovery_b. This paired backend does not qualify native fixed images; fixed roles and device-specific hardware evidence are handled separately.
