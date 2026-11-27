# 环境光、自动亮度与自动旋转

liuqin 通过原厂 SLPI 固件、FastRPC 和 SSC 用户态读取环境光，不需要另写
Linux I²C 光感驱动。桌面使用标准 `net.hadess.SensorProxy` 接口：

```text
TCS3701 → SLPI / SSC → libssc → iio-sensor-proxy
                                      ↓ lux
                            Plasma KWin 自动亮度
                                      ↓
                            PowerDevil 原生背光
                                      ↓
                         ktz8866-backlight（0–4095）
```

## 原厂依据与边界

固定输入为 `OS2.0.6.0.VMYCNXM`，镜像身份见
[`liuqin-rom-images.json`](../shared/liuqin-rom-images.json)。以下路径相对原厂 vendor：

| 来源 | 内容 |
| --- | --- |
| `etc/sensors/config/tcs3701.json` | ALS 使用 `hw_id=0`，非 DRI；prox 配置不能作为接近传感器可用的证明 |
| `etc/sensors/config/sm8450_tcs3701.json` | `sns_tcs3701`，总线实例 1、I²C 地址 57（`0x39`）、400 kHz、IRQ 14、`sensor_vddio` 电源，以及通用光学系数 |
| `etc/sensors/config/sm8450_tsl2522_0.json` | 另有 TSL2522 配置；文件存在不表示本机实际安装或当前 SSC 选择了它 |
| `overlay/FrameworksResTarget_Vendor.apk` | 28 个 lux 分界点和 29 个 8 位亮度值，作为初始自动亮度曲线的参考 |

SSC 实际枚举出的默认 `ambient_light` 为 `tcs3701`，厂商 `ams AG`，以
`on-change` 模式上报。`libssc` 0.4.4 读取测量报文 `1025` 的第一个 float
作为 lux；第二个原始量不参与调光。该库过滤负值，零 lux 是有效读数。
加速度计通路与环境光分别请求、分别释放，共享 SLPI 生命周期。

