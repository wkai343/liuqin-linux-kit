# 构建端

从仓库根目录通过 Devbox 构建 Fedora。配套的 boot 和 rootfs 默认保存在 `out/build/from-source/`。交付时用 `devbox run build -- --export-dir <新目录>` 导出本次构建。`devbox run outputs` 可查看已有缓存。配置及全部目标见[构建指南](../docs/BUILD.md)。

Devbox 会向底层脚本传入选定的源码快照和构建目录。直接调用时，须按脚本接口指定源码（`--source`、`KERNEL_DIR` 或 `KERNEL_SOURCE`）和对应构建目录。

原生 ADB Rec 使用独立的内核配置和构建入口。见 [Rec 构建与交付](../docs/NATIVE-RECOVERY.md#构建与交付)和[项目结构](../docs/STRUCTURE.md)。
