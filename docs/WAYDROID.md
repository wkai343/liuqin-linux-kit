# Waydroid 实验范围

[项目首页](../README.md) ｜ [内核构建](BUILD.md)

默认内核配置保留 Waydroid 所需的 Binder IPC、binderfs、策略路由、PSI 和相关网络特性，
见 `device/configs/liuqin-waydroid.config`。默认 rootfs 不预装 Waydroid，
当前 Fedora 系统的容器启动、联网、图形和外设尚未完成真机验收。

## 配置与验证

Waydroid 使用宿主 Linux 内核承载 Android 用户空间。除 Binder 外，需核对网络 namespace、
cgroup、veth 和 IPv4/IPv6 策略路由；Android netd 缺少这些配置时可能无法设置路由或 DNS。

自定义内核可通过配置片段调整相关功能，按 [构建指南](BUILD.md) 重建匹配的 Image、模块和 boot。
不要只换 Image 而继续使用另一个版本的模块，也不要为普通目标配置改写安装器的固定基线校验值。

软件安装、容器镜像下载和网络配置应使用对应发行版的当前方案。
本项目没有已经验收的 Fedora 自动安装教程，也不把上游 Ubuntu 的 APT 命令作为 Fedora 步骤。

## 限制

内核配置齐备不等于完整 Waydroid 支持。默认 Android 容器镜像、Google 服务、图形加速、
相机、麦克风、传感器与休眠恢复均需分别验证，不沿用原厂 Android 的功能承诺。
