# 原生 Rec 的安装与维护后端

[GUI 使用](INSTALLER-GUI.md) ｜ [多系统设计](MULTIBOOT-DESIGN.md) ｜ [Android ROM 安装](FULL-ROM-FLASH.md)

电脑 GUI、原生 Rec 和分区/系统安装共用一份确认计划。临时与固定 Rec 使用各自镜像角色；能否执行后加功能取决于运行镜像的内嵌能力与载荷，不能只更新电脑端或修改状态文案。

## ADB 连接与传输

Rec 使用 USB ADB 提供 root 维护端点。电脑需要 ADB 驱动和支持 `adb raw` 的 Platform-Tools；无需配置 USB 网卡、IP 或共享网络。只连接可信的物理 USB 电脑。

### 连接与传输

电脑通过 ADB shell v2 执行绑定当前 boot ID 的命令，保留二进制输出和真实退出码。安装材料经 ADB reverse 传送，电脑服务只监听环回地址。

原生内核没有 IPv6，传输前先在已绑定会话中启用 `lo`，再通过 `adb raw` 请求
`reverse:forward:norebind:tcp:localhost:<port>;tcp:<host-port>`，建立 IPv4 环回监听。客户端校验 OKAY/FAIL 和端口，占用时有限重试；任务结束只移除自身映射。

`adb raw` 的输入管道保持打开，直到设备响应完成；提前关闭可能导致「ADB 环回映射返回了无效响应」。连接失败时先检查驱动、ADB 状态及安装器版本，保持当前 Rec，导出日志排查。

### FunctionFS 与运行时

启动器先核验设备及存储只读，再建立 FunctionFS；adbd 在描述符和端点就绪后绑定 UDC。失败时留在 RAM Rec 和本地界面。ADB 启动日志可在平板的运行日志中查看。

内核启用 USB_F_FS、USB_CONFIGFS_F_FS 和 AIO。Rec 不创建 RNDIS/NCM 网卡，不启动 DHCP、Telnet、无线 ADB 或网络降级通道。桌面 Linux 的 OTG 和 RAM 故障救援由各自启动流程管理。

