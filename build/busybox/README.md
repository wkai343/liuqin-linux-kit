# 静态 ARM64 BusyBox

Fedora initramfs 和板级包使用 BusyBox 上游源码，通过 Devbox 的 ARM64 musl 编译器构建，不下载发行版二进制包。`source.json` 固定版本与 SHA256；`busybox.config` 基于 Buildroot 2026.08.1 的 BusyBox 配置及项目 Rec 片段，与已验证 Rec 的 BusyBox 1.38.0 功能配置一致，保留静态链接及启动、存储保护、压缩、模块和 USB 所需功能。配置来源保留 BusyBox/Buildroot 原许可。

默认 Fedora 构建自动执行此阶段，缓存绑定源码、配置、构建脚本和编译器身份；boot 缓存也绑定 BusyBox 摘要。独立构建使用新目录：

```sh
devbox run -- python3 build/scripts/build-busybox.py --out out/build/busybox-arm64
devbox run -- python3 tests/busybox-runtime.py --busybox out/build/busybox-arm64/busybox
```

产物包括 `busybox`、实际 `.config`、来源和摘要清单、许可证及 `busybox-sources.tar`。分发二进制时一并保留对应源码材料。手工调用 initramfs 构建器时显式设置 `BUSYBOX`，不会回退到旧下载缓存。

Rec 仍由 Buildroot 构建自己的 BusyBox；本配方不改变 Rec runtime，也不改变 Fedora 桌面的 libc。
