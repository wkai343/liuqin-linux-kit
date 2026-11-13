# 原生 Rec 内核

Rec 独立使用 kernel.org 源码和本仓库补丁，不再获取原作者的内核仓库，也不需要从旧 Recovery 镜像提取内核、配置或 DTB。桌面内核配方仍在 `build/kernel/source.json`。

默认配方为 [source.json](source.json)，版本为 **7.3.4**，与 Fedora 使用同一 kernel.org 提交和 `build/kernel/source.json` 中的 51 个公共补丁文件。独立的 [7.3.4.config](7.3.4.config) 从原 Rec 配置迁移；Rec 不使用桌面配置或启动参数。公共源码中的音频恢复改动不代表 Rec 开启了音频，也不替代当前源码配方的 Rec 实机验证。

CPU 功耗系数与两级 BWMON 共享桌面的设备树配置；Rec 将 `CONFIG_QCOM_ICC_BWMON` 内置，
不依赖未打包的带宽监测模块。新增配置在 Rec 侧仅完成源码、配置和 DTB 离线校验；
Fedora 的实机结果不代表新的 Rec 镜像已通过验收，详见[设备树对照](../../../docs/DEVICE-TREE.md)。

Rec 专用差异集中在 `patches/linux-7.3/`：内核补丁隔离 ABL 占位节点的供应依赖、固定 USB peripheral，并保留 PMIC 电池/充电服务；外部模块补丁仅应用到触控接管模块的构建副本，通知新版 NVT 驱动已有画面就绪。

维护环境继续使用启动器保留的 simplefb 画面、手指触控、ADB 和只读存储封存。DTB 禁用完整 DRM/GPU 接管、音频与笔输入，保留显示时钟/电源依赖和 ADSP 电池通信。触控服务在发布衔接面板之前设置 `nt36523_ts.liuqin_tddi_recovery=0`，沿用 Rec 的 ReK 就绪判断；simplefb 没有桌面 A3 校准恢复所需的面板回调。初始画面就绪检查和禁止工程复位仍保留。共享显示补丁不表示 Rec 启用了桌面的刷新率切换。

公共背光驱动在启动时按默认亮度点亮，不再提供 `kinetic,backlight-initially-off` 属性；Rec 无需额外的设备树覆盖。完整 DRM 面板的首帧时序由桌面路径独立管理。

内核、组件和基础镜像必须使用同一配方。固定启动、限定写入、完整重装和长期运行需分别验收。

设备适配保留原作者及后续贡献者的文件级作者、SPDX 和许可。见 [CREDITS](../../../CREDITS.md)。

## 验证范围

7.3.4 配合 Buildroot 用户态已在一台天马屏设备上通过 Fastboot 临时启动、背光自动开启、界面显示、菜单点按/滑动、ADB、触控固件初始化、电池读取，以及六个 UFS LUN、141 个块设备节点的只读检查。运行时文件校验和 GPT 只读读取通过；触控工程复位和看门狗计数为零，固件只加载一次；`boot_a` 和 `recovery_a` 摘要与此前基线一致。当前镜像与 `hardware-boot-validation.json` 位于 `out/releases/liuqin-native-rec-7.3.4-buildroot/`；记录绑定镜像摘要、构建 ID 和启动 ID，不能用于其他产物。

离线检查覆盖公共补丁绑定、组件身份、内置维护驱动、两种 USB 设备树结构、ARM64 对齐入口、触控策略及现有安装流程。尚未验证真实安装、格式化、固定刷入或长期运行，也未验收华星屏及磁吸键盘。启动日志中的音频 pinctrl、早期 QRTR 服务告警和一次键盘 I2C 取消超时保留在验证记录中，尚未专项处理。

## 构建

在项目根目录执行，所有输出使用新目录。Devbox 提供固定交叉工具链、DTC、CMake 等工具。准备本项目锁定的 OS2.0.6.0.VMYCNXM ROM：DTB 装配只读取其中 `vendor_boot.img` 和 `dtbo.img`，校验值见 `shared/liuqin-rom-images.json`。

