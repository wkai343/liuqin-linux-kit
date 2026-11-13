# 设备树与原厂配置对照

[硬件清单](HARDWARE.md) ｜ [构建指南](BUILD.md) ｜ [测试说明](../tests/README.md)

Linux 7.3.4 的原厂配置对照覆盖 CPU 调频、空闲状态、供电和互连带宽。
`0035` 补充 CPU 容量，`0036` 补充动态功耗系数和两级 BWMON。
截至 2026-11-11，Fedora 已完成编译及 Fastboot 临时启动验证：三个 CPU 能耗域、核心空闲驻留、
两级 BWMON 及负载下的带宽请求均有实机证据。各两次启动对照未显示明显加速，整机功耗尚未测量。
新增配置在 Rec 侧仅完成源码、配置和 DTB 离线检查，尚未构建并启动对应的新 Rec 镜像。

## 来源与移植原则

当前设备树由 kernel.org 7.3.4、保留已有 Liuqin 适配的
[`0001` 补丁](../build/kernel/patches/linux-7.3/0001-preserve-liuqin-adaptation.patch)及后续板级补丁组成。
`0001` 将 `linux-sm8450-liuqin` 的 `fe81794b` 基线和项目既有适配迁移到当前内核；
SoC、PMIC 与板级节点须配合本项目使用的主线驱动接口。

原厂设备树用于核对硬件地址、IRQ、频率表、电源约束和时序。移植时逐项转成当前驱动支持的绑定，
不能直接用原厂 DTB/DTBO 替换项目设备树；厂商 DCVS、memlat 等节点依赖不同的驱动接口。
来源与署名见 [CREDITS](../CREDITS.md)，实际输入与保留差异如下。

## 对照输入

- 当前内核基线、补丁顺序和最终源码树由
  [桌面源码锁](../build/kernel/source.json)与 [Rec 源码锁](../build/recovery/kernel/source.json)固定。
  板级 DTS 是 `arch/arm64/boot/dts/qcom/sm8475-xiaomi-liuqin.dts`，继承 SM8475、SM8450 和 PMIC 描述。
