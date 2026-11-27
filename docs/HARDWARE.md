# 内核适配总清单

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [测试说明](../tests/README.md)

目标设备为 Xiaomi Pad 6 Pro（liuqin，SM8475）。本清单记录内核实现、已有验证和剩余工作；
固件与用户态依赖单列。实现以 [source.json](../build/kernel/source.json)、
[内核配置](../build/kernel/configs/linux-7.3-preserve.config)和其中的补丁为准。
板级设备树位于最终源码的 `arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts`，并继承 SoC/PMIC 配置。
CPU、空闲、供电与带宽的原厂对照及补齐项见[设备树对照](DEVICE-TREE.md)。
表中驱动名用于定位源码；详细操作、日志和构建身份保存在所链接的专题及测试报告中。

## 状态口径

- **已实现**：已接入当前配方；“仅配置启用”表示尚未确认设备运行状态，“未启用/待核实”单独标注。
- **已验证**：注明实机范围或离线检查。进入桌面、配置启用或上游支持，不等于所有路径通过验收。
- **待解决**是已知故障或功能缺口；**待验证**是缺少证据，不能据此判断功能失效。

以下实机结论只覆盖所述设备与场景。显示验证限一台天马屏；华星、其他内存容量及硬件批次不能沿用该结论。

## 平台基础

