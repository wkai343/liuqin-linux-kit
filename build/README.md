# 构建端

统一入口为根目录的 `devbox.json`，从 [构建指南](../docs/BUILD.md) 开始。
默认构建官方 Fedora ARM64 + Plasma，Ubuntu 使用独立配方。

`scripts/` 放构建入口，`lib/` 放装配工具，`devbox/` 放环境与编排，
`kernel/`、`rootfs/` 放配置和来源，`data/` 放工具与 ROM 版本数据。
公共接口来自 `shared/`，不依赖 Qt 安装器，也不访问平板。

缓存位于 `out/build/`，成品位于 `out/releases/`；见 [项目结构](../docs/STRUCTURE.md)。
