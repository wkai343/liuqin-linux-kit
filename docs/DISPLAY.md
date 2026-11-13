# 显示驱动与排查

[项目首页](../README.md) ｜ [内核适配总清单](HARDWARE.md) ｜ [测试说明](../tests/README.md)

当前桌面配方支持天马和华星 M81 屏，2880 × 1800，默认开启 60/90/120/144 Hz 在线切换，首选 120 Hz。
实机验证限一台天马屏；华星已接入代码并核对原厂配置，尚未真机验收。

## 当前实现

| 环节 | 当前行为 |
| --- | --- |
| 面板识别 | 显示与触控共用 ABL 主屏参数 `msm_drm.dsi_display0`，选择天马/华星初始化表及触控固件；未知或冲突时保留通用显示命令和设备树固件 |
| 初始化与 PHY | 按原厂包类型、分组、等待和广播方式发送寄存器配置及 PPS；已识别的面板和双 DSI 链路使用原厂 D-PHY 时序 |
| 供电与复位 | 保留原厂电压、稳定时间和复位波形；关屏保留 IOVDD/偏压，关闭两路 1.3 V，尚未实现完整五路断电 |
| DSI 启停 | 初始化及 PPS 完成后启动视频；关屏依次停显示控制器、停视频、发送 OFF；时钟门控保持关闭 |
| 背光 | 驱动探测时按默认亮度点亮；DRM 接管后仍按首帧流程开启背光，完成正常显示和 CABC，再执行延迟调光；关屏取消过期任务 |
| 命令调度 | 视频运行时为双路命令选择相同扫描行并核对行计数；等待有上限，传输后恢复寄存器，部分失败不原地重发 |

时序、命令及校验来源见[四档切换补丁](../build/kernel/patches/linux-7.3/0024-liuqin-refresh-modes-and-live-switching.patch)和[来源说明](../CREDITS.md)。

### 四档在线切换

在线切换成功时不关闭面板、不重载触控固件。驱动要求面板身份明确、双路 DSI 就绪，且显示资源与链路时钟保持不变。
涉及 90/144 Hz 时，在垂直消隐区提交 DSI/DPU 时序，随后发送新档位的 B3/B2 扫描命令，再等待帧边界、核对寄存器并更新缓存。
命令发送使用当前帧的旧缓存；每路 DSI 关闭时序双缓冲前保留约 2 ms 等待及写屏障。
120 ↔ 60 Hz 仅更新垂直消隐，省略相同的扫描命令。

准入失败、时序/命令错误、关闭屏幕或改变显示资源时，仍执行完整模式切换，可能短暂黑屏并重新加载触控固件。
驱动记录拒绝阶段与实际值；已失败的部分命令不在原事务中重发。

### 显示与触摸恢复时序

已识别的天马屏按以下顺序唤醒；华星及不满足条件的面板保留各自完整 ON 表。

1. 完成寄存器配置及 PPS，启动视频并等待主机首帧。
2. 发送 `0x11`（等待 120 ms）与 `0x29`（等待 20 ms），每个显示周期只执行一次。
3. 显示激活后加载触控固件，连续 20 次、间隔 10–11 ms 确认正常状态后开放输入，最多查询 150 次。

天马固件校准超时时，最多协调一次显示休眠/唤醒和固件重载，不独立复位共享 TDDI 的电源、GPIO、DSI 或 PHY。
失败或关屏取消时保持输入关闭。显示就绪依赖独立于校准恢复开关。
若仅笔启用命令失败，手指输入仍开放，并在约 100 ms 后重试一次；关屏、挂起和移除会取消重试。

### 开关机画面

Fedora 图形启动先使用固件帧缓冲，再交给原生 DRM；接管仍需短暂熄屏、复位面板。
开机动画按保留的小米标识确定方向，识别失败时横屏；关机使用最后一次有效 Wayland 内屏方向，缺失时回退到开机方向或横屏。
动画不通过重力传感器实时旋转，Fastboot 临时启动的黑帧也不能代替普通启动验收。

根系统对 Plymouth 客户端等待设置上限，超时记录状态并尝试退出已核对身份的动画进程，避免阻塞后续服务。
此保护未查明动画进程失去响应的内部原因。升级图形启动需包含配套主题、方向服务和插件。

### 登录与会话启动

