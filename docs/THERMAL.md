# 温度监测与温控

[硬件支持](HARDWARE.md) ｜ [构建指南](BUILD.md) ｜ [充电](FASTCHARGE.md)

## 传感器与控制路径

TSENS 提供 SoC 内部温度，电池温度由 PMIC GLINK 固件提供。读取
`/sys/class/thermal/thermal_zone*/type` 确定名称，不依赖温区编号。
thermal 的 `temp` 单位是毫摄氏度；电池 power_supply 的 `temp` 单位是十分之一摄氏度。
PMK8350 的内部 ADC 可从 IIO 的 `in_temp_pmk8350_die_temp_input` 读取，单位为毫摄氏度。

| 温区 | 主机侧策略 |
| --- | --- |
| CPU0–3，各一个 TSENS；CPU4–6，各顶部和底部 TSENS；CPU7，顶部、中部和底部 TSENS | 每个传感器的 90°C、95°C 被动阈值均绑定所属 CPU 频率域；回差 2°C；被动调节期间每 250 ms 采样；保留 110°C critical 阈值 |
| GPU 顶部、底部 TSENS | 共用 GPU devfreq cooling；85°C 被动限频、1°C 回差；保留 90°C hot 和 110°C critical 阈值 |
| PM8350、PM8350B、PM8350C、PM8450、PMR735A | SPMI 温度告警；当前未接连续测温 ADC，低于告警阈值时的固定 37°C 是驱动估算值，不代表精确芯片温度 |
| 电池与机身热敏电阻 | `XM_THERMAL_TEMP` 与 ADC7 读数由充电温控服务组合，驱动 `qcom-battmgr-charge` cooling device；独立的 power_supply 电池温区仍没有 trip |

CPU 的 `0077` 补丁补齐板级 cooling maps。仅打开 `CONFIG_CPU_FREQ_THERMAL`
或看到 `cpufreq-cpu*` cooling device，不能证明温区已经连接到限频执行器。
每个 map 列出频率域中的所有 CPU，适应共享策略由不同 CPU 注册的情况。
空闲时依靠 TSENS 中断，被动调节期间的轮询使 `step_wise` 能继续收紧和解除限制。
这些配置使用板级设备树，不依赖 ABL 可能改写的根节点 compatible。

普通 DVFS 降频与热限频不同。核验时需要同时看到温度越过被动阈值、绑定的
`cooling_device*/cur_state` 增加，以及 CPU `scaling_max_freq` 或 GPU `max_freq`
实际降低。停载后的低频本身不能证明限频已解除；还需核对 cooling state 回到 0
且频率上限恢复。

## 充电温控

`0078` 提供 M81 原厂主机控制接口。`qcom-battmgr-bat/charge_control_limit` 和
`qcom-battmgr-charge` cooling device 的 `cur_state` 使用 **0～15 档位**，不是微安。
0 撤销主机热限制，15 是最高限制档；具体电流表仍由固件决定。
`charge_control_limit_max` 和 cooling `max_state` 返回最大有效值 15；原厂驱动的
`num_thermal_levels=16` 是档位数量，不能作为有效档位写入。

