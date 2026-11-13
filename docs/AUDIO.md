# 音频与麦克风

[硬件支持](HARDWARE.md) ｜ [DP 音频](DISPLAY.md#usb-c-displayport) ｜ [构建](BUILD.md)

板载录音经过 WCD9380 模拟前端、SoundWire、LPASS TX macro 和 AudioReach DSP。
内核提供这些硬件模块及其连接，`tplg` 描述 DSP 采集图和 ALSA PCM，UCM 则选择
模拟输入、增益和路由。出现录音设备只说明 PCM 已注册，不能证明物理麦克风已经接通。

## 原厂接线与主线映射

原厂 `OS2.0.6.0.VMYCNXM` 的 `vendor/etc/audio/sku_cape/mixer_paths_overlay_static.xml`
及其基础 `mixer_paths_waipio_mtp.xml` 使用四个模拟麦克风。该 ROM 的 Liuqin
DTBO（entry 15，`xiaomi,miboard-id = <0x10 0>`）提供板级偏置和传输参数，
偏置电压为 2.75 V：

| 原厂用途 | 模拟输入 | ADC / 输入选择 | 偏置 | 主线 TX 输入 | 采集通道 |
| --- | --- | --- | --- | --- | --- |
| handset-mic | AMIC1 | ADC1 | MIC BIAS1 | SWR_MIC4 → DEC0 | 0 |
| sidebottom-mic | AMIC3 | ADC2 / INP3 | MIC BIAS3 | SWR_MIC5 → DEC1 | 1 |
| back-mic | AMIC4 | ADC3 / INP4 | MIC BIAS3 | SWR_MIC8 → DEC2 | 2 |
| speaker-mic | AMIC5 | ADC4 / INP5 | MIC BIAS4 | SWR_MIC9 → DEC3 | 3 |

主线 WCD938x 使用固定的从端口映射：ADC1/2 在从端口 1 的位 0/1，
ADC3/4 在从端口 2 的位 0/1。原厂 mixer 将两组送到不同主端口 TX2/TX3，
对应 TX 输入 4/5/8/9。本机设备树用 `qcom,tx-port-mapping = <2 3 1 4>`
表达这条连接，另两个未用的数字麦克风从端口各有独立分配。

不能沿用只使用一组 ADC 时的 `<1 1 2 3>`：同时启用两组时，主线驱动不会把
第二组的位 0/1 自动重排成位 2/3，会造成主端口冲突。还需同时匹配原厂 UC0
传输参数：从端口 1 使用 lane 1、offset 0，从端口 2 使用 lane 2、offset 0；
ADC_NORMAL 下采样间隔字段为 1。这里依据该 ROM 的
`qcom,swr-tx-port-params` 第一组参数；通用 Cape 源码给第二组 ADC 配置的
lane 0、offset 2 与本机接线不同，不能直接照搬。原厂
[SoundWire 主控实现](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/soc/swr-mstr-ctrl.c)
按从端口读取这些参数，并拒绝同一主端口上混用不同数据线或采样间隔。
内核补丁
[`0049`](../build/kernel/patches/linux-7.3/0049-liuqin-four-microphone-capture.patch)
补齐板级偏置与音频路由，限定本机 `TX_CODEC_DMA_TX_3` 为四通道，并修复 TX macro
多路选择时的重复计数、关闭遗漏和仅静音最后一路的问题。

[`UCM`](../device/board-overlay/usr/share/alsa/ucm2/Qualcomm/sm8450/Xiaomi-Pad-6-Pro/HiFi.conf)
按原厂设置各 ADC 增益为 8；桌面使用软件音量与静音。不能把 `ADC1` 暴露为
`CaptureMixerElem`：其简单混音器中的 playback switch 实际是 SoundWire 端口选择，
桌面管理这个控件会使界面显示未静音，而底层 `ADC1 Switch` 已关闭。

## 多路 ADC 时钟与高通滤波

补丁 [`0057`](../build/kernel/patches/linux-7.3/0057-liuqin-microphone-clock-and-offline-lifecycle.patch)
按原厂 [WCD938x](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/asoc/codecs/wcd938x/wcd938x.c)
的最后一路 ADC 关闭规则管理公共模拟时钟。主线复用已有 ADC 活动位图，
关闭单路只撤销该路的状态、模式与数字时钟；仍有 ADC 工作时保留公共 TX 和 DIV2
时钟，最后一路退出后才一并关闭。DAPM 的 ADC 关闭事件先于 REQ mixer，
因此后者可依据更新后的位图决定是否停钟。其他用途的状态位不计入 ADC 活动判断。

本机的 codec-DMA 载体固定为四通道，还需区分物理 ADC 和 TX decimator 的采样时钟。
移除一路模拟输入时，DAPM 会关闭对应 decimator；若此时也停掉它的采样时钟，
四通道 DMA 会一起停止。补丁在四路载体仍有实际输入时保持四个采样时钟，
把退出通道置为静音，整条流解除静音时也保留这些静音位；最后一个实际输入关闭后
统一释放这些时钟。重新接入的通道继续遵循正常的滤波稳定和延迟解除静音顺序，
其他板型与非四路路由保持原处理。

TX macro 的模拟高通滤波切换采用原厂
[Bolero TX macro](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/asoc/codecs/bolero/tx-macro.c)
的采样率等待时间，在 gate 从 `0x02` 切到 `0x01` 之间等待：

| 采样率 | 等待时间 |
| --- | --- |
| 8 kHz／未知编码 | 125–130 µs |
| 16 kHz | 62–65 µs |
| 32 kHz | 31–32 µs |
| 48 kHz | 20–21 µs |
| 96 kHz | 10–11 µs |
| 192 kHz | 5–6 µs |

本机采集拓扑仍固定为 48 kHz。该时序表不表示其他采样率已开放或通过实机验证。
录音启动时临时使用 150 Hz 截止频率，随后恢复用户原有设置；快速关闭录音时也必须
恢复。关闭路径先同步取消解除静音和高通滤波任务，再设置静音并恢复截止频率，避免旧工作
在关闭过程中重新解除静音。声卡拆除会排空这两类任务，之后才释放旧 component 引用。

ADSP 离线后拒绝新的 ADC、MICBIAS 和 decimator 开启请求；关闭仍清理 ADC 位图、
偏置引用和非易失寄存器缓存，以免恢复时同步回旧的开启状态。MICBIAS 引用由锁保护，
共用 BIAS3 的两路分别释放引用。易失的 SoundWire bank 时钟率寄存器 `0x3405`
在离线关闭时不再读写；在线访问失败仍返回错误。已运行但因离线退出的高通滤波任务
仍保留恢复义务，关闭时把原截止频率和最终 gate 状态写回缓存，不在失效硬件上产生脉冲。
这些改动通过 `liuqin_board_quirks()` 限定于本机。

## 拓扑与桌面

[`板级拓扑`](../device/audio-topology/liuqin-speakers.m4)提供三个独立 PCM：

| PCM | 格式 | 用途 |
| --- | --- | --- |
| MultiMedia1 Playback | 48 kHz、S24_LE、四通道载体 | 受保护的平板扬声器 |
| MultiMedia2 Capture | 48 kHz、S16_LE、四通道 | 四个物理麦克风原始采集 |
| MultiMedia3 Playback | 48 kHz、S16_LE、双通道 | DisplayPort |

应用可通过桌面声音服务使用单声道或双声道录音，由软件转换格式。原始四通道
不等同于原厂通话降噪、回声消除或波束成形；当前 DSP 拓扑不包含这些处理模块。
四路采集需要配套内核、拓扑和 UCM，不能只给旧的双通道内核替换 UCM。

已有系统保留旧内核时，可将配套拓扑安装到
`/usr/lib/firmware/updates/<kernel-release>/qcom/sm8450/Xiaomi-Pad-6-Pro-tplg.bin`，
按完整内核版本选择，同时保留基础目录中的旧拓扑用于回退。UCM 的录音和 DP
设备按对应路由控件是否存在启用，兼容只有扬声器的旧拓扑。升级前备份 UCM
及两处拓扑，正常重启后核对版本、文件摘要和实际 PCM；不能仅凭 RPM 安装成功判定配套完成。
配套版本经项目更新器设为默认后，普通重启已验证四路桌面采集均有数据、扬声器校准加载，
以及无声测试流的播放指针推进；未接外屏时 Speaker、Mic 和 DP PCM 均可枚举。
无声流只验证播放通路，不能替代实际扬声器出声；外屏播放的验证范围见[DP 音频](DISPLAY.md#usb-c-displayport)。

原始 codec-DMA 采集的 DSP 通道标识为连续的 `1/2/3/4`。
四声道播放的默认映射为 `1/2/4/5`，直接用于录音会使格式转换器丢弃麦克风 3，
并为不存在的通道 5 输出零。补丁
[`0051`](../build/kernel/patches/linux-7.3/0051-asoc-q6apm-map-liuqin-microphone-channels.patch)
只为本机四路录音选择连续映射；扬声器播放、DP 双声道及其他板型仍用原有映射。

DP 未连接时，桌面仍会探测其 PCM。补丁
[`0050`](../build/kernel/patches/linux-7.3/0050-asoc-probe-disconnected-display-pcm.patch)
允许已路由但被显示链路明确暂停的播放前端打开，保持 DP 后端关闭；真正缺失的路由
仍报错。这样 DP 的连接状态不会阻止桌面识别板载扬声器和麦克风。

## SoundWire 中断与停钟

WCD9380 的 RX SoundWire 总线（`3210000.soundwire`）曾出现 `SWR bus clsh`
和 `Parity error`，早于四路麦克风改动的版本也有记录；还观察到
`wcd_interrupt_callback` 调用 `regmap_read` 时的空指针异常。四路录音使用的
TX 总线为 `33b0000.soundwire`，实际采集通过不能证明 RX 中断路径稳定。

原厂 [`wcd938x_bind`](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/asoc/codecs/wcd938x/wcd938x.c)
只把 codec IRQ domain 交给持有 regmap 的 TX 从设备。补丁
[`0052`](../build/kernel/patches/linux-7.3/0052-asoc-wcd938x-use-tx-register-interrupt.patch)
按此划分中断：RX 不启用 implementation-defined IRQ，回调也拒绝走 RX 寄存器路径；
TX 继续处理真实 codec 中断。共用回调检查映射与 IRQ 资源，读失败立即退出，
持续置位最多处理八轮后报错，避免空指针及无限循环。两侧的 parity/clash 检测仍保留。

SoundWire 停钟广播完成后，中断线程可能尚未退出，之后还可能到达停钟完成中断。
补丁 [`0053`](../build/kernel/patches/linux-7.3/0053-soundwire-qcom-quiesce-irq-before-clock-stop.patch)
先禁用并等待该 IRQ，再等帧发生器停止，最后关时钟；mode 0 恢复时等链路就绪后
才重新启用 IRQ。需要重新枚举的路径仍提前开启 IRQ，以接收枚举完成事件。
时钟开启失败则恢复原来的唤醒中断状态。停钟后的 IRQ 排空参考
[上游修复](https://lkml.iu.edu/2610.1/02715.html)，停止状态轮询及恢复期间的互斥参考
[原厂主控](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/soc/swr-mstr-ctrl.c)。

SoundWire 1.7 的 link manager 选择 EE1 时，命令也需使用 CPU1 的 FIFO。
原厂 `CONFIG_SWRM_VER_1P7` 的寄存器定义与该 ROM 的 `swr_ctrl_dlkm.ko`
均使用以下地址和中断位；补丁
[`0054`](../build/kernel/patches/linux-7.3/0054-soundwire-qcom-use-v17-cpu1-command-fifo.patch)
按实际硬件版本选择它们：

| 项目 | 旧 CPU0 配置 | 1.7 的 CPU1 配置 |
| --- | --- | --- |
| 写命令 / 读命令 | `0x300` / `0x304` | `0x31c` / `0x320` |
| FIFO 状态 / 读回数据 | `0x30c` / `0x318` | `0x328` / `0x334` |
| 读 FIFO 溢出 / 下溢中断 | bit 4 / 5 | bit 18 / 19 |
| 广播完成中断 | bit 10 | bit 20 |

只改变 FIFO 地址会漏掉广播完成事件，因此命令与中断配置必须一起更新。
处理程序将 CPU1 中断转换为共用的分派标识，但仍用原始硬件状态 ACK；
CPU0 的完成事件不能结束 CPU1 的广播等待。总线冲突及真实 FIFO 错误继续报告。
可从上述 ROM 的 `vendor_dlkm` 分区提取模块，用 `llvm-objdump -dr` 核对
`swrm_cmd_fifo_wr_cmd`、`swrm_runtime_suspend` 和中断处理函数。
该模块 SHA256 为 `64d696d65834ef37da6b398ea17c2eb239e95aeac7ecfd5c431d165afbeb25bd`。

旧的 CPU0 配置能收到命令完成，却不能让 EE1 的帧发生器真正停止；仅调整 IRQ
排空顺序不能解决这个问题。临时内核已验证启动、总线自动休眠/恢复、四路录音
反复启停、桌面静音恢复及受保护扬声器与麦克风同时运行，未复现总线冲突、
奇偶校验错误、停钟超时或 IRQ 异常。全系统睡眠与固件异常恢复的处理见下节，
长期录放音仍需单独验收。

## 系统睡眠与音频服务恢复

补丁 [`0056`](../build/kernel/patches/linux-7.3/0056-liuqin-audio-sleep-and-dsp-recovery.patch)
为本机补齐整卡睡眠和 ADSP 音频服务重新建立后的生命周期。

系统使用 `s2idle`。睡眠前先在 PCM 自旋锁之外停止 DSP graph，禁止已暂停的
采集回调继续提交读取，再在声卡 `suspend_pre` 中停用 SoundWire 数据端口，
最后才让 DAPM 关闭音频路径、时钟，并按依赖关系暂停 codec、总线和 macro。
把端口停用放在 `snd_soc_suspend()` 之后已经太晚：总线仍在传输，时钟却已关闭，
会在系统睡眠时产生 TX 总线冲突及后续 FIFO 错误。
唤醒时先恢复供应设备与端口，再由 ALSA 重新准备播放、采集；
不能仅恢复 DSP graph 而让 SoundWire 仍停在关闭端口的状态。
已 prepare、尚未 start 的采集也必须停止向 DSP 追加读取。
恢复时 ALSA 会把应用指针归零，AudioReach 的 `prepare` 也必须清零播放排队位置；
否则无需经过 STOP 的恢复路径会漏掉首批数据，表现为 RUNNING 但硬件指针不动。

ADSP 异常恢复与系统睡眠不同：服务会销毁、重新创建，LPASS 寄存器和引脚可能丢失，
但主机侧的时钟句柄、macro 与 SoundWire 设备仍然存在。实现参考原厂
[TX macro](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/asoc/codecs/bolero/tx-macro.c)、
[VA macro](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/asoc/codecs/bolero/va-macro.c)
及 WCD938x 的停机、复位和重新枚举顺序：

- PAS 保留 PRM 时钟提供设备；传输离线时拒绝新请求，及时结束正在等待的命令。
  新 PRM 先恢复硬件资源投票与仍有效的时钟请求，再注册声卡。
- 收到离线通知后，先将已登记设备的 regmap 切入缓存模式并标为需同步，
  避免拆除旧声卡时访问可能已经失去时钟的寄存器；设备恢复时再同步硬件。
- 旧声卡拆除后，先暂停 codec 和总线，再暂停 macro、VA 与引脚控制器；恢复顺序相反。
  这样即使设备仍处于自动休眠延迟窗口，也会重新执行硬件恢复。
- SoundWire 恢复总线复位、bank 状态和从设备枚举。LPASS 引脚先切入原厂 sleep
  配置，再切回 default，避免 pinctrl 因软件状态未变而省略写寄存器。
  扬声器 TDM 引脚也在声卡重建时重设。
- WCD9380 在新的恢复代次中重新复位、等待稳定并同步寄存器；本机未连接的
  耳机、AUX 和插孔检测中断不注册，实际 TX codec 中断和总线错误仍保留。
- 新声卡的 TX 软件混音路由初始为断开，必须清零存活 macro 中的通道掩码和计数。
  否则 UCM 的重复开启会被误判为无变化：PCM 显示 RUNNING，但 TX_MCLK 没有开启，
  四路采集均无数据。旧 jack 回调也须在释放声卡前注销。

恢复代次与板型判断由内建的 PAS 提供，兼容 SoundWire 内建、PRM 作为模块的组合；
板型判断继续使用 `liuqin_board_quirks()`，覆盖 ABL 改写根节点标识的启动方式。

APM 关闭还需区分正常停止与 DSP 已离线后的本地清理。原厂
[GPR 传输层](https://github.com/xiaomi-sm8450-kernel/android_vendor_qcom_opensource_audio-kernel/blob/721c4fb115d50aea74a2dec9ee864bf0f12682c8/ipc/gpr-lite.c)
在 DSP 不可用时返回 `-ENETRESET`（`-102`）；本项目据此在已确认服务离线时，
省去失效 graph 的 STOP/CLOSE 请求，继续释放启动引用、端口和内存。
请求等待期间发生离线时，也只将这两种关闭命令的 `-ENETRESET` 作为本地清理成功处理。
服务在线时的错误和超时照常返回，START、PREPARE、FLUSH 不使用此例外。

受控整 ADSP 停启会同时影响充电服务，只能作为诊断测试，不能作为日常音频修复命令。
M81 在传输暂时不可读时保留最近有效电量，并将充电状态标为未知，避免桌面把读取失败
显示为 0%；真实的低电量和首次读取失败仍保留，见[电量刷新](FASTCHARGE.md#电量刷新)。
原厂固件拒绝音频 PD 单独重启请求时，不绕过其限制，也不能把整 ADSP 停启结果视为
该接口或所有真实 DSP 崩溃场景已经验证。`deep` 睡眠不在本节的验收范围内。

临时内核已验证受保护扬声器与四路麦克风同时运行时的 `s2idle`、播放录音中的
受控整 ADSP 重启，以及重启后再次睡眠；播放、采集指针恢复推进，四路均有数据，
并确认扬声器自动恢复出声。原始 ALSA 采集运行中和 PREPARED 状态下的睡眠恢复
也已分别检查：前者重新准备后继续采集，后者唤醒后能开始四路录音。
诊断构建启用了 `CONFIG_PM_DEBUG` 和
`CONFIG_PM_SLEEP_DEBUG`；后续组合验证关闭了运行时 PM 调试输出，项目正式配置不增加这两个选项。
已确认离线的 graph STOP/CLOSE 会完成本地清理；临时内核复测受控停启后四路数据恢复，
未再出现这两种关闭请求的 `-102`。其他真实传输失败仍会报告，
应与服务重新就绪后的总线冲突、超时或持续无数据区分。

## 验证

先检查声音服务在未接外屏时能创建 Speaker 和 Mic，并以实际应用录音、回放确认
电平与内容。检查四路时使用配套内核及 UCM，以 48 kHz、S16_LE、四通道打开原始
采集 PCM，分别检查每路有信号；不要仅凭文件长度或 RUNNING 状态判定通过。
通过 `parec` 检查时，指定 `--channels=4 --channel-map=front-left,front-right,rear-left,rear-right --no-remix`，
与当前 Mic 信源的四声道映射一致。只指定通道数可能使用不同的默认映射，产生缺失或混合的测试通道。
重复开始、停止录音，调整桌面音量和静音后再录音，并检查睡眠恢复和全双工。
这些检查不需要连接外屏。
睡眠及服务恢复验收需同时确认播放、采集的硬件指针持续推进，并实际听到声音；
仅有录音数据或 PCM 的 RUNNING 状态，不能证明扬声器播放恢复。
测试音量使用 10%，结束后恢复原来的音量与静音状态。

```sh
arecord -l
pactl list short sources
amixer -c XiaomiPad6Pro cget "name='ADC1 Switch'"
amixer -c XiaomiPad6Pro cget "name='ADC1 Volume'"
cat /proc/asound/card0/pcm1c/sub0/hw_params
journalctl -k -b -g 'ASoC|APM|[Ss]ound[Ww]ire|wcd938|timeout|audio backend|Oops|Call trace'
```

离线检查集中为四个入口，均使用 `--source /path/to/assembled-kernel`：

- `tests/microphones.py`：四路接线、DSP 通道映射、TX mixer/mute、共享 ADC 时钟、
  高通滤波延时与离线收尾；包含声卡重建、360 种 ADC 子集和关闭顺序及任务竞态。
- `tests/dp-audio.py`：链路停启、首帧恢复、关闭顺序和未接外屏时的 PCM 探测。
- `tests/soundwire.py`：WCD 中断归属、SoundWire 停钟、FIFO/完成事件及复位恢复。
- `tests/audio-recovery.py`：服务与设备恢复顺序、时钟和端口、播放队列、回调注销，
  以及离线 graph 的引用释放；保留在线错误和非关闭请求的失败处理。

`microphones.py` 可追加 `--stock-overlay /path/to/entry.15` 从原厂 DTBO 读取传输参数；
先核对 SHA256 `295ef2f097fb1e75e0b1c2d4064d14b0fc244635bc18ee56347601df0c5deb87`
及板型。`soundwire.py` 同时追加上述参数和 `--dtb /path/to/sm8475-xiaomi-liuqin.dtb`，
还会比较两条总线的工作/休眠引脚状态。各入口的覆盖和参数见[测试说明](../tests/README.md#音频源码检查)。

实机还应检查只启用一路输入时其余时隙保持静音，并在保持四通道 PCM 运行时逐路切换
`ADCn_MIXER Switch`，确认其余通道继续采集，随后恢复全部路由；再检查短录音反复启停、
睡眠和受控恢复后的四路数据。不能用 PCM 的 RUNNING 状态代替实际采样推进。
离线检查不能替代四路实际采集与应用回放验收；当前验收范围见[硬件表](HARDWARE.md)。
