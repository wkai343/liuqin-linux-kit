# 充电原厂依据

[充电与快充](FASTCHARGE.md) ｜ [准备原厂资料](BUILD.md#准备原厂资料)

本文记录可复核的原厂输入、函数位置和实现边界。二进制结论来自锁定 ROM 的静态分析，
不代表充电功率验收；公开内核与量产 ROM 版本不同，运行时属性编号以量产模块为准。

## 输入与公开源码

ROM 为 `OS2.0.6.0.VMYCNXM`，镜像身份固定在
[liuqin-rom-images.json](../shared/liuqin-rom-images.json)，获取方式见[构建指南](BUILD.md#准备原厂资料)。
认证程序来自 `super.img` 的 `vendor_a`，充电模块来自 `vendor_boot.img`。
程序、模块和反汇编产物留在本地，不随仓库分发。

公开源码使用小米官方 [MiCode / liuqin-t-oss](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/tree/eb49e10028e27fa98856cb5e458c0f0ec5b4554e)，
固定提交 `eb49e10028e27fa98856cb5e458c0f0ec5b4554e`：

| 原厂位置 | 能直接支持的结论 |
| --- | --- |
| [`qti_battery_charger.c`：`usbpd_request_vdm_cmd()`、`request_vdm_cmd_store()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L3490) | UVDM 命令含义、参数字节序及 GLINK 转发；该入口不编排用户态调用顺序 |
| [`verify_digest_store()` / `verify_digest_show()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L2671) | 电芯挑战及摘要读写接口；主机摘要计算在用户态完成 |
| [`pd_verifed_store()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L3683) | 用户态结果写入固件门控；接口本身不执行摘要校验 |
| [`xm_batt_update_work()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L6450) | 周期读取 `XM_THERMAL_TEMP` 的原厂路径 |
| [`pd.h`：PPS RDO 定义](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/include/linux/usb/pd.h#L381) | PPS 请求电压、电流的位段与 20 mV / 50 mA 单位 |
| [`ucsi.c`：`ucsi_get_src_pdos()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi.c#L520) | 从固件读取 source PDO、在协商变化后更新缓存 |

公开 UCSI 未提供当前主线的 PD 能力设备注册路径；能力目录替换仍需引用 Linux 的相应实现。
这些主线 API 依据与 [NOTICE](../NOTICE) 中的代码来源、作者署名不能改称原厂实现。

## 日志通道生命周期

公开原厂 [`charger-ulog-glink.c` 的 `ulog_en_set()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/soc/qcom/charger-ulog-glink.c#L282)
在 `enable=0` 时只执行 `cancel_delayed_work_sync()`，保留 `chg_ulog` 客户端及
`PMIC_LOGS_ADSP_APPS` 通道。原厂
[`pmic_glink_unregister_client()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/soc/qcom/pmic_glink.c#L352)
也只撤销客户端路由，不关闭其父设备的 RPMsg 通道；日志和主充电分别匹配两个
具名 RPMsg 设备。因此停止日志轮询不需要发送 GLINK `CLOSE`，也不需要取消
充电属性投票、发送额外日志控制命令或重启 ADSP。

锁定 ROM 的 `charger-ulog-glink.ko` SHA-256 为
`162dc041e4f30171027a0194c946a7a01c982a1959b30b772ef694441d5e4ded`。
其 `.text` 中 `ulog_en_set` 位于 `0xc10`，关闭分支 `0xc60` 的重定位目标为
`cancel_delayed_work_sync`；该路径不调用客户端注销或通道关闭。模块符号可能附带
编译器哈希后缀，地址只适用于此哈希的 ELF，可用 ARM64 `objdump -dr` 复核。

当前 Linux 的 `drivers/rpmsg/rpmsg_char.c` 区分两种所有权：通过 `rpmsg_ctrl`
创建的端点由打开的文件持有，释放文件或 `RPMSG_DESTROY_EPT_IOCTL` 会调用
`rpmsg_destroy_ept()`；绑定 `rpmsg_chrdev` 的默认端点由驱动持有，关闭文件仅
释放文件状态和接收队列，默认端点不接受销毁 ioctl。`liuqin-charge-logs` 使用后者，
使采集进程退出保持原厂关闭轮询的语义。该工具本身不解绑驱动。

锁定固件的 GLINK 行为是：主机向日志通道发送 `CLOSE`，远端回复该通道的
`CLOSE_ACK`，随后远端主动向主充电通道发送 `CLOSE`，Linux 才进入主通道清理
与对向关闭。两个通道的 ID 不同，ADSP remoteproc 仍为 `running`；此时电源属性
请求不可用，Type-C 伙伴消失。这个顺序将触发点定位到诊断传输关闭后的固件行为，
不能解释成 Linux 在最初的请求中误用了主通道 ID。主机修复遵循原厂持有方式，
不依靠忽略远端关闭事件、伪造成功应答或自动重启 ADSP。

### 驱动解绑适配

原厂 GLINK 的端点销毁同样发送 `CLOSE`，未发现可以单独关闭日志传输又保留主充电
的额外主机握手。原厂充电日志客户端与 RPMsg 传输是分层持有的；当前主机适配
[`0079-liuqin-retain-charger-log-transport.patch`](../build/kernel/patches/linux-7.3/0079-liuqin-retain-charger-log-transport.patch)
将这种生命周期分离用于驱动解绑，范围由 `liuqin_board_quirks()`、`lpass` 边和
精确的 `PMIC_LOGS_ADSP_APPS` 名称共同限定。

解绑前撤销并排空数据及流控回调，释放客户端状态；已经完成握手的传输继续由原有
本地／远端 ID 引用持有。重绑复用通道，不重新分配 ID 或重复公告基础 intents。
打开失败、重复占用及 `rpmsg_ctrl` 同名别名不能接管活跃端点。收到远端关闭时，
包括客户端已经解绑的情况，仍完成双方关闭与引用释放；整个 GLINK 边移除也照常
清理保留的通道。保留传输不意味着缓存充电成功状态或忽略固件服务掉线。

`tests/charge-logs-kernel.py --source <完整适配源码>` 编译执行上述真实函数，
覆盖重复解绑、回调生命周期、握手失败、远端关闭、传输移除和其他通道行为。

可复现检查包括：正常退出、SIGTERM、SIGKILL 后重新打开日志文件，并确认主充电
属性、温控状态和 Type-C 伙伴持续有效。需要调查底层关闭行为时，应使用临时跟踪
内核记录 `qcom_glink_cmd_open/open_ack/close/close_ack` 的方向、通道名和 ID，
区分主机主动关闭、远端关闭和通道重新公告；测试产物保存在忽略的 `out/` 中。

## 认证程序

`/vendor/etc/init/hw/init.batterysecret.rc` 在开机完成及关机充电模式下启动
`/vendor/bin/batterysecret`，并配置 `request_vdm_cmd`、`pd_verifed` 和电芯认证节点权限。
该 ELF 的 SHA-256 为 `7a450cdd5c1b65f584f470bb31d4c7a3b4c9cfd6b8cab4473dda6a06e084a1e0`；
函数名来自其 `.gnu_debugdata`，下表地址为该文件的 ELF 虚拟地址，不能套用于其他 ROM。

| 原厂函数 / 位置 | 静态分析结论 |
| --- | --- |
| `main()` `0x23b4`、`verify_fg_digest()` `0x36e8` | Liuqin 分支先分别检查、认证主从电芯，再进入适配器处理 |
| `main()` `0x270c`–`0x279c` | 常驻循环使用 `select()` / `recvfrom()` 接收事件，再调用处理函数；不是按事件重新启动的 oneshot |
| `calc_fg_digest()` `0x2ea0` | 使用 32 字节挑战和 HMAC-SHA256；Liuqin 分支选用的电芯密钥与当前 `FG_KEYS[0]` 一致 |
| `verify_pd_digest()` `0x4fc0` | PD/PPS 路径中先调用 `usbpd_dr_swap()`，再调用 `usbpd_conmunication()`；处理结束时写 `pd_verifed` |
| `check_pd_auth()` `0x4cd8` | 读取处理标志与认证门控以判断完成状态；读取失败返回错误，不在该检查路径写零撤销门控 |
| `usbpd_dr_swap()` `0x459c` | 检查 `SNK_Ready`，向 Type-C `data_role` 写 `host`，再读取角色确认结果 |
| `usbpd_conmunication()` `0x4a1c`、数据表 `0x7710` | 有效命令顺序为 `1 → 2 → 3 → 4 → 5 → 7 → 6`；表尾另有零项，发送函数不把它作为有效 UVDM 命令写出 |
| `usbpd_send_vdm_cmd()` `0x3d44` | 命令 8 在入口直接返回；发送其他有效命令前检查 `adapter_svid=0x2717`，7/6 携带摘要校验结果 |
| `calc_usbpd_digest()` `0x38d0` | 对 16 字节挑战与 4 字节适配器 ID 拼接的 20 字节消息计算 HMAC-SHA256，比对应答与摘要的前 16 字节 |

现有 10 组适配器种子及密钥按相同索引与原厂表逐字节一致；文件偏移分别为
`0x6530`（10 × 16 字节）和 `0x65d0`（10 × 32 字节）。Liuqin 电芯密钥位于 `0x6510`。
当前 `FG_KEYS[1]` 是既有实现额外接受的候选，本次在该原厂 ELF 中未找到，不能将整个候选集称为原厂策略。

### 等待与失败处理的边界

原厂命令表每项包含命令号和应答轮询次数：

| 命令 | 次数 | 状态未匹配时的轮询间隔 |
| --- | --- | --- |
| 1、2、3、7、6 | 5 | 10 ms |
| 4、5 | 500 | 10 ms |

发送函数先读应答；未匹配才调用 `usleep(10000)`。因此 `5/500` 不是固定的 5/500 ms 延时，
轮询等待上限分别约为 50 ms / 5 s，另有 sysfs/GLINK 调用耗时及发送前的连接状态等待。
轮询耗尽返回 `-EAGAIN`，`usbpd_conmunication()` 对该返回值继续后续项；其他负错误会提前退出。
发送前等待非零 UVDM 状态：命令 1 最多读取 20 次，其余命令 4 次，间隔 20 ms。
应答阶段读到状态 0 则终止，不能当作普通应答超时继续。

`verify_pd_digest()` 调用角色切换前写 `verify_process=1`，结束时先清零该标志再写 `pd_verifed`。
其角色切换可执行四轮，遇到就绪等待的 `-EAGAIN` 会继续等待；认证处理后记住已尝试状态，
在非 PD/PPS 类型或 UVDM 断开且 USB offline 时清除。未发现跨连接的固定 120 秒冷却。

当前实现采用上述轮询次数、间隔、命令顺序和处理标志，并保留以下 Linux 适配边界：

| 项目 | 当前处理与原厂的差异 |
| --- | --- |
| 轮询上限 | 同时限制次数和约 50 ms / 5 s 的总耗时，避免慢 GLINK 读取将 500 次轮询放大成数分钟；单次阻塞仍由内核超时约束 |
| 错误分类 | 仅本地应答状态未匹配超时可继续下一命令；读取 `EAGAIN` / `EBUSY` 在当前预算内重读，GLINK 写错误或读取超时直接终止，不重发 |
| 认证缺失 | 命令 5 无有效摘要时，按失败值发送 7 → 6；不设置认证门控，也不走诊断回退 |
| 角色重试 | 仅暂时错误或角色尚未就绪时重试，最多 4 次、总计 12 秒；原厂就绪等待没有同样的总时限。每次先读角色，已完成切换便不再写入 |
| 连接保护 | 保留 PDO USB 数据能力检查及绑定 partner 的成功凭据；同一 partner 不重放整轮，新 partner 可重新尝试，不使用跨连接冷却 |
| 就绪恢复与事件调度 | 原厂常驻处理事件；本项目由 systemd path 启动常驻进程，再以 inotify 等待标记，事务返回后补处理执行期间的事件。被动就绪等待对同一失败观察值最多三轮，间隔 2 秒；读取恢复或能力变化可以继续。标记、监听和超时预算是 Linux 适配策略，不是原厂时序常量 |
| 成功复用 | 原厂检查 `verify_process=0` 与 `pd_verifed=1`；本项目还要求本次连接的成功凭据及非断开的 UVDM 状态，不强求最后状态必须是 6/7 |
| 完成后读取失败 | 原厂检查路径不因读取失败写零门控；本项目对绑定当前 partner 的成功凭据延后复核，明确断线或门控复位仍清除。诊断和通知放在事务完成之后，不能撤销成功结果；连接凭据及三态读取是本项目实现 |
| 电芯重试 | 保留原厂每个电芯最多 10 次、挑战后 1.4 秒和两电芯之间 1 秒等待；暂时传输失败也消耗一次尝试，重试间隔 0.2 秒为本项目策略 |

命令 7/6 的短轮询超时不会抹去已校验的本轮摘要，但写入失败仍必须终止。
权限错误、身份变化、断线和收到无效数据不能被当作成功；异常路径负责清除处理标志与未完成认证的门控。

## 总输入观测

原厂 [`usb_psy_get_prop()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c)
按 USB 属性表读回固件数值，没有在 `CURRENT_NOW` 分支叠加电荷泵电流。
同文件的 `bq2597x_bus_current_show()`、`bq2597x_slave_bus_current_show()` 和
`bq2597x_bus_voltage_show()` 分别导出小米属性 24、25、27。

锁定 M81 模块 `qti_battery_charger_main_m81.ko`（SHA-256
`b13b976e8a0d5ce2256df4f96368a6de0999aecf4dd311a9ded0f993e2cedf6b`）中的对应函数位于
`0x2d68`、`0x64f8`、`0x65a8`、`0x6708`，保持同样的分路导出。
模块中的地址是 ELF 虚拟地址，不能直接套用于其他版本。

`liuqin-charge-status` 使用这些已有只读接口汇总输入电流，并按各通路电压估算输入功率。
汇总公式、连接一致性检查和 JSON 格式是本项目观测接口，原厂没有提供同名总功率属性。
它不改变 USB 原始电流语义，不把 PPS 请求电流或最大功率能力作为采样值，
也不根据快充模式标志推测电荷泵电流。分次读取不是硬件原子快照。

## 充电温控接口与 normal 策略

同一 M81 模块的 `battery_psy_set_prop()`（`0x3630`–`0x36c0`）验证输入在
`0 <= level < num_thermal_levels`，以 battery SET `0x31`、属性 10 发送档位。
probe 的 `0xd98`–`0xda0` 将档位数量设为 16。公开源码对应
`battery_psy_set_charge_current()`；旧的微安辅助函数不能用于解释这个量产路径。
独立 USB 输入电流入口的协议和 PD 能力限制见下节；它与热档位使用不同属性，
不能把 USB 输入投票被拒绝推导为热档位失效。

`mi_thermald` 和其 normal 配置同样来自锁定 ROM 的 `vendor_a`：

| 文件 | SHA-256 |
| --- | --- |
| `/vendor/bin/mi_thermald` | `4b7043146a5ecb7afb1dc7ee855450fed4da1e65dfa329fee060a77907310c4b` |
| `/vendor/etc/thermal-normal.conf` | `30b448aa005664047f60eef507d743932dac7652ee8b6748505535896be4fe1b` |
| normal 配置解码后的文本 | `ae62ce8b2264ee9907d11c0d6a3e83cfa93ef80aeadb4ba15c7a93b084e55276` |

按本页的只读提取步骤获取文件。`mi_thermald` 的 `.gnu_debugdata` 使用 XZ 压缩，
解压后的 ELF 符号表标识 `en_de_crypt()` 为 `0x1273c`、`virtual_read_temp()` 为
`0xbf1c`、`generic_set_level()` 为 `0xe584`、`screen_monitor()` 为 `0xe778`。
解码使用 AES-128-CBC；`0x848c` 调用点通过相对重定位 `0x17278` / `0x17280`
引用 key / IV。它们的虚拟地址为 `0x189ec` / `0x189fd`，对应文件偏移各减 `0x1000`，
分别取 16 字节。将解码结果校验到上表摘要后再分析配置，不把未知版本按固定偏移处理。

normal 的 `VIRTUAL-SENSOR` 给出五路传感器、权重、分母和补偿值；`MONITOR-BAT`
给出六组触发／解除阈值和编码目标。`generic_set_level()` 的 `0xe630`–`0xe660`
将 battery 的编码目标除以 100，屏幕状态非零取商，零取余数；原始编码不进入固件。
`virtual_read_temp()` 在 `0xc03c`–`0xc048` 对加权和作有符号整数除法后加入补偿。
Linux 采用这个 normal 充电表，未宣称复制游戏、相机等全部场景策略。

传感器接线同时核对公开 `liuqin-sm8475.dtsi` 和锁定 ROM 的 DTBO：
ADC7 通道 `0x144/145/146/147/347/34d/44c/44`、100 kΩ 上拉、比例校准、
预缩放 `1 1`、稳定时间 200 µs。标准 battery `TEMP` 与用于热策略的
`XM_THERMAL_TEMP` 是不同属性；后者的原厂 `power_supply_read_temp()` 以原值乘 100
转换为毫摄氏度。主机接口、Linux 生命周期规则及复现检查见[温控](THERMAL.md#充电温控)。

## USB 独立输入电流接口与 0x204

公开 [`usb_psy_set_icl()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L1385)
先读 `USB_ADAP_TYPE=7`，仅接受 SDP（1）或固定 PD（6），再以 USB SET `0x33`、
属性 5 发送微安值，负数编码为 `UINT_MAX`。量产 M81 模块的
`usb_psy_set_prop()`（`0x2f4c`，主要路径 `0x2fe0`–`0x306c`）同样发送 24 字节，
包含 owner=32778、type=1、opcode=0x33、battery_id=0、property=5 和 value。
这条路径没有额外认证或使能写入；服务恢复时通知订阅和 200 ms 等待属于初始化流程，
不是每次 SET 的解锁序列。Linux 保留该协议，但仅接受 `-1` 作为撤销值。

锁定 ROM 的 `adsp.b14`（SHA-256
`a708e6266aecf1f44fa5434635411f4e81d4ab3107e89d5751b3523aa66f2973`）
给出了拒绝条件。下表是 Hexagon 虚拟地址，分段基址为 `0x60000000`；
须先校验文件摘要，不能将这些偏移用于其他固件。

| 位置 | 行为 |
| --- | --- |
| `0x600ccf30`，`battmngr_plat_scpqchg_exfg_r1_set_usb_icl` | SDP 直接进入电流投票；PD 类型 6／7／8 进入额外能力检查，其他类型返回 `0x204`。主机仍保留原厂较窄的 1／6 类型范围 |
| `0x600cd0a4`–`0x600cd0d0` | 从输入信息读取 `bPDSuspendSupported`；为零时立即返回 `0x204`，未执行电流投票 |
| `0x60032740` → `0x60181ec0` | 查询端口 PD 状态；已有所选 PDO 时，从首个 source PDO 提取 bit 28，作为上述能力标志 |
| `0x6002bf44`–`0x6002bf70` | 同一个标志被送入 `charger detection` 的 `bPDSuspendSupported` 日志字段，格式位于 `0x602653fd` |
| `0x600cd0d4`–`0x600cd0e8` | 能力检查通过后，普通电流值仍被改成 `UINT_MAX`；PD 入口不提供任意电流上限保证 |

bit 28 的含义与原厂 [`PDO_FIXED_SUSPEND`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/include/linux/usb/pd.h)
一致。它由供电端声明，不能通过修改主机缓存或设置适配器认证位补齐。
该结论限定于此固件、此属性和上述分支，不把所有属性中的 `0x204` 一律解释成 PD 能力错误。
当前驱动保留 `EREMOTEIO`，不吞掉失败，也不将失败请求保存为已成功的控制值。

复查时从当前连接的 partner 查找能力目录，避免固定 `pdN` 编号：

```sh
cat /sys/class/typec/port0/power_role
cat /sys/class/typec/port0-partner/usb_power_delivery/source-capabilities/1:fixed_supply/usb_suspend_supported
cat /sys/class/power_supply/qcom-battmgr-usb/usb_type
cat /sys/class/power_supply/qcom-battmgr-usb/input_current_limit
```

在供电在线、sink 角色、固定 PD 且上述能力为 0 时，对 500000、750000 和 `-1`
的独立请求应分别核对实际固件应答与未变的 GET 限额；不能用 `EREMOTEIO` 单独证明
返回码一定是 `0x204`。使用现有 `pmic_glink_trace` 可记录线上 24 字节应答，完成后恢复
原跟踪开关。`tests/charge-thermal-kernel.py` 覆盖完整 `0x204` 应答的错误传播与失败不覆盖
成功缓存，实机记录保存在 `out/`。SDP 限流及能力位为 1 的 PD 电源仍需分别实测。

需要补充固件执行路径时使用 `liuqin-charge-logs`，按[原厂生命周期](#日志通道生命周期)
停止轮询并保留端点。新内核的驱动解绑行为见[驱动解绑适配](#驱动解绑适配)。
旧内核或直接发送底层关闭命令仍会触发锁定固件关闭主充电通道；若已发生，正常重启
后核对电量通信、Type-C 和温控服务恢复。

## 固件服务恢复

公开原厂 [`battery_chg_state_cb()` / `battery_chg_subsys_up_work()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L1934)
在 GLINK 上线后重新订阅电源通知；`battery_chg_notify_enable()` 失败时没有定时重试。
公开原厂 [`ucsi_qti_state_cb()` / `ucsi_setup()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi_glink.c#L536)
在下线时注销并销毁 UCSI 对象，上线时重新创建；设置工作同样没有自动重试。
这与主线 PMIC GLINK 保留并复用 UCSI 对象的生命周期不同，不能直接复制状态回调中的同步清理。

主线 GLINK 在客户端自旋锁内调用服务状态回调，不能在该回调里等待请求结束或同步取消工作。
项目的 `0043` 以延迟工作处理有限重试，用服务代次隔离旧结果；UCSI 复用前重置初始化预算、
事件标志和通知状态。10 次尝试、1 秒间隔、代次检查以及工作停用顺序是本项目策略，
不是原厂二进制或公开源码中的常量。`0042` 所引用的上游修复只纠正注册所有权，并未自带定时重试。

## UCSI 读写应答

原厂公开驱动在解析返回码前检查整包长度，读应答为 64 字节，写应答为 16 字节。
[原厂 UCSI 读写应答处理](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi_glink.c#L154-L195)

上游 UCSI 2.x 的 GLINK 数据区为 528 字节，加上报文头和返回码后，完整读应答为 544 字节。
`0044` 在版本确定前接受 64 或 544 字节的完整读应答，版本确定后按对应格式校验。写应答固定为
16 字节。长度不符的应答会被丢弃，请求继续等待有效应答直至原有超时。
[上游 UCSI 2.x 缓冲区定义](https://lkml.iu.edu/hypermail/linux/kernel/2510.2/09182.html)

## 离线复核

按构建指南准备锁定 ROM，在仓库根目录进入 `devbox shell`。下面只读取镜像并向新的
`out/stock-charger-reference` 目录提取文件，使用现有 sparse/super 解析器，无需挂载或连接平板。
输出目录已存在时先改用新的目录，避免覆盖先前材料。

```sh
python3 - <<'PY'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, 'build/lib')
from android_images import vendor_partition

rom = Path('inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images/super.img')
lock = json.loads(Path('shared/liuqin-rom-images.json').read_text())
with rom.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == lock['images']['super.img']['sha256']
out = Path('out/stock-charger-reference')
out.mkdir(parents=True, exist_ok=False)
vendor_partition(rom, out / 'vendor.img')
PY
fsck.erofs --extract=out/stock-charger-reference/vendor --no-preserve out/stock-charger-reference/vendor.img
```

校验认证程序、读取命令表及比较现有常量，不输出密钥内容：

```sh
python3 - <<'PY'
import ast, hashlib, struct
from pathlib import Path

blob = Path('out/stock-charger-reference/vendor/bin/batterysecret').read_bytes()
assert hashlib.sha256(blob).hexdigest() == '7a450cdd5c1b65f584f470bb31d4c7a3b4c9cfd6b8cab4473dda6a06e084a1e0'
commands = [struct.unpack_from('<II', blob, 0x6710 + 8 * i) for i in range(8)]
assert commands == [(1, 5), (2, 5), (3, 5), (4, 500), (5, 500), (7, 5), (6, 5), (0, 0)]
print('command, poll_count:', commands)
tree = ast.parse(Path('device/board-overlay/usr/local/libexec/liuqin-mipps-auth').read_text())
constants = {}
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('SEEDS', 'KEYS', 'FG_KEYS'):
                constants[target.id] = ast.literal_eval(node.value)
for name, offset, size in (('SEEDS', 0x6530, 16), ('KEYS', 0x65d0, 32)):
    assert len(constants[name]) == 10
    assert all(bytes.fromhex(value) == blob[offset+i*size:offset+(i+1)*size]
               for i, value in enumerate(constants[name]))
    print(name, 'matches stock')
assert bytes.fromhex(constants['FG_KEYS'][0]) == blob[0x6510:0x6530]
print('Liuqin fuel-gauge key matches FG_KEYS[0]')
PY
```

用 ARM64 `objdump` 复核表的调用方式、命令 8 的跳过分支、应答轮询与角色切换。
例如下列范围覆盖 `usbpd_send_vdm_cmd()`、`usbpd_dr_swap()` 和 `usbpd_conmunication()`：

```sh
aarch64-unknown-linux-musl-objdump -d --start-address=0x3d44 --stop-address=0x4bd0 \
  out/stock-charger-reference/vendor/bin/batterysecret
```

命令表的第二列需结合 `usbpd_send_vdm_cmd()` 中的循环及 `usleep()` 参数解释；
仅凭字符串、枚举顺序或表中数字不能得出完整时序结论。
