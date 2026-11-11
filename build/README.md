# 构建端

统一入口为根目录的 `devbox.json`，从 [构建指南](../docs/BUILD.md) 开始。
默认构建官方 Fedora 45 ARM64 + Plasma，目标内核为 Linux 7.3.4。`build` 构建配套 boot/rootfs；原生 ADB Rec 在 `recovery/` 独立构建。

`scripts/` 放构建入口，`lib/` 放装配工具，`devbox/` 放环境与编排，
`kernel/`、`rootfs/` 放当前内核与 Fedora 配方，`runtime/` 放独立 ARM64 工具环境来源，`recovery/` 放原生 Rec、ADB/LVGL、固定封装和启动链验证，`data/` 放 ROM 版本数据。
公共接口来自 `shared/`，不依赖 Qt 安装器，也不访问平板。

直接调用底层脚本时，应显式传入源码路径（`--source`、`KERNEL_DIR` 或 `KERNEL_SOURCE`）及对应构建目录。
Devbox 会传入选定的源码快照和构建目录。

缓存位于 `out/build/`，成品位于 `out/releases/`；见 [项目结构](../docs/STRUCTURE.md)。
