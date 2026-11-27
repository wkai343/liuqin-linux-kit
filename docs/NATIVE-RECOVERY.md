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

### USB 方向与速率

正式内核和 Rec 共用的板级设备树将 USB 控制器最高速率设为 SuperSpeed（5 Gbit/s），与
[小米平板 6 Pro 的 USB 3.2 Gen1 官方规格](https://www.mi.com/xiaomi-pad-6-pro/specs)
一致，并保留 USB 2 回退。ADB 启动脚本不另行设置 gadget 的 `max_speed`；即使该值
保留通用默认值，实际连接也受控制器上限约束。可读取
`/sys/class/udc/*/maximum_speed` 核对控制器上限，并用
`/sys/class/udc/*/state` 与 `current_speed` 核对实际枚举状态和连接速率。

Rec 保留连接器到 QMP PHY 的 SuperSpeed 设备树连接，通过电池服务读取
小米 `XM_CC_ORIENTATION`（属性 36），驱动 `orientation-switch` 与
`mode-switch`，让正反插头使用对应的高速通道。该属性来自
[原厂充电驱动](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c)。
普通 USB 插拔不保证产生 DP PAN 通知，因此不能只依赖 altmode 客户端。
方向查询与其他电池请求串行，USB 通知触发立即读取，另以 500 毫秒周期补查
迟到或遗漏的状态；读取失败和未知值不会改变通道。切换前通过控制器的
`NONE`/`DEVICE` 专用开关同步停止 gadget 并撤销 VBUS 有效状态，方向和 USB
通道就绪后再允许连接；拔线或电池服务离线也撤销连接。这个开关拒绝主机模式，
`dr_mode` 仍为 `peripheral`，UCSI 角色切换和 DisplayPort
显示路径不在 Rec 启动范围内。

方向缓存只代表最后完整成功的会话。断开、方向切换、通道切换或重新连接任一步骤失败，
都会保留“会话未完成”状态，后续有效读数触发完整重试；即使方向回到缓存值，也不能
跳过恢复。供电服务重新上线同样需要重新建立会话，未知方向和通信错误不触发盲目切换。

方向可从 `pmic_glink.power-supply.*/sm8475_raw/raw_xm_cc_orientation`
只读属性核验：0 表示拔线，1/2 分别表示 CC1/CC2。测试时每次翻面后保持插头
不动，分别确认 UDC 已配置、方向从 1 切到 2 或从 2 切到 1，两面的 `current_speed` 均为 `super-speed`，主机 USB 设备的 `speed` 均为 `5000`。测试覆盖插线启动、拔线冷启动后再插线，以及两个方向的数据往返哈希校验。USB 2 连通性单独记录。

这个上限也有原厂板级配置依据：锁定 ROM 的 Liuqin DTBO（entry 15）在 USB 节点设置
`qcom,force-gen1`，并在 DWC3 子节点设置 `maximum-speed = "super-speed"`。
[原厂驱动](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/dwc3/dwc3-msm-core.c)
将前者映射到 `LLUCTL.ForceGen1`（偏移 `0xd024`，bit 10）。通用 DWC3 驱动在没有
板级速率上限时依据控制器 `GHWPARAMS3` 选择默认能力；控制器支持 Gen2 并不证明
整机 PHY 配置也支持 Gen2。公共补丁 `0071` 使用主线支持的 `maximum-speed` 属性，
现有 DWC3 核心据此约束 gadget，并为此 DRD 控制器设置 `LLUCTL.ForceGen1`。
不添加主线驱动不识别的原厂私有属性，也不依赖 Rec 用户态单独限速。

排查训练阶段时应先核对 IP 类型：Liuqin 使用 DWC_usb31，其 `GDBGLTSSM` 位于
`0xd050`，不是旧 DWC_usb3 的 `0xc164`。原厂驱动同样区分这两个地址；通用
debugfs `regdump` 中名为 `GDBGLTSSM` 的条目若仍使用旧偏移，不能用于解码训练
子状态。`link_state` 从独立的 `DSTS` 获取，不受这个转储地址问题影响。枚举未完成时
不能只凭 `current_speed` 宣告回退成功，应同时确认 UDC 已配置、链路有效及主机已识别。

USB 状态区分输入供电与数据链路。启动时从 SDP 转入 PD 的过程中，PMIC 的 `online`
可能暂时为零，而已经枚举的数据链路仍保持连接；反过来，固定 peripheral 模式也可能
在拔线后保留 UDC 的 `configured` 状态。Rec 以只读方式挂载 debugfs，仅在供电读数
为零或未知、UDC 仍显示已枚举时，补读此内核的 DWC3 `link_state`：有效 USB 3 链路或
USB 2 活跃链路可以证明数据连接仍在，拔线后的 USB 2 `Suspend` 不能覆盖供电离线。
诊断接口不可读时保留供电与 UDC 判断，不猜测连接成功；等待枚举显示“连接中”。
这项显示判断不重置控制器，也不改变充电协议或 ADB 授权状态。

### 触控与维护界面

ADB Rec 的准备画面和触控初始化与 UFS 枚举、只读检查并行。准备阶段只使用 RAM 中
已经核验的程序和触控固件，不开放维护套接字。PID 1 完成最终存储只读校验、等待触控
准备结束后，发布绑定当前启动 ID 的结果，再开放维护界面及 ADB；触控失败可使用按键
和电脑控制，存储校验失败则保持维护入口关闭。界面在切入维护页时才枚举输入设备，
避免遗漏并行初始化中稍晚出现的触控节点。

天马屏的 Rec 触控驱动在固件下载后、开放中断前，要求连续约 1 秒没有 FD/FE 看门狗
数据；每次检查最多轮询 150 次，轮询间隔为 10～11 毫秒，另需 SPI 传输和调度时间。
持续异常只允许在本次初始化中重载一次固件，沿用无
工程复位的路径，保留屏幕和输入设备；SPI 错误、面板关闭或重试失败均停止初始化。
这是有次数上限的故障恢复，不代表首次固件异常的根因已经消除。板级判断使用
`liuqin_board_quirks()` 和 Rec 标记，桌面面板的恢复流程不变。
`touch_state` 中的 `rec_health_checked`、`rec_fw_retries` 和 `rec_wdt_polls` 分别记录
检查通过、固件重试及异常轮询次数；轮询次数不等同于中断帧数。用户态随后再观察
1 秒的实际中断运行状态，失败则报告触控不可用，不解绑或重新绑定驱动。

图形界面的电量由独立线程读取，未知时每 200 毫秒重试，有效后每 2 秒刷新，0% 也是
有效读数。读取不会阻塞界面，也不触发高频分区扫描。它仍需等待 ADSP 的 PMIC 通信
服务就绪；界面先显示未知电量，不能把首次读取失败当作电池耗尽。

启动后封存块设备。电脑确认计划绑定 boot ID、构建、分区、备份、材料摘要与当前引导，只开放必要目标，结束后恢复只读并回读核验。

运行镜像的能力清单决定是否提供分区编辑、Android ROM、多个 Linux rootfs、引导切换、固定 Rec 保护、BCB 清理及引导存储管理。更新 GUI 不会升级正在运行的 Rec。

平板端提供系统列表、默认引导切换、设备/分区/日志查看、日志导出、亮度和电源操作。系统安装及分区编辑由电脑 GUI 准备和确认，详见[界面](RECOVERY-LVGL.md)。

## Android data

当前不支持解密 Android data。安装材料由电脑传入，不需要读取原有加密数据；重装会初始化 userdata/metadata，个人文件需另行备份。

后续解密需要配套的文件系统、元数据加密、硬件封装密钥、KeyMint/Keymaster、Gatekeeper 和厂商服务，不能只按 Android 大版本判断。设计依据见 [AOSP FBE](https://source.android.com/docs/security/features/encryption/file-based) 和 [元数据加密](https://source.android.com/docs/security/features/encryption/metadata)。

## 构建与交付

默认 Rec 内核使用 kernel.org 的 **7.3.4**，复用 Fedora 来源锁中的公共补丁，另加 Rec 专用补丁和独立配置，保留 simplefb、手指触控、固定 USB peripheral、ADB 与存储封存路径。不获取原作者内核仓库，也不依赖旧 Rec 基础镜像。源码锁、从零构建及排查方法见[Rec 内核构建](../build/recovery/kernel/README.md)。

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
