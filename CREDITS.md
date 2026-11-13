# 来源、作者与许可证声明

## 本项目维护

当前衍生项目：[wkai343/liuqin-linux-kit](https://github.com/wkai343/liuqin-linux-kit)。
本项目的构建、安装和发行版适配改动不替代上游作者与第三方的权利声明。

## 原项目

Liuqin Linux Kit 衍生自 **yzddmr6** 的[xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline)。原项目提供了小米平板 6 Pro 的 Linux 适配、参考系统、构建和安装基础。

## 内核与设备适配

- 原设备内核：[yzddmr6/linux-sm8450-liuqin](https://github.com/yzddmr6/linux-sm8450-liuqin)。
- 相关主线基础：[sm8450-mainline/linux](https://github.com/sm8450-mainline/linux)。
- 本项目使用的确切来源与提交记录在 `build/kernel/source.json`。
- 文件中的 SPDX、Copyright、作者说明及补丁来源继续有效。

默认 Linux 7.3.4 基于 kernel.org stable `v7.3.4`，其 7.3 基线来自 Torvalds 主线 `v7.3`；来源锁见 `build/kernel/source.json`。
原生 Rec 默认复用 kernel.org `v7.3.4` 及 Fedora 公共补丁，另加维护专用配置和补丁；来源锁见
`build/recovery/kernel/source.json`。设备适配延续原作者和后续贡献者的实现，保留文件级作者与许可；
原作者仓库用于来源追溯，当前桌面和 Rec 构建均从 kernel.org 获取源码。
Iris SM8450 平台适配参考 [Seeker0472/liuqin-nixos](https://github.com/Seeker0472/liuqin-nixos/tree/73e8071e09880aeb47f0a427b9a65b8cc10927c4)
的 `0006-liuqin-media-iris.patch`，按新版固件描述接口调整；原文件中的 Qualcomm、Linaro 等作者与许可保留。
Caps Lock 指示灯的原厂管理命令参考同一提交的 `0002-liuqin-input-hid-touchscreen.patch`
和 `docs/PORTING-NOTES.md`，结合本项目的连接状态与睡眠生命周期接入。
背折检测也参考该提交实测的 GPIO23 低电平语义和驱动侧输入屏蔽，结合本项目的
MCU 连接事件、独立 GPIO 所有权及按键/触点释放处理实现。
其余设备适配由本项目现有内核迁移，未直接替换为该项目的整套补丁。

显示时钟的上游修正保留补丁作者、评审与来源链接；面板命令、PHY、背光和电池接口的
本机对照使用 OS2.0.6.0.VMYCNXM 原厂资料，版本与镜像校验锁见 `shared/liuqin-rom-images.json`。
天马和华星 M81 的四档在线切换还参考了 `lineage-24.0-20261022-UNOFFICIAL-liuqin.zip`
中的 DTBO 与显示模块。ROM 校验值见[四档在线切换补丁](https://github.com/wkai343/liuqin-linux-kit/blob/main/build/kernel/patches/linux-7.3/0024-liuqin-refresh-modes-and-live-switching.patch)。
主屏标识和笔坐标精度分别对照同版原厂 ROM 的 DTBO 和 `nt36532_touch.ko`，模块校验值见[笔输入与诊断补丁](https://github.com/wkai343/liuqin-linux-kit/blob/main/build/kernel/patches/linux-7.3/0031-liuqin-pen-input-and-diagnostics.patch)。
磁吸键盘背折防抖对照了[小米公开的 liuqin 设备树](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/liuqin-sm8475.dtsi)。
CPU 容量、动态功耗系数及 CPU/LLCC 带宽监测参数参考同一提交的
[`qcom/cape.dtsi`](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/cape.dtsi)，
并与固定 OS2.0.6.0 原厂 ROM 核对，按主线 OPP、能耗模型及 interconnect 绑定表达；
具体移植范围和验证边界见[设备树对照](docs/DEVICE-TREE.md)。
四档在线刷新率切换的时序双缓冲与主从更新顺序还参考了 [Qualcomm display-drivers](https://android.googlesource.com/kernel/msm-extra/display-drivers/+/e7ab2aa8bd2f6f27b311ccada3722d20dee4bcef/msm/dsi/dsi_display.c)，面板时序与扫描命令继续按固定 M81 ROM 核对。
这些适配补丁继续遵循所修改内核文件的许可，不由项目根目录的 MIT 许可替代。

## 第三方组件

[NOTICE](NOTICE) 保留传感器、充电、AOSP、BusyBox、指纹和固件等来源声明。
第三方目录及其许可证保留；例如 QSEE supplicant、QCBOR、hexagonrpc、libssc、
iio-sensor-proxy 以及 xiaomi-mipps-auth 的原作者与条款。

CS35L41 扬声器保护程序和通用调音来自 Xiaomi 的 OS2.0.6.0.VMYCNXM 原厂 ROM，
权利归 Cirrus Logic、Xiaomi 及其授权方。`build/lib/speaker_firmware.py` 记录提取路径、
主线驱动文件名和 SHA256；文件名转换不改变其许可，亦不包含每台设备的工厂校准数据。

默认 Fedora 镜像中的 FPC 指纹 trusted application 从同版原厂 ROM 的 `NON-HLOS.bin`
提取，固定 SHA256 与来源声明见 `device/fingerprint/oem/firmware/`。构建保留原厂字节，
不包含个人指纹状态；固件权利归原权利人，不由项目 MIT 许可替代。

原生 Rec 工具运行环境由 [Buildroot](https://buildroot.org/) 构建，使用 musl、BusyBox、Python、GNU tar、e2fsprogs、Btrfs、F2FS、GPT fdisk 等上游项目。固定来源与配置见 `build/runtime/`，包版本和许可证随镜像保留，对应源码及构建材料保存在 `runtime-sources.tar`；各组件继续按自身许可分发。

Fedora 基础系统来自 Fedora Project 的官方 ARM64 Container Base，固定来源、SHA256 和签名指纹
见 `build/rootfs/fedora-source.json`。Fedora 初始化直接使用 Fedora 官方 `plasma-setup` 软件包，
由发行版维护 KDE Plasma Setup 的打包与更新，包括 Kristen McWilliam、Carl Schwan、Devin Lin 等贡献者的工作；原作者、SPDX 和许可证随官方包保留，
本项目不再分发 Fedora 向导的自编译版本或账号创建补丁。

Fedora 的 GPU 使用率监测使用 KDE 的 [KSystemStats](https://invent.kde.org/plasma/ksystemstats)，
包括 Arjen Hiemstra、David Edmundson、David Redondo 等贡献者。守护进程与其他统计插件使用 Fedora 官方包，
本项目仅编译并独立分发带 Adreno 适配的 GPU 插件。固定源码见
`build/rootfs/fedora-stats-source.json`；本项目的 Adreno 平台传感器与补丁位于
`device/ksystemstats/`。原有 AMD、Intel、NVIDIA 后端及作者声明保留，软件包携带
上游许可证、带补丁的对应源码归档、本项目适配源码、构建脚本和对应来源记录；
这些组件按各自文件的 GPL 等声明分发。

Rusticl 的 SPIR-V 内建函数使用 Karol Herbst 维护的
[Mesa libclc](https://gitlab.freedesktop.org/karolherbst/mesa-libclc) 分支，源自 LLVM libclc。
固定来源与官方发布文件摘要见 `build/rootfs/mesa-libclc-source.json`。软件包保留对应源码归档、
`LICENSE.TXT`、`CREDITS.TXT` 与作者声明，遵循上游 Apache-2.0 WITH LLVM-exception、NCSA、MIT
等文件级许可。Khronos [OpenCL-CTS](https://github.com/KhronosGroup/OpenCL-CTS) 用于设备兼容性验证；
测试源码和日志保存在忽略的 `out/` 目录，不作为项目自有实现或认证声明。

NanoSic 键盘连接状态适配保留主线驱动作者与 GPL 声明；协议参考 Xiaomi 开源内核
`pipa-t-oss` 的 `drivers/input/keyboard/nanosic_driver_v2/nano_i2c.c`，
提交 `77dc8e2290f3f5f15861821564f68d30ea193449`，原作者 Nanosic Inc. / Bin.yuan，GPL-2.0。
项目补丁位于 `build/kernel/patches/`，不包含固件修改。

排查期间的 KDE [KWin](https://invent.kde.org/plasma/kwin) 补丁与回归材料保留在
`device/kwin/`，固定研究源码见其中的 `source.json`。保留 Marco Martin、Harald Sitter、
Xaver Hugl 等上游作者、SPDX 与文件许可；默认系统继续使用 Fedora 官方 KWin 软件包。
第三方许可不由本项目 MIT 许可替代。

根目录 [LICENSE](LICENSE) 保持原许可文本与版权行。该文本不替代文件级或第三方许可证，
也不把固件或 Linux 内核改为 MIT。发行时应同时携带本文件、LICENSE 和 NOTICE，
以及所分发组件各自需要的声明与对应源码材料。

项目名称调整、文档精简和宣传素材移除不改变上述来源及权利归属。

Fedora initramfs 与板级包的 BusyBox 从 [BusyBox 上游](https://busybox.net/) 源码构建，版本、配置和对应源码材料见 `build/busybox/`。指纹 libfprint TOD 与 fprintd 直接使用上游归档，固定记录见 `device/fingerprint/oem/SOURCE_LOCK.json`。
