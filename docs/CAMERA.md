# 小米平板 6 Pro 相机

[项目首页](../README.md) ｜ [硬件说明](HARDWARE.md) ｜ [rootfs 软件配置](BUILD.md)

## 当前支持

后摄 S5KJN1 的 IFE680 硬件 ISP 支持 4080×3060、4080×2296 的 30 fps 模式和 3840×2160 的 60 fps 模式。配套的 IPE 宽屏配置支持后摄两档宽屏的 TF21 单曝光预览以及独立命令的三曝光 HDR401 拍照、HLG 录像。HDR 输出速率取决于每组三帧的采集、配准、融合与编码。原厂 8160×6120 模式经 RDI 输出约 10 fps 的打包 RAW10。
原软件路径支持普通用户采集、Kamoso 预览与拍照。前摄 AAC IMX596 已接入原厂
2592×1952 RAW10 模式，通过连续采集、关闭重开、前后摄切换及 GStreamer 采集；
软件 ISP 去除水平边界后的图像为 2584×1952。
后摄 GT9764 镜头驱动与 libcamera 对比度对焦已接通，已验证位置扫描、单次与
连续模式、取消、暗场失败状态，以及正常自动加载后的采集。
前摄已验证有光场景的彩色出图与默认画面方向；后摄已用近处文字和约 2 米远景确认扫描后
实际清晰度改善，单次与连续模式均可合焦。低照度、目标移动或缺少纹理时仍可能失败，
这些条件下的稳定性与速度需要继续调校。
驱动另开放原厂 2592×1472 裁剪和 5184×3904 全分辨率模式。RAW 时序已验证
30/60/10 fps，三种尺寸均通过软件 ISP 彩色输出；停止采集后系统睡眠再重开也已验证。
前摄已接通固定帧率控制和硬件编码，验证 1080p30 H.264/H.265、720p60 与
1080p60 H.264 文件及关闭重开。60 fps 路径仍有启动间隙和偶发丢帧，不能保证
所有负载下持续满帧。Kamoso 和官方 Snapshot 的普通录像均已接通麦克风和 Iris H.264 硬编。
后摄 Kamoso 短片已核对拍手同步和本机回放，长录漂移、采集中睡眠恢复仍待验证。高分辨率软件预览不保证 30 fps。

配套内核与 libcamera 还提供 [IFE680 硬件 ISP 路径](CAMERA-ISP.md)：前摄 RAW10
经硬件镜头阴影、白平衡、黑电平、去马赛克、原厂 D50 基础色彩矩阵和色彩转换直接输出 NV12。
libcamera 从硬件统计执行基础 AE/AWB，并载入本机 EEPROM 校准；支持
2592×1952 30 fps 与 2592×1472 60 fps。有效校准和 ABI 缺失时保留软件路径。
后摄三档合并像素模式均可经 IFE680 输出 NV12，使用独立原厂色彩参数和基础 AE/AWB，
并从亮度平面完成对比度对焦；单机 EEPROM 提供白平衡、阴影与 AF 扫描范围。
前后摄可选实时 IPE 桥接支持恒等处理和原厂 TF21 单层时域降噪。独立工具能调用 IPE HDR401
融合三曝光照片和录像。桌面预览使用单曝光，运动补偿和原厂场景调校的覆盖范围见下表。

## 前后摄待办

