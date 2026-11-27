# 项目结构

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [安装助手](INSTALLER-GUI.md)

构建与安装在同一仓库分目录维护；素材、缓存、成品和设备私有数据分别保存。

```text
build/
  scripts/          构建与素材准备入口
  lib/              镜像、归档和装配工具
  devbox/           主机环境、构建编排与配置示例
  kernel/           内核来源、配置与补丁
  rootfs/           Fedora 基础系统来源、软件配方与 overlay
  ffmpeg/           固定 Fedora SRPM、Iris V4L2 补丁和播放器默认设置
  firefox/          系统 FFmpeg 启动适配与已验证兼容名单
  obs-v4l2/         独立 OBS 硬件编码器、CMake 构建与本地化资源
  runtime/          Rec Buildroot/musl 来源、配置与说明
  recovery/         原生 Rec、ADB/LVGL、固定封装与离线启动链验证
  data/             工具与 ROM 版本数据
installer/          GUI、CLI、备份管理和源码打包
  liuqin_gui/       QML 界面、Qt 桥接、独立后端
  pyproject.toml    uv 环境配置，配合 uv.lock 和 .python-version
device/
  board-overlay/    公共板级服务、硬件策略与兼容组件
  installer/        安装、分区和校准导入脚本
  recovery/         原生 Rec 状态、UI 服务、受控写入与引导维护
  boot/             系统启动、initramfs、引导更新与恢复
  …                 固件及其他设备组件
shared/             不依赖 Qt 的安装包接口、布局与项目标识
docs/               中文文档
tests/              按被测功能分组的离线回归与设备测试
  fixtures/         固定测试数据
inputs/             本机素材、ROM 与下载缓存
sources/            外部源码检出
out/                中间文件、成品和报告
```

`inputs/`、`sources/`、`out/` 和本机 `build.json` 不提交 Git。
各测试目录的职责和运行命令见[测试说明](../tests/README.md#按功能查找)。
组件自身的 `tools/` 目录包含实际构建工具，仍需保留。

## 构建与安装的依赖

- [构建端](../build/README.md)生成配套的 boot/rootfs。[电脑安装器](../installer/README.md)使用独立 Rec，运行时无需编译器或 Devbox。
- 原生 Rec 的内核、DTB、ADB、LVGL 与运行载荷独立封装，默认镜像目录见下表。
- Fedora 构建器使用 [rootfs_common.py](../build/lib/rootfs_common.py) 装配用户态，再嵌入目标内核包及模块。[pack-liuqin-rootfs.py](../build/scripts/pack-liuqin-rootfs.py) 将已适配的根系统打包成发布文件。更换内核不必重新装配桌面。
- 构建端和安装器共用[数据格式与接口](../shared/README.md)，确认计划后由[设备侧安装脚本](../device/installer/README.md)执行。

各阶段命令及产物见[构建指南](BUILD.md#当前构建产物)。
内核升级包见 [内核更新](KERNEL-UPDATES.md)。

### 板级与桌面 overlay

`device/board-overlay` 保存睡眠、RTC、背光、CABC、显示恢复、启动方向、Wi-Fi MAC、
蓝牙、充电认证、音频、存储保护和救援等板级文件。显示恢复当前使用 KScreen 后端，
服务通过 `ConditionPathExists` 检查其是否可用。
默认系统构建 Fedora RPM。独立的 [DEB 设备包脚本](../build/scripts/build-liuqin-debs.sh)提供通用板级包，[内核打包工具](../build/scripts/build-liuqin-kernel-packages.py)也支持 DEB。仓库提供的桌面镜像配方使用 Fedora。
两种包格式通过 [device_overlay.py](../build/lib/device_overlay.py) 装配 `board-overlay`，
复制前检查已有目标文件与异常父路径。DEB 可用 `BOARD_OVERLAY` 指定来源；
Fedora 设备包缓存跟踪板级目录及装配代码，服务激活链接由打包入口生成。
旧 GNOME 设置面板、会话 overlay、Snap 与 flash-kernel 集成已移除。
`liuqin-storage-guard.service` 由 `basic.target` 强制启动，在桌面登录前复核内置 UFS 的读写状态。

## 输出与私有数据

| 位置 | 内容 |
| --- | --- |
| `out/build/from-source/` | 完成阶段缓存与 `.attempt-*` 失败目录 |
| `--export-dir` 指定的新目录 | 本次 Fedora 构建的 `boot.img`、`rootfs.tar.gz`、启动契约与校验文件 |
| `out/releases/native-recovery-flash/` | 当前默认临时 Rec 及匹配的清单/状态 |
| `out/releases/fixed-installers/<SHA256>/` | 设备绑定的固定安装版校验镜像与资格文件 |
| `out/reports/` | 构建报告 |
| `out/login/` | 仅预置账号模式生成的登录信息，权限 0600 |
| 安装器 state-dir | 安装运行记录；设备备份使用单独选择的目录 |

`python3 installer/package.py` 默认将安装器、公共模块、设备脚本和说明打包到 `out/releases/liuqin-linux-kit-installer-source.zip`。源码包排除构建环境、`installer/.venv`、系统镜像和设备备份。使用 `--native-recovery`、`--fixed-installer` 等选项时另附所选镜像、清单及相应的验收与许可材料。镜像声明见[原生 Rec 分发说明](../build/recovery/NATIVE-NOTICE.md)和[历史 TWRP 配套版说明](../build/recovery/NOTICE.md)。

原作者与第三方来源见 [CREDITS](../CREDITS.md)，原 LICENSE/NOTICE 及文件级许可保留。