| 项目 / 内核入口 | 实现状态 | 已验证范围 | 待解决 / 待验证 |
| --- | --- | --- | --- |
| 引导与板型识别：ABL、DTB、`liuqin.h` | 已实现装载对齐、板型校验与 ABL 元数据处理 | 7.3.4 临时启动、默认 A 槽写入回读及普通重启到桌面；更新流程见[内核更新](KERNEL-UPDATES.md) | 待验证不同固件与批次 |
| CPU：SMP、PSCI、`cpufeature` | 已接入；限制已知 SpecSEI 差异的兼容范围 | 正常启动；兼容边界离线检查 | 待验证：各核压力及长期稳定性 |
| CPU 调频/空闲：`qcom-cpufreq-hw`、`cpuidle-psci` | 三组硬件调频域已接入；补齐原厂功耗系数，PSCI 状态与原厂一致 | 7.3.4 临时启动验证三个能耗域、57 个频点、schedutil 频率变化、8 核深度空闲及簇驻留计数，见[设备树对照](DEVICE-TREE.md) | 待验证：整机功耗、长时间空闲与睡眠恢复；20 秒采样中未观察到新的 APSS/LLCC 深度状态进入 |
| 时钟、供电、复位、引脚与中断 | 已接入 GCC/DISPCC/CAMCC、RPMh、PMIC、pinctrl 等板级资源 | 随已验证外设覆盖部分链路 | 待验证：资源释放、延迟探测及错误回退；保留未使用时钟/电源域的启动策略 |
| SMMU 与互连带宽：ARM SMMU、SM8450 interconnect | 已接入显示、GPU、相机等依赖；补齐 CPU/LLCC 两级 BWMON | 两级 BWMON 自动绑定；内存负载使中断与带宽请求上升，结束后回落；显示和相机覆盖部分既有链路 | 待验证：并发 DMA、长期负载、睡眠恢复、IOMMU 故障恢复；原厂 memlat/L3 策略尚未移植 |
| 内存与预留区 | 已接入；保留必要的固件内存区域 | 8 GB 设备约 6.8 GiB 可用，基础读写与启动 | 待验证：其他容量；SMEM DDR 未知字段仍待核实，MPSS 预留回收未通过 |
| UFS / GPT：`ufs-qcom`、分区解析 | 限定条件下兼容原厂 LUN 3/5 备份头，批量复查节点只读状态 | ext4/Btrfs 安装与启动、快速就绪及非根分区只读保护。短时连续只读 I/O 期间三个符号时钟均启用，结束后回到空闲。见[存储保护](BUILD.md#启动与设备适配) | 长时读写负载、I/O 跨休眠及错误恢复待验证 |
| DSP 通信：Q6 remoteproc、GLINK、SMEM、SMP2P、FastRPC | ADSP/CDSP/SLPI 节点及固件已接入；音频服务恢复时保留时钟提供设备、恢复资源投票并按依赖顺序重建硬件状态 | 音频、充电、加速度计链路实机；临时内核下受控整 ADSP 停启后，播放及四路采集自动恢复，随后再次 s2idle 通过 | 待验证：CDSP 独立任务、任意固件崩溃与长时并发负载；固件拒绝的音频 PD 单独重启未验证 |
| 安全调用：SCM、`tee/qseecom` | 已接入指纹所需 TA/RPMB 通路 | 配套指纹栈的录入与认证实机 | 待验证：系统重启、休眠及 Android 往返共存 |
| NVMEM：QFPROM、SPMI SDAM | 驱动配置已启用；RTC 无持久偏移单元 | 未做独立验收 | 待核实：本机单元、消费者及读取结果 |
| 加密与随机数 | ARM64 CE、高通 RNG 已配置；QCE/ICE 适配状态待核实 | 未做独立验收 | 待验证：实际硬件后端、磁盘加密通路及吞吐 |
| 故障记录：watchdog、pstore/ramoops | 驱动配置及 ramoops 区域已接入 | 有运行日志。Rec 经 Fastboot 热重启后未读回 pstore 测试标记 | 待验证看门狗恢复及崩溃日志跨重启保存 |

M81 的原厂 UFS 节点列出 TX0、RX0 和 RX1 三个符号时钟。小米驱动按 lane 管理这些时钟，
允许双 lane 配置缺少 TX1，并记录时钟是否已经使能。Linux 7.3.4 的通用层还会读取
`freq-table-hz` 对应的时钟。补丁 `0101` 让已识别的 M81 由高通驱动单独管理这些 lane 时钟，
使控制器反复启用、门控和恢复时的引用数配对。[小米 UFS 驱动](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/scsi/ufs/ufs-qcom.c#L307-L409) · [UFS 补丁](../build/kernel/patches/linux-7.3/0101-liuqin-ufs-lane-clock-ownership.patch)

小米 FastRPC 驱动会找回中断前保存的用户调用上下文，继续等待同一笔 DSP 请求。
补丁 `0103` 在主线驱动中让用户等待点接受系统冻结，解冻后接续等待。普通信号仍可中断用户调用，
内核调用沿用 10 秒超时。[小米 FastRPC 驱动](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/char/adsprpc.c#L3385-L3399) · [FastRPC 补丁](../build/kernel/patches/linux-7.3/0103-fastrpc-freeze-user-wait.patch)

## 显示与输入

| 项目 / 内核入口 | 实现状态 | 已验证范围 | 待解决 / 待验证 |
| --- | --- | --- | --- |
| GPU：DRM MSM / Adreno 730、GMU、devfreq | 按原厂补齐 220–900 MHz 共 13 档 GX/CX 等级与 ACD；恢复 A730 图像地址寄存器；Liuqin A730 采用 5000 ms 挂起检查周期，见 [GPU](GPU.md) | 临时启动及默认内核普通重启；13 档切换、900 MHz 负载、自动调频与空闲休眠、GLES 回读、Vulkan、s2idle 后计算恢复；长计算与受控卡死恢复；原厂表、GMU CX/HFI 和寄存器列表有离线检查 | 软件超时恢复可能更慢；实际高温限频、长期压力、其他 GPU 故障及其他批次待验证 |
| OpenCL：Fedora Mesa Rusticl / libclc | 保留官方 Mesa RPM，关闭 early preamble 规避跨步复制错误；配套 Mesa libclc SPIR-V 包消除兼容性警告 | 图像、矩形复制和双向跨步复制定向 CTS 通过；5000 ms 运行时策略下完整 `full_3d_explicit_local` 单项通过；修复内核的定向验证范围见 [专题](GPU.md#当前方案与验证范围) | 完整 CTS 尚未完成；跨步复制错误的底层根因、长期应用兼容性及发行版升级后的 libclc 配套仍需验证 |
| 内屏：NT36532、双 DSI、DSC | 已实现 2880 × 1800；天马/华星默认四档在线切换，首选 120 Hz；关闭前清理旧帧请求，共享 CTL 的正常帧只提交一次 | 天马四档全部 12 个方向、画面与手指正常；旧/新/旧内核对照验证重复提交修复，临时与默认启动绘图、s2idle 和 DPMS 短测未复现提交超时，帧计数与刷新寄存器正常 | 待验证：华星全部路径、长期切换、60 Hz 首次点亮/熄屏恢复；30/48/50 Hz 仅诊断，见[显示](DISPLAY.md) |
| 面板供电与复位 | NT36532 按 M81 量产顺序启停五路电源，KTZ8866 负责背光和偏压编程 | 天马屏灭亮与 s2idle 恢复，电源引用、使能引脚、触控就绪状态及五路切换顺序 | 华星屏、长期唤醒和板上电压波形待验证，见[显示供电](DISPLAY.md#当前实现) |
| 背光与 CABC：`ktz8866`、面板命令 | 已实现亮度曲线、首帧背光与调光 | 手动亮度、普通亮灭屏实机 | 待解决：启动接管短暂黑屏；待验证：CABC 观感与功耗 |
| 触控：`nt36523/nt36xxx` | 已实现面板固件选择、共享 TDDI 顺序、校准保护与看门狗诊断 | 天马点击、滑动、普通熄屏恢复及四档切换实机 | 待验证：华星、长期唤醒与异常恢复，见[触控时序](DISPLAY.md#显示与触摸恢复时序) |
| 手写笔：`NVTCapacitivePen` | 已实现坐标精度、压感、报文计数及笔唤醒故障隔离 | 戴尔 PN7522W 在 120 Hz、在线切入的 60 Hz 有基本输入 | 已知限制：90/144 Hz 无笔事件，暂不排查；待验证：其他笔、侧键、防误触与睡眠恢复 |
| 磁吸键盘/触控板：`hid-nanosic` | 已接入按键、Caps Lock、合盖/背折屏蔽、150 ms 防抖与残留报文过滤；按原厂方式持续处理 GPIO46 状态中断，睡眠时保留双中断唤醒 | 基础输入、接卸/平板模式实机；临时内核覆盖静置/快速/按键后的睡眠、按键唤醒与禁用策略、重绑定后的实际按键；默认内核普通重启后核对 GPIO46 持续启用及 RTC 睡眠恢复；防抖、输入隔离与中断生命周期有离线检查 | 旧版偶发 GPIO46 提前打断 s2idle 未稳定复现，改动后短测未再出现；长期待机、多指手势、快速反复接卸及睡眠中姿态变化待验证 |
| 指纹：FPC1264 SPI、IRQ 接口 | 已接入 `fpc1264_spi_diag` 与配套 QSEE 通路 | 随用户态录入/认证覆盖，见[指纹](FINGERPRINT.md) | 待解决：偶发识别失败；待验证：反复休眠和异常恢复 |
| 电源/音量键、霍尔开关 | 已接入按键及合盖/平板模式事件 | 按键、亮屏与键盘姿态基本操作 | 待验证：所有状态下的唤醒、抖动与事件丢失 |

### 键盘状态中断与睡眠

WN8030 使用 GPIO50 通知 HID 数据，GPIO46 通知状态与唤醒；GPIO42 是主机控制的桥接芯片唤醒线。
[小米公开驱动的 `Nanosic_GPIO_register()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/77dc8e2290f3f5f15861821564f68d30ea193449/drivers/input/keyboard/nanosic_driver_v2/nano_gpio.c)
以 rising-edge、oneshot 方式请求 GPIO46，清醒期间也接收中断；固定 ROM 中的
`nanosic_driver.ko` 注册路径与此一致。原厂在屏幕关闭时报告唤醒键，系统睡眠时另行配置 IRQ wake。

补丁 [`0060`](../build/kernel/patches/linux-7.3/0060-nanosic-service-awake-status-irq.patch)
保留设备树的 `data`、`wakeup` 名称，释放 I2C 核心预先注册的专用唤醒处理器，
再请求持续启用的 `nanosic-wakeup` 线程中断，并用 `devm_pm_set_wake_irq()` 交给系统 PM 管理唤醒。
清醒时处理状态边沿，不发起 I2C 读写或生成按键；实际输入仍来自数据中断。
睡眠边界上的两路处理器均遵守 `power/wakeup`，避免在总线休眠时读取帧；待处理数据在恢复后读取。
初始化失败和移除时先解除 PM 关联，再释放中断，避免与 I2C 核心重复持有。

临时启动已覆盖清醒时状态中断、首次及快速反复入睡、按键唤醒、关闭唤醒开关、
驱动重新绑定及之后的完整按下/释放报文。通过项目更新器设为默认后，普通重启再次
核对了 GPIO46 的启用状态和 RTC 睡眠恢复，未见 IRQ 归属冲突或提前唤醒。
这些结果限单台 Liuqin 和短周期测试，不能外推为所有姿态、长时待机或 `deep` 已通过。

`bridge_health` 的 `irq` 和 `wakeup_irq` 分别统计数据、状态线程的调用次数。
可结合 `/proc/interrupts`、`/sys/power/pm_wakeup_irq` 和睡眠前后的实际驻留时间验收：
静置、清醒时按键后立即睡眠、快速反复睡眠、睡眠中按键唤醒，以及关闭 `power/wakeup` 后的 RTC 唤醒。
短周期测试通过不能证明偶发的提前唤醒已根治；也不能仅凭 `rtcwake` 成功退出判定已进入睡眠。

## 无线、USB 与媒体

| 项目 / 内核入口 | 实现状态 | 已验证范围 | 待解决 / 待验证 |
| --- | --- | --- | --- |
| Wi-Fi：`ath11k` / WCN6855 | 已接入 QCA6490、固件及板级数据 | 扫描、5 GHz 联网及一次 s2idle 后重连 | 待验证：其他频段、吞吐、反复休眠与蓝牙共存 |
| 蓝牙：`hci_qca` | WCN6855 在准备休眠时等待在途 HCI 请求完成。GENI UART 在串口核心挂起前屏蔽主中断，启动 RX 后开放中断 | 实机覆盖首次 s2idle、服务 UUID 增删、LE 扫描恢复，以及反复关开后的 IBS 握手和 GATT 读取。主机回归覆盖端口开关、TTY 唤醒和挂起失败回退。[HCI 休眠验证](#蓝牙-hci-与休眠) · [蓝牙关开验证](#蓝牙关开与-wcn6855-供电) · [原厂 UART 中断顺序](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/tty/serial/msm_geni_serial.c#L3718-L3810) | 待实机验证配对、音频、蓝牙外设唤醒与长期连接 |
| USB 主机/角色：DWC3、QMP PHY、eUSB2 repeater | 已接入 OTG、角色切换与 SuperSpeed | 预接 U 盘启动、挂载读取及 5 Gbit/s 协商 | 待验证：集线器、带载拔插与睡眠恢复 |
| USB gadget：configfs、FunctionFS | 内核已配置，桌面未默认创建 gadget | 当前目标内核未专项验收；Rec 使用独立内核 | 待验证：目标系统所需设备功能及主从往返 |
| 外屏：DRM DP、Type-C Alt Mode、FSA4480 | HBR2 上限，按 Type-C 分配选择两条或四条通道；独立 DP 音频前端与 UCM，按显示链路停启 DSP 后端，见[显示](DISPLAY.md#usb-c-displayport) | 四通道下单台外屏以 2560 × 1600、120/144 Hz 扫描输出，内屏同时启用；临时内核的 ALSA 48 kHz、S16_LE 双声道出声与停止后重开、ALSA/桌面播放中换档、桌面热插拔恢复通过，未见音频关闭超时；配套 UCM 下未接外屏的临时启动和默认内核普通重启均可识别板载音频 | 未接外屏启动后再接入外屏待复测；换档有短暂停顿；其他外屏、双通道扩展坞、长时间高刷及音频睡眠恢复待验证 |
| 四扬声器：CS35L41、AudioReach、SoundWire | 已接入声卡、保护固件和本机校准。按实际输出切换[原厂音乐与免提通话参数](AUDIO.md#扬声器音乐与免提通话调音)。整卡睡眠与重新准备时重置播放队列 | 立体声播放、音量及 USB 连接下普通重启；临时内核中同时录放的 s2idle、受控整 ADSP 重启及重启后再次睡眠，均确认自动恢复出声与播放指针推进 | 待验证：四路单独通道、长时播放及异常固件崩溃；音质仍需调校 |
| 麦克风：ASoC / DSP 拓扑 | AMIC1/3/4/5 的原厂偏置与 SoundWire 接线、四路采集及连续 DSP 通道映射；公共 ADC 时钟按最后一路退出释放，四通道载体保留未用时隙的时钟与静音；采用原厂 HPF 等待时序，离线关闭清理引用、缓存及延迟任务；重建声卡时重置路由；桌面使用软件音量，见[音频](AUDIO.md) | 四路 48 kHz / S16_LE、应用录音回放、静音与软件音量及普通重启通过；临时内核验证单路起录、逐路关闭/恢复、快速启停及停止后释放时钟与偏置；运行态和 PREPARED 态采集跨 s2idle、同时录放、受控整 ADSP 重启后恢复及再次睡眠通过，恢复后四路均有数据 | 长期并行录放和真实固件崩溃待验收；原始四通道不包含原厂通话降噪、回声消除或波束成形 |
| WCD9380 / SoundWire 中断与停钟 | TX 独占 codec 寄存器中断；1.7 使用 CPU1 FIFO 与中断；关时钟前排空 IRQ，系统睡眠在 DAPM 关路径前停用数据端口；ADSP 恢复时重设引脚、复位并重新枚举，见[音频](AUDIO.md#系统睡眠与音频服务恢复) | 临时内核启动、自动休眠/恢复、反复录音、同时录放及 s2idle 恢复通过，未复现总线冲突、奇偶校验、FIFO 错误、停钟超时或 IRQ 异常；受控 ADSP 重启后四路采集恢复，离线 graph 关闭未再出现 `-102`，停止后时钟与偏置释放 | 长期压力、任意 DSP 崩溃和 Recovery 音频待验收 |
| 视频编解码：Iris2 / V4L2 | H.264、H.265、VP9 解码；H.264、H.265 编码，见[视频](VIDEO.md) | 完整内核临时与默认启动通过三种 8-bit 720p 解码逐帧校验及两种硬编；另覆盖 1080p/854×480 编码、并发、停止重开、模块重载、自动休眠和空闲 s2idle 后重开；应用范围见下方软件接口 | 待验证：4K、10-bit、HDR、其他 profile、睡眠中保持的视频会话、吞吐及长时间运行 |
| 后摄：S5KJN1、CAMSS、GT9764 | 三档合并像素模式经 IFE680 输出 NV12，使用原厂色彩参数、单机校准及 libcamera 对比度对焦。可选 IPE TF21 单层降噪 | 普通用户预览、拍照、扫码、近远景合焦及关闭后释放电源。Kamoso 带声短片与回放。独立工具可三曝光 HDR 拍照、宽屏 HLG 录像 | 长录音画漂移、采集中睡眠、低照度与运动对焦、长期持有镜头时的功耗及完整 TF 运动补偿待验证或适配。见[相机支持与待办](CAMERA.md) |
| 前摄：AAC IMX596 | 三种原厂 RAW10 模式，两个合并像素模式可经 IFE680 输出 NV12，含基础 AE/AWB、EEPROM 校准、CC131 原厂矩阵及可选 TF21 | 30/60/10 fps RAW、彩色出图、曝光限幅、切换重开、停止后睡眠与电源释放。1080p30 H.264/H.265、720p60/1080p60 H.264 无声录像。独立工具支持三曝光 HDR 拍照和 640×480 HLG 录像 | 软件 ISP 的 60 fps 仍有启动间隙和偶发丢帧。完整 TF 运动补偿、场景调校、长录同步及采集中睡眠待覆盖。见[硬件 ISP](CAMERA-ISP.md)及[相机待办](CAMERA.md#前后摄待办) |
| 运动传感器：SLPI / FastRPC | 加速度计通路已接入，依赖 SSC 用户态 | 实际采样与自动旋转 | 待核实：陀螺仪等其他传感器；待验证：精度、轴向与睡眠恢复 |
| 环境光：TCS3701 / SSC | lux 经 SensorProxy 接入 Plasma 原生自动亮度，桌面默认曲线参考固定原厂 vendor 资源 | SSC 型号、真实 lux、KWin 接收和原生背光联动 | 待验证：校准精度、其他面板观感、长期休眠；其他光感/接近传感器配置不代表硬件已验收，见[传感器](SENSORS.md) |

### 蓝牙 HCI 与休眠

小米 GENI UART 驱动在 runtime suspend 中先停 RX，再屏蔽主中断，随后关闭串口资源。恢复时先开资源、启动 RX，再开放中断。补丁 `0097` 针对 Linux 串口核心的系统挂起流程，在调用 `uart_suspend_port()` 之前就屏蔽主中断，避免核心标记端口挂起后还有电平中断待应答。[小米 UART 驱动](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/tty/serial/msm_geni_serial.c#L3718-L3810) · [UART 补丁](../build/kernel/patches/linux-7.3/0097-liuqin-geni-bt-suspend-irq.patch)

BlueZ 增删本地服务 UUID 时，内核用 `Write Extended Inquiry Response`（`0x0c52`）更新经典蓝牙查询响应中的服务列表。若休眠准备在控制器回包前取消这个请求，BlueZ 会收到管理命令失败，内核也会提前补回一份命令额度。此时关闭 LE 扫描的 `LE Set Extended Scan Enable`（`0x2042`）可以先行发出，后续的成功回包就可能与内核正在等待的命令错配。

小米公开的 `liuqin-t-oss` 内核将休眠准备放入请求工作队列，等待扫描等任务完成，等待上限为两秒。量产 ROM `OS2.0.6.0.VMYCNXM` 的 `vendor_a` 在 `etc/vintf/manifest_cape.xml` 声明蓝牙 HIDL 服务，启动配置指定 QTI 服务程序，HAL 实现包含 UART 传输。Fedora 的 WCN6855 驱动在准备休眠时先获取 HCI 请求锁。控制器空闲时可立即进入休眠，发生竞争时先让当前持有者完成请求。超过两秒仍阻塞时，沿用内核的取消路径。[原厂休眠队列](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/net/bluetooth/hci_core.c#L3469-L3513) · [原厂超时定义](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/include/net/bluetooth/hci_core.h#L93) · [量产镜像校验值](../shared/liuqin-rom-images.json)

实机验证时，从开机后第一次 `s2idle` 开始，在休眠前启动 `btmon -i hci0`。分别覆盖空闲、LE 扫描中，以及用独立的临时服务 UUID 在休眠入口交错注册和注销三种情况。对照 MGMT 的 Add/Remove UUID 结果、HCI 命令和 Command Complete 的次序。在控制器只给一份命令额度时，上一条的回包应先于下一条的发送。RTC 唤醒后检查 `/sys/power/suspend_stats` 的成功计数，并再次读取控制器版本。临时 UUID 用后注销。

### 蓝牙关开与 WCN6855 供电

量产 ROM `OS2.0.6.0.VMYCNXM` 的 `vendor_a/lib64/hw/android.hardware.bluetooth@1.0-impl-qti.so` 为每次 UART 上电重建带内休眠（IBS）会话。`UartController::Cleanup()` 调用 `IbsHandler::StopAllTimers()` 后析构处理器，`UartController::Init()` 重新构造处理器并调用 `InitInt()`。[量产镜像来源](../shared/liuqin-rom-images.json)

Linux 的 `hci_qca` 会复用收发状态，最后的 HCI 关机命令仍可重启发送空闲定时器。Liuqin 在拉低 BT_EN 前阻止旧会话收发，依次收尾唤醒工作、定时器和时钟投票，将收发两侧恢复为 ASLEEP。下一次上电下载固件后，首条普通 HCI 命令重新走 WAKE/ACK。[板级补丁](../build/kernel/patches/linux-7.3/0106-bluetooth-liuqin-reset-ibs-on-power-off.patch)

复测时先读取 HCI 本地版本，随即用 BlueZ 关闭适配器，在 `/sys/kernel/debug/bluetooth/hci0/ibs/` 读取 `tx_ibs_state`、`rx_ibs_state`、`tx_vote` 和 `rx_vote`。BT_EN（TLMM GPIO81）拉低后应分别为 0、0、N、N。分别立即开蓝牙，或等待超过 `tx_idle_delay` 再开。掉电期间 `ibs_sent_sleeps` 应保持稳定。重开后验证本地版本、LE 扫描及已知外设的 GATT 读取。

#### 射频启动与固件保持模式

量产 M81 的 RPMh 驱动用毫伏表示电压。它读取设备树中的初始请求并缓存。下列四路在原厂设备树中均未设置 `qcom,send-defaults`。蓝牙和 Wi-Fi 在上电前分别向稳压器申请固件启动电压，固件就绪后各自撤销下限。共用电源轨采用所有消费者请求的交集。[原厂 RPMh 请求单位](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/regulator/rpmh-regulator.c#L980-L1007) · [原厂初始电压](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/regulator/rpmh-regulator.c#L1564-L1580) · [原厂默认请求发送条件](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/regulator/rpmh-regulator.c#L1958-L1968)

| 电源轨 | PMIC 初始请求 | 物理轨范围 | 蓝牙固件启动请求 | Wi-Fi 固件启动请求 |
| --- | ---: | ---: | ---: | ---: |
| S11B，蓝牙 AON/DIG/RFACMN/RFA_0p8 和 WLAN DIG | 952 mV | 382–1170 mV | 966–1170 mV | 966–1170 mV |
| S12B，RFA2 | 1256 mV | 1224–2040 mV | 1350–2024 mV | 1350–2040 mV |
| S1C，RFA1 | 1880 mV | 1800–2024 mV | 1900–2040 mV | 1900–2024 mV |
| S2E，WLAN AON | 500 mV | 500–1040 mV | — | 1012–1040 mV |

两路还共用固定 1800 mV 的 S10B（I/O）和固定 2800 mV 的 L7E（天线开关）。S1C 上蓝牙的有效上限受物理轨的 2024 mV 限制。Wi-Fi 在 CNSS 固件初始化成功后撤销启动下限。量产 `OS2.0.6.0.VMYCNXM` 的 `android.hardware.bluetooth@1.0-impl-qti.so` 在 `UartController::Init()` 中先调用 `SetPower(true, false)`，初始化末尾调用 `SetPower(true, true)`。`PowerUpChip()` 将第二次调用转为模式 2，原厂内核在该模式保留使能并撤销启动下限。Linux 在蓝牙补丁和 NVM 下载成功、Wi-Fi 固件就绪时分别执行这个交接，重新启动固件时重新申请。[小米蓝牙供电定义](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/cape.dtsi#L553-L569) · [小米 WLAN 供电定义](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/cape.dtsi#L3049-L3064) · [原厂蓝牙保持模式](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/bluetooth/btpower.c#L705-L715) · [原厂 WLAN 固件交接](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/net/wireless/cnss2/main.c#L668-L710) · [板级实现](../build/kernel/patches/linux-7.3/0107-liuqin-wcn6855-firmware-voltage-votes.patch)

发布内核可直接采样 `/sys/kernel/debug/regulator/regulator_summary` 的 `bt-*`、`wlan-*` 消费者。要追踪实际发送的命令，用同一源码临时构建启用 `CONFIG_FTRACE=y`、`CONFIG_ENABLE_DEFAULT_TRACERS=y` 的诊断内核，在独立 tracefs 实例记录 `regulator_set_voltage` 和 `regulator_set_voltage_complete`。从 `/sys/kernel/debug/cmd-db` 查出 `smpb11`、`smpb12`、`smpc1`、`smpe2` 的资源地址，用 `rpmh_send_msg` 核对发往 VRM 的毫伏数。先单独关开蓝牙，再在 WLAN 固件重新启动期间开蓝牙。可从启动请求、其中一路先就绪、两路均就绪三个阶段核对下限与使能计数。在共享轨上，另一方仍有启动请求时应保留其电压。用 HCI 本地版本、LE 扫描及 Wi-Fi 重新关联确认无线功能。trace 与 sysfs 的数值表示内核请求，板上实际电压需用仪器测量。

### USB-C 方向与速率

公共补丁 [`0071`](../build/kernel/patches/linux-7.3/0071-arm64-dts-liuqin-usb-gen1-limit.patch)
将板级最高速率设为 `super-speed`（5 Gbit/s）。方向由 CC 状态决定，速率上限本身
不会选择正反面的高速通道。正式系统通过
[`0072`](../build/kernel/patches/linux-7.3/0072-liuqin-usb-cc-orientation.patch)
在 UCSI 的连接状态回调中读取小米电池服务属性 `XM_CC_ORIENTATION`（36），
在设置 USB 角色前调用 Type-C 方向开关。编码为 0 拔线、1 CC1、2 CC2；查询与
电池请求共用锁，并保持服务设备绑定直到请求结束。短暂的 0 或 `EAGAIN` 最多查询
十次、间隔 50 毫秒，其他错误或无效值保留错误且不猜测方向。GPIO91 是 PHY
端口选择输入，不能作为 CC 方向来源。

UCSI 初次注册端口时，QMP 仍处于 USB＋DP 模式。对尚未注册伙伴的普通 USB 连接，
同一回调在方向设置成功后选定 USB 通道，再交给 UCSI 设置角色。若等后续热插拔通知
才切到 USB 模式，PHY 会在主机训练期间重启。已有伙伴、报告替代模式能力的设备，
以及音频和调试附件继续使用原有通道策略。

板级默认主机策略还会让 DWC3 在 ADSP/UCSI 就绪前启动 xHCI。反面预接 U 盘可能
先完成 USB 2 枚举，此后仅修正 PHY 方向不会结束这个已有会话。
[`0073`](../build/kernel/patches/linux-7.3/0073-liuqin-usb-wait-initial-cc.patch)
让 Liuqin 的 DRD 核心与 Qualcomm glue 首次都处于设备模式，由 UCSI 在处理方向后
发出第一个角色请求。后续 `NONE` 请求仍使用板级主机回退，兼容固件未完整报告
伙伴类型或 USB 标志的扩展坞；固定主机、固定设备及 Rec 的角色策略不变。

[`0076`](../build/kernel/patches/linux-7.3/0076-liuqin-usb-retry-connection.patch)
将方向和初始通道准备纳入连接准入：查询或切换失败时不发布新的连接状态，也不继续
请求 USB 角色。初始注册沿用 UCSI 的有限重试；已注册端口保留已经确认的变化位，
以 500 毫秒间隔最多安排十次补查，每次重新读取固件状态，避免拔线后重放旧角色。
挂起和卸载先禁止重试任务再次排队，再等待当前事件处理结束。

SM8475 的 USB SerDes 公共寄存器采用 4 nm/V6 布局。
[`0075`](../build/kernel/patches/linux-7.3/0075-liuqin-usb-complete-serdes-init.patch)
补齐原厂初始化表中的 `VCO_TUNE2_MODE1`、`CORE_CLK_EN` 和 `CMN_CONFIG_1`，
分别位于 PHY 基址的 `0x104c`、`0x1170`、`0x1174`，在释放 PHY 复位前写入
`0x02`、`0x20`、`0x04`。其他寄存器地址和值保持不变；原先借用的 V5 COM 名称
改为与实际地址对应的 V6 名称。来源为固定原厂 ROM 的 DTBO 第 15 项，以及
MiCode 提交 `eb49e10028e27fa98856cb5e458c0f0ec5b4554e` 中
`liuqin-sm8475.dtsi` 的 `usb_qmp_dp_phy` 初始化表。
`tests/usb-phy-oem.py --source <完整适配源码>` 编译执行实际初始化表及写入函数，
对照原厂的 173 项寄存器配置；该检查不能代替实机链路训练验收。

普通 USB 插线不保证产生 DisplayPort PAN 通知，因而不能仅靠 altmode 客户端设置
方向。正式系统保留 UCSI 角色和 DP 策略；Rec 的固定设备模式使用独立的连接时序，
见 [Rec USB 配置](NATIVE-RECOVERY.md)。两者共用电量属性定义及板级速率上限。

验收时通过 Wi-Fi 保持控制，先核对 U 盘身份，再分别翻转平板端插头并读取
`/sys/bus/usb/devices/<U盘节点>/speed`；两面都应为 `5000`。同时核对
`/sys/class/typec/port0/orientation` 与电池服务的
`sm8475_raw/raw_xm_cc_orientation`，确认方向实际改变。根集线器声明的速率
不能替代 U 盘叶节点的协商结果。翻面前卸载该 U 盘分区，并覆盖预接启动和热插拔；
日志及设备身份保存在忽略的 `out/` 目录。

## 电源、温控与时间

| 项目 / 内核入口 | 实现状态 | 已验证范围 | 待解决 / 待验证 |
| --- | --- | --- | --- |
| 电池/充电：PMIC GLINK | 周期电量刷新、双电芯认证、MiPPS/PPS 和 M81 热限制。传输中断时保留最近有效电量并将充电状态标为未知 | 基础充电、电量、双电芯认证。电脑固定 PD 限流、解除限流、普通重启和自动温控。受控 ADSP 停启期间 UPower 保有有效电量。临时进入关机充电模式，电脑口为 PD，已测快充头为 PD_PPS | 固件以 `0x204` 拒绝独立 USB 输入限额。实际输入功率、低电量/高负载、热限制跨固件重启、长期断联和关机插线冷启待验证。见[充电](FASTCHARGE.md) |
| 温控：TSENS、PMIC ADC、thermal cooling | 补齐 13 个 CPU 温区到三个频率域的 cooling maps；GPU 已有绑定；接入 PMK8350 内温及八路原厂 ADC7 外部热敏电阻，按原厂 normal 表实现充电温控服务；其他 PMIC 内部告警温区的 37°C 是估算值 | 单台设备：CPU/GPU 在降低的被动阈值下实际限频和自然降温恢复；充电在降低的实温阈值下自动进入 15 档、停载自然降温后解除，USB 实流约 0.5 A，电池电流约 0.15→0.60 A；默认启动核对读数及服务恢复，保留既有 CPU/GPU 绑定 | 默认高温阈值、固件独立热保护、critical 保护、长时间负载、挂起及 Recovery 充电温控仍待验证；方法与边界见[温控](THERMAL.md) |
| s2idle | 内核已支持；默认系统选择此路径；补齐音频整卡的端口与 DSP 暂停/恢复顺序 | 600 秒待机，RTC、键盘、电源键唤醒；音频临时内核的空闲、录音及同时录放短周期 RTC 唤醒与数据恢复 | 旧版偶发键盘提前唤醒的原因尚未确认；状态中断改动后的短周期检查通过，见[键盘状态中断与睡眠](#键盘状态中断与睡眠)；待验证多轮长时待机、其他外设恢复及功耗，音频诊断配置范围见[AUDIO.md](AUDIO.md#系统睡眠与音频服务恢复) |
| deep | 有试验路径，非默认策略 | 已观察到异常 | 待解决：提前唤醒、串口/蓝牙恢复；再入睡原型未加入默认构建 |
| RTC：`rtc-pm8xxx` | 计数/闹钟已接入；设备树禁止直接写时 | RTC 唤醒，配套用户态的离线重启/关机后校时 | 待验证：长期漂移、拔线关机和掉电重置，见 [RTC](RTC.md) |

内核成功唤醒不等于桌面已亮屏。Plasma 恢复输出时可能漏掉一次 DPMS On，
使系统保持运行而屏幕仍关闭；可选的独立用户服务在单内屏、按键相关唤醒和
未合盖条件下补偿，保留官方桌面包，见[显示恢复说明](DISPLAY.md#自动恢复与启动日志)。

## 固件与用户态边界

通用固件按[固件清单](../device/firmware/README.md)锁定；MAC、蓝牙地址、扬声器校准、传感器注册表和指纹数据属于本机状态。
更换内核时须保持模块、DTB、固件及板级服务配套。以下端到端功能不能只靠内核完成。

| 功能 | 固件 / 用户态职责与当前边界 |
| --- | --- |
| 图形与启动画面 | Mesa/Freedreno、KWin、Plymouth；CABC 策略及显示恢复由板级服务提供，见[显示](DISPLAY.md) |
| 音频 | ADSP 固件、DSP 拓扑、CS35L41 保护/校准、ALSA UCM 与声音服务；音频模块延后到根系统加载，见[充电初始化](FASTCHARGE.md#初始化与普通重启) |
| 旋转与键盘姿态 | hexagonrpcd/libssc、iio-sensor-proxy、桌面策略；接键盘停转、拆下恢复；合盖锁屏/熄屏由 PowerDevil/logind 决定 |
| 指纹认证 | TA、QSEE 客户端、libfprint/fprintd；两枚录入、分别解锁及未录手指拒绝已验证；五枚录满、多账号与重启专项待验证，登录界面仍用密码 |
| 相机 | libcamera 前后摄 IFE680 硬件 ISP，旧内核可回退软件 ISP；对焦、调校及录像限制见[相机](CAMERA.md) |
| 视频播放与录制 | 默认构建包含修复版 FFmpeg、mpv/Haruna 自动硬解设置、Qt 独立播放入口、Firefox 兼容启动适配及 OBS V4L2 插件；已覆盖短片显示/重播和 H.264/H.265 标准录像。Chromium 的 VP9 结束/重播与 HEVC 支持仍有限制，网站 MSE/DRM、长片和长时录像待验证，见[视频软件接口](VIDEO.md#应用播放与自动硬解) |
| 充电策略 | 电芯/适配器认证由 `liuqin-mipps-auth` 配合 ADSP 执行；不以运行中重启 ADSP 或 PD 复位作为常规修复 |
| 睡眠与时间 | 板级 tmpfiles 在开机时将 `mem_sleep` 设为 `s2idle`，系统以 `SuspendState=freeze` 直接选择同一路径；RTC 偏移由 `liuqin-rtc` 保存到 Linux 文件系统，不写 Android persist |
| 设备监测 | [KSystemStats 插件](../device/ksystemstats/README.md)读取 GPU 忙碌率与频率；不提供独立显存、GPU 温度或功耗 |
| 安装、存储保护与更新 | initramfs/安装器负责根系统身份、分区及只读保护；多系统与 ext4/Btrfs 引导已验证；[自动内核写入](KERNEL-UPDATES.md)默认停用，Rec 独立验收 |

中文、字体、输入法、浏览器与桌面应用属于[系统配方](BUILD.md#发行版与桌面配方)，不列为内核适配项。

## 验收与维护

| 验收维度 | 必须分别记录 |
| --- | --- |
| 启动 | Fastboot 临时启动、固定引导、普通重启、拔线冷启动与关机 |
| 生命周期 | 亮灭屏、s2idle/deep、热插拔、异常恢复及重复操作；记录恢复后的实际输入/输出 |
| 负载与组合 | CPU/GPU/UFS 长时负载，充电与外设并用；记录温度、频率、错误和功耗 |
| 设备范围 | 面板厂商、内存/存储容量、硬件批次、固件、笔和充电器型号 |
| 证据 | 对应源码树、配置、镜像/模块身份、操作、日志和结果；离线与实机结果分开 |

每次修改只更新有证据支持的状态。内核回归入口见[测试说明](../tests/README.md)，笔与触控诊断见[显示文档](DISPLAY.md#笔与触控诊断)。