| 范围 | 待完成项 | 验收方法 |
| --- | --- | --- |
| 桌面拍照尺寸 | 在定制 Kamoso 增加标准和宽屏选择。前摄已有 2592×1952、2592×1472，后摄已有 4080×3060、4080×2296 和 3840×2160。官方 Snapshot 通过 PipeWire 选择默认尺寸。 | 切换前后摄、拍照并解码 JPEG，检查实际尺寸、取景范围和方向。后摄闪光选帧也要按所选模式验证。 |
| 后摄对焦 | 接入 S5KJN1 相位检测自动对焦（PDAF）。原厂 Qtech 模组在上述三个后摄合并像素模式配置了 VC1、数据类型 `0x30` 的相位流。先做 4080×3060，核对相位校准区并沿用 GT9764 单机行程，低置信度时沿用对比度对焦。Kamoso 已支持点按，libcamera 已支持手动锁焦。前摄 IMX596 为固定焦点。 | 用帧号配对图像和相位数据。让镜头分别偏近、偏远，核对相位给出的方向。在暗光、低纹理和移动目标下比较合焦时间、成功率和录像中来回扫焦次数。 |
| 最高像素单拍 | 优先评估后摄 8160×6120 的彩色照片，现有路径能采集 RAW10。前摄 5184×3904 的优先级较低，现有软件 ISP 彩图为 5176×3904。两档都超过单路 IFE FULL 的 4096×4096 输出上限。 | 先在光线充足时用同一位置拍细字，和默认尺寸比较同一裁剪区域的实际细节，再比较暗场噪声、快门到保存的时间及预览恢复。 |
| 两路画质与校准 | 前后摄已有单机白平衡、镜头阴影和按色温插值的色彩矩阵。待补原厂按照度、色温和增益选择参数的分支、混合光分区白平衡及人脸优先曝光。后摄阴影表仍以原厂固定光源分支为基准。 | 在日光、暖光、冷光、混合 LED 和暗光下，用灰卡、色卡和平场测中性灰、肤色、暗部噪声与边角色差。再用有亮背景的人脸验证曝光。 |
| 硬件时域降噪 | IPE TF21 已有单层历史帧，待补随增益变化的噪声模型、多尺度历史帧、运动补偿和曝光突变时的历史管理。 | 对同一输入比较降噪开关，分别测静止暗部噪声、移动边缘拖影和切换场景后的恢复，覆盖后摄实景运动和弱光摇机。 |
| HDR 拍照与曝光 | 两路独立工具已支持原尺寸三曝光、CPU 配准和 IPE HDR401 融合，输出 SDR JPEG。前摄已参考拍摄前的 AE 提亮中间调。待补最终色调标定、快速运动和视差下的去重影、EVA 硬件配准，以及通用 libcamera 曝光元数据与实际帧的逐帧对应。 | 用已知曝光序列核对帧号和传感器响应。对同一组三曝光比较人脸中间调、亮窗细节和移动边缘，分别保存融合与中曝光对照。 |
| HDR 录像 | 独立命令已能保存无声 10 位 HLG，使用软件 VP9 编码。前摄目前为 640×480，待扩展到 720p／1080p。后摄已有 4080×2296 和 3840×2160，4K60 为单曝光采集速率。三曝光成片帧率、录音同步，以及开录锁定增益、白平衡和后摄焦点后的场景适应，仍有提升空间。 | 分别核对文件的分辨率、位深、色彩标记、音轨和成片时间戳。后摄 4K 逐帧交替录三分钟，首秒最长间隔不超过 200 ms，末分钟每 10 秒至少保存 120 帧，再测跟拍和跨明暗场景。 |
| 桌面快门与对焦入口 | Kamoso、官方 Snapshot 均缺三曝光 HDR 拍照和录像入口。Kamoso 后摄已有关闭／打开／自动闪光。官方 Snapshot 的后摄仍缺快门闪光、点按对焦和手动锁焦界面，目前默认中央连续对焦。 | 分别核对应用实际下发的快门和对焦请求、镜头位置与输出文件。闪光照片检查全幅照明，HDR 照片检查三帧参与融合。 |
| 普通录像与稳定性 | 长录音画漂移、前摄 60 fps 启动与持续帧率、采集中睡眠恢复仍需实机覆盖。后摄普通录像已共用单调时钟和录像起点。 | 在长录片头、片尾分别拍手，逐帧核对偏差。回放分别检查显示丢帧和解码丢帧。睡眠恢复后再验证前后摄切换、开流、保存及设备释放。 |
| 可选 IPE 的系统升级 | 本机匹配的 IPE 模块可开机加载。内核或启动目标变化后，还需为新系统生成并验证匹配模块。 | 升级后核对模块与启动目标、前后摄 IPE 预览及原尺寸 HDR 拍照，并检查模块不匹配时的回退路径。 |
| 后置景深副摄 | 原厂模组配置另列出 OFILM SC202CS，当前 Linux 只枚举前摄和后置主摄。景深副摄是独立的低优先级适配项。 | 先对照原厂供电、CCI 和 CSI 布线，确认本机传感器 ID，再验收单路 RAW。开展景深融合前另验双路同步和几何标定。 |