`qcom-battmgr-usb/input_current_limit` 保留原厂 SDP／固定 PD 写入入口：单位微安，
`-1` 请求撤销主机输入电流投票，但两种电源的固件语义不同。SDP 进入电流投票路径；
PD 先要求供电端首个 PDO 的 `USB Suspend Supported` 位为 1，为 0 时在投票前返回
`0x204`，Linux 如实返回 `EREMOTEIO`，限额保持不变。这是电源能力条件，
不是缺少主机认证或使能命令。即使该位为 1，锁定固件的 PD 分支也会将普通电流请求
转成撤销投票，不能把这个入口当作通用 PD 电流调节器。地址、摘要及复核步骤见
[USB 独立输入电流接口](FASTCHARGE-SOURCES.md#usb-独立输入电流接口与-0x204)。

读值是固件限额，不是实际电流；应同时检查 SET 结果、GET 限额和实际电流，
不能将拒绝或仅写入成功记录为限流成功。温控服务使用独立的热档位接口。
手动检查接口前应停止温控服务，完成后恢复原档位、撤销测试输入投票并启动服务。

`sm8475_raw/raw_xm_thermal_temp` 导出策略电池温度；`raw_bat_charge_limit` 和
`raw_bat_charge_limit_max` 保留固件属性 10／11 的原始电流报告。属性 10 的 SET 输入是档位，
GET 返回的却不是档位，不能拿 GET 数值当作档位确认。标准档位接口只报告已成功应答的主机请求。

控制写入校验完整应答的 owner、type、opcode、property 和返回码，拒绝其他属性、
短包及服务换代前的结果。失败不更新成功档位；失联期间读档位返回错误。
固件服务恢复后重放最后成功确认的热档位与输入限额，最多尝试十次；正常调用仍返回
真实错误。协议没有事务 ID，主机无法区分同一服务中同一属性的任意延迟旧应答。

桌面系统的 `liuqin-charge-thermal.service` 每两秒采样，使用原厂 normal 配置：

```text
T = (800 × 电池 - 74 × quiet + 131 × charger + 184 × cpu - 46 × wifi) / 1000 - 389
```

所有输入和计算结果都以毫摄氏度表示。电池输入来自原厂 `XM_THERMAL_TEMP`，
原始单位为十分之一摄氏度；外部热敏电阻由 ADC7 IIO `in_temp_<名称>_input` 提供。
`cpu_therm` 是机身热敏电阻，不能用 CPU 的 TSENS 核心温度代替。

| 升温触发 / 降温解除 | 亮屏档位 | 灭屏档位 |
| --- | --- | --- |
| 37 / 35°C | 2 | 0 |
| 39 / 37°C | 3 | 1 |
| 41 / 40°C | 7 | 3 |
| 42 / 41°C | 9 | 4 |
| 43 / 42°C | 12 | 5 |
| 45 / 44°C | 15 | 15 |

原厂 `200、301、703、904、1205、1515` 在用户态按屏幕状态拆成上述两组档位，
不能直接传给充电固件。Linux 用背光 `bl_power` 判断内屏状态，未知时选亮屏档位。
配置位于 `/usr/share/liuqin/charge-thermal.json`，观测结果位于
`/run/liuqin-charge-thermal/status.json`。服务退出保留限制；重新启动时在回差区间采用
较严格档位。短暂读数失败保留限制，连续三次失败请求 15 档，只有完整的新读数才能放宽。
这些失效处理、状态记录和服务重启规则是 Linux 适配，不是原厂时序常量。

八路 ADC7 外部通道对应原厂 DTBO 的 `0x144、0x145、0x146、0x147、0x347、0x34d、
0x44c、0x44`，保留 100 kΩ 上拉、比例校准、1:1 预缩放及 200 µs 稳定时间。
分别导出 `cpu_therm、flash_therm、quiet_therm、backlight_therm、conn_therm、charger_therm0、
wifi_therm、xo_therm`。外部热敏电阻与 PMIC 内部告警温区是不同传感器。

电池实际充电电流还受输入功率、系统负载、电量和固件策略影响；增加负载后电流下降，
不能单独作为热限流证据。主机服务覆盖桌面运行期，不替代固件的电芯／JEITA／充电芯片保护，
也不等同于 Android 全部场景策略、挂起期间机身温控或 initramfs 关机充电策略。

软件 thermal 温度模拟不会改变电池固件看到的真实温度，因此不能用它验收固件热限流。
真实高温充电保护及恢复仍需在有独立温度和输入电流测量的条件下单独验证，
并核对所用电源、协议和电量范围。原厂接口来源与缺口见[充电说明](FASTCHARGE.md)。

离线检查与实机命令：

```sh
devbox run -- python3 tests/charge-thermal.py
devbox run -- python3 tests/charge-thermal-kernel.py --source /path/to/assembled/source
sudo systemctl stop liuqin-charge-thermal.service
sudo python3 tests/charge-thermal-device.py --mode limits > out/charge-limits.jsonl
sudo python3 tests/charge-thermal-device.py --mode cycle > out/charge-cycle.jsonl
sudo systemctl start liuqin-charge-thermal.service
```

`limits` 在同一电源和空闲负载下检查 0→7→12→15→0 档位、实际充电电流，
再检查 500 mA 输入限额；固件拒绝时单独记录错误及限额未变，不宣称该独立接口通过。
接受时继续检查撤销投票。`cycle` 临时使用低温单级策略，加载四个小核 CPU 0～3，
真实升温越过起始温度 +0.8°C 后请求 15 档；停载自然降至起始温度 +0.2°C 以下时解除，
并检查充电恢复。该方法验证主机控制链路，不加热到原厂 45°C 阈值。
测试要求外部电源在线、初始有可测充电电流，电池到 38°C 或 SoC 到 70°C 时中止；
普通异常、SIGINT、SIGTERM 会停载并恢复原档位。不要用 SIGKILL 中断测试。

单台设备的电脑固定 PD 供电测试已覆盖上述真实温升—限流—自然降温解除循环，
并在默认内核普通启动后核对服务自启动、九路 IIO 温度和低温下恢复 0 档。
15 档下 USB 实际输入约 0.5 A，循环中的电池充电电流约 0.15 A，解除后恢复约 0.60 A。
这些是该电源、电量与空闲负载下的读数，不是固定档位电流承诺。
接近满电时固件会另行降低电池电流；复查应同时记录档位、USB 输入限额和实际电流，
避免将充满后的电流回落误判为无法解除热限制。

## 可复现检查

单台设备的临时启动和更新后的默认启动已覆盖三个 CPU 频率域、两个 GPU 温区的
真实温升限频、自然降温恢复，以及 13 个 CPU 温区的实时绑定。正常结束和受控
SIGTERM 中断后的阈值恢复也已检查。该范围使用降低的被动阈值，不能替代默认高温
阈值、长时负载、充电固件热保护或 Recovery 的实机验收。

编译后的 DTB 检查覆盖 13 个 CPU 温区、两级被动阈值、完整频率域成员、
critical 阈值及现有 GPU 策略：

```sh
devbox run -- python3 tests/thermal-cooling.py --dtb /path/to/sm8475-xiaomi-liuqin.dtb
```

将 `thermal-device.py` 与 `thermal-gpu-load.py` 放到平板上的 `tests/` 目录，
在其上级目录逐项运行，前一项结束且设备冷却后再开始下一项：

```sh
mkdir -p out/thermal
sudo timeout --signal=TERM --kill-after=15 240 python3 tests/thermal-device.py \
  --zone cpu0-thermal --cpu 0 > out/thermal/cpu0.jsonl
sudo timeout --signal=TERM --kill-after=15 240 python3 tests/thermal-device.py \
  --zone cpu4-bottom-thermal --cpu 4 > out/thermal/cpu4.jsonl
sudo timeout --signal=TERM --kill-after=15 240 python3 tests/thermal-device.py \
  --zone cpu7-middle-thermal --cpu 7 > out/thermal/cpu7.jsonl
sudo timeout --signal=TERM --kill-after=15 240 python3 tests/thermal-device.py \
  --zone gpu-top-thermal --workload python3 tests/thermal-gpu-load.py \
  > out/thermal/gpu-top.jsonl
sudo timeout --signal=TERM --kill-after=15 240 python3 tests/thermal-device.py \
  --zone gpu-bottom-thermal --workload python3 tests/thermal-gpu-load.py \
  > out/thermal/gpu-bottom.jsonl
```

脚本临时将所选温区的第一个被动阈值降至当前温度以上 4°C、且不低于 35°C，
使用真实负载和传感器读数检查限频；停止负载后等待自然降温跨过回差、解除限制。
CPU 负载覆盖指定核心所属频率域的全部核心，GPU 负载使用 FD730 离屏 GLES，并检查绘制和计算回读。
测试不改 critical/hot 阈值、频率 governor 或最低频率。
结束、普通异常和 SIGINT/SIGTERM 均恢复被动阈值，并核对所有温区的温度阈值未变。
温度读取失败、SoC 达到 75°C 或电池达到 40°C 时停止负载；这些是测试停止条件，
不代替设备硬件保护。不要用 SIGKILL 中断正在修改阈值的测试。

此方法验收的是较低实温下的传感器—thermal governor—限频—恢复链路，
不等同于持续运行到默认 85/90/95°C 的高温验收，也不覆盖 critical 关机、
硬件独立保护或充电固件的热策略。临时启动候选镜像仍须遵守
[现有系统绑定要求](BUILD.md#已安装系统的临时启动)，测试后回到已安装默认内核。
精确镜像、日志及测试结果保存在忽略的 `out/` 目录。

## 源码依据

- Linux `arch/arm64/boot/dts/qcom/sm8450.dtsi`：CPU/GPU TSENS、阈值和 GPU maps。
- Linux `sm8150.dtsi`、`sm8250.dtsi`：共享 CPU 频率域的 cooling maps 和 250 ms 被动轮询。
- Linux `drivers/thermal/thermal_of.c`、`cpufreq_cooling.c`、`devfreq_cooling.c`：绑定和频率约束。
- Linux `drivers/thermal/qcom/qcom-spmi-temp-alarm.c`：未提供 ADC 时根据告警档位估算温度。
- Linux `drivers/power/supply/power_supply_core.c`、项目适配后的 `qcom_battmgr.c`：电池温区和固件属性边界。
