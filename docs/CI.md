# 工作流与发布

[项目首页](../README.md) ｜ [构建指南](BUILD.md)

所有工作流都只通过 **Actions → Run workflow** 手动触发，push 和 PR 不会自动启动。
构建使用与本地相同的 Devbox 入口。目标系统内核由 `build/kernel/source.json` 选择；
GUI 使用的原生 Rec 由 `build/recovery/` 独立构建。

| 工作流 | 执行环境 | 内容与产物 |
| --- | --- | --- |
| `checks.yml`：集成检查 | GitHub Ubuntu runner | 语法、文档、构建缓存、安装器与设备保护等离线回归 |
| `windows.yml`：Windows 安装器检查 | GitHub Windows runner | 主机兼容、worker、当前六组 QML 回归及安装器源码包 |
| `build.yml`：内核构建 | GitHub Ubuntu runner + Devbox | 默认目标 Image、DTB、模块、配置及来源记录 |
| `image.yml`：系统镜像构建 | 专用 Linux 自托管 runner，仅可信 `main` | 本次配套 boot/rootfs、内核标识和 SHA256SUMS |

离线和 QML 演示不连接平板；Windows CI 也不验证 USB 驱动或真机安装。
依赖阶段内核源码和 ROM 证明材料的显示测试单独运行，尚未纳入此工作流，见 [测试说明](../tests/README.md)。

## 目标内核

使用 `build/devbox/build.example.json` 的默认 Fedora 45 / Linux 7.3.4 配方，在 CI 中设置并行数为 2，
执行 `devbox run build -- --target kernel` 和 `devbox run build -- --target modules`，包含来源清单选定的键盘、指纹、显示和电池接口补丁。
不需要原厂 ROM，不构建 boot、rootfs 或安装镜像。
Devbox 使用固定版本的[官方安装 Action](https://github.com/jetify-com/devbox-install-action)，依赖由锁文件选择。

## 完整系统镜像

本地 rootfs 默认使用 Podman 用户命名空间，无需 sudo 密码。镜像 CI 工作流仍显式执行 sudo -n true 预检，因此当前自托管任务另要求 runner 的管理员策略满足该检查；不能把它当作本地构建的统一权限要求。任务还需要较多磁盘、ARM64 装配环境及 Devbox/Nix，
使用标签 `self-hosted`、`linux`、`X64`、`liuqin-images`。
该高权限任务只执行可信 `main`，不执行外部 PR；完整镜像 CI 需要专用 runner。

可设置仓库变量 `LIUQIN_BUILD_CONFIG` 为 runner 上配置文件的绝对路径，
否则使用仓库默认 Fedora 配方。工作流把配置复制到临时目录，并替换 `rom_images`，不改原文件。
下载 ROM 前先通过统一 CLI 校验配置，缺少系统配方或包含过时/未知字段时停止；构建只使用 `build` 和 `--target`，不接受旧别名。

ROM 从[指定的 cpidMi 页面](https://www.cpidmi.com/xiaomi-pad-6-pro-liuqin-os2-0-6-0-vmycnxm-15675)
对应镜像源获取，校验固定完整包后提取并核对四份镜像；来源锁为 `build/data/stock-rom-source.json`。
随后执行 `plan` 和 `build --export-dir`，从本次构建结果导出 `boot.img`、`rootfs.tar.gz`、
`native-root.contract` 和 `SHA256SUMS`。导出核对两个内核标识及完成记录中的文件摘要；
缺失、损坏或不配套时停止。目录包含本次 run ID 和重试编号，上传不扫描历史缓存，也不包含 installer.img。
仅上传这四个文件，不附带构建配置、独立登录信息、缓存目录或设备备份。原生 Rec 独立交付。

## 发布要求

Actions 产物是候选构建，不自动创建 Release 或标记硬件验收通过。
发布应注明源码、内核配置、基础系统来源与实际验收范围，并附所需许可证和对应源码。
新安装、内核更新和 Android 恢复分别验证；设备私有校准与备份不提交源码仓库。
