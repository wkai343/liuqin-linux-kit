# 项目结构

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [安装助手](INSTALLER-GUI.md)

构建与安装在同一仓库分目录维护；素材、缓存、成品和设备私有数据分别保存。

```text
build/
  scripts/          构建与素材准备入口
  lib/              镜像、归档和装配工具
  devbox/           主机环境、构建编排与配置示例
  kernel/           内核来源、配置与补丁
  rootfs/           官方基础系统来源、软件配方与 overlay
  recovery/         原生 Rec、ADB/LVGL、固定封装与离线启动链验证
  data/             工具与 ROM 版本数据
installer/          GUI、CLI、备份管理和源码打包
  liuqin_gui/       QML 界面、Qt 桥接、独立后端
  pyproject.toml    uv 环境配置，配合 uv.lock 和 .python-version
device/
  board-overlay/    公共板级服务、硬件策略与兼容组件
  gnome-overlay/    GNOME 的 dconf、按键策略、设置面板与 Snapshot 集成
  installer/        安装、分区和校准导入脚本
  recovery/         原生 Rec 状态、UI 服务、受控写入与引导维护
  boot/             系统启动、initramfs、引导更新与恢复
  …                 固件及其他设备组件
shared/             不依赖 Qt 的安装包接口、布局与项目标识
docs/               中文文档
tests/              离线测试和回归验证
inputs/             本机素材、ROM 与下载缓存
sources/            外部源码检出
out/                中间文件、成品和报告
```

`inputs/`、`sources/`、`out/` 和本机 `build.json` 不提交 Git。
组件自身的 `tools/` 目录包含实际构建工具，仍需保留。

## 构建与安装的依赖

- 当前目标系统构建生成配套 boot/rootfs，GUI 使用独立 Rec；安装器不调用编译器或 Devbox。
- 原生 Rec 的内核、DTB、ADB、LVGL 与运行载荷独立封装；默认目录为 out/releases/native-recovery-flash/。
- rootfs 先装配用户态，再嵌入目标内核包及模块。更换内核不必重新装配桌面。
- `shared/` 由两端共用；`device/installer/` 提供经过确认后在平板执行的操作。

各阶段命令及产物见[构建指南](BUILD.md#当前构建产物)。
内核升级包见 [内核更新](KERNEL-UPDATES.md)。

### 板级与桌面 overlay

`device/board-overlay` 保存睡眠、RTC、背光、CABC、显示恢复、启动方向、Wi-Fi MAC、
蓝牙、充电认证、音频、存储保护和救援等板级文件。显示恢复当前使用 KScreen 后端，
服务通过 `ConditionPathExists` 检查其是否可用。
`device/gnome-overlay` 只保存 GNOME 会话集成，包括 dconf/GDM、GNOME 按键动作、
电源设置 schema 和 Snapshot 启动入口。

| 打包入口 | 读取的 overlay |
| --- | --- |
| Fedora RPM | `board-overlay`，通用配置 |
| Ubuntu Plasma DEB（`DEVICE_DESKTOP=generic`） | `board-overlay`，通用配置 |
| Ubuntu GNOME DEB（`DEVICE_DESKTOP=gnome`） | `board-overlay` 与 `gnome-overlay` |

两种包格式通过 [device_overlay.py](../build/lib/device_overlay.py) 装配文件，
同一安装路径出现在两层或出现文件/目录冲突时，会在复制前报错；已有目标文件和异常父路径也会被拒绝。
历史 Ubuntu 的 Snap 权限检查、flash-kernel、
GNOME 包所需的旧根标记、Snap 与 flash-kernel 集成保留在板级目录；通用包排除这些组件。
已有的 apt 更新屏蔽链接保持未打包状态，服务激活链接由相应打包入口处理。

DEB 可分别用 `BOARD_OVERLAY` 和 `GNOME_OVERLAY` 指定两层来源；通用模式不要求 GNOME 层存在。
Fedora 和 Ubuntu Plasma 的设备包缓存跟踪板级目录及装配代码，GNOME 配置变更不影响通用包缓存。
服务文件名不决定桌面类型，例如 `liuqin-gnome-storage-guard` 也用于通用设备包。

## 输出与私有数据

| 位置 | 内容 |
| --- | --- |
| `out/build/from-source/` | 完成阶段缓存与 `.attempt-*` 失败目录 |
| `out/releases/native-recovery-flash/` | 当前默认临时 Rec 及匹配的清单/状态 |
| `out/releases/fixed-installers/<SHA256>/` | 设备绑定的固定安装版校验镜像与资格文件 |
| `out/reports/` | 构建报告 |
| `out/login/` | 仅预置账号模式生成的登录信息，权限 0600 |
| 安装器 state-dir | 安装运行记录；设备备份使用单独选择的目录 |

`python3 installer/package.py` 输出 `out/releases/liuqin-linux-kit-installer-source.zip`，
包含安装器、公共模块、设备安装脚本和必要说明；默认不包含构建环境、镜像或设备备份；传入 --native-recovery、--fixed-installer 等参数时，仅嵌入指定镜像、对应清单、验收及许可材料。
安装器的 uv 环境位于忽略的 `installer/.venv`，不会进入源码包。

原作者与第三方来源见 [CREDITS](../CREDITS.md)，原 LICENSE/NOTICE 及文件级许可保留。