- 小米公开设备树固定在
  [`MiCode/kernel_devicetree` 的 `1c507816`](https://github.com/MiCode/kernel_devicetree/tree/1c507816ba8aa6117dcec41f577ef7a66d2091f7)，
  重点核对 `qcom/cape.dtsi`、`cape-regulators.dtsi`、`liuqin-sm8475.dtsi` 和 `xiaomi-sm8475-common.dtsi`。
- 原厂 ROM 固定为 OS2.0.6.0.VMYCNXM；先按
  [镜像锁](../shared/liuqin-rom-images.json)验证 `vendor_boot.img`、`dtbo.img`，
  再读取 `dtb-02.dtb` 和 Liuqin 的 `entry.15`。

公开源码与量产 ROM 分开核对。原厂 DTBO 在通用 `fdtoverlay` 下返回 `FDT_ERR_BADVALUE`，
离线对照因此分别读取基树和覆盖层，ABL 交付的运行设备树另行实机核验。
对照脚本解析全部 fragment 的符号目标，
将对已有节点的标量覆盖用于供电约束比较；CPU、BWMON、DCVS 总线宽度和频率表均确认未被
该 DTBO 覆盖。新增参数同时与公开源码和 ROM 基树核对。

## 检查结果

| 范围 | 修改前配置与原厂对照 | 7.3.4 处理 |
| --- | --- | --- |
| CPU 容量 | 缺少区分大小核能力的容量描述 | `0035` 补充原厂 `capacity-dmips-mhz`，由调度器结合最高频率归一化 |
| CPU 调频 | 三个 EPSS 域、CPU 归属已接入；主线驱动从硬件 LUT 读取频率和电压 | 保留硬件频率表、schedutil 和 fast switch，不添加静态 CPU OPP 表 |
| CPU 能耗模型 | 缺少原厂动态功耗系数 | `0036` 给 8 个 CPU 补充 `dynamic-power-coefficient` |
| CPU 空闲 | 两种核心状态、两种簇状态的 PSCI 参数与时延均一致；通过电源域引用 | 保留，不重复添加 `cpu-idle-states` |
| 供电 | 核对 17 个 PMIC 稳压器及全部 68 条启用设备的 supply 引用；存在既有范围差异 | 保留现有电压、HPM、常开策略；差异单列如下 |
| 已有互连 | 19 个启用消费者的路径均有有效提供者，涵盖 UFS、USB、显示、PCIe、QUP、视频等 | 保留原有路径与请求 |
| CPU→LLCC、LLCC→DDR | 原厂有两级 BWMON；修改前缺少监测节点 | `0036` 按主线 BWMON/OPP/ICC 绑定补齐，新增 2 个消费者 |
| L3 与 memlat | 原厂通过厂商 DCVS、PMU 采样与 memlat 策略调速 | 尚未移植；不能直接复制厂商节点或将阈值表改名为 CPU OPP 表 |

## CPU 能耗与空闲

| CPU | 容量 `capacity-dmips-mhz`（`0035`） | 动态功耗系数（`0036`） |
| --- | ---: | ---: |
| 0–3，A510 | 1024 | 100 |
| 4–6，A710 | 2253 | 257 |
| 7，X2 | 2386 | 509 |

`qcom-cpufreq-hw` 已通过 `register_em` 调用 OPP 能耗模型注册接口，配置也已启用
`CONFIG_ENERGY_MODEL`。新增系数与硬件 LUT 提供的电压、频率一起用于计算动态功耗估计；
这不是实际整机功耗测量，也不修改 CPU 电压。

空闲状态参数按“进入时延 / 退出时延 / 最短驻留时间”列出，单位为微秒：

| 状态 | PSCI 参数 | 时延与驻留 |
| --- | --- | --- |
| 小核 rail power collapse | `0x40000004` | 800 / 750 / 4090 |
| 大核、超大核 rail power collapse | `0x40000004` | 600 / 1550 / 4791 |
| L3 off | `0x41000044` | 1050 / 2500 / 5309 |
| APSS / LLCC off | `0x4100c344` | 2700 / 3500 / 13959 |

主线 `drivers/of/cpu.c` 的 `of_get_cpu_state_node()` 优先读取
`power-domains → domain-idle-states`，随后才尝试传统 `cpu-idle-states`。
本机 8 条 CPU 电源域链及簇状态均可解析，缺少传统属性不代表空闲状态缺失。
实机计数结果见下文；PSCI 计数不能代替整机功耗测量。

## 两级带宽监测

| 路径 | 寄存器与 GIC SPI | 原厂硬件 | 主线驱动回退 |
| --- | --- | --- | --- |
| CPU→LLCC | `0x190b6400`，581 | BWMON v4 | `qcom,sdm845-bwmon` |
| LLCC→DDR | `0x19091000`，81 | BWMON v5 | `qcom,sc7280-llcc-bwmon` |

两者都是 19.2 MHz 采样时钟、64 KiB 计数单位。V4 的原厂 global 区域与 monitor 区域重叠，
主线用一个 `0x300` 字节窗口；global IRQ 寄存器对应 monitor 基址的 `+0x8/+0xc`。
V5 保留 `0x1000` 字节窗口。两个窗口与现有 SoC 节点的寄存器区间不重叠。

原厂 LLCC 频率为 300、466、600、806、933、1066 MHz，总线宽度 16 字节；
DDR 频率为 547、768、1555、1708、2092、2736、3196 MHz，DCVS 总线宽度 4 字节。
补丁以 `频率(kHz) × 总线宽度(字节)` 换算成绑定规定的十进制 `opp-peak-kBps`，
没有套用其他 SoC 的带宽表，也没有把 kB/s 与 MiB/s 混用。

互连端点分别为 `MASTER_APPSS_PROC → SLAVE_LLCC` 和 `MASTER_LLCC → SLAVE_EBI1`，
使用当前主线 SM8450 提供者的编号。`QCOM_ICC_TAG_ACTIVE_ONLY` 包括运行与唤醒请求，
不添加睡眠请求。BWMON 根据流量选择已有档位，不给 CPU 锁频，也不替代原厂 memlat 的延迟策略。
Fedora 保留 `CONFIG_QCOM_ICC_BWMON=m`，由模块加载后接管；Rec 只构建内置内核和触控衔接模块，
因此将其改为 `CONFIG_QCOM_ICC_BWMON=y`，避免新增节点没有可用驱动。

GPU 带宽由 Adreno GMU 管理。当前 A730 驱动仍有 `a730_build_bw_table()` 回退路径，
不能仅因 GPU 节点缺少普通 `interconnects` 就判定没有带宽请求。SM8475 的 GPU 频率、
GX/CX 等级和 ACD 参数按原厂 13 档表补齐至 900 MHz；GMU 200/500 MHz 与既有
12 档带宽表已一致，详见 [GPU 与 OpenCL](GPU.md)。

原厂 L3 控制器位于 `0x17d90000`，共享投票偏移为 `0x90`，与主线 EPSS L3 接口有对应关系。
但仅添加提供者无法建立完整的消费策略。原厂 memlat 表是采样阈值，既不是硬件全部 CPU 档位，
也不等同于无条件带宽下限；直接作为静态 CPU OPP 表会让当前调频驱动过滤 LUT，并关闭
fast switch。该策略仍需独立移植与验证。

## 保留的供电差异

| 电源轨 | 当前主线配置 | 原厂有效约束 | 处理依据 |
| --- | --- | --- | --- |
| PM8350 L1 | 固定 0.912 V | 0.830–0.920 V，初值 0.912 V | 保留已有 USB PHY 工作点 |
| PM8350C S1、PMR735A S2 | 电压范围一致，另设常开 | 无对应常开标志 | 保留已有无线供电生命周期 |
| PM8350C BOB | 3.000–3.980 V | 3.008–3.960 V | 范围差异待音频、相机供电专项核实，不自动扩大或收窄 |
| PM8350C L9 | 2.960–3.008 V | 固定 3.300 V | 当前指纹部署校验明确要求现有范围；改变需同步指纹驱动及认证回归 |
| PM8350C L12 | 固定 1.800 V、常开 | 1.800–1.980 V、初值 1.800 V、常开 | 当前工作点在原厂范围内 |
| PM8350C L13 | 固定 3.304 V、常开 | 2.700–3.544 V、初值 3.300 V、常开 | 当前工作点在原厂范围内 |

其他列入对照的 PMIC 电压范围一致。UFS 的 L7B/L9B 和 PHY 的 L5B/L10C 供电连接已在；
原厂 `vdd-hba-supply` 对应的 UFS GDSC 在主线通过 `power-domains` 表达，无需再造稳压器。
相机两个固定供电轨的 100 µs 建立时间，也已用主线 `startup-delay-us` 表达。
原厂 ARC 虚拟电压等级由主线 RPMh 电源域表示，不能因稳压器节点名称不同而重复添加。

## 构建与验证

对应配置的离线检查：

- 重新应用 Fedora、Rec 补丁链，核对最终源码树和全部摘要。
- 在新组装的 Rec 源码上运行 `olddefconfig`，配置无漂移，BWMON 保持内置。
- 编译修改前后板级 DTB；原有 DTC 警告均为 28 条、类别不变，相关绑定的 `dt-validate` 诊断为零。
- 校验新增 BWMON 绑定，并用实际 Rec 装配流程验证 ABL 元数据和维护环境 DTB。
- [对照脚本](../tests/oem-device-tree.py)校验原厂输入摘要、CPU 功耗数据、PSCI 引用链、
  带宽表换算、寄存器不重叠、供电/互连提供者可用性；解析 phandle 重编号后，既有属性不变。
- 四种故意改错的 DTB（功耗系数、空闲域、带宽单位、既有供电约束）均被拒绝。
- 构建流程测试 44 项通过；Rec 源码测试 18 项通过，4 项需要完整 Rec 构建产物的检查未执行。

## CPU 与带宽实机结果

| 能耗域 | 核心 | 频点数 | 硬件频率范围 |
| --- | --- | ---: | --- |
| `cpu0` | 0–3 | 16 | 300–2016 MHz |
| `cpu4` | 4–6 | 20 | 633.6–2745.6 MHz |
| `cpu7` | 7 | 21 | 787.2–3187.2 MHz |

三个域的 debugfs 能耗表共 57 个频点，功率估计均非零，频率与硬件调频表逐项相符。
原厂系数为 100 / 257 / 509；三组 governor 均保持 `schedutil`，负载采样中观察到各组频率变化。
去掉本次配置的同内核对照镜像没有 CPU 能耗域，也没有两级 BWMON 设备实例。

`psci_idle` / `menu` 下，20 秒空闲观察中 8 个核心的深度状态均增加了驻留次数与时间，
各核新增驻留约 18.79–19.78 秒。簇级 S0（L3 off）新增约 16.05 秒、1351 次；
S1（APSS/LLCC off）自启动累计 698 ms、53 次，但在这 20 秒窗口内没有新增进入。
这些计数验证了状态注册与运行路径；长期空闲、睡眠恢复及实际功耗仍待独立测量。

两级 BWMON 均由模块自动绑定到 `qcom-bwmon`。使用四个内存复制工作线程、共 256 MiB 缓冲区
运行约 12 秒，得到以下请求变化，单位为绑定使用的十进制 kB/s：

| 路径 | 低负载/结束后请求 | 负载中最高请求 | 中断计数 |
| --- | ---: | ---: | --- |
| CPU→LLCC | 4,800,000 | 17,056,000 | 119→122 |
| LLCC→DDR | 2,188,000 | 12,784,000 | 136→141 |

两条路径的 tag 均为 3（ACTIVE + WAKE）。负载结束后请求回落，验证了动态投票，而非仅有设备节点。
这些数值是互连带宽请求，不是直接测得的总线时钟、DDR 实际吞吐或功耗。原厂 memlat/L3 策略仍未移植。
