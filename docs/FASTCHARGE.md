# 充电与快充

[项目首页](../README.md) ｜ [构建指南](BUILD.md#启动与设备适配)

充电档位由 ADSP 固件协商；项目提供双电芯认证、小米 MiPPS 适配器握手及标准 PPS 开关。

| 路径 | 条件与行为 |
| --- | --- |
| MiPPS | 小米适配器通过 UVDM 认证后，由固件选择快充档位 |
| 标准 PPS | 未认证时通常保持 9 V；启用下述开关且线缆满足条件时可开放高压通路 |
| 普通 PD、DCP、SDP | 沿用固件行为 |

基础充电、电量显示与电芯认证已验证；MiPPS/PPS 适配器尚未完成完整功率测试，不能仅凭接口或构建通过认定快充功率。

## 初始化与普通重启

USB、充电和显示保持内建；Qualcomm 音频通信、时钟与声卡由根文件系统加载，避免早期音频资源请求与充电固件初始化重叠。完整音频功能和电芯认证保留，启动时不执行 PD 硬复位。

保持电脑 USB 的普通重启充电和扬声器已验证。充电初始化通过延后音频模块加载与早期 Q6 资源请求隔离；底层固件内部状态链仍未完全确定，其他电源及高负载需分别测试。

Rec 使用独立维护内核和固定 USB 设备模式，冷启动充电正常。临时启动、普通重启和其他充电器仍须分别验证。回退内核时还需恢复配套设备包，见[内核更新](KERNEL-UPDATES.md)。

输入限流改写、warm 重启和运行中重启 ADSP 不属于当前修复方案。

## 如何判断充电是否正常

`input_current_limit` 是固件报告的请求/限额，不是实际输入电流。满电时低输入可以正常；应观察输入是否随负载增加、电池是否持续补供，以及 Full/Charging 状态。满电短测不能代替低电量、高负载或其他充电器验收。

## 组件

| 路径 | 作用 |
| --- | --- |
| `/usr/local/libexec/liuqin-mipps-auth` | 认证守护进程（Python，GPL-2.0-only，见 `NOTICE`） |
| `/etc/systemd/system/liuqin-mipps-auth.service` | 运行守护进程的 oneshot 单元 |
| `/etc/systemd/system/liuqin-mipps-auth.service.d/10-allow-unverified-adapter.conf` | 启用标准 PPS 开关 |
| `/etc/udev/rules.d/90-liuqin-mipps-auth.rules` | Type-C 连接与 USB 供电上线时拉起单元 |

守护进程通过项目内核在 PMIC GLINK 电源设备上导出的 `xiaomi` 属性组与充电固件通信：

```
/sys/devices/platform/pmic-glink/pmic_glink.power-supply.0/xiaomi/
```

该属性组不存在时单元自动跳过，软件包可在没有此接口的内核上正常安装。

## 认证流程

USB 供电上线后，守护进程依次执行：

1. 通过 `verify_digest` 对两个电芯做燃料计认证：写入随机挑战，读回燃料计的 HMAC-SHA256
   应答并比对，按结果设置 `authentic` / `slave_authentic`。
2. 通过 `request_vdm_cmd` 做适配器认证：UVDM 握手（命令 1–3）、会话种子（命令 4）、随机
   挑战（命令 5）、判定（命令 6、7），通过时再发反向认证应答（命令 8）。
3. 按判定写入 `pd_verifed`。固件随后选择充电档位，结果体现在 `fastchg_mode`、`power_max`
   与 `real_type`。

握手所用密钥在小米设备与适配器之间通用，已包含在守护进程中。

## 标准 PPS 适配器

非小米适配器无法通过握手，固件会把合同保持在 9 V。安装 `10-allow-unverified-adapter.conf`
后，守护进程在同时满足下列条件时为此类适配器置位 `pd_verifed`：

- 已协商的合同为 `PD_PPS`；
- 线缆额定电流满足高压通路的需求。内核导出线缆身份时，e-marker 必须标注 5 A；未导出时，
  依据 USB-PD 规范中"源端只有自行读到 5 A e-marker 才会提供 3 A 以上档位"的规则做判定。

判定及其原因记录在单元日志中，形如 `unverified_adapter=allowed: …` 或
`unverified_adapter=refused: …`。桌面通知显示实际协商的档位（PPS 或 PD），不会显示 MiPPS。

如需保持严格的认证门控行为，删除该 drop-in：

```sh
sudo rm /etc/systemd/system/liuqin-mipps-auth.service.d/10-allow-unverified-adapter.conf
sudo systemctl daemon-reload
```

## 验证

```sh
X=/sys/devices/platform/pmic-glink/pmic_glink.power-supply.0/xiaomi
journalctl -u liuqin-mipps-auth -n 40
cat "$X/real_type" "$X/pd_verifed" "$X/fastchg_mode" "$X/power_max"
cat /sys/class/power_supply/qcom-battmgr-usb/voltage_now
cat /sys/class/power_supply/qcom-battmgr-bat/current_now
```

固件按电量与温度限制充电档位。快充在电量约 5 %–70 % 区间可观察到；电量高于 90 % 时无论
是否认证固件都选择标准档位。

## 排障

| 现象 | 检查 |
| --- | --- |
| 单元从不运行 | `xiaomi` 属性组不存在：运行中的内核早于该接口 |
| 小米适配器下 `pd_verifed` 仍为 0 | 看 `journalctl -u liuqin-mipps-auth`：`adapter auth mismatch` 表示握手失败；`pdo2 reports no source PDO` 表示适配器未提供 PD 合同 |
| `pd_verifed` 为 1 但 `fastchg_mode` 为 0 | 电量高于 90 %、电池温度在快充窗口之外，或适配器未提供 PPS APDO |
| `unverified_adapter=refused` | 原因随后给出：线缆额定低于 5 A，或合同不是 PD_PPS |
