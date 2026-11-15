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
| 电池 | PMIC GLINK 提供温度读数；独立的 power_supply 电池温区没有 trip，充电固件热保护须另行验证 |

CPU 的 `0077` 补丁补齐板级 cooling maps。仅打开 `CONFIG_CPU_FREQ_THERMAL`
或看到 `cpufreq-cpu*` cooling device，不能证明温区已经连接到限频执行器。
每个 map 列出频率域中的所有 CPU，适应共享策略由不同 CPU 注册的情况。
空闲时依靠 TSENS 中断，被动调节期间的轮询使 `step_wise` 能继续收紧和解除限制。
这些配置使用板级设备树，不依赖 ABL 可能改写的根节点 compatible。

普通 DVFS 降频与热限频不同。核验时需要同时看到温度越过被动阈值、绑定的
`cooling_device*/cur_state` 增加，以及 CPU `scaling_max_freq` 或 GPU `max_freq`
实际降低。停载后的低频本身不能证明限频已解除；还需核对 cooling state 回到 0
且频率上限恢复。

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
[现有系统绑定要求](BUILD.md#当前构建产物)，测试后回到已安装默认内核。
精确镜像、日志及测试结果保存在忽略的 `out/` 目录。

## 源码依据

- Linux `arch/arm64/boot/dts/qcom/sm8450.dtsi`：CPU/GPU TSENS、阈值和 GPU maps。
- Linux `sm8150.dtsi`、`sm8250.dtsi`：共享 CPU 频率域的 cooling maps 和 250 ms 被动轮询。
- Linux `drivers/thermal/thermal_of.c`、`cpufreq_cooling.c`、`devfreq_cooling.c`：绑定和频率约束。
- Linux `drivers/thermal/qcom/qcom-spmi-temp-alarm.c`：未提供 ADC 时根据告警档位估算温度。
- Linux `drivers/power/supply/power_supply_core.c`、项目适配后的 `qcom_battmgr.c`：电池温区和固件属性边界。