高通 FastRPC listener 在首次缓冲区放不下 DSP 请求时，沿用同一上下文，以已接收长度为偏移调用 `get_in_bufs2`，并检查报告的长度是否超出缓冲区。[高通 listener](https://github.com/qualcomm/fastrpc/blob/3b7f929145db1a52a8d0a334805defefd287e97e/src/listener_android.c#L211-L241) · [高通接口定义](https://github.com/qualcomm/fastrpc/blob/3b7f929145db1a52a8d0a334805defefd287e97e/inc/adsp_listener.h#L49)

本项目的 `hexagonrpcd` 首次用 `next2` 接收最多 256 字节，长请求从偏移 256 续取。守护进程要求两次报告的总长度一致、参数完整，才分派请求。已编码的输入上限为 16 MiB。[Linux 移植与协议测试](../device/sensors/patches/0007-hexagonrpcd-fetch-complete-listener-inputs.patch)

平板上的 ARM64 协议测试使用构造载荷，覆盖 256 字节接收边界两侧以及 4 KiB、64 KiB 长请求。复核时可在已应用补丁的 hexagonrpc 构建目录执行 `meson test -C <构建目录> listener-input-232 listener-input-233 listener-input-4096 listener-input-65536`。

通用配置由构建复制；本机校准继续使用 `/var/lib/liuqin-sensors/registry`，
通过现有导入流程获得，不进入可发布镜像。不能把另一台机器的注册表当作通用固件。

[`liuqin-auto-brightness.json`](../build/rootfs/liuqin-auto-brightness.json)
记录原厂资源 APK 的 SHA-256 和完整数组。补上零 lux 的首点，将亮度值除以
255，得到 KWin 的 0–1 控制点。例如：

| 环境照度 | 初始亮度 |
| --- | --- |
| 0–2 lux | 1.57% |
| 30–83 lux | 18.04% |
| 200–400 lux | 23.53% |
| 1000 lux | 31.37% |
| 2000 lux | 52.94% |
| 4000 lux 及以上 | 100% |

这是**原厂 vendor 参考曲线**，不是完整 MIUI 自动亮度算法：其他 Android 资源覆盖、
小米学习/防抖策略和 HBM 还可能改变结果；百分比也不等于已校准的 nits。
Linux 背光原有的 4095 → 2047 映射及最低非零亮度保持原有行为，见
[显示与背光](DISPLAY.md#亮度与-cabc)。

## Plasma 接入

[Plasma 6.6](https://kde.org/announcements/plasma/6/6.6.0/) 起提供原生自动亮度。
6.8.2 的 [LightSensor](https://invent.kde.org/plasma/kwin/-/blob/v6.8.2/src/utils/lightsensor.cpp)
监听 `HasAmbientLight`，请求 `ClaimLight`，并接收单位为 `lux` 的 `LightLevel`。
无需另起一个不断写 sysfs 的亮度服务。

KWin 6.8.2 默认曲线只有 `[0, 1]`，所以单独打开开关可能始终保持全亮。
构建在桌面的 `/etc/xdg/kwinoutputconfig.json` 中为 `DSI-1` 提供上述参考点并启用
`automaticBrightness`。KWin 仍须确认光感和原生背光可用才会调节。
登录界面保留独立的静态显示配置；已有 `~/.config/kwinoutputconfig.json`
优先，构建不会覆盖已有用户的开关、曲线、旋转或外接屏设置。

[KWin 6.8.2 策略](https://invent.kde.org/plasma/kwin/-/blob/v6.8.2/src/workspace.cpp)
采用约 20% 亮度死区和 2 秒后续采样延迟，控制点间线性插值。
自动亮度开启时手动调亮/调暗，会更新当前照度附近的学习曲线。其设置入口为
「系统设置 → 显示与监视器 → 显示配置 → 自动亮度」。桌面终端也可执行：

```sh
kscreenctl DSI-1 set-auto-brightness on
kscreenctl DSI-1 set-auto-brightness off
kscreenctl DSI-1 --json
```

现有用户可保留自己的曲线并直接开启。需要导入系统参考曲线时，先退出该用户的
Plasma 会话，再备份 `~/.config/kwinoutputconfig.json`，只把系统默认配置中
`DSI-1` 的 `autoBrightnessCurve` 和 `automaticBrightness` 两项复制到对应用户条目，
再登录。不要在 KWin 运行时改这个文件：它不会重新加载这些点，并会用内存状态覆盖磁盘。

发行版 `80-iio-sensor-proxy.rules` 已为 `fastrpc-sdsp` 添加 `ssc-light`；板级规则
额外添加 `ssc-accel`。仅检查板级规则会漏掉光感的实际准入来源。
保留现有 SLPI/真实加速度采样启动门控，环境光可用性仍由 SSC 驱动枚举决定。
Fedora 在启动时拉起 `iio-sensor-proxy.service`。本地桌面通过 Polkit 的
`net.hadess.SensorProxy.claim-sensor` 授权读取加速度和环境光，登录时无需重启代理。

## 可复现检查

1. 核对原厂材料：从固定 ROM 的 vendor 提取资源 APK，执行
   `python3 tests/display/ambient-light-reference.py --stock-overlay /path/to/FrameworksResTarget_Vendor.apk --aapt2 /path/to/aapt2`。
   脚本先要求 SHA-256 相同，再通过 Android `aapt2 dump resources` 对照完整数组。
2. 运行 `python3 tests/build/fedora-build.py` 和 `python3 tests/input/sensor-build-environment.py`。
   检查桌面/登录界面配置分离、已有用户设置保留、曲线单调性及构建环境。
3. `build/scripts/build-liuqin-sensors-stack.sh` 在固定 libssc 的 mock server 上运行
   `tests/input/ssc-accel-integration.py`。六项必须实际通过：加速度、加速度提前请求、
   环境光释放重取、环境光提前请求、光感/加速度共用，以及提前请求后释放。
   测试通过 `LIUQIN_SSC_TEST_SERVER_PID` 暂停专用 mock server，让 coldplug 竞态
   在原生主机和 QEMU 下都确定发生；不能把跳过或旧的 light 测试失败当作通过。
4. 实机先运行 `timeout --kill-after=2s 10s ssccli --sensor=light --timeout=5`；
   需要型号和 SSC 属性时增加 `G_MESSAGES_DEBUG=all`。必须有真实 `Light sensor measurement`。
5. 桌面终端运行 `monitor-sensor --light`，遮挡、放开并增加照明，检查 lux 随之变化。
   SSH 不属于本地活动会话，可能被 Polkit 拒绝；诊断时可用 `sudo monitor-sensor --light`，
   不要因此放宽系统授权。单次 `busctl ClaimLight` 退出即释放连接，不能替代持续订阅。
6. 自动亮度开启后，同时记录 `LightLevel`、`kscreenctl DSI-1 --json` 的 brightness 和
   `/sys/class/backlight/ktz8866-backlight/brightness`，验证变化方向与延迟；再检查关闭开关后
   手动亮度保持、重新开启、光感服务重启重连、自动旋转共存及熄屏恢复。

短测不代表照度计精度、所有面板的主观观感或长时间休眠可靠性均已验收。
设备日志和验证记录放在忽略的 `out/` 目录。
