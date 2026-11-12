# 原生 Recovery

[GUI 使用](INSTALLER-GUI.md) ｜ [安装后端与 ADB](NATIVE-RECOVERY-INSTALLER.md) ｜ [LVGL 界面](RECOVERY-LVGL.md) ｜ [固件匹配](RECOVERY-FIRMWARE-PROFILES.md)

原生 Rec 是 liuqin 的独立安装与维护环境，自带 Linux 内核、DTB、RAM 文件系统、LVGL、触屏衔接模块、ADB 和安装工具。不启动 Android init/framework，也不使用已安装系统的 boot 内核。

它仍依赖 Bootloader、ABL、DTBO、AVB、底层固件和分区布局。不同 ROM 的启动条件按实际固件检查，只支持 liuqin。

## 镜像角色

| 角色 | 用途与边界 |
| --- | --- |
| 临时安装版 | 从 Bootloader Fastboot 用 `fastboot boot` 加载，启动时存储只读；确认计划后可安装和维护，不能直接刷入 Recovery 分区 |
| 固定安装版 | 使用独立固定封装及 DTBO/AVB 元数据；支持 A 槽固定、冷启动与回刷，按设备和固件匹配资格 |
| 离线候选 | 用于构建与启动链检查，不能仅凭构建成功用于刷写 |

镜像 SHA256、构建 ID、能力及资格由内嵌清单和分发包配套文件核对。临时版使用 `hardware-boot-validation.json`，固定版使用 `hardware-qualification.json`；这些文件用于将镜像绑定到设备和测试状态。通用 GUI 没有任意 Rec 固定刷入按钮。

## 运行与维护

GUI 连接后先识别设备，不自动重启。点击读取分区时，从已解锁的 Bootloader Fastboot 临时启动配套镜像，或连接已运行且能按本地镜像核验载荷的 Rec。

```sh
uv run --project installer installer/install-gui.py \
  --recovery-image /path/to/recovery.img
```

显示使用 Bootloader simplefb 和保留的时钟，触屏通过专用面板状态衔接加载 RAM 固件。USB 使用 [ADB](NATIVE-RECOVERY-INSTALLER.md#adb-连接与传输)，不创建 USB 网卡；USB 已连接不代表已建立 ADB 会话。桌面 Fedora 使用自己的 DRM、音频与 OTG 配置。

启动后封存块设备。电脑确认计划绑定 boot ID、构建、分区、备份、材料摘要与当前引导，只开放必要目标，结束后恢复只读并回读核验。

运行镜像的能力清单决定是否提供分区编辑、Android ROM、多个 Linux rootfs、引导切换、固定 Rec 保护、BCB 清理及引导存储管理。更新 GUI 不会升级正在运行的 Rec。

平板端提供系统列表、默认引导切换、设备/分区/日志查看、日志导出、亮度和电源操作。系统安装及分区编辑由电脑 GUI 准备和确认，详见[界面](RECOVERY-LVGL.md)。

## Android data

当前不支持解密 Android data。安装材料由电脑传入，不需要读取原有加密数据；重装会初始化 userdata/metadata，个人文件需另行备份。

后续解密需要配套的文件系统、元数据加密、硬件封装密钥、KeyMint/Keymaster、Gatekeeper 和厂商服务，不能只按 Android 大版本判断。设计依据见 [AOSP FBE](https://source.android.com/docs/security/features/encryption/file-based) 和 [元数据加密](https://source.android.com/docs/security/features/encryption/metadata)。

## 构建与交付

默认 Rec 内核使用 kernel.org 的 **7.3.4**，复用 Fedora 的 31 个公共补丁，另加 Rec 专用补丁和独立配置，保留 simplefb、手指触控、固定 USB peripheral、ADB 与存储封存路径。不获取原作者内核仓库，也不依赖旧 Rec 基础镜像。源码锁、从零构建及排查方法见[Rec 内核构建](../build/recovery/kernel/README.md)。

Rec 工具集采用 [Buildroot + musl](../build/runtime/README.md)，包含静态 BusyBox、Python、GNU tar 和 GPT/ext4/Btrfs/F2FS 工具；不需要 Ubuntu Base 或 APT。内核、LVGL 和 adbd 仍分别构建。

基础镜像由 [build-native.py](../build/recovery/build-native.py) 和内部 RAM 装配器生成；当前 ADB 安装版由 [build-native-installer.py](../build/recovery/build-native-installer.py) 组合与同一源码配方匹配的基础镜像、维护内核、UI 和 adbd。

```sh
python3 build/recovery/build-native-installer.py \
  --base-image /path/to/native-base/recovery.img \
  --kernel-runtime /path/to/kernel-components \
  --ui-runtime /path/to/arm64-ui-runtime \
  --adbd-runtime /path/to/arm64-adbd-runtime \
  --out out/build/native-recovery-new

python3 installer/package.py \
  --native-recovery out/build/native-recovery-new/recovery.img \
  --adbd-sources /path/to/adbd-source.tar.xz \
  --out out/releases/native-installer.zip
```

可选 `--qualified-fixed` 绑定与设备和镜像匹配的固定资格；打包器用 `--fixed-installer` 附带连接固定版所需的校验镜像，不自动部署。固定封装及启动检查见[固件匹配](RECOVERY-FIRMWARE-PROFILES.md#固定启动条件)。

新镜像分别测试临时启动、显示/触控/USB、存储保护、限定写入及恢复；固定版另测固定启动、冷启动与回刷。源码构建和打包不访问平板。
