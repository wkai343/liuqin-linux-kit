# 发行版与桌面适配

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [硬件说明](HARDWARE.md)

默认使用 Fedora 44 ARM64 + KDE Plasma，保留 Ubuntu 26.04 Plasma 配方。
在 `build.json` 选择对应 `rootfs_config` 即可切换。实际设备结果统一记录在 [硬件说明](HARDWARE.md)。

## 共用与独立的部分

| 层 | 内容 |
| --- | --- |
| 板级适配 | SM8475 内核/DTB、固件、UFS 启动链及本机校准 |
| 安装接口 | 三文件格式、单根 ext4/Btrfs、分区检查、备份与引导回读 |
| 发行版用户态 | 官方 ARM64 基础系统、软件依赖、包格式、服务及登录流程 |
| 用户配置 | 内核片段、软件增减、overlay、首次设置或预置账号 |

独立 installer 使用官方 Ubuntu Base 的最小 runtime，不要求目标系统也是 Ubuntu。
rootfs 需要 systemd、设备服务和 `fastrpc` 账号/组，并携带所选 boot 的同版模块与内核元数据。
统一根接口允许桌面和普通设备服务独立更新，系统内引导更新的状态见 [内核更新](KERNEL-UPDATES.md)。

Fedora 使用 DNF/RPM、官方 Plasma Setup 和 Plasma Login；Ubuntu 使用 APT/DEB、项目打包的 Plasma Setup 和 SDDM。
两个配方均支持裁剪和 overlay，默认不安装 Snap。Plasma 使用通用设备包，不需要原作者的 GNOME 根文件系统。
通用设备包从 `device/board-overlay` 装配；Ubuntu GNOME 模式再叠加 `device/gnome-overlay`。
GNOME 的 dconf、按键和 Snapshot 集成只进入 GNOME 包，两层边界与兼容组件见[项目结构](STRUCTURE.md#板级与桌面-overlay)。

## 引入其他发行版

1. 固定官方 ARM64 基础系统来源，增加软件包装配方式。
2. 移植设备依赖、账号、服务、固件位置及桌面登录配置。
3. 装入同版模块和内核元数据，生成配套 boot，复用独立安装镜像。
4. 适配内核包与更新工具，验证启动、恢复和 [硬件功能](HARDWARE.md)。

内核、用户态与设备组件可分别开发，首次安装仍须组合为接口相容的三个文件。
原项目及第三方署名见 [CREDITS](../CREDITS.md)，历史双系统组件不属于当前安装流程。
