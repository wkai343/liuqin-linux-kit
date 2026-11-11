# Rec 工具运行环境

原生 Rec 使用 **Buildroot + musl** 从源码交叉编译的 ARM64 工具集，不需要 Ubuntu Base、APT、systemd 或完整发行版 rootfs。来源与 SHA256 固定在 [buildroot-source.json](buildroot-source.json)，软件包版本和补丁由该 Buildroot 版本锁定；[rec.defconfig](rec.defconfig) 与 [busybox.config](busybox.config) 定义功能。

| 组件 | 用途 |
| --- | --- |
| 静态 BusyBox | initramfs 启动、shell、基础命令和 gzip |
| Python 3 标准库 | 安装协议、状态服务、存储保护、UI 后端及归档处理 |
| GNU tar、ACL、attr、libcap | 保留 rootfs 所有者、权限、ACL、扩展属性和 capabilities |
| sgdisk | GPT 检查及受限分区编辑 |
| e2fsprogs、Btrfs、F2FS 工具 | 现有文件系统创建与检查 |

本地 `patches/` 回移上游的 xattr ABI 兼容修复，并应用到 ACL 携带的同一实现，使 attr/ACL 可配合 musl 与当前锁定的头文件编译；保留原作者及 LGPL 许可，补丁纳入摘要与对应源码归档。

Rec 继续使用项目的 init、7.3.4 内核、静态 LVGL 和 adbd，分别构建。Buildroot 的 Linux headers 只用于编译用户态，不替换 Rec 内核。musl 运行库放在 `/lib`，加载器为 `/lib/ld-musl-aarch64.so.1`；静态 BusyBox 不依赖加载器。GNU coreutils 整包不进入镜像，`install` 使用 BusyBox 实现。

## 构建

在项目根目录使用 Devbox，普通用户执行：

```sh
devbox run -- python3 build/recovery/build-runtime.py \
  --work out/build/rec-runtime-work \
  --out out/build/rec-tools
```

构建入口仅完成目标工具树，不导出可迁移的 Buildroot SDK；保留宿主工具对 Nix store 的加载路径，避免 SDK 路径清理导致宿主编译器失效。

首次构建包括独立 musl 交叉工具链，耗时比增量构建长；源码缓存位于 `inputs/buildroot/`。`--work` 可继续同一配置的中断构建；修改来源或配置后使用新 work 目录。`--out` 必须是新目录，`--jobs` 控制编译并行度。

产物：

- `runtime/`：传给 `build-native.py --runtime` 的最小工具树及逐文件 SHA256；打包强制使用其中的 BusyBox。
- `runtime-manifest.json`：来源、配置、包清单和产物摘要。
- `runtime-sources.tar`：对应源码、Buildroot 原始发布包、构建配置、项目脚本和 `legal-info`。分发 runtime 时一并保留。

镜像内的 `/usr/share/licenses/liuqin-runtime/` 保存目标软件包许可证和版本清单。Buildroot 不负责本项目的内核、UI、ADB 和固件，它们的源码及许可材料单独保留。Fedora 桌面构建使用 Fedora 用户态；其 initramfs 与板级包使用独立的[源码 BusyBox 配方](../busybox/README.md)。Rec 继续使用本配方生成的 BusyBox。

## 验证与排查

```sh
devbox run -- python3 tests/android-runtime.py
devbox run -- python3 tests/rec-runtime.py \
  --runtime out/build/rec-tools/runtime \
  --report out/build/rec-tools/runtime-test.json
```

第一项检查 ELF 依赖提取。第二项执行真实 ARM64 二进制：通过用户、挂载、PID 命名空间和 QEMU 检查 Python/实际服务导入、BusyBox applets、GPT 与继承文件描述符、三种文件系统工具，以及 tar 的权限/ACL/xattrs/capabilities/软硬链接。只创建临时普通文件，不暴露主机块设备，不访问平板，也不更改全局 binfmt 注册。

若用户命名空间被宿主禁用，先在允许该功能的构建主机执行测试，不以跳过检查代替通过。下载失败检查网络和下载缓存；摘要不符时保留现场排查，不能关闭 Buildroot 的哈希校验。配置改变后不复用旧 work；缺失 applet、Python 模块或动态库应修正配方或提取器，不能从桌面 rootfs 手工拼装。

可选 `--baseline /path/to/previous-runtime` 用旧工具创建 ext4/Btrfs 普通文件镜像，再由新工具检查；旧目录仅用于兼容性对照，不是构建输入。

当前运行环境已通过上述 ARM64 检查及旧 ext4/Btrfs 默认格式的兼容性对照，已组装为 7.3.4 原生 Rec。工具目录约 36.5 MiB，包含许可证；对应迁移前工具目录连同 BusyBox 约 58.6 MiB。候选镜像及离线报告位于 `out/releases/liuqin-native-rec-7.3.4-buildroot/`，报告绑定镜像与 runtime 摘要。

该镜像已在一台天马屏设备上通过 Fastboot 临时启动：背光自动开启，用户确认画面和手指触控正常；ADB、运行时文件校验、GPT 只读读取及六个 UFS LUN、141 个块设备节点的只读检查通过。触控工程复位和看门狗计数为零，固件只加载一次；`boot_a`、`recovery_a` 摘要与此前基线一致。

验收记录为上述产物目录内的 `hardware-boot-validation.json`，绑定本次镜像 SHA256、构建 ID 和设备启动 ID。内嵌 `DEVICE_UNTESTED` 保留构建时状态，验收结果由配套记录表达，不改写镜像，也未替换默认发布镜像。真实安装写入、格式化、固定启动、长期运行和其他面板仍待验证。
