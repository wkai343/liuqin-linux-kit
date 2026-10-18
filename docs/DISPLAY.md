# 显示与画质

[项目首页](../README.md) ｜ [硬件支持](HARDWARE.md) ｜ [待改进与验证](DEVELOPMENT-STATUS.md)

默认目标内核按 OS2.0.6.0.VMYCNXM 对齐面板流程，保持 2880 × 1800、120 Hz。
目前真机验证限一台天马屏 liuqin；CSOT 命令和 PHY 参数已离线核对，尚未真机验收。
开机仍可能出现条纹、闪烁或黑屏，自动恢复已实测生效，但不代表根因已解决。

## 亮度与 CABC

桌面亮度通过原厂曲线，将 0–4095 的逻辑范围映射到 0–2047 的背光控制值。
CABC 是随画面内容调整的背光策略，不等于色彩校准；实际观感和耗电收益仍需测量。

```sh
liuqin-display-policy status
sudo liuqin-display-policy set ui
sudo liuqin-display-policy set ui --save
```

| 模式 | 原厂策略 |
| --- | --- |
| `off` | 关闭 CABC |
| `ui` | 界面 |
| `still` | 图片 |
| `movie` | 视频 |

不加 `--save` 时只影响本次启动，所选模式会在睡眠唤醒及显示恢复后重新应用。
加 `--save` 写入 `/etc/liuqin/display-policy.json`，由启动服务恢复。
删除该配置并重启即可取消保存的选择；无配置时保留既有初始化策略，不强制开启 CABC。
`programmed` 仅表示命令发送成功，不是硬件寄存器回读，也不保证与 Android 默认策略相同。

## 自动恢复与日志

恢复服务只在拥有当前活动 Wayland 会话的用户中操作，协调关闭、重新开启显示输出。
它核对面板告警 GPIO 的实际电平，并限制为每用户每 10 分钟最多两次、两次间隔至少 60 秒。
启用 CABC 自适应模式后，PWM 为零可能是正常调光，服务不会仅凭这一项触发恢复。
恢复不重启系统或应用；关屏时不触发恢复，操作前核对活动会话与系统状态。

查看当前用户的恢复记录：

```sh
journalctl --user -b -u liuqin-display-recovery.service
```

查看包含登录界面的记录：

```sh
sudo journalctl -b _SYSTEMD_USER_UNIT=liuqin-display-recovery.service
```

报告故障时记录出现阶段、是否自行恢复、是否按键或睡眠过。正常重启、拔线冷启动和
登录界面睡眠应分别验收；一次成功启动不能排除偶发问题。

## 已对齐的驱动流程

| 环节 | 当前行为 |
| --- | --- |
| 显示接管 | 先熄背光再接管时钟；修正 MDP/PLL、DSI 频率与 SMMU 交接 |
| 面板初始化 | 按引导选型发送天马／CSOT 原厂命令与 PPS，保留包类型、分组、等待和广播拆包；未知选型保留原路径 |
| 供电与复位 | 对齐原厂申请顺序与复位波形；关屏保留 IOVDD、偏压，关闭两路 1.3 V；保留电压及两处 120 ms 保护等待 |
| DSI 启停 | 开屏命令完成后启动视频；显示控制器停帧后再停视频、发送 OFF；时钟门控保持关闭 |
| PHY | 已知面板、连接关系和链路频率匹配时，使用原厂 D-PHY 时序表 |
| 背光与调光 | 首帧完成后开启六路 LED，再恢复正常显示、异步执行额外调光；关屏取消过期任务 |

时钟门控开启时曾复现登录界面睡眠唤醒长时间卡住，因此正常引导显式设置
`msm.liuqin_oem_clk_gating=0`。完整五路断电、动态刷新率和引导画面连续接管尚未实现。

## 开发对照参数

下列参数用于构建故障对照镜像，不能代替匹配内核、模块与完整回退镜像。
补丁启用顺序由 `build/kernel/source.json` 的 `target_patches` 决定，不按文件编号补全试验补丁。

| 内核参数 | 对照作用 |
| --- | --- |
| `panel_novatek_nt36532.first_frame_backlight=0` | 停用首帧背光等待 |
| `panel_novatek_nt36532.liuqin_retained_power=0` | 使用旧关屏供电状态，仍保留新的启动供电与触控探测顺序 |
| `panel_novatek_nt36532.oem_batch_commands=0` | 逐条发送开关屏命令 |
| `panel_novatek_nt36532.oem_normal_display=0` | 跳过背光开启后的恢复正常显示命令 |
| `msm.liuqin_oem_dphy_timing=0` | 使用主线计算的 PHY 时序，保留其余面板和背光适配 |
| `ktz8866.oem_brightness_curve=0` | 使用原来的 0–2047 线性亮度接口 |
| `panel_novatek_nt36532.oem_delayed_dimming=0` | 停用首帧之后的额外调光命令 |
| `panel_novatek_nt36532.esd_monitor=0` | 停用 GPIO 告警接口及依赖它的自动恢复服务 |
| `panel_novatek_nt36532.esd_confirm_level=0` | 对照旧告警逻辑，不再核对中断到达时的电平 |
| `msm.liuqin_oem_video_order=0` | 停用原厂开屏阶段排序，项目正常引导设置为 1 |
| `msm.liuqin_oem_shutdown_order=0` | 停用显示控制器停帧后的关屏排序，项目正常引导设置为 1 |
