# 来源、作者与许可证声明

## 本项目维护

当前衍生项目：[wkai343/liuqin-linux-kit](https://github.com/wkai343/liuqin-linux-kit)。
本项目的构建、安装和发行版适配改动不替代上游作者与第三方的权利声明。

## 原项目

Liuqin Linux Kit 衍生自 **yzddmr6** 的[xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline)。原项目提供了小米平板 6 Pro 的 Linux 适配、参考系统、构建和安装基础。

## 内核与设备适配

本项目将已有的 Liuqin 适配迁移到 Linux 7.3 系列，沿用 yzddmr6 的设备内核和 SM8450 主线社区的工作。Fedora 和原生 Rec 都从 kernel.org 的 Linux stable `v7.3.4` 构建，该版本基于 Torvalds 主线 `v7.3`。Rec 在公共补丁上增加维护专用配置和补丁。两套内核的来源锁分别是 `build/kernel/source.json` 和 `build/recovery/kernel/source.json`。[原设备内核](https://github.com/yzddmr6/linux-sm8450-liuqin) · [SM8450 主线](https://github.com/sm8450-mainline/linux)

本机硬件行为对照小米公开源码和 M81 量产 ROM `OS2.0.6.0.VMYCNXM`。镜像校验值见 `shared/liuqin-rom-images.json`，相应补丁和来源声明记录原厂依据。Linux 适配沿用所修改文件的许可，保留原作者、SPDX 和评审记录。[来源声明](NOTICE)

- **面板与触控**的命令、PHY 和背光对照小米量产资料。天马和华星面板的四档在线切换还参考了 `lineage-24.0-20261022-UNOFFICIAL-liuqin.zip` 中的 DTBO 和显示模块。主屏标识与笔坐标精度分别对照量产 DTBO 和 `nt36532_touch.ko`，关屏时的 RESX 低电平处理和五路电源切换也参考了 yzddmr6 的实现及首次唤醒实测。[四档切换与 ROM 校验值](https://github.com/wkai343/liuqin-linux-kit/blob/main/build/kernel/patches/linux-7.3/0024-liuqin-refresh-modes-and-live-switching.patch) · [笔输入与模块校验值](https://github.com/wkai343/liuqin-linux-kit/blob/main/build/kernel/patches/linux-7.3/0031-liuqin-pen-input-and-diagnostics.patch) · [RESX 修复](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/ecd6482c15e6777c1f5a14fd1099c7017bdf6325) · [五路启停与首次唤醒](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/4ffc6807f5093ec5567ecf625ef9b44b97c4af50)
- **显示时钟与双路 DSI**参考了 yzddmr6 对 MDP 恢复和链路启钟失败的处理。DPU 及扬声器 I²C 的双域请求也参考了其 Cape 适配。在线切换时的时序双缓冲与主从更新顺序参考 Qualcomm 显示驱动，面板时序与扫描命令对照小米 ROM。[MDP 恢复](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/8ec7416f90f6058acf343aacef33d1761bcde913) · [DSI 错误处理](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/9bc411d03020c9db63b445a3985b92f5988b4d41) · [Cape 双域供电](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/40d6ee58b96ec499b1c3f1eea3e88d26141063a1) · [Qualcomm 显示驱动](https://android.googlesource.com/kernel/msm-extra/display-drivers/+/e7ab2aa8bd2f6f27b311ccada3722d20dee4bcef/msm/dsi/dsi_display.c)

  Marijn Suijten 与 Dmitry Baryshkov 在双路 DSI/DSC 的讨论中说明了共享 CTL 如何以一次提交更新两路接口。该工作用于本项目的 `build/kernel/patches/linux-7.3/0064-drm-msm-dpu-single-flush-bonded-video.patch`。[双路 DSI/DSC 讨论](https://lore.kernel.org/all/mrg3m7awzvozhyg2zwwbawfa6cb6xo7obzooiq3iph6mu3ybfc@b5h2pzxvcjvf/)
- **Iris 视频编解码**的平台布局参考 Seeker0472 的 `0006-liuqin-media-iris.patch`，本项目按新版固件描述接口调整。原文件保留 Qualcomm、Linaro 等作者与许可。[Seeker0472 的 SM8450 适配](https://github.com/Seeker0472/liuqin-nixos/tree/73e8071e09880aeb47f0a427b9a65b8cc10927c4)
- **磁吸键盘**的 Caps Lock 管理命令和背折输入屏蔽参考 Seeker0472 同版的 `0002-liuqin-input-hid-touchscreen.patch`、`docs/PORTING-NOTES.md` 和 GPIO23 低电平实测。本项目将这些改动接入 MCU 连接事件和睡眠流程，单独管理 GPIO，处理按键与触点释放。背折防抖对照小米公开设备树。[Seeker0472 的键盘适配](https://github.com/Seeker0472/liuqin-nixos/tree/73e8071e09880aeb47f0a427b9a65b8cc10927c4) · [小米 Liuqin 设备树](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/liuqin-sm8475.dtsi)

  NanoSic 键盘连接状态协议参考小米公开内核 `pipa-t-oss` 的 `drivers/input/keyboard/nanosic_driver_v2/nano_i2c.c`，提交 `77dc8e2290f3f5f15861821564f68d30ea193449`。原作者为 Nanosic Inc. / Bin.yuan，许可为 GPL-2.0。`build/kernel/patches/` 中的这项适配仅修改主线驱动，保留其作者与 GPL 声明。
- **CPU 与互连**使用小米 Cape 设备树中的 CPU 容量、动态功耗系数和 CPU/LLCC 带宽监测参数。这些数值与量产 ROM 核对后，通过 OPP、能耗模型和 interconnect 绑定接入 Linux。[小米 Cape 设备树](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/cape.dtsi) · [设备树对照](docs/DEVICE-TREE.md)
- **Adreno GPU** 启动 GMU 前屏蔽 HFI 中断的实现参考了 Armada 的修复。[Adreno GMU](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/b9e43fbe6b2392c41fe810554bdd8ac2bd0be562)
- **UFS 驱动**对 lane 符号时钟的管理参考了 yzddmr6 的修复。[UFS 时钟](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/df0b681a2594c063773dfaf37becdef43e753bdb)
- **蓝牙**串口挂起时的中断处理参考 yzddmr6 的 GENI UART 修复。Birk Skyum 和 Vladyslav Tymoshchuk 定位了关机命令重新启动 IBS 定时器的问题，并给出了修复。[GENI UART](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/59413493369901ffc7bae4b2be707e2b0d511673) · [BlueZ 原补丁](https://github.com/bluez/bluetooth-next/pull/937/commits/23ed62819eacbb91d806d11e9fec87d1c2a5fb4c) · [Liuqin 分支移植](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/e4a305e114378f4b39cf217288909b2f9f10a3b3)
- **WCN6855 无线供电**参考了 Seeker0472 的同机型射频实测。其固定电压提案用于方案对照。本项目的蓝牙和 Wi-Fi 各自按小米驱动的启动和保持流程请求电压。[原始实测与提案](https://github.com/yzddmr6/linux-sm8450-liuqin/pull/9/commits/5e0586c001affdf6cc3dd1890894d6ba956632de) · [分支提交](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/188e803672a1afca58454730f16c2256c09f857c)
- **音频与 DSP**方面，录音前端启动和 LPASS 时钟顺序参考了 yzddmr6 的适配。FastRPC 在系统冻结期间保留用户调用的实现来自 slwyts。[PCM 录音启动](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/ecc36e1df9a9d93cd6e115c9161540aef47511fe) · [LPASS 时钟](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/cbfdce9aec69e700d8561765e96be7596800afe4) · [FastRPC 休眠](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/9c741c403a4b07357d4ad2848fa0b1bc2f99927a)
- **充电器分类**参考 yzddmr6 的 QC 类型上报和 KUnit 协议测试。M81 的类型取值范围按量产充电模块核定。[QC 类型上报](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/c25f056e0c20e7a9fd0632598909568005fdfc56) · [KUnit 用例](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/4775c39c10196fbee2fa0780c8accd4bb0c1a7a8)

## 第三方组件

[NOTICE](NOTICE) 保留传感器、充电、AOSP、BusyBox、指纹和固件等来源声明。
第三方目录及其许可证保留；例如 QSEE supplicant、QCBOR、hexagonrpc、libssc、
iio-sensor-proxy 以及 xiaomi-mipps-auth 的原作者与条款。

`hexagonrpcd` 的长请求分段读取由 Fady 实现，yzddmr6 将长度检查和协议测试移植到 Liuqin。该移植还引用了 Fady 对错误应答长度的修正。[分段读取](https://github.com/linux-msm/hexagonrpc/commit/f0dfd5095d3b7defc3b368f227337bfc6d6aef42) · [Liuqin 移植与测试](https://github.com/yzddmr6/xiaomipad-6pro-mainline/commit/64c5ac577ac75b8b1575fe960d361e3e496cb3b4) · [错误应答修正](https://github.com/linux-msm/hexagonrpc/commit/40e50412812adce9dddc2be9a4510ebce05eef89)

关机充电的认证流程参考 yzddmr6 的集成，也对照过其早期温控方案。小米量产系统通过两条 init 规则分别启动认证和温控，本项目沿用这一安排。[关机充电认证](https://github.com/yzddmr6/xiaomipad-6pro-mainline/commit/7c208813bcbe013806e0edf37677b1a3c908c886) · [早期温控实现](https://github.com/yzddmr6/xiaomipad-6pro-mainline/commit/b089200f0c96723dbdd736ffbe0698eebe9f9f18)

CS35L41 扬声器保护程序和通用调音来自 Xiaomi 的 OS2.0.6.0.VMYCNXM 原厂 ROM，
权利归 Cirrus Logic、Xiaomi 及其授权方。`build/lib/speaker_firmware.py` 记录提取路径、
主线驱动文件名和 SHA256；文件名转换不改变其许可，亦不包含每台设备的工厂校准数据。

默认 Fedora 镜像中的 FPC 指纹 trusted application 从同版原厂 ROM 的 `NON-HLOS.bin`
提取，固定 SHA256 与来源声明见 `device/fingerprint/oem/firmware/`。构建保留原厂字节，
不包含个人指纹状态；固件权利归原权利人，不由项目 MIT 许可替代。
指纹 libfprint TOD 与 fprintd 直接使用上游归档，固定记录见 `device/fingerprint/oem/SOURCE_LOCK.json`。

原生 Rec 工具运行环境由 [Buildroot](https://buildroot.org/) 构建，使用 musl、BusyBox、Python、GNU tar、e2fsprogs、Btrfs、F2FS、GPT fdisk 等上游项目。固定来源与配置见 `build/runtime/`，包版本和许可证随镜像保留，对应源码及构建材料保存在 `runtime-sources.tar`；各组件继续按自身许可分发。
Fedora initramfs 与板级包的 BusyBox 从 [BusyBox 上游](https://busybox.net/) 源码构建，版本、配置和对应源码材料见 `build/busybox/`。

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

视频用户态基于 Fedora 打包的 FFmpeg 与上游 V4L2 实现，固定 SRPM、补丁和来源见
`build/ffmpeg/README.md`。保留 Fedora 的拆包与编解码器配置以及上游
文件的作者和许可；镜像携带修订版 SRPM、配方与构建记录。
`build/obs-v4l2/` 插件使用 OBS Project 的模块、编码器和标准封装接口，
插件及 C 包适配测试按 GPL-2.0-or-later 分发，并随 RPM 保留对应源码。
官方 OBS 与 Firefox 包不被替换；`build/firefox/` 启动适配使用
Bubblewrap 私有挂载视图，兼容判断参考 Mozilla 的媒体模块接口。各组件继续遵守自身许可。

排查期间的 KDE [KWin](https://invent.kde.org/plasma/kwin) 补丁与回归材料保留在
`device/kwin/`，固定研究源码见其中的 `source.json`。保留 Marco Martin、Harald Sitter、
Xaver Hugl 等上游作者、SPDX 与文件许可；默认系统继续使用 Fedora 官方 KWin 软件包。
第三方许可不由本项目 MIT 许可替代。

根目录 [LICENSE](LICENSE) 保持原许可文本与版权行。该文本不替代文件级或第三方许可证，
也不把固件或 Linux 内核改为 MIT。发行时应同时携带本文件、LICENSE 和 NOTICE，
以及所分发组件各自需要的声明与对应源码材料。

项目名称调整、文档精简和宣传素材移除不改变上述来源及权利归属。
