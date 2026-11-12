# Device work

Before using Fastboot to temporarily boot an existing Linux installation, read
`docs/BUILD.md`'s existing-system boot instructions. A build output is an
**unbound release image**, not a ready-to-boot test image for a multiboot install.

- Read the current `/etc/liuqin-boot-target` before rebooting. Preserve all of its
  fields, including `boot_generation`; do not substitute an older test's target.
- Use `shared/liuqin_boot_target.py` to bind a separate test image. Keep the
  release image unbound for the package updater.
- Immediately before `fastboot boot`, parse the exact image with
  `liuqin_artifacts.boot_interface()` and require its `boot_target` to equal the
  saved current target. Missing or mismatched binding must stop the command.
- After temporary-boot verification, return to the installed default kernel
  before planning a persistent update. The updater requires the running kernel
  and `boot_a` to match; do not bypass that check.
- Preserve the existing `liuqin_board_quirks()` helper for board-specific kernel
  changes. ABL can replace the root device-tree identifiers; matching only
  `xiaomi,liuqin` is insufficient.

Keep device logs and test records in ignored `out/` directories. Documentation
should describe behavior, sources and reproducible checks, not session records.
