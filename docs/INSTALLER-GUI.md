# 安装助手

电脑端 QML 安装向导，支持 Linux、Windows 10/11 x64 与 Xiaomi Pad 6 Pro（liuqin）。
当前提供 userdata 单根 ext4/Btrfs 安装和 A 槽引导。
镜像构建使用 Linux/Devbox，安装器不需要构建环境。

## 启动

准备 uv、Fastboot、USB 连接及 USB 网络环境，在项目根目录运行：

```sh
uv run --project installer installer/install-gui.py
```

uv 自动准备独立的 Python 3.12 和锁定的 Qt 依赖，首次运行需要下载，之后复用缓存。
Linux 主机仍需正常图形会话及 Qt 系统运行库，GUI 以普通用户运行。

在界面选择镜像目录，或添加 `--bundle /path/to/images`。
目录只需要 `installer.img`、`boot.img`、`rootfs.tar.gz`；可选 `bundle.json` 仅用于验收说明。
程序读取实际镜像接口，核对 boot 与 rootfs 中的内核/模块身份，不要求目标内核与安装内核相同。
自定义或尚未验收的包需在确认页接受其测试状态。

## 安装步骤

1. 平板解锁 Bootloader、进入 Fastboot，连接电脑并核对序列号。
2. 选择三个安装文件，在“安装材料”中选择支持的文件系统。
3. 在“连接平板”页创建设备备份，或选择同一设备的已有备份。
4. 点击“检查安装方案”，读取真实容量和布局；准备阶段不格式化。
5. 核对方案、勾选确认项并输入序列号，开始安装。
6. 进入桌面后检查网络、声音、旋转和键盘，再正常重启，最后确认首次启动结果。

**首次安装和重装都会清空整个 userdata，包括原 Linux 和 `/home`。**
请先把个人文件备份到平板以外。设备备份只含 boot_a、boot_b、persist 和 GPT，不含个人文件或完整 Android 系统。
安装前检查电量至少 30%，RAM 需能暂存系统归档；备份操作不要求满足归档暂存门槛。

打开界面只枚举设备、查找本地备份，不自动安装。
写入前会重新核验设备、会话、文件、分区边界及备份；boot_a 写入后回读校验，再选择 A 槽启动。

## 文件系统

可选格式由三个文件的实际支持范围共同决定。ext4 使用普通 `native-root` 目录；
Btrfs 使用同名子卷、Zstd 三级压缩和元数据 DUP，`/home` 位于其中。
两者均不增加 GPT 分区或独立 home；Btrfs 不自动配置快照或回滚策略。
更换格式需取消已有方案后重新检查，确认页会显示最终选择。

首次使用 Btrfs 须重新构建三个文件：`devbox run build`。
旧镜像的驱动为模块，不能挂载 Btrfs 根系统。修复后的安装与启动路径已在本机 256GB 上验证桌面启动和正常重启。

## 备份复用

| 操作 | 行为 |
| --- | --- |
| 创建设备备份 | 优先复用已核验备份；没有时临时启动 RAM，备份完成后返回 Fastboot |
| 选择/查找已有备份 | 按序列号导入或查找，支持此前 CLI 与早期安装流程的备份 |
| 另建备份 | 保存新时间点的快照，保留旧目录 |
| 检查安装方案 | 核验已有备份和当前设备，不重复传输分区备份 |

检查包含 SHA256、设备序列号、分区边界与大小。切槽造成的 GPT 属性变化、安装后 boot 内容变化，
不会直接使原始备份失效。新快照不能替代安装前的原厂恢复材料。

persist 全分区哈希包含文件系统元数据，差异不一定表示校准损坏。
安装器会提示快照差异，并以只读方式检查当前校准能否导入；不改写原备份或自动另建备份。
准备方案后 persist 再次变化，会在格式化前停止。

备份目录位于安装包之外，可在界面选择。安装记录另存于 state-dir 的 `runs/SESSION_ID/`，
包含 `receipt.json`、`events.jsonl`；默认位置为 Qt 用户数据目录，可通过 `--state-dir DIRECTORY` 更改。

## Windows 10/11 使用

1. 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，解压安装器源码包。
2. 解压 [Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools)，
   将含 `fastboot.exe` 和 DLL 的 `platform-tools` 目录放在 `installer` 旁边。
   也可使用 PATH、Android SDK 或 `LIUQIN_FASTBOOT` 指定路径。
3. 安装平板 Fastboot 驱动，确认设备管理器中的 Bootloader 设备正常；
   参见 [OEM USB 驱动说明](https://developer.android.com/studio/run/oem-usb)。
4. 双击 `installer/start-windows.cmd`，按前述步骤选择文件、设备和备份。
5. 备份或检查方案时，平板临时进入 RAM 环境。点击底部“设置 USB 网络”，授权管理员设置；
   脚本最多等待 60 秒，配置电脑 `192.168.7.1/24`，平板为 `192.168.7.2`。
6. 继续核对方案并确认安装。GUI 本身保持普通用户权限。

Fastboot 驱动与 RAM 的 USB 网卡是两种设备。

专用网卡使用 USB ID `1d6b:1041`、主机 MAC `02:00:00:00:07:01` 和 Microsoft OS 描述符，
适配 Windows 10/11 自带 RNDIS 驱动；已安装桌面的 USB 配置仍为 NCM。
参见 [Microsoft USB 类驱动](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/supported-usb-classes)
与 [RNDIS 说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/network/remote-ndis--rndis-2)。

网络脚本仅操作匹配的专用网卡，不改 Wi-Fi 或默认网关；防火墙只放行平板到该网卡 TCP 37631。
规则保留供下次使用。清理时，在管理员 PowerShell 运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\windows-network.ps1 -Remove
```

这会移除项目规则，并为当前连接的专用网卡恢复 DHCP；执行策略参数仅影响本次进程。

## 取消、失败与验证

准备阶段可以有序取消；写入期间不能取消或正常关闭窗口。
断电、强制结束进程或 USB 断线仍可能中断安装，不支持任意阶段自动恢复。
失败后保留备份和安装记录，导出诊断报告，先核对已完成阶段，避免直接重跑格式化。
报告会替换已知序列号和私人路径，不复制原始分区备份。

安装器不提供多分区、双系统、Android 自动恢复或内核构建。
文件服务仅绑定平板网络地址，并使用会话令牌与允许列表；安装后不新增持久管理员通道。
本机 256GB 已完成 Linux 主机上的 GUI 单根 Btrfs 安装与重启验证，Windows 主机 GUI 安装也已确认成功；范围见 [硬件说明](HARDWARE.md)，其他容量仍待实测。

开发演示、测试、源码位置和 JSON 协议见 [安装器开发说明](../installer/README.md)。
独立安装器源码包由 `python3 installer/package.py` 生成；镜像另行提供，构建见 [构建指南](BUILD.md)。
