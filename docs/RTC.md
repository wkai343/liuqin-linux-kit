# RTC 与离线时间保持

[硬件说明](HARDWARE.md) ｜ [构建指南](BUILD.md)

PMK8350 使用 `rtc-pm8xxx` 驱动。当前设备树不允许直接写 RTC，也没有配置保存偏移的 NVMEM 单元；`liuqin-rtc` 将校准保存在 Linux 文件系统，结合硬件计数恢复开机时间。实现见 [liuqin-rtc](../device/board-overlay/usr/libexec/liuqin-rtc)。

联网校准、离线重启及关机后恢复时间已验证；长期漂移、拔线关机和掉电重置仍需扩大测试。`/dev/rtc*` 保留原始计数，不使用 `hwclock --systohc` 写时。

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

服务随 Fedora RPM 的 `liuqin-device-support` 板级载荷进入镜像。
`sysinit.target.d` 与 `timers.target.d` 拉起服务，不依赖安装器修改固件或引导分区。

机型识别兼容 ABL 覆盖根节点 `compatible` 的行为：原始 `xiaomi,liuqin` 可直接识别；
原厂 ABL 路径必须同时匹配完整 Liuqin 型号和 `xiaomi,miboard-id = <0x10 0>`。
chrony 状态通过 `127.0.0.1:323` 的只读监控接口读取。helper 直接发送协议 v6 的
TRACKING 请求，并检查应答版本、类型、长度、随机序号、时间及同步误差界；格式来自
[chrony 协议定义](https://github.com/mlichvar/chrony/blob/4.9/candm.h)。服务直接查询该接口，保留 `NoNewPrivileges=yes` 和 SELinux Enforcing，本地监控端口不对外开放。
若自行禁用 chrony 的本地监控接口，自动保存会跳过，仍可显式人工校准。

校准不占用 `/dev/rtc*`，不设置闹钟，不写 Android 的 `persist`，也不启用硬件写时。
chrony 的 `rtcfile` 功能需要 RTC 更新中断；当前驱动由闹钟队列提供该机制，因此使用 sysfs 只读采样，避免引入新的 RTC 定时唤醒。相关实现见
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

## 回归与日志

`python3 tests/rtc.py` 执行实际 helper 的保存/恢复逻辑，用模拟时钟和 sysfs 检查经过时间、错误状态拒绝及保存故障，不修改测试主机时钟。服务语法和依赖可用 `systemd-analyze verify` 检查。

日志入口：

```sh
journalctl -b -u liuqin-rtc-restore.service -u liuqin-rtc-save.service
systemctl status liuqin-rtc-save.timer
```