```sh
devbox run -- python3 build/recovery/build-kernel.py \
  --out out/build/rec-kernel
```

输出包含 `assembled/source`、`kernel` 和 `components`。首次获取固定的 kernel.org 提交；已有匹配的官方源码缓存时可离线重建。可选 `--source-cache`、`--rom-images`、`--jobs`；`--configure-only` 只装配源码和检查配置，不产生可打包内核。

另外准备 [Buildroot + musl 工具 runtime](../../runtime/README.md)、板级固件目录、UI 和 adbd。板级目录使用项目从锁定 ROM 提取的 `firmware-pool`、`dtbs`、`dtbo`、`wlan` 和 `topology.bin`。这些输入不来自旧 Rec 镜像或设备内核仓库。

```sh
devbox run -- python3 build/recovery/build-runtime.py --out out/build/rec-tools
devbox run -- python3 build/recovery/build-ui.py --out out/build/rec-ui
devbox run -- python3 build/recovery/build-adbd.py --out out/build/rec-adbd

devbox run -- python3 build/recovery/build-native.py \
  --kernel-source out/build/rec-kernel/assembled/source \
  --kernel-out out/build/rec-kernel/kernel \
  --runtime out/build/rec-tools/runtime \
  --board /path/to/prepared-board \
  --ui-runtime out/build/rec-ui \
  --out out/build/rec-base

devbox run -- python3 build/recovery/build-native-installer.py \
  --base-image out/build/rec-base/recovery.img \
  --kernel-runtime out/build/rec-kernel/components \
  --ui-runtime out/build/rec-ui \
  --adbd-runtime out/build/rec-adbd \
  --out out/build/rec-installer
```

`rec-base` 是内部装配中间产物；临时启动使用最后的 `rec-installer/recovery.img`。固定 Rec 封装及设备资格仍按[原生 Recovery](../../../docs/NATIVE-RECOVERY.md)单独处理。


## 一致性与排查

源码锁固定官方提交、补丁顺序、配置、DTB 输入及最终 Git tree。`shared_kernel` 同时固定 Fedora 公共来源锁的摘要，并检查共同的提交和补丁顺序，防止两套配方静默分叉。公共来源升级后需同步迁移 Rec 配方和配置，再构建验证。构建检查完整源码树和工作区；只允许未启用的 Rust/BTF 工具探测元数据变化，其他 Kconfig 差异均拒绝。编译输出、组件和打包基础镜像分别绑定同一配方及 SHA256。

`kernel/recovery-build.json` 记录编译输出；`components/kernel-manifest.json` 记录组件和生成源码摘要。完整配置、Rec/公共来源锁、内核与触控模块补丁及对齐入口随最终镜像附在 `/usr/share/licenses/liuqin-recovery-kernel/`。硬件验证绑定最终镜像 SHA256，存放在配套记录中，不写入可复用的源码锁。

```sh
devbox run -- python3 tests/recovery-source.py
LIUQIN_RECOVERY_COMPONENTS=out/build/rec-kernel/components \
  devbox run -- python3 tests/recovery-source.py
```

设置 `LIUQIN_RECOVERY_BASELINE_DTB=/path/to/reference.dtb` 可逐项比较候选与参考 DTB；该参考仅供回归验收，不是构建依赖。ARM64 对齐入口仿真见 `tests/recovery-kernel-components.py`，需要 Unicorn。

哈希或源码树不符时检查本地补丁和来源锁；组件与基础镜像不匹配时使用同一次配方重建，不能修改清单绕过检查。触控排查查看 `/run/liuqin-native-touch.json`、对应日志及 `dmesg` 中的 NVT 固件状态；显示排查先确认 simplefb、保留的显示时钟和电源依赖，不能为消除 deferred-probe 日志随意裁剪设备树。

若启动日志短暂出现后黑屏，检查 `ktz8866-backlight/bl_power` 和 `raw_brightness`：正常点亮应为 `0` 和非零值。该驱动的 `brightness` / `actual_brightness` 只反映逻辑请求，非零不代表 LED 已开启；旧镜像还需核对是否携带初始关闭背光属性。