adbd 使用固定版本的 [Roboparty/adbd4linux](https://github.com/Roboparty/adbd4linux) 和 AOSP 源码。来源与提交在 `build/recovery/adbd-sources.json`，构建入口为 `build/recovery/build-adbd.py`；分发包附许可证和对应源码归档。FunctionFS 顺序依据[内核文档](https://docs.kernel.org/usb/functionfs.html)。

安装范围校验用于防止误操作，不隔离已取得 root shell 的主机。

协议回归见 [native-adb.py](../tests/native-adb.py)，覆盖 shell v2、退出码、二进制传输、boot ID、分片响应、映射清理和超时释放。QEMU 环回检查与真实 USB 枚举分别测试。

## 能力与会话

| 能力标记 | 用途 |
| --- | --- |
| `installer_version=1` | 可建立受控写入会话，默认仍只读启动 |
| `generic_ota_version=2` | 完整 Android ROM 的系统/启动/固件执行描述 |
| `combined_install_version=1` | 一次确认 Android 和/或多个 Linux 子任务 |
| `boot_switch_version=1` | 电脑端独立切换默认引导 |
| `tablet_boot_switch_version=1` | LVGL 系统列表及确认切换 |
| `fixed_recovery_restore_version=1` | ROM 安装时保护设备绑定的固定 Rec |
| `boot_store_version=1` | 中断恢复与引导存储清理 |
| `bcb_cleanup_version=1` | 退出时处理启动时已消费的无参数 BCB 请求 |

写入确认绑定设备序列号、boot ID、Rec 构建、计划摘要、目标分区 GUID/范围、材料和旧 boot。实际设备再次核对，才短暂开放计划内分区。GPT 修改另外绑定完整编辑方案摘要，不能借系统任务发送任意分区命令。

准备失败且未写盘时可结束会话。开始写入后的失败保留诊断状态；活动任务不能被其他维护请求接管，恢复需要新检查和独立确认。操作结束或异常退出后恢复只读，回读不通过时不报告成功，也不自动重启或切槽。

## 系统安装

- 分区编辑仅处理已确认的 userdata、受管理 rootfs 和引导存储，原厂前缀及底层固件不开放通用删除/格式化。
- Android 按[Android ROM 安装](FULL-ROM-FLASH.md)的实际包、分区容量、回滚元数据、格式和备份生成范围，不要求所有固件与本机哈希相同，也不限定 AVB flags=3。
- Linux 为每个所选 rootfs 校验 boot/rootfs 接口、模块和文件系统，格式化目标后核验根绑定。其他系统保留。
- 新 boot 先完整传入 RAM、校验和复核旧引导，再写 boot_a 并整分区回读；最后提交登记。
- 完整 ROM、分区、Linux 和默认引导可组成一份计划，所有材料在首次写入前检查，最终默认系统最后处理。

Fastboot anti 观测仅用于相应路径的检查，不代替真实 TEE 回滚计数。

## ROM 安装时保护固定 Rec

只采用具有匹配固定资格文件的镜像。`hardware-qualification.json` 与 `restore-compatibility.json` 绑定序列号、镜像、构建及对应固件摘要，准备阶段推算安装后的 ABL、DTBO 和 AVB。

资格文件中匹配的 ABL、符合 AVB 条件且底层固件不变时可复用自带内核/DTBO。ROM 元数据变化只在限定条件下重新绑定；陌生 ABL、固件变更或无法证明兼容时停止自动保护，用户可明确选择按 ROM 原样处理 Recovery。此保护资格不等于普通 ROM 白名单。

计划绑定当前 recovery_a 完整备份和目标镜像。执行器在 ROM 写入前完整暂存、核验固定镜像及旧 Recovery，先保留/恢复固定 Rec，再跳过 ROM 内 Recovery；失败尝试恢复本次旧 Recovery。此协议不处理 recovery_b，不重新签名或修改 ROM 的 AVB flags，也不承诺物理断电原子性。

完成记录保存实际保留的 Recovery 摘要。提交前重新核验安装后启动链和存储只读。Android 内部 OTA 或首启脚本的后续写入不由电脑安装器控制。

## 默认系统切换

切换只开放 boot_a 与 linux_boot。核验当前默认、旧快照、目标完整镜像及其系统依据：Linux 检查根绑定，Android 检查数据分区身份、完整安装记录、ROM 和固件。新 boot 回读后原子提交默认字段。

若 Android 安装记录早于单独固定 Rec 的操作，可使用当前构建携带的设备绑定的固定镜像资料：设备、recovery_a 和整分区摘要必须匹配，其余 ROM/固件检查仍保留。该判断也用于中断恢复，旧切换计划没有显式 Recovery 覆盖字段时仍可核验。

临时 Rec 的电脑 GUI 与平板菜单共用这一固定资格校验。预览解析出的固定镜像摘要进入确认计划，执行前再次核对实际分区和构建内的设备绑定资料；电脑传入的摘要不能代替绑定资料或绕过其他 ROM/固件检查。

## 中断恢复与空间回收

GUI 的「引导存储与中断恢复」只读统计空间、默认匹配、事务和可恢复动作，写操作重新生成计划并确认。

- **引导事务**：重建原确认快照，核对完整旧 boot、原登记、校验清单和分区；可恢复原引导/默认登记。目标已完整写入时也可仅补交目标登记。
- **安装提交**：新版先保存 `commit-intent.json`、原登记和校验清单。`root-installed.json` 或 `rom-installed.json` 存在且真实系统/ROM、数据初始化、引导和固定 Rec 启动链都匹配后才补交登记。
- **保留策略**：`boot_store_retention_version=1` 保留全部现用系统，每个 Linux 最多一个最近、同文件系统且有完整更新记录的旧内核，以及最近 8 份已完成分区审计。当前更新的回退镜像、根绑定和登记保留；不沿已完成历史计划递归保留整条版本链。旧 Android boot 不能恢复已经替换的 super/固件/数据，不当作 ROM 回滚。
- **历史失败**：同一目标后来完整安装，或原分区被已完成的明确编辑删除后，可核验为已取代历史；可以清理其过期材料，但不把旧尝试标成安装成功。未确认被取代的失败继续阻止清理。
- **自动回收**：整份联合计划成功之后、写入租约关闭之前执行；不在 Android/Linux 子任务提交时清理。安装成功和清理暂缓分别记录。固定版平板切换共用此逻辑；旧 Rec 没有该能力时只执行原等价快照回收协议。
- **清理中断**：先持久保存 `.retention/active.json`，绑定登记、保留材料和逐项摘要；过期事务目录原子移入暂存后删除。恢复时只重放同一计划，登记或保留材料变化即停止。清理未完成时不开始新的安装或切换；只读检查不自动写盘。成功后仅保留一份清理结果，避免清理记录继续堆积。

检查接口返回 `cleanup_available` 和 `cleanup_pending`；GUI 根据可执行工作启用清理入口，预计回收 0 字节也能续接。对于已发布的旧 Rec，GUI 从原有的保留计划或重复快照列表判断可清理状态。仅分区操作也保存并显示会话完成时返回的清理结果。

普通清理只开放 linux_boot，恢复原引导额外开放 boot_a。未结束事务、未知状态、变化的恢复材料或默认引导不匹配会阻止普通清理。未写完的 rootfs/ROM、没有新持久记录的旧安装、没有 linux_boot 的单 Android 失败，仍需原电脑报告和备份分析。

事务检查见 [boot-store.py](../tests/boot-store.py) 和 [transaction-recovery.py](../tests/transaction-recovery.py)；离线注入不覆盖真实物理断电。

## BCB 退出清理

启动时只读记录 misc 中的 BCB 快照。正常重启、返回 Bootloader 和关机共用退出检查，仅清理启动时已读取、内容未变且没有待处理安装事务的无参数 boot-recovery。

OTA/清除数据参数、阶段标记、Virtual A/B 合并、启动后新增或变化的请求及未结束会话保留。清理前在 RAM 备份完整 misc，仅改写命令字段所在扇区，整分区回读并恢复只读；失败尝试恢复原扇区并留在 Rec。快照与结果可随日志导出。

## 构建、连接与验收

当前安装版由 `build/recovery/build-native-installer.py` 组合专用内核、UI 和 adbd；固定封装由 `build-universal.py --installer` 等流程生成。原生基线、内核组件、DTB、触屏模块和全部运行文件均有哈希，构建步骤不访问平板。

专用维护内核默认使用 kernel.org 7.3.4 和 Fedora 公共补丁，另加 Rec 配置与专用补丁，见[Rec 内核配方](../build/recovery/kernel/README.md)。保留显示时钟/电源依赖与 ADSP 电池通信，使用 USB peripheral 和触控衔接路径。Rec/公共来源锁、内核配置、全部补丁、对齐入口及组件摘要附在镜像和分发包中，不与桌面 Fedora 的完整 DRM/音频/OTG 配置混用。

`recovery_fast_start_version=1` 在确认 UFS LUN、两份有效 GPT、分区项和内核几何一致后结束固定等待，再封存分区；缺少完整证明时走有上限的等待，不写盘修表。不能为清除 sync_state 日志随意裁掉显示时钟和电源消费者。

GUI 连接正在运行的固定版时，需要本地对应镜像才能核验构建 ID 和载荷。分发包用 `--fixed-installer` 附带它的校验镜像，不自动部署固定版。新增能力需要实际启动对应新 Rec。

分发时核对镜像、内嵌能力和配套资格文件，并分别测试临时启动与固定启动。可信物理 USB 主机拥有 root 端点；范围校验用于防止误操作，不是对恶意 root 主机的隔离。
