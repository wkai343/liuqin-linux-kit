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

默认 Linux 7.3 基于 Torvalds 主线 `v7.3`，来源锁见 `build/kernel/source.json`；旧版来源保留在 `source-6.17.json`。
Iris SM8450 平台适配参考 [Seeker0472/liuqin-nixos](https://github.com/Seeker0472/liuqin-nixos/tree/73e8071e09880aeb47f0a427b9a65b8cc10927c4)
的 `0006-liuqin-media-iris.patch`，按新版固件描述接口调整；原文件中的 Qualcomm、Linaro 等作者与许可保留。
Caps Lock 指示灯的原厂管理命令参考同一提交的 `0002-liuqin-input-hid-touchscreen.patch`
和 `docs/PORTING-NOTES.md`，结合本项目的连接状态与睡眠生命周期接入。
背折检测也参考该提交实测的 GPIO23 低电平语义和驱动侧输入屏蔽，结合本项目的
MCU 连接事件、独立 GPIO 所有权及按键/触点释放处理实现。
其余设备适配由本项目现有内核迁移，未直接替换为该项目的整套补丁。

显示时钟的上游修正保留补丁作者、评审与来源链接；面板命令、PHY、背光和电池接口的
本机对照使用 OS2.0.6.0.VMYCNXM 原厂资料，版本与镜像校验锁见 `shared/liuqin-rom-images.json`。
这些适配补丁继续遵循所修改内核文件的许可，不由项目根目录的 MIT 许可替代。

## 第三方组件

[NOTICE](NOTICE) 保留传感器、充电、GNOME、AOSP、BusyBox、指纹和固件等来源声明。
第三方目录及其许可证保留；例如 QSEE supplicant、QCBOR、hexagonrpc、libssc、
iio-sensor-proxy 以及 xiaomi-mipps-auth 的原作者与条款。

CS35L41 扬声器保护程序和通用调音来自 Xiaomi 的 OS2.0.6.0.VMYCNXM 原厂 ROM，
权利归 Cirrus Logic、Xiaomi 及其授权方。`build/lib/speaker_firmware.py` 记录提取路径、
主线驱动文件名和 SHA256；文件名转换不改变其许可，亦不包含每台设备的工厂校准数据。

默认 Fedora 镜像中的 FPC 指纹 trusted application 从同版原厂 ROM 的 `NON-HLOS.bin`
提取，固定 SHA256 与来源声明见 `device/fingerprint/oem/firmware/`。构建保留原厂字节，
不包含个人指纹状态；固件权利归原权利人，不由项目 MIT 许可替代。

Ubuntu 登录前初始化使用 KDE 的 [Plasma Setup](https://invent.kde.org/plasma/plasma-setup)，
包括 Kristen McWilliam、Carl Schwan、Devin Lin 等贡献者；固定来源见
`build/rootfs/plasma-setup-source.json`。构建时保留上游文件的作者和 SPDX 声明，
生成的软件包携带上游许可证全文。本项目的完成状态保护与失败账号回退补丁位于
`build/rootfs/plasma-setup/`，不替代上游文件的许可。

Fedora 基础系统来自 Fedora Project 的官方 ARM64 Container Base，固定来源、SHA256 和签名指纹
见 `build/rootfs/fedora-source.json`。Fedora 初始化直接使用 Fedora 官方 `plasma-setup` 软件包，
由发行版维护 KDE Plasma Setup 的打包与更新；原作者、SPDX 和许可证随官方包保留，
本项目不再分发 Fedora 向导的自编译版本或账号创建补丁。

Fedora 的 GPU 使用率监测使用 KDE 的 [KSystemStats](https://invent.kde.org/plasma/ksystemstats)，
包括 Arjen Hiemstra、David Edmundson、David Redondo 等贡献者。固定源码见
`build/rootfs/fedora-stats-source.json`；本项目的 Adreno 平台传感器与补丁位于
`device/ksystemstats/`。原有 AMD、Intel、NVIDIA 后端及作者声明保留，软件包携带
上游许可证、本项目适配源码和对应来源记录；这些组件按各自文件的 GPL 等声明分发。

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
