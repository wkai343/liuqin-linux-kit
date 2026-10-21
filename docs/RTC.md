# RTC 与离线时间保持

[硬件说明](HARDWARE.md) ｜ [待改进与验证](DEVELOPMENT-STATUS.md)

PMK8350 的 RTC 已由 `rtc-pm8xxx` 驱动启用。已有真机日志确认 `rtc0` 注册成功，
但原始计数被解释为 1973 年；临时内核有定时唤醒短测记录。
当前设备树既未允许直接写 RTC，也未提供保存时间偏移的 NVMEM 单元；启动日志没有 UEFI。
尚未确认本机存在可安全分配的持久化 PMIC 存储，因此不直接新增写寄存器或 SDAM 配置。

新增 `liuqin-rtc` 在 Linux 文件系统保存校准，再结合硬件 RTC 的持续计数恢复开机时间。
**已通过真机联网校准、停用 NTP 后重启，以及关机再开机的短测（未单独验证拔线）。**
验证运行于 SELinux Enforcing；拔线关机、长期漂移和 RTC 掉电重置仍待测。
这不意味着 `hwclock --systohc` 已可用，也不改变 `/dev/rtc*` 读出的原始时间。

## 保存与恢复

- `liuqin-rtc-restore.service` 在本地文件系统可用后、`sysinit.target`、
  `time-set.target` 和 chrony 之前执行一次。校准存在且有效时，恢复值为
  `保存的系统时间 + 当前 RTC 计数 - 保存的 RTC 计数`。
- `liuqin-rtc-save.timer` 在开机约 45 秒后开始，每 5 分钟尝试保存一次。
  只接受 chrony 最近一小时内的外部同步，系统剩余校正小于 1 秒且报告的误差界小于 5 秒。
  未联网或仍在调整系统时间时保留之前的校准。计时器不会主动唤醒睡眠中的平板。
- 状态保存在 `/var/lib/liuqin-rtc/anchor.json`，目录为 root/0700，文件为 root/0600。
  写入使用临时文件、同步、原子替换，并绑定本机 machine-id 和 RTC 身份。
  成品镜像不携带校准；重新安装或 machine-id 改变后需要重新校准。
- 每次保存和恢复都确认 RTC 正在走时。计数倒退/回绕、超过 366 天未校准、状态损坏、
  时钟在读取中跳变等会拒绝恢复；正常联网校准可以重建有效状态。
  恢复只在开机前五分钟允许执行，且不会把已经更新的系统时间向后拨。

服务随 Fedora RPM 和 Ubuntu DEB 的 `liuqin-device-support` 板级载荷进入镜像。
`sysinit.target.d` 与 `timers.target.d` 拉起服务，不依赖安装器修改固件或引导分区。
内核配置和补丁清单没有变更。

机型识别兼容 ABL 覆盖根节点 `compatible` 的行为：原始 `xiaomi,liuqin` 可直接识别；
原厂 ABL 路径必须同时匹配完整 Liuqin 型号和 `xiaomi,miboard-id = <0x10 0>`。
chrony 状态通过 `127.0.0.1:323` 的只读监控接口读取；Fedora SELinux 不允许 chronyd
向普通服务创建的 Unix 客户端套接字回复。没有修改 SELinux 策略或对外开放监控端口。
若自行禁用 chrony 的本地监控接口，自动保存会跳过，仍可显式人工校准。

校准不占用 `/dev/rtc*`，不设置闹钟，不写 Android 的 `persist`，也不启用硬件写时。
chrony 的 `rtcfile` 功能需要 RTC 更新中断；本机驱动由闹钟队列提供该机制，因此本次使用
sysfs 只读采样，避免引入新的 RTC 定时唤醒。相关实现见
[Linux RTC 中断接口](https://github.com/torvalds/linux/blob/master/drivers/rtc/interface.c) 和
[chrony RTC 跟踪](https://github.com/mlichvar/chrony/blob/master/rtc_linux.c)。

## 使用与检查

升级板级软件包后执行 `sudo systemctl daemon-reload`，并启动保存计时器：

```sh
sudo systemctl start liuqin-rtc-save.timer
chronyc tracking
sudo /usr/libexec/liuqin-rtc save
sudo /usr/libexec/liuqin-rtc status
sudo /usr/libexec/liuqin-rtc restore --dry-run
```

`status` 输出 RTC 计数、推算时间和与系统时间的差值；校准尚未建立时会明确提示。
应看到最近的校准和约秒级的差值，具体精度由 NTP 质量、RTC 漂移及读取相位决定。
此实现没有拟合 RTC 的长期频率漂移，不承诺长期离线误差界。
不要用 `hwclock --hctosys` 恢复时间，它读到的仍是未经偏移校正的硬件计数。

没有网络时，可在人工确认系统 UTC 时间正确后显式保存：

```sh
sudo /usr/libexec/liuqin-rtc save --trust-system-time
```

只保存可信校准；自动路径不会把上一次启动残留的错误日期当作新校准。
首次安装、清除状态或 RTC 掉电重置后，需要先联网或人工校准一次。
仅靠一个 RTC 计数不能识别所有复位/改写方式，尤其是计数被外部改为更大的值；
拔掉电池后的保时能力仍取决于硬件供电，尚无实测结论。

## 验证范围

本机离线回归：`python3 tests/rtc.py`。测试执行实际 helper 的保存/恢复逻辑，
通过模拟时钟和 sysfs 检查离线经过时间、拒绝错误状态及保存故障；不会修改测试主机时钟。
另外用 `systemd-analyze verify` 检查服务语法、依赖和顺序。

2026-10-21，一台 HW16.9.0 / 256GB 设备的记录：

| 检查 | 结果 |
| --- | --- |
| 内核 | `7.3.0-liuqin-folio-test-gb98f45849b3d`；两轮均使用同一镜像经 Fastboot 临时启动 |
| 联网保存 | systemd 保存服务成功，SELinux 为 Enforcing |
| 无 NTP 重启 | chronyd 的启动条件被临时阻止；RTC 恢复服务约在启动 14.9 秒成功；5 次 SSH 时间对比中点差约 +0.52～+0.57 秒 |
| 关机再开机 | `systemctl poweroff` 后人工进入 Fastboot；本轮未安排拔线步骤，chronyd 仍未启动；恢复服务约在启动 15.7 秒成功；对比中点差约 +0.53～+0.56 秒 |
| RTC 连续计数 | 关机前后采样计数增加 117 秒；后一次内核运行约 46 秒，包含约 71 秒的关机、人工开机和引导器阶段 |

SSH 对比往返约 0.12～0.26 秒，上述数字用于短测，不是长期精度保证。
测量结束后恢复 chrony，移除测试专用启动条件和兜底计时器，保留正常 RTC 服务。
校准和设备日志留在本机，不进入源码或软件包。

后续验收：

1. 拔掉 USB 后关机更长时间，测量仅靠电池供电的 RTC 保时和长期漂移。
2. 验证睡眠恢复和既有 RTC 定时唤醒，检查保存计时器未增加唤醒；本轮没有改变闹钟驱动。
3. 验证丢失校准、RTC 真正掉电重置后拒绝恢复及重新联网校准，Ubuntu 也需实机验收。
4. 固定引导后的默认构建另行验收；本轮 RTC 测试没有将 folio 临时内核写入 boot 分区。

日志入口：

```sh
journalctl -b -u liuqin-rtc-restore.service -u liuqin-rtc-save.service
systemctl status liuqin-rtc-save.timer
```
