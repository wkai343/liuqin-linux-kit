# 工作流与发布

[项目首页](../README.md) ｜ [构建指南](BUILD.md)

所有工作流都只通过 **Actions → Run workflow** 手动触发，push 和 PR 不会自动启动。
构建使用与本地相同的 Devbox 入口，内核来源由 `build/kernel/source.json` 选择。

| 工作流 | 执行环境 | 内容与产物 |
| --- | --- | --- |
| `checks.yml`：集成检查 | GitHub Ubuntu runner | 语法、文档、构建缓存、安装器与设备保护等离线回归 |
| `windows.yml`：Windows 安装器检查 | GitHub Windows runner | 主机兼容、PowerShell 语法、worker、QML 演示及安装器源码包 |
| `build.yml`：内核构建 | GitHub Ubuntu runner + Devbox | 默认目标 Image、DTB、模块、配置及来源记录 |
| `image.yml`：系统镜像构建 | 专用 Linux 自托管 runner，仅可信 `main` | 三个安装文件 |

离线和 QML 演示不连接平板；Windows CI 也不验证 USB 驱动或真机安装。

## 目标内核

使用 `build/devbox/build.example.json` 的默认 Fedora 配方，在 CI 中设置并行数为 2，
执行 `devbox run kernel` 和 `devbox run modules`，包含默认目标配置及键盘/指纹补丁。
不需要原厂 ROM，不构建 boot、rootfs 或安装镜像。
Devbox 使用固定版本的[官方安装 Action](https://github.com/jetify-com/devbox-install-action)，依赖由锁文件选择。

## 完整系统镜像

任务需要较多磁盘空间、ARM64 chroot 和非交互 sudo；runner 预装 Devbox/Nix，
使用标签 `self-hosted`、`linux`、`X64`、`liuqin-images`。
该高权限任务只执行可信 `main`，不执行外部 PR；完整镜像 CI 尚未在专用 runner 上验收。

可设置仓库变量 `LIUQIN_BUILD_CONFIG` 为 runner 上配置文件的绝对路径，
否则使用仓库默认 Fedora 配方。工作流把配置复制到临时目录，并替换 `rom_images`，不改原文件。

ROM 从[指定的 cpidMi 页面](https://www.cpidmi.com/xiaomi-pad-6-pro-liuqin-os2-0-6-0-vmycnxm-15675)
对应镜像源获取，校验固定完整包后提取并核对四份镜像；来源锁为 `build/data/stock-rom-source.json`。
随后执行 `plan` 和 `build`，上传 `out/releases/*-plasma-*/` 中的三个安装文件。
报告、本机配置、凭据、缓存和设备备份不上传。

## 发布要求

Actions 产物是候选构建，不自动创建 Release 或标记硬件验收通过。
发布应注明源码、内核配置、基础系统来源与实际验收范围，并附所需许可证和对应源码。
新安装、内核更新和 Android 恢复分别验证；设备私有校准与备份不提交源码仓库。