模式与默认选择见[桌面拍照尺寸](LIBCAMERA.md#默认拍照尺寸)和[硬件 ISP 支持范围](CAMERA-ISP.md#支持范围)。原厂参数与时间戳的验证方法见[时域降噪与 HDR](CAMERA-ISP.md#时域降噪与-hdr-的硬件边界)、[运动配准与 HDR 录像](CAMERA-ICP.md#运动配准与-hdr-录像)和[桌面有声硬编](LIBCAMERA.md#桌面相机硬件录像)。桌面稳定性另有一项历史 GPU 问题，首次 GMU 超时仍需在复现时采集固件队列和中断状态。[GMU 追踪方法](GPU.md#首次-gmu-超时与后续故障)

## 后摄闪光灯

S5KJN1 通过媒体图的 ancillary link 关联 PM8350C 闪光灯，实体名为
`white:flash`。标准 LED 类提供手电筒和单次闪光，V4L2 子设备提供模式、
电流、超时、触发、停止和状态读回。打开 V4L2 闪光灯子设备期间由该接口
控制 LED，关闭后可再使用 sysfs。Fedora 桌面配方的 Kamoso 通过此接口触发后摄快门闪光。
[Linux 闪光灯接口](https://docs.kernel.org/leds/leds-class-flash.html)

在 Kamoso 选中「内置后摄像头」，点击顶部闪光灯按钮，按「关闭 → 打开 → 自动 → 关闭」切换。
「打开」在每张照片曝光时闪一次，连拍也是每张一次。「自动」在每张拍摄前测光，按需补光。
首次使用默认关闭。升级和重新打开 Kamoso 时会沿用上次选择的档位。
闪光灯按钮仅在后摄拍照时显示。需要闪光时请先关闭照片特效。

自动档同时检查曝光和实际画面。Kamoso 从 S5KJN1 读取曝光行数、模拟增益、水平消隐和像素时钟，
将实际曝光时间乘以模拟增益，得到折算到 1 倍增益的曝光时间。它也从添加特效前的硬件 ISP 预览帧按
32×24 网格采样亮度，取第 75 百分位（Y75，范围 0–255）。曝光折算值达到 240 ms 且 Y75 不超过 80
时才补光；曝光折算值降到 150 ms 或以下，或 Y75 达到 96 时退出补光。键盘等深色物体仍能拍清楚时，
即使增益偏高，自动档也能保留现场光。这些阈值由本项目设置。快门每隔 100 ms 采样一次，
连续三次判断一致才拍照。切换相机后先给自动曝光
至少 800 ms 稳定时间，闪光后至少 1.2 秒才开始下一次自动测光。测光失败时界面提示重试或手动选择档位。

使用本项目 Kamoso 包时，「打开」和「自动」两档共享同一把跨进程快门锁。自动档等待上次闪光后的 1.2 秒恢复期，再从新到达的预览帧测光。桌面镜像会将该包加入 DNF 版本锁。

后摄 4080×3060、30 fps 拍照使用 250 mA、200 ms 的定时脉冲。Kamoso 读取 PMIC 的点亮状态，
按 PipeWire 时间戳选取脉冲稳定段的完整 ISP 帧，保留画面方向并将该帧编码为 JPEG。
选帧探针在触发前装好。只有触发写入本身的前后时间用来界定窗口：帧时间戳距写入返回至少 130 ms，
距写入开始至多 190 ms。稍后读回点亮状态时可以保留一个候选帧，读回失败则不保存。
跳帧后的首个完整原始帧仍可使用；损坏、缺少像素或落在窗口外的帧会被拒绝。没有合格帧时提示重拍，
不会自动补闪。连拍沿用 Kamoso 的 2.5 秒间隔，计数以成功保存为准。

两个窗口可以同时预览后摄。预览端可以跳过显示不及的帧，后摄快门仍从方向和镜像处理后的完整帧中选片。
两个窗口同时要求闪光时，会依次使用同一颗闪光灯；等待中的窗口保持响应，切换相机可取消等待。
照片名包含毫秒和 Kamoso 进程号，两个窗口在同一秒向同一目录拍照时各自保存一张。

验收时固定平板，让后摄朝向能覆盖画面、照明稳定的近处目标。在暗场先用关闭档拍两张，
再用自动档单拍和连拍。下列检查读取保存的原图，逐行比较亮度，并覆盖 12×6 个区域。
在普通室内光下用自动档拍摄可辨认键帽的键盘，再对准大面积白色亮屏，分别用「自动」和「打开」拍一张。轮询
`/sys/class/leds/white:flash/flash_strobe` 可核实亮场下「自动」全程为 0，「打开」产生一次脉冲。
启动 Kamoso 时设置 `QT_LOGGING_RULES=kamoso.rearflash.debug=true` 可同时查看每次自动判定的曝光折算值和 Y75。

```sh
python3 tests/camera/kamoso-flash-rows.py --off off1.jpg off2.jpg --flash single.jpg burst*.jpg
```

只测选帧逻辑时，在装有 GStreamer 视频开发包及 JPEG 插件的主机上，用 Fedora 配方解包并应用补丁后，
令 `kamoso_src` 指向该源码目录。下面的测试用内存帧验证状态读回延迟、跳帧、无效帧和 JPEG 编码，不操作摄像头：

```sh
mkdir -p out/kamoso-test
c++ -std=c++20 -I"$kamoso_src/src/video" tests/camera/kamoso-flash-window.cpp \
    $(pkg-config --cflags --libs gstreamer-video-1.0) -pthread -o out/kamoso-test/flash-window
out/kamoso-test/flash-window </dev/null
```

要复测双窗口，请在同一桌面会话中打开两个 Kamoso 进程，均选择后摄并将闪光灯设为「打开」，
指定同一测试照片目录。下面的脚本通过 AT-SPI 按下两个快门，核对每对快门的两次独立脉冲，
并解码两张新保存的 4080×3060 JPEG。测试使用简体中文或英文界面，需要 Python 的
`pyatspi`、PyGObject 和 GdkPixbuf。可在启动测试窗口时设置 `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`。

```sh
python3 tests/camera/kamoso-flash-pair.py --pids "$pid_a" "$pid_b" --directory "$photo_dir"
```

若要在不替换系统版的情况下验证 Fedora 官方 Kamoso 能否保存独立插件返回的帧，见[官方版快门插件测试](../tests/camera/kamoso-gst/README.md)。该测试单独选择一个相机源，不触发闪光。

以下命令让后摄闪光一次，电流 250 mA、时长 120 ms。PMIC 硬件计时结束后
自动熄灯。最后写入 0 释放通道。

```sh
sudo sh -c 'cd /sys/class/leds/white:flash && echo 250000 > flash_brightness && echo 120000 > flash_timeout && echo 1 > flash_strobe && sleep 0.2 && echo 0 > flash_strobe'
```

手电筒亮度使用 0–255。以下两条命令分别开启和关闭常亮。

```sh
echo 64 | sudo tee /sys/class/leds/white:flash/brightness
echo 0 | sudo tee /sys/class/leds/white:flash/brightness
```

应用可按实体名查找子设备，在同一个文件描述符上设置 `V4L2_CID_FLASH_LED_MODE`、
`FLASH_TIMEOUT`、`FLASH_INTENSITY` 后触发 `FLASH_STROBE`。曝光与软件触发的
配合由拍摄应用负责。可在开流时固定曝光，比较触发前、脉冲内和自行熄灭后的图像亮度。

原厂 OS2.0.6.0 的 DTBO `entry.15` 把广角后摄连接到 `flash_0`、`torch_0`
和通道掩码 1。Linux 的 `led-sources` 从 1 编号。原厂
`vendor/etc/camera/camxoverridesettings.txt` 使用 130 mA 常亮、380 mA 拍照。
板级上限为常亮 130 mA、闪光 375 mA，拍照电流按 PMIC 的 12.5 mA 步进向下取整。
单次脉冲上限采用原厂驱动的默认 200 ms。可用以下命令对照原厂与编译后的 DTB。

```sh
python3 tests/camera/camera-oem.py --kernel /path/to/patched-linux --vendor /path/to/vendor \
  --dtb /path/to/sm8475-xiaomi-liuqin.dtb --stock-overlay /path/to/entry.15
```

## Kamoso

从应用列表打开 Kamoso。默认 Fedora 配方包含它及需要的 libcamera/GStreamer 组件：
`libcamera-gstreamer`、`libcamera-ipa`、`gstreamer1-plugins-good`、
`gstreamer1-plugins-good-qt6` 和 `gstreamer1-plugins-bad-free`。
缺少 Qt6 GStreamer 插件时，预览界面可能无法加载。

「配置 Kamoso…」中的摄像头选择框按当前预览的设备标识选中对应镜头。选择后摄，关闭并重新打开应用，
在配置页运行 `python3 tests/camera/kamoso-camera-selection.py` 可比对界面名称和实际 PipeWire 视频流。
切到前摄后可用同一命令复核。

前摄软件 ISP 已加入原厂黑电平、基础色彩矩阵、白平衡平滑，并修正重开时短快门与
高增益组合导致的噪声。硬件路径另使用单机校准和 CC131 色彩修正；可选 IPE TF21 单层时域降噪已接入，完整原厂场景调校仍未实现；
支持范围与录像复现命令见 [libcamera 适配](LIBCAMERA.md)。
安装 FFmpeg 命令行工具不会补齐对焦、调校或加速。

### 后摄预览卡顿

后摄使用 V4L2 的单调采集时间作为帧时间戳。帧经过 PipeWire、预览格式转换和 GL 上传后，
取景器会按管线时钟判断是否已错过显示期限。定制 Kamoso 从 `1.liuqin15` 起为板载后摄的
`qml6glsink` 设置 120 ms 的迟到容限，减少交付时间波动引起的连续丢帧。按时到达的帧照常显示。
切到前摄或其他相机时，取景器恢复创建时读到的容限。闪光选帧和录像继续使用原始采集时间戳。
后摄的 `qos=false` 关闭向上游发送的 QoS 事件，显示端仍可丢弃超期帧。[Kamoso 取景器设置](../build/kamoso/0010-rear-preview-lateness.patch) · [GStreamer 显示期限与 QoS 说明](https://gstreamer.freedesktop.org/documentation/base/gstbasesink.html?gi-language=c)

从终端打开 Kamoso，选择后摄普通预览，等画面稳定后观察一段时间，关闭窗口并检查日志。
下面三类记录分别表示显示端收到了帧、因迟到丢弃了帧和 Qt 更新了预览画面。

```sh
mkdir -p out/kamoso-preview
GST_DEBUG_NO_COLOR=1 GST_DEBUG='basesink:6,qtglwidget:7' \
  GST_DEBUG_FILE="$PWD/out/kamoso-preview/gst.log" kamoso
rg 'gst_base_sink_chain_unlocked:<videosink> got times start:|<videosink> buffer late, dropping|TRACE +qtglwidget .*:updatePaintNode: .* updatePaintNode$' \
  out/kamoso-preview/gst.log
```

用相邻输入的 PTS 检查 30 fps 的约 33.3 ms 周期，再用日志行首的时间计算相邻 Qt 更新的实际间隔。
若停顿期间输入仍连续、`buffer late, dropping` 连续出现，检查实际运行的 Kamoso 发行号和取景器的
`max-lateness`（单位为纳秒）。保持 `sync=true`，按后摄、前摄、后摄顺序切换，在当前配套
GStreamer 上应分别读到 120、5、120 ms。单窗口和双窗口都应分别核对输入帧数、迟到丢帧数与 Qt 更新间隔。

## Snapshot

默认 Fedora 桌面同时提供官方 Snapshot，菜单名称为「相机」。WirePlumber 通过
`pipewire-plugin-libcamera` 枚举镜头，桌面应用通过 `pipewire-gstreamer` 读取视频流。
`gst-inspect-1.0 --plugin pipewire` 应列出 `pipewiredeviceprovider` 和 `pipewiresrc`。
在定制 Kamoso 切到板载后摄，可点按画面对焦，在拍照模式还可选择闪光灯的关闭、打开和自动三档。前摄为固定焦点。
前后摄的首选照片尺寸和对焦控制见 [Snapshot 适配说明](LIBCAMERA.md#fedora-snapshot)。

## 诊断

关闭相机应用后运行：

```sh
cam -l
cam -c1 --capture=3
media-ctl -p
```

后摄路径为 CCI0 的 `i2c-bus@1/camera@10`，前摄路径为
`i2c-bus@0/camera@10`。启用前摄后编号可能改变，应先用 `cam -l` 确认相机 ID，
再指定相机采集；不能把 camera 1 或 `/dev/video0` 固定当成后摄。
相机运行期间避免同时枚举或启动另一应用；检查模块和内核日志时关注 `imx596`、`s5kjn1`、`gt9764`、`qcom_camss`、`camcc` 与 `cci`。

两颗传感器共用 `msm_csid0` 的输入端。前摄经过 `msm_csiphy2`，后摄经过
`msm_csiphy3`。即使没有采集流，独立运行的 `cam --list-controls` 也会在枚举时
改变活动链路。桌面进程重新配置硬件 ISP 时会先关闭竞争输入，再启用目标链路。
要复测，可先用 Snapshot 切到前摄，关闭窗口，运行 `cam -c 2 --list-controls`，
再打开 Snapshot。在 `wpctl status` 中核对前摄流为 active，并用 `media-ctl -p`
核对 CSID0 只有 PHY2 输入启用；切到后摄后应只有 PHY3 输入启用。

前后摄、镜头和 CCI 在最后一个客户端关闭后可进入运行时休眠，相关传感器与
镜头电源请求随之释放。WirePlumber 等相机枚举服务可能长期持有镜头子设备句柄，
使镜头保持供电；检查电源释放时同时用 `fuser /dev/v4l-subdev*` 确认持有者。
这与系统睡眠/恢复验收是不同的检查。
IMX596 的像素尺寸和曝光/增益延迟已有原厂属性；软件 ISP 专用画质调校仍不完整。
关闭客户端后已验证前后摄、镜头和 CCI 运行时休眠、相机电源请求归零，
以及系统睡眠约 15 秒后前后摄重新出帧。采集不中断的系统睡眠尚未承诺。
桌面枚举涉及 GPU 探测，应配套 [GPU 恢复死锁修复](GPU.md#gpu-故障转储与恢复)。
WirePlumber 连续十次停止/启动及故障后的图形回读已验证；此项不替代长期稳定性检查。
服务反复无法退出时应同时保存 GPU、SMMU 与服务线程栈。

## 前摄和对焦的适配边界

前摄沿用 OS2.0.6.0.VMYCNXM 的 AAC 模组公共初始化与 2×2 合并模式：
19.2 MHz MCLK5、CCI0 master 0、CSIPHY2、四条 RAW10 数据通道；
GPIO119 控制 2.9 V AVDD，GPIO74 控制 1.1 V DVDD，GPIO127 为低有效复位，
IO 电源为 L1C 的 1.8 V。驱动按原厂配置提供以下模式：

| 传感器输出 | 最高帧率 | 行长 / 默认帧长 | VT 像素时钟 | 原始阵列区域 |
| --- | --- | --- | --- | --- |
| 2592×1952 | 30 fps | 4644 / 6302 | 878.08 MHz | 5184×3904，2×2 合并 |
| 2592×1472 | 60 fps | 4644 / 6302 | 878.08 MHz | 从 y=480 裁出 5184×2944，再 2×2 合并 |
| 5184×3904 | 10 fps | 7288 / 9975 | 727.04 MHz | 全阵列，原厂硬件 remosaic |

裁剪模式默认 30 fps，`V4L2_CID_VBLANK=1679` 将帧长设为 3151，选择原厂
60 fps 时序；30/60 帧寄存器表只有帧长和初始曝光不同。其余两个模式不允许缩短到
超出原厂最高帧率。模式切换会更新只读 HBLANK、像素时钟、VBLANK 范围和曝光上限，
曝光始终保留 24 行裕量；缩短帧长时同一次 group hold 写入被收紧的曝光。
`TRY` 格式只影响试探状态，采集期间修改活动格式返回 `EBUSY`。
这是传感器 RAW 时序，CPU 软件 ISP 和应用的实际帧率须另测。

前摄安装方向通过设备树 `rotation = <180>` 表达，由 libcamera 配置传感器
水平与垂直翻转；翻转后的 Bayer 格式随控制值更新，不通过应用层倒转图片补偿。

后摄马达地址为 0x0c，L7C 必须保留原厂精确的 3100 mV RPMh 请求。
通用 PMIC5 PLDO 表会将它取整到 3104 mV，实机在启用该电源时重启；
板级适配使用 `liuqin_board_quirks()`，仅为 liuqin 的 `ldoc7` 提供 3100 mV 档位，
不修改其他 PMIC 电源。GT9764 驱动保留原厂
`02=02 / 06=40 / 07=6a` 初始化和逐次 100 µs 延迟，以及 3 ms 上电等待。
`lens-focus` 将镜头连接到 S5KJN1。镜头位置控制还需要用户态对焦算法，
因此必须配套 [libcamera 适配包](LIBCAMERA.md)。
它默认使用中央区域的连续对比度对焦，也支持单次触发和取消；未接入 PDAF。
定制 Kamoso 支持轻点后摄预览中的物体，让 GT9764 根据该区域的清晰度完成一次扫描。
前后摄共用一个 CSID/VFE 路径，当前验收应逐个打开、关闭和切换，不承诺同时采集。

可对解出的同版 vendor 分区检查原厂寄存器表：

```sh
python3 tests/camera/camera-oem.py --kernel /path/to/patched-kernel --vendor /path/to/vendor
```

真机检查应包括前后摄各自连续出帧、关闭后再次打开、交替切换；后摄应分别使用
近处文字和远处有纹理目标，检查焦点扫描后画面确实清晰，而不只检查 `AfState`。
测试图像与内核日志放在忽略的 `out/` 目录。

## 来源

相机支持由 reisa 贡献。S5KJN1 驱动改编自 Vladimir Zapolskiy（Linaro）的上游驱动，提交 [e38fd0933c75](https://github.com/torvalds/linux/commit/e38fd0933c759bfd51aafbee07f11e8d69da4366)，保留了原版权声明和模块作者。liuqin 的 19.2 MHz 模式数据根据设备厂商的相机模组配置重建，不分发任何厂商二进制文件。CSIPHY v2.1.3 通道参数参照高通 GPL-2.0 下游相机驱动，保留了其版权声明。贡献者说明板级适配部分借助了 AI 辅助开发。
