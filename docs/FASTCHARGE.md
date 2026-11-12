# 充电与快充

[项目首页](../README.md) ｜ [构建指南](BUILD.md#启动与设备适配)

充电档位由 ADSP 固件协商；项目提供双电芯认证、小米 MiPPS 适配器握手及标准 PPS 开关。
这些接口尚不等于完成 67 W 快充适配，协议握手、充电通路和实际输入功率需要分别确认。

| 路径 | 条件与行为 |
| --- | --- |
| MiPPS | 支持小米私有协议的适配器通过 UVDM 认证后，由固件选择快充档位 |
| 标准 PPS | 默认保留固件合同；认证门控覆盖只保留为显式诊断选项，见下述限制 |
| 普通 PD、DCP、SDP | 沿用固件行为 |

基础充电、电量显示与电芯认证已验证；MiPPS/PPS 适配器尚未完成完整功率测试，不能仅凭接口或构建通过认定快充功率。

## 原厂对照与适配范围

对照[锁定 ROM](../shared/liuqin-rom-images.json) `OS2.0.6.0.VMYCNXM`
中的充电模块与 `batterysecret` 用户态认证程序；公开源码引用固定到 MiCode 的 Liuqin 提交，
版本、函数位置与离线复核方法见[原厂依据](FASTCHARGE-SOURCES.md)。其中
`qti_battery_charger_main_m81.ko` 的扩展属性编号须按量产模块核对。该模块 SHA-256 为
`b13b976e8a0d5ce2256df4f96368a6de0999aecf4dd311a9ded0f993e2cedf6b`。

| 项目 | 当前实现与缺口 |
| --- | --- |
| 功率属性 | `0038` 将 `apdo_max` / `power_max` 对齐为原厂 M81 的 45 / 46；旧编号 43 / 44 不能用于判断功率能力，43 实际是 `soc_decimal_rate` |
| 快充类型原始值 | `sm8475_raw/raw_xm_quick_charge_type` 对齐为 M81 的 44；它只是原始属性，原厂 `quick_charge_type_show()` 还结合健康状态、协议、认证和最大功率分类 |
| 标准 PPS 回退 | `--allow-unverified-adapter` 只在私有握手完整结束、有效摘要比对失败后检查；PDO2 为零、传输失败、挑战应答或适配器 ID 缺失均不进入回退 |
| 连接来源识别 | `0039` 为 Liuqin 的 UCSI 1.0 固件补读当前供电对端的 PDO；首个固定 PDO 明确不支持 USB 数据且合同为 PPS 时，可先切换角色发现小米身份；USB 数据来源优先保留原角色，即使 SVID 残留 |
| UCSI 协商读数 | `0040` 按所选 PDO 类型解码请求；PPS 使用独立位段、20 mV / 50 mA 单位及所选 APDO 的上下限，并为 M81 保留协商功率变化通知以刷新 RDO |
| Type-C 能力目录 | `0041` 在完整 source PDO 集合变化时替换能力子目录，保留当前连接、PD 对象和链接；读取失败保留旧目录，临时注册失败延后重试 |
| 固件服务恢复 | `0042` 修正 UCSI 注册所有权；`0043` 为通知订阅和 UCSI 版本读取增加每次服务上线的有限重试，隔离旧请求结果并清理未完成初始化的端口 |
| UCSI 应答长度 | `0044` 在读取 GLINK 返回码和复制数据前校验完整应答长度，丢弃长度不符的应答。见[原厂和上游格式](FASTCHARGE-SOURCES.md#ucsi-读写应答) |
| 协商时序与重试 | 按原厂命令表轮询应答、先切 host、按 7 → 6 收尾；区分应答未匹配和传输失败，角色切换有限重试，每次连接只发送一轮握手 |
| 厂商事件 | `0038` 按原厂方式将 `0x50` 转成延后执行的厂商设备变更事件，udev 再调度认证服务；标准电池 `0x80` 通知独立处理 |
| 功率提示 | 瓦数标为固件报告的最大能力；MiPPS 提示还要求 `fastchg_mode` 开启，实时输入功率须另行测量 |

双电芯认证、适配器认证门控和充电泵总线电压/电流所用的已核对属性编号与该 M81 模块一致。
项目随附的 ADSP 元数据及 25 个分段与锁定 ROM 对应文件逐字节一致
（原厂 `adsp.mdt` 在项目中命名为 `adsp.mbn`）。因此现阶段优先排查主机接口与认证流程，
尚无证据表明需要替换充电固件或在主机侧重写充电泵控制器。

原厂还提供充电芯片状态、主从 SMB1396 在线状态及输入电流、连接器温度、屏幕状态上报和
主机侧热限流接口。当前接口覆盖不完整，需要补齐观测并核对策略；这些差异是否阻止最高档位，
仍须结合固件响应判断，不能把缺少主机接口直接等同于固件没有温控保护。

## 电量刷新

M81 充电固件需要主机周期读取小米扩展属性 `XM_THERMAL_TEMP`（编号 86），
以触发电量计快照更新。原厂 `qti_battery_charger_main_m81.ko` 每隔 10/20 秒执行此读取，
原厂温度读取路径也使用它。仅查询标准 `capacity`、`charge_counter`、`temp` 属性时，
固件可能持续返回旧快照。

`0037` 补丁为 SM8475 增加每 10 秒一次的请求，使用现有 GLINK 互斥锁与应答校验。
固件完成采样后发出 `0x80` 电池通知，现有 `power_supply_changed()` 路径再通知 UPower。
系统休眠时工作队列冻结，恢复后立即发起一次读取；服务离线时跳过请求，传输失败后按周期重试。
主机请求周期与固件实际更新周期各自独立，百分比仍遵循固件的平滑算法。

原厂公开实现见 MiCode 的 Liuqin 充电驱动中的
[`xm_batt_update_work()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L6450)
与 [`power_supply_read_temp()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L1856)。

小米专有 `0x50` 通知包含其他厂商状态，电量快照通过 `0x80` 通知更新。
离线回归由 `tests/kernel-common-fixes.py --source <完整适配源码>` 覆盖刷新请求、
服务离线、超时后重试及平台限定的唤醒行为；电池单位换算仍由 `tests/battery-readings.py` 覆盖。

## 初始化与普通重启

USB、充电和显示保持内建；Qualcomm 音频通信、时钟与声卡由根文件系统加载，避免早期音频资源请求与充电固件初始化重叠。完整音频功能和电芯认证保留，启动时不执行 PD 硬复位。

保持电脑 USB 的普通重启充电和扬声器已验证。充电初始化通过延后音频模块加载与早期 Q6 资源请求隔离；底层固件内部状态链仍未完全确定，其他电源及高负载需分别测试。

Rec 使用独立维护内核和固定 USB 设备模式，冷启动充电正常。临时启动、普通重启和其他充电器仍须分别验证。回退内核时还需恢复配套设备包，见[内核更新](KERNEL-UPDATES.md)。

输入限流改写、warm 重启和运行中重启 ADSP 不属于当前修复方案。

## 如何判断充电是否正常

`input_current_limit` 是固件报告的请求/限额，不是实际输入电流。满电时低输入可以正常；应观察输入是否随负载增加、电池是否持续补供，以及 Full/Charging 状态。满电短测不能代替低电量、高负载或其他充电器验收。

实时输入观测使用 `qcom-battmgr-usb` 的 `voltage_now` / `current_now`，需要独立功率计确认实际功率。
UCSI 辅助电源节点提供当前协商电压和请求电流，不提供电流传感器采样；它们的乘积表示请求功率，
不能当作实际输入功率。供电能力目录用于识别当前来源，不参与设定充电电压或电流。
Type-C 的 `source-capabilities` 目录随完整 source PDO 集合变化而刷新，包含认证后的 APDO 上限。
只改变当前请求、电源能力不变时保留原目录；临时读取失败时目录可能仍显示上一次完整能力。

## UCSI 协商读数

[原厂公开 UCSI](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/psy.c)
与[上游 UCSI](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/ucsi/psy.c) 都用固定 PDO/RDO
格式解析电压和电流；PPS 请求的位段与单位不同。`0040` 对固定 PD 保留现有来源能力语义，
对 PPS 从 RDO 解码协商电压和请求电流，以所选 APDO 填写电压上下限与最大电流，并报告
`USB_TYPE=PD_PPS`、`ONLINE=2`（可编程供电）。

PPS 位段与单位与[原厂 `pd.h`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/include/linux/usb/pd.h#L381)
一致；协商值与实测值的区分也与[原厂 TCPM](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/tcpm/tcpm.c#L6214)
的电源属性语义一致。锁定 ROM 的 `boot.img`
中，`ucsi_psy_get_prop()` 同样按固定格式提取电压、电流；M81 充电模块的
`usb_psy_get_prop()` 则通过 GLINK 读取固件属性，不依赖这段 UCSI 解码。

原厂 UCSI 启用协商功率变化通知，主线则按 `PDO_DETAILS` 能力位过滤它。M81 缺少该位却实现了
PDO 读取与协商变化状态；`0040` 仅为既有刘琴 UCSI 1.0 兼容路径保留这项通知，使缓存 RDO
随固件协商更新。锁定 ROM 的 `ucsi_init_work()` 也设置 `0xdbe70000` 通知掩码，包含该通知。
本项目其余可选通知仍按能力位过滤。

读数路径检查 PDO 索引和 PPS 请求范围，未就绪或不支持的格式返回无数据；拔线或作为供电端时
输入电压、电流为零。断开连接会清除旧合同，PDO 分段读取全部成功后才发布新缓存。
这些操作不修改充电请求、不触发 PD 硬复位，也不把未经认证的来源标成已认证。

## Type-C 供电能力刷新

[原厂 UCSI](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi.c#L520)
在协商变化后更新 PDO 缓存，但没有当前主线的 PD 能力设备注册路径。
[上游 UCSI](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/ucsi/ucsi.c)
在 `partner_pd` 已存在时跳过注册；能力目录的 PDO 是注册时的副本，缓存刷新不会改动它。

`0041` 参考[上游 TCPM](https://github.com/torvalds/linux/blob/master/drivers/usb/typec/tcpm/tcpm.c)
的能力替换方式，在协商变化和受支持的 PDO 变化通知后重新读取完整集合。只复制返回数量内的
PDO，忽略响应缓冲区未被本次回复覆盖的尾部。只有内容变化才替换
`source-capabilities` 及其 PDO 子项，包括数量或类型变化；连接、PD 对象和链接保持稳定，
不会因目录更新重新建立连接或重放认证。读取失败或暂时无数据时保留旧能力目录。

初次注册失败会清理半成品；已连接时替换失败保留 PD 对象，将能力指针清空，
使用已有工作队列对临时错误最多重试 30 次、间隔 1 秒，后续通知也可再次触发。
替换子目录期间存在短暂的目录缺失窗口，调用方应重新读取；内存分配失败时缺失可持续到重试成功。
断开时释放能力对象并清除比较缓存。源码回归入口为
`tests/ucsi-source-pdos.py --source <完整适配源码>`，覆盖认证前后能力变化、无变化时不重建、
短列表替换、读取失败、注册失败清理与重试及 PD 对象稳定性。

`0042` 另修正 UCSI 接口本身的注册状态与注销所有权，采用上游的
[注册错误处理](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/9f9cd5ac3bc1fade2c2584a051913a2b03d506d2)
与[连接器指针清理](https://github.com/yzddmr6/linux-sm8450-liuqin/commit/f43a4aa7fa89c6e227c5ec763e695576e66e2eb7)。
首次版本读取失败不再误标为已注册；后续服务事件可以重新注册。注销后清空已释放的连接器指针，
避免固件服务恢复时初始化失败、再次注销使用旧数组。

`0043` 在此基础上为电源通知订阅与 UCSI 同步注册增加恢复调度。暂时的通信错误和 UCSI 零版本
每次服务上线最多尝试 10 次，失败后间隔 1 秒；重复的 UP 通知不刷新预算，真正的 DOWN → UP
才开始新一轮。次数包括首次请求，单次通信耗时另由原有内核超时约束，不能把它解释为 10 秒总时限。
不可重试错误或预算耗尽时停止，服务下线则取消等待中的重试。请求与服务代次绑定，旧请求不能确认
新服务的订阅成功，也不能推迟新 UP 的处理；即使 DOWN/UP 被合并调度，旧 UCSI 端口仍须注销。

卸载及探测失败回滚先停用、排空电源工作，再释放 GLINK 客户端。UCSI 注销前阻止旧连接器通知派发，
保留完成在途命令所需的 ACK；每次重新注册重置异步初始化预算和旧连接事件标志。
初始化末尾 CCI 读取失败也会清理已创建的端口，只有完整成功才发布连接器数组。
这些是本项目的 Linux 恢复策略；原厂公开驱动的处理边界见[原厂依据](FASTCHARGE-SOURCES.md#固件服务恢复)。
恢复路径不主动重启 ADSP，不新增充电电流、电压策略写入，保留现有 Liuqin PPM reset 限制。

`tests/battmgr-recovery.py` 和 `tests/ucsi-lifecycle.py` 使用 `--source <完整适配源码>`
编译执行实际工作函数，注入超时、零版本、服务变更和部分初始化失败，验证重试上限及端口、工作队列的清理。

## 组件

| 路径 | 作用 |
| --- | --- |
| `/usr/local/libexec/liuqin-mipps-auth` | 认证守护进程（Python，GPL-2.0-only，见 `NOTICE`） |
| `/etc/systemd/system/liuqin-mipps-auth.service` | 运行守护进程的 oneshot 单元 |
| `/etc/systemd/system/liuqin-mipps-battery-auth.service` | 启动时先认证双电芯；适配器单元等待它完成 |
| `/etc/udev/rules.d/90-liuqin-mipps-auth.rules` | Type-C 连接与 USB 供电上线时拉起单元 |

守护进程通过项目内核在 PMIC GLINK 电源设备上导出的 `xiaomi` 属性组与充电固件通信：

```
/sys/devices/platform/pmic-glink/pmic_glink.power-supply.0/xiaomi/
```

该属性组不存在时单元自动跳过，软件包可在没有此接口的内核上正常安装。

## 认证流程

协议接口依据原厂充电驱动的
[`usbpd_request_vdm_cmd()` / `request_vdm_cmd_store()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L3490)，
实际命令顺序、host 角色切换和摘要格式直接核对锁定 ROM 的 `/vendor/bin/batterysecret`，
见[原厂认证依据](FASTCHARGE-SOURCES.md#认证程序)。公开内核负责转发命令，完整调用顺序由该用户态程序决定。
现有 Python 实现及移植代码的作者、许可证和上游来源仍见 [NOTICE](../NOTICE)。

1. 启动单元先通过 `verify_digest` 校验两个电芯，按 HMAC-SHA256 比对结果设置
   `authentic` / `slave_authentic`。每个电芯最多尝试 10 次，暂时的传输失败可以重试；
   按原厂在两个电芯之间等待 1 秒。适配器单元等待该单元，手动执行时会补做缺失的电芯校验。
2. 最多等待 12 秒，直到 `real_type` 为 `PD` 或 `PD_PPS`、`current_state` 为
   `SNK_Ready` 且 PDO2 非零。等待期间可重读暂时失败的属性；后续厂商事件报告能力变化时可以再次等待。
3. 读取当前 Type-C partner 的首个固定供电 PDO。明确支持 USB 数据通信的来源保持原角色，
   即使厂商 ID 仍残留为小米，也不自动切换。对 SVID 为 `0x2717` 的适配器，或明确不支持 USB 数据
   的未知 PPS 来源，取得 host **数据角色**；不改变 sink 供电角色。未知来源还需在切换后报告
   `0x2717` 才会继续。开始前设置原厂 `verify_process=1`；角色切换暂时失败时最多尝试 4 次，
   总时限 12 秒，每次重试先检查是否已成为 host。切换后和每条命令发送前重新检查身份；切换失败不发送适配器命令，
   探测不到小米身份时恢复原来的 device 角色。
4. 依次发送 UVDM 命令 1–3、会话种子 4、随机挑战 5，并核验应答；使用原厂的 10 ms
   轮询间隔，1–3、7、6 最多 5 次，4、5 最多 500 次。同时限制轮询总耗时约 50 ms / 5 s，
   单次 GLINK 调用耗时仍受内核超时约束。发送前也按原厂短暂等待非断开 UVDM 状态。
   随后先发 REMOVE_COMPENSATION（7），再发 VERIFIED（6）；不发送命令 8。
5. 收尾后清除 `verify_process`，通过认证才设置 `pd_verifed`，记录本次 Type-C partner 的成功凭据；固件决定充电档位。
   `fastchg_mode` 表示快充模式状态，`power_max` 是最大能力值，不能代替实时功率。

命令已成功写入、但应答状态未及时匹配时，按原厂的本地 `-EAGAIN` 分支继续下一项，不重发该命令。
挑战应答缺失或格式错误时，以失败结果完成 7 → 6 收尾，保持门控关闭，也不进入标准 PPS 诊断回退。
GLINK 写入失败、读取传输超时、身份变化或断线立即终止；短暂的 `EAGAIN` / `EBUSY` 读取错误只在当前预算内重读。
异常退出会清除 `verify_process` 和未完成认证的门控。

每次连接在切换角色之前记录一次尝试；同一 partner 的后续通知不会重放已失败或完成的握手。
新 partner 可以重新认证，不再跨连接施加固定 120 秒冷却。就绪等待记录实际失败时的观察值，
读取错误单独表示为不可读，不能用等待前的正常快照阻止恢复。对同一连接、同一失败观察值，
自动等待最多三轮，每轮最多 12 秒，轮间等待 2 秒；耗尽后仍会在事件到来时读取当前状态，
通信恢复或能力变化可继续处理。此预算只控制被动等待，不允许重发已尝试的适配器握手。
默认认证的成功凭据要求本轮摘要通过且 7/6 写入成功；复用还要求同一 partner、`pd_verifed=1`、`verify_process=0`
和非断开的 UVDM 状态，避免 7/6 的短应答窗口超时后误清除有效认证。拔线、连接替换或认证未完成时的失败会清除本地成功凭据。
已完成认证的连接遇到暂时不可读或格式无效的状态时，仅延后检查，不写门控、不重放握手，也不重新宣告认证成功；
同轮读取明确发现门控复位、处理标志异常或 UVDM 断开时仍会清理。USB online 不可读与明确 offline 分开处理，
没有本次连接成功凭据时不能据此保留门控或启动认证。成功凭据在诊断输出与桌面通知之前保存，
后续读数失败、通知失败或报告阶段中断不会撤销已完成的认证；实际断线仍会清除。
PDO 数据能力保护、连接凭据和等待总时限为本项目适配；与原厂的具体差异见[原厂依据](FASTCHARGE-SOURCES.md#等待与失败处理的边界)。
udev 只记录待处理标记并启动 `liuqin-mipps-auth.path`，不做充电接口 I/O。
该 path 单元监视 `/run/liuqin-mipps-auth/pending`；oneshot 工作进程在读取充电接口之前取走标记，
执行期间的新事件重新留下标记。systemd 在工作进程退出后重新检查路径，因此认证中途或退出窗口
收到的重插、能力变化事件会再次调度，不依赖下一条固件通知。启动与路径触发不施加频率封锁，
执行由 `flock` 串行化，握手次数与等待预算仍按连接限制；没有无条件自动重启循环。
路径单元随设备支持包启用，udev 触发也会启动它。接口尚未出现时先消费事件再退出，避免残留标记反复启动。

适配器单元总时限 120 秒，电芯启动单元总时限 80 秒。默认不安装旧的
`10-allow-unverified-adapter.conf`；RPM 升级移除包自带的旧覆盖，个人额外配置须自行核对。

### 身份发现与供电能力

有些 PPS 适配器在 device 角色下报告 `adapter_svid=0000`，切换 host 后才返回小米身份。
单看 SVID 会形成先有身份才能切换、先切换才能发现身份的循环，而且固件可能保留上一个来源的 ID。
因此未知来源的自动探测依据当前 partner 的首个固定供电 PDO，要求 5 V、有效电流和明确的
`usb_communication_capable=0`；不是仅凭 PPS、线缆额定电流或品牌推断。

M81 UCSI 1.0 未声明 `PDO_DETAILS`，但实现了供电对端的 `GET_PDOS`。`0039` 只为 Liuqin
在已连接且处于 PD sink 时补读对端 source PDO，不放开本机或对端 sink PDO。`0040` 对协商变化通知的处理见[UCSI 协商读数](#ucsi-协商读数)。
设备判断兼容 ABL 改写顶层兼容名的情况，同时核对原厂模型名称和小米板号。
能力目录随 partner 拔线销毁；晚到的能力就绪后发出 partner change 事件，再由 udev 调度服务。
原厂公开 UCSI 驱动同样从连接状态解析 USB/替代模式标志；项目自动探测进一步使用标准 PDO 的
USB 通信能力，认证密钥及摘要校验沿用既有实现。

```sh
cat /sys/class/typec/port0-partner/usb_power_delivery/source-capabilities/1:fixed_supply/usb_communication_capable
```

自动探测不绕过摘要认证，第三方普通 PPS 充电器若不返回小米身份，不发送小米认证命令。
能力缺失、读取失败或格式不符均不能当作“不支持 USB 数据”，即使 SVID 已报告小米也不自动切换。旧内核未导出 PDO 时，已知独立
PPS 充电器仍可用 `--probe-unknown-pps` 显式诊断；默认服务不传入该选项，也不依赖 debugfs。

## 标准 PPS 适配器

不支持小米私有认证的适配器不能凭标准 PPS 能力通过认证，也不能仅凭品牌判断是否支持
私有认证。默认只在认证通过后设置门控，保持未认证来源的固件合同。

`--allow-unverified-adapter` 保留为显式诊断选项，只有握手完整结束但摘要比对失败时，
才会检查 `PD_PPS` 合同及线缆能力：导出 e-marker 时要求 5 A；未导出时依赖源端限制。
PDO2 为零、身份未知、传输失败、挑战应答缺失/格式错误及 ID 缺失均不会进入这个分支，尚未实现这些情况下的标准 PPS 回退。
该选项不是默认快充路径，不能用来证明小米认证成功。

现有实现的 10 组适配器种子和密钥已与[原厂认证程序](FASTCHARGE-SOURCES.md#认证程序)逐字节核对一致；
代码来源与署名保留在 [NOTICE](../NOTICE)。桌面通知中的瓦数仅为固件报告的最大能力。
MiPPS 提示还要求本次连接认证通过且 `fastchg_mode` 开启。

## 验证

```sh
X=/sys/devices/platform/pmic-glink/pmic_glink.power-supply.0/xiaomi
journalctl -u liuqin-mipps-auth -n 40
cat "$X/real_type" "$X/current_state" "$X/verify_process" "$X/pd_verifed" "$X/fastchg_mode" "$X/power_max"
cat /sys/class/typec/port0/data_role
cat /sys/class/power_supply/qcom-battmgr-usb/voltage_now
cat /sys/class/power_supply/qcom-battmgr-usb/current_now
cat /sys/class/power_supply/qcom-battmgr-bat/current_now
```

USB 的 `voltage_now` / `current_now` 单位分别为微伏、微安，其乘积除以 `10^12` 得到
输入功率的瓦数估计。它与电池侧功率不同，也不是 `voltage_max` 与 `current_max` 的乘积。
电荷泵工作时 USB 的 `current_now` 可能不覆盖全部充电电流，须同时观察电池侧功率与
`bq2597x_bus_current` / `bq2597x_slave_bus_current`。节点分次读取且可能存在采样延迟，
应连续观察；需要精确确认峰值时使用兼容所测协议的功率计。

固件按电量与温度调整充电档位。验证最高功率应在较低电量、正常温度下进行；满电时低功率
不能证明快充不支持。当前没有足够依据将固定电量阈值或未认证时的固定电压作为所有适配器的规律。

## 排障

| 现象 | 检查 |
| --- | --- |
| 单元从不运行 | `xiaomi` 属性组不存在：运行中的内核早于该接口 |
| 小米适配器下 `pd_verifed` 仍为 0 | 看 `journalctl -u liuqin-mipps-auth`：`adapter auth mismatch` 表示摘要比对失败；`adapter capabilities not ready` 表示等待期限内 PD 合同、SNK_Ready 或 PDO2 未就绪 |
| `role_first=already_attempted_this_attach` | 本次连接已进行过主动尝试，重启服务不会重放握手 |
| `vdm_response=timeout cmd=… action=continue` | 命令写入成功但状态应答未匹配，继续下一项，不重发；仍须取得并验证本轮挑战摘要才能认证成功 |
| `role_first=no_host_no_adapter_commands` | 角色切换失败或耗尽重试预算；看前面的 `role_request` 返回码，检查供电来源与当前角色 |
| `role_first=source_data_capability_unknown` | 当前供电能力缺失或无效，不使用可能残留的 SVID 自动切换 |
| `role_first=usb_data_source_no_role_swap` | 当前供电来源声明支持 USB 数据，保持原数据角色 |
| `role_first=power_only_pps_probe` | 正在对明确不支持 USB 数据的 PPS 来源探测身份；后续仍需小米身份和摘要认证 |
| `role_first=unidentified_or_non_xiaomi` | 未确认小米身份且缺少纯充电 PPS 依据；检查当前 partner 的 PDO 目录 |
| `pd_verifed` 为 1 但 `fastchg_mode` 为 0 | 门控置位不等于快充已开启；继续检查电量、温度、协议能力、输入电压/电流和充电通路状态 |
| `unverified_adapter=refused` | 原因随后给出：线缆额定低于 5 A，或合同不是 PD_PPS |