[liuqin-login-ready](../build/rootfs/plasma-overlay/usr/libexec/liuqin-login-ready)只等待内屏 DRM、
同一设备的渲染节点、触摸、电源键和背光完成 udev 初始化，代替全局 udev 等待。
它核对机型、字符设备号、内屏连接状态和模式；必要设备缺失或检查失败时阻止登录启动，
等待上限为 8 秒。存储保护、logind 和 Plymouth 交接顺序保持不变。

PowerDevil 设置 `POWERDEVIL_NO_DDCUTIL=1`，省去内屏不使用的 DDC/I²C 探测；
原生背光、电池与电源管理保留，外接显示器的 DDC 亮度控制随之停用。
initramfs 动画准备与 UFS 校验并行，在根系统准入前等待动画准备结束并独立复查存储。
[RTC 采样](RTC.md)和 WLAN 模块在根系统早期并行初始化，配套要求见
[启动与设备适配](BUILD.md#启动与设备适配)。

### USB-C DisplayPort

板级 DP 链路速率上限沿用原厂 Liuqin DTBO 的 HBR2（每条通道 5.4 Gbit/s）。
纯 DP 的 Type-C C/E 分配允许四条通道，和 USB 3 共用的 D/F 分配允许两条。
驱动同时受显示器、链路中继器和已生效的 QMP 复用器状态限制，无法确认四条可用时按两条处理。
每次连接都从两条 RBR 通道初始化 AUX，再按接收端能力和复用器状态训练链路。

连接后可用 `journalctl -k -b -g 'DP: using'` 查看驱动用于筛选模式的通道数和速率上限，
用 `kscreen-doctor -o` 查看 Plasma 提供的模式和当前选择。通道数和速率上限还需与训练结果核对。

DP 音频由声卡中的 `DISPLAY_PORT_RX_0` 后端连接到 `mdss_dp0` 的 HDMI codec。
[板级 AudioReach 拓扑](../device/audio-topology/liuqin-speakers.m4) 用独立的
`MultiMedia3` 前端驱动 DSP 的 DisplayPort sink（`0x07001069`），固定为
48 kHz、S16_LE、双声道；受保护的四通道扬声器仍使用 `MultiMedia1`。
该参数与原厂 `backend_conf.xml` 的 `DISPLAY_PORT-RX` 一致；设备树接法沿用
主线 Qualcomm 声卡的 DP 后端，见 `sm8650-ayaneo-pocket-s2.dts`。

[UCM](../device/board-overlay/usr/share/alsa/ucm2/Qualcomm/sm8450/Xiaomi-Pad-6-Pro/HiFi.conf)
在 DP mixer 存在时添加 DisplayPort 输出，使用 PCM 2 和 `DP0 Jack` 插拔检测。
路由在 HiFi verb 阶段建立，以便 PipeWire 探测 PCM；DSP 增益固定为 unity（8192），
桌面使用软件音量。旧拓扑没有该控件时不会声明不存在的 DP PCM。

关闭与恢复参考 Qualcomm 下游驱动将显示会话和音频引擎分开管理的做法：
[`dp_audio.c`](https://github.com/LineageOS/android_kernel_xiaomi_sm8450-modules/blob/3b30ba7df6608c97c0ba2d55d679a6b5987c4c66/qcom/opensource/display-drivers/msm/dp/dp_audio.c)
与 [`dp_display.c`](https://github.com/LineageOS/android_kernel_xiaomi_sm8450-modules/blob/3b30ba7df6608c97c0ba2d55d679a6b5987c4c66/qcom/opensource/display-drivers/msm/dp/dp_display.c)。
该参考不证明与出厂 M81 模块源码完全一致。`0048` 通过 HDMI codec 的同步链路通知，
在 PUSH_IDLE 或 PHY 关闭前停掉 DPCM 音频后端，DPU 确认首次显示提交完成后为仍然打开的前端
重新执行后端的参数配置、prepare 和启动。仅恢复 SDP/ACR 不足以恢复被链路复位打断的
AudioReach DMA。后端可用性与 DPCM 路由共用声卡 PCM 锁；已经关闭的前端不会重新启动。
显示侧另用互斥锁协调 ALSA prepare/shutdown 和已上电的音频寄存器访问。
桥接器 enable 早于 DPU kickoff，不能在其中同步启动 DSP；显示提交失败不恢复音频，
拔出或再次关闭会取消待恢复状态。启用链路管理的播放流将 HDMI 音频引擎关闭延后到
component close，等 CPU DAI shutdown 停止 DSP 图后再执行。PCM trigger 中不等待同步
DSP 停止应答：音频周期通知与应答共用接收工作线程，前者可能正在等待 PCM 锁。
普通模式切换保留 Jack 状态，物理拔插仍上报连接变化；未使用链路通知的驱动和录音
保留原关闭时序，DPCM 路由筛选不作用于静态 PCM。

单台外屏的临时内核验证覆盖 ALSA 48 kHz、16 bit 双声道实际出声、停止后重新播放，
以及 ALSA 和桌面默认 DisplayPort 输出在播放中切换 2560 × 1600 的 120/144 Hz。
切换时声音短暂停顿，随后自动恢复；桌面默认输出在物理拔插后也能自动恢复。
这些检查中未出现 `audio comp timeout`、DSP 命令超时或 `Unexpected DP AUX IRQ`。
这不代表无缝播放；其他外屏、双通道扩展坞、长时间往返及睡眠恢复仍待验证。

没有外屏时，链路管理会排除尚不可用的 DP 后端，但桌面仍会探测其 PCM。
若前端返回 `no backend DAIs enabled`，PipeWire 会将整个 HiFi 配置判为不可用，
连带隐藏内置扬声器和麦克风。现在对“有效路由但链路被明确暂停”的播放前端允许
独立打开，后端继续保持关闭，等待链路恢复后再连接；真正缺失的路由和连接错误仍报错。
离线检查已覆盖该区分；配套 UCM 下，未接外屏的临时启动和默认内核普通重启
均可识别板载扬声器、四路麦克风及 DP PCM。
该启动场景下之后接入外屏仍需复测。

核验时使用每秒变调、有间隔的测试音，先确认拔出或切换前有声，再检查操作后的实际
声音、播放指针和内核日志；最后停止并重新播放，确认关闭没有额外等待或影响下次启动。
不能仅凭 RUNNING 或画面恢复判断通过。使用 `pw-cat` 测试桌面默认输出时省略
`--target`；`--target 0` 会禁止自动连接。

```sh
aplay -l
amixer -c XiaomiPad6Pro cget "iface=CARD,name='DP0 Jack'"
cat /proc/asound/card0/pcm2p/sub0/hw_params
cat /proc/asound/card0/pcm2p/sub0/status
journalctl -k -b -g 'audio comp timeout|Unexpected DP AUX IRQ|APM|ASoC|audio backend'
```

## 默认配置

### 刷新率

四档使用相同的 DSI 请求时钟：像素时钟 167838626 Hz、字节时钟 125878969 Hz。
运行时核对实际时钟与缓存，M81 目标容差为 ±100 Hz，不依赖重复调用时钟取整所得的结果。

| 档位 | 全屏 HFP | VFP | DRM 像素时钟 kHz |
| --- | --- | --- | --- |
| 120 Hz（首选） | 200 | 26 | 785245 |
| 144 Hz | 52 | 26 | 874197 |
| 90 Hz | 500 | 26 | 672478 |
| 60 Hz | 200 | 3148 | 785245 |

HFP 为完整双路图像宽度；DSC 只压缩有效像素，表中的 DRM 时钟不等于 DSI 链路时钟。
30/48/50 Hz 不在默认模式和在线切换范围内，仅保留诊断入口。

以下面板参数均加前缀 `panel_novatek_nt36532.`；桌面默认值由[启动参数](../device/native-bootargs.txt)和驱动共同决定。

| 参数 | 桌面默认 | 用途 |
| --- | --- | --- |
| `liuqin_multi_refresh` | `1` | 设为 `0` 只提供 120 Hz |
| `liuqin_live_60`、`liuqin_live_60_csot` | 均为 `1` | 分别允许天马/华星的 60 Hz 模式与在线路径 |
| `liuqin_live_all` | `1` | 开启四档在线切换；设为 `0` 后只保留 120 ↔ 60 Hz 在线路径 |
| `liuqin_low_refresh` | `0` | 设为 `1` 暴露完整低频诊断组，不代表这些档位通过验收 |
| `liuqin_late_wake`、`first_frame_backlight` | 均为 `1` | 天马首帧后唤醒与首帧背光 |
| `liuqin_retained_power` | `1` | 关屏保留部分供电 |
| `liuqin_skip_power_on_wait`、`liuqin_skip_post_reset_wait`、`liuqin_skip_post_pps_wait` | 均为 `1` | 对已识别面板省去三处额外的 120 ms 等待，保留原厂命令等待；设为 `0` 可恢复对照 |

三个 `liuqin_live_*` 开关在驱动未收到参数时默认为关闭。旧镜像不会随仓库配置更新而改变。
桌面还启用 `msm.liuqin_oem_video_order=1`、`msm.liuqin_oem_shutdown_order=1` 和
`msm.liuqin_cmd_dma_schedule=1`，保持 `msm.liuqin_oem_clk_gating=0`。
未知面板或缺少双 DSI 能力接口时使用完整模式切换。

DPU 关闭链路时，先清空软件缓存中的上一帧刷新请求，再构造本次关闭所需的更新。
硬件已经消费上一帧，并不代表软件缓存也已清空；停止时序后重放旧请求，可能让
CTL 的刷新位无法消退并触发 `vblank timeout`。此处采用
[上游修复及审查](https://lists.openwall.net/linux-kernel/2026/09/13/976)
中的处理顺序，保留硬件复位、本次关闭提交和后续帧完成检查。
`tests/display-live-refresh.py --source /path/to/assembled-kernel` 执行实际关闭和提交等待函数，模拟 32 种残留请求组合；
移除入口清理后，同一模型应重新报提交超时。实机仍需结合全系统睡眠、桌面熄屏恢复、
帧计数推进与内核日志验证，单次未出现超时不能证明所有唤醒场景均已覆盖。

### 亮度与 CABC

逻辑亮度 0–4095 按原厂曲线映射到背光 0–2047；最低非零值 `1` 映射为 `17`。
直接向背光 `brightness` 写 `0` 仍会熄背光。CABC 可选 `off/ui/still/movie`，分别对应关闭、界面、图片和视频策略。

```sh
liuqin-display-policy status
sudo liuqin-display-policy set ui
sudo liuqin-display-policy set ui --save
```

不加 `--save` 只影响本次启动，关屏恢复后仍会重用该选择；加上后保存到 `/etc/liuqin/display-policy.json`。
删除该文件并重启可取消保存。无配置时保留初始化策略，不强制开启 CABC。
`programmed` 只表示命令发送成功，不是硬件读回或画质、耗电效果的验证。

## 验证结论

| 项目 | 已确认范围 |
| --- | --- |
| 四档在线切换 | 一台天马屏通过全部 12 个方向；画面与连续手指输入正常，面板初始化、固件重载、看门狗、ESD 和 underrun 均未增加 |
| 实际刷新率 | 约 60.002 / 89.902 / 120.004 / 143.749 Hz，已核对实际帧率而非仅看桌面档位 |
| 亮灭屏 | 天马 90/120/144 Hz 普通换档及熄屏恢复、首帧后唤醒、手动亮度可用；命令调度另有普通重启和冷启动检查 |
| 关闭刷新状态 | 清理残留请求后，单台天马屏在 120 Hz 的重复 s2idle 和桌面熄屏恢复中，帧计数继续推进，CTL/DSC 刷新位清零，未复现提交超时，用户确认画面正常；仍属于短时验证 |
| 启动默认亮度 | 取消初始关闭背光后，Fedora 与 Rec 均通过 Fastboot 临时启动，用户确认画面和手指触控正常；长期与冷启动待验证 |
| 手写笔 | 戴尔 PN7522W 在 120 Hz 和在线切入的 60 Hz 有坐标、落笔和压感；90/144 Hz 未收到笔事件 |
| 离线回归 | 双面板 24 个切换方向、57 项故障/范围检查及 84 项显示与触控初始化检查通过；源码树合并前后一致 |

实机结论为短时、单块天马屏验证。Fedora 7.3.4 已复测全部 12 个方向，画面与连续手指输入正常；
对应日志、内核与产物身份保存在本地 `out/releases/liuqin-fedora-7.3.4/`。
手写笔等既有专项记录保留在 `out/releases/liuqin-live-refresh-r6/`；回归范围和执行方法见[测试说明](../tests/README.md#刷新率模式)。

## 待解决与待验证

| 项目 | 当前边界 |
| --- | --- |
| 华星屏 | 初始化、时序、PHY 和扫描命令已离线核对；点亮、切换、熄屏恢复及触控笔全部待真机验证 |
| 60 Hz 与低频 | 60 Hz 首次点亮/熄屏恢复待验收；普通换档至 60 Hz 及更低诊断档位存在闪烁、告警或黑屏记录 |
| 长期运行 | 四档长期往返、反复唤醒、异常恢复、其他面板批次及功耗待验证 |
| 显示接管与供电 | 尚无连续无黑屏接管、完整五路断电和原生显示状态无缝继承；DSI 时钟门控保持关闭 |
| 画质与笔 | CABC 观感及节电收益待测；笔适配重点为 60/120 Hz，90/144 Hz 无笔输入暂不排查 |

## 排查方法

### 在线切换与告警

从 120 Hz 进入桌面，在每次切换前后记录画面、手指输入、实际帧率及以下计数：

```sh
sudo journalctl -k -b -g 'Liuqin.*live|initial DDIC wake|TDDI|resume failed closed'
cat /sys/bus/mipi-dsi/devices/ae94000.dsi.0/esd_status
cat /sys/bus/spi/devices/spi0.0/touch_state
cat /sys/module/panel_novatek_nt36532/parameters/liuqin_live_all
```

- 在线成功应有 `Liuqin: live refresh … Hz`；`cycle`、`events`、`firmware_update_count` 和看门狗计数不增加。
- `Liuqin live reject: stage=…` 给出面板、时钟、硬件状态、帧窗口或锁存等拒绝原因；随后可能执行完整换档。
- 面板告警还需核对 GPIO 电平。固件版本、A3 状态有效或 SPI 错误为零，不能代替实际输入与看门狗检查。
- 出现黑屏、触控失效、连续 `FE` 或新增 `wdt_frames/wdt_events` 时停止测试并保存日志。普通换档不保证能恢复；必要时通过 Fastboot 临时启动已验证镜像。

已有四档实现的内核可在本次启动切换开关；写回 `0` 停用扩展，不中断正在执行的切换：

```sh
echo 1 | sudo tee /sys/module/panel_novatek_nt36532/parameters/liuqin_live_all
```

60 Hz 测试结束后回到 120 Hz，避免桌面下次启动恢复到尚未验收的首次点亮路径。
常用对照参数包括 `panel_novatek_nt36532.liuqin_multi_refresh=0`（单一 120 Hz）、
`msm.liuqin_cmd_dma_schedule=0`（关闭命令排程）和 `plymouth.enable=0`（关闭动画）。

### 笔与触控诊断

在 60/120 Hz 使用同一支笔，按相同悬停与落笔动作比较计数差值。
用 `libinput list-devices` 找到 `NVTCapacitivePen` 对应的 `/dev/input/eventN`，
再用 `evtest /dev/input/eventN` 或 `libinput debug-events --device /dev/input/eventN` 核对输入。

```sh
tr ' ' '\n' </sys/bus/spi/devices/spi0.0/touch_state |
  grep -E '^(pen_|wdt_|resume_count=|firmware_update_count=)'
```

| 字段 | 判断方法 |
| --- | --- |
| `pen_pos`、`pen_none`、`pen_id`、`pen_other` | IRQ 收到的位置、无笔、笔 ID 和未知报文；普通手指帧也可能带无笔标记 |
| `pen_reported` | 已提交的位置帧；`pen_pos` 增长而它不增长表示驱动过滤；两者均增长则继续查输入事件和应用 |
| `pen_spi_errors` | SPI 失败计数；两类位置计数都不增长时结合无笔帧判断是否有效采样 |
| `pen_enable_attempts`、`pen_enable_failures`、`pen_enable_err` | 笔唤醒命令的尝试、失败及最后返回值；返回 0 仅表示发送成功 |
| `wdt_frames`、`wdt_events` | 前六字节均为 `FD/FE` 的看门狗报文；连续 11 帧记一次事件，不进入正常输入解析 |
| `wdt_streak`、`wdt_pattern` | 连续计数上限 11，正常帧或 SPI 错误清零；模式保留最近值 |

计数从驱动绑定起累积。首次看门狗报文释放已有触点，但不从 IRQ 中复位共享 TDDI。

### 自动恢复与启动日志

内核校准恢复与面板告警会话服务独立。后者只操作当前活动 Wayland 会话，核对 GPIO 电平后关闭、重新开启输出，
每用户每 10 分钟最多两次、间隔至少 60 秒；关屏时不触发，也不把 CABC 下 PWM 为零单独视为故障。

```sh
journalctl --user -b -u liuqin-display-recovery.service
sudo journalctl -b _SYSTEMD_USER_UNIT=liuqin-display-recovery.service
sudo journalctl -b -u plymouth-read-write.service
```

Plymouth 超时保护记录位于 `/run/liuqin-plymouth-guard/events.jsonl`。
报告故障时注明发生在 initramfs、登录界面还是桌面，以及是否经历按键、睡眠或恢复。
