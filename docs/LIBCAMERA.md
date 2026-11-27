# Liuqin libcamera 适配

在固定的 Fedora `libcamera-0.7.2-5.fc45.src.rpm` 上增加前摄增益转换和后摄
对比度对焦及前后摄硬件 ISP。输入与 spec 摘要在 [source.json](../build/libcamera/source.json)。保留 Fedora 的补丁、
构建选项、拆包和 IPA 签名步骤。当前发行号见上述配方。

## 行为

- IMX596 使用 Sony 的 `1024 / (1024 - code)` 模拟增益换算。
  S5KJN1 驱动暴露的控制值已经是整数增益，使用线性转换。
- IMX596 的静态属性对应原厂 AAC 配置：800 nm 像素、两帧曝光延迟和两帧增益延迟。
  原来的通用增益延迟为一帧。simple 管线据此将统计帧与已生效的控制配对。
  软件 ISP 的帧长控制与曝光使用相同的两帧延迟，并先写 VBLANK；
  其他未使用的延迟不视为已经测得的传感器特性。
- 仅当 S5KJN1 有可用的 `V4L2_CID_FOCUS_ABSOLUTE` 镜头设备时公布对焦控制。
  前摄固定焦点，不公布这些控制。
- 默认连续对焦，支持 `AfModeAuto`、`AfTriggerStart/Cancel` 和 `AfState`。
  后摄硬件 ISP 有本机物距标定时还支持 `AfModeManual` 和 `LensPosition`。
  单位是屈光度（米的倒数）。设置一次即保持位置，直到应用更改位置或退出手动模式。
- 统计原始 Bayer 中央区域的绿色梯度，按亮度归一化。扫描期间保持曝光和增益，
  先粗扫再细扫，回到最佳位置后复核。统计统一为 8-bit 原始电平；中央区域平均
  绿色不高于黑电平加 8 时不采用梯度，避免暗场噪声冒充景物纹理。
  暗场或无足够对比度峰值时报告失败；低照度阈值仍需更多实景调校。
- 镜头写入有独立回执；写入失败停止扫描。使用 CAMSS 的单调时钟拍摄时间戳，
  排除写入之前已经采集或排队的帧，并预留 100 ms 移动与读出时间。
- 单次扫描中重复发送 Start 会被忽略；镜头写入失败后，连续模式不会自动重试。
  修复故障后可重新打开相机，或切换到单次模式重新触发。
- 统计沿用软件 ISP 每四帧一次的节奏。低帧率下扫描会较慢；它不使用原厂 PDAF、
  Chromatix 或 ISP 固件，不能视为原厂对焦速度和画质的复现。

配套内核与有效本机校准存在时，前摄优先使用 [IFE680 硬件 ISP](CAMERA-ISP.md)。
`0009` 在 IQ ABI 2 上为后摄选择 IFE680；硬件条件不满足时仍选择 CPU 软件 ISP。
配置位于 `/etc/libcamera/configuration.yaml`；用户自己的 libcamera 配置或
`LIBCAMERA_SOFTISP_MODE` 环境变量可覆盖它。GPU 去马赛克尚未纳入相机验收。

### 后摄手动对焦标定

将带细字的平面固定在已量出的物距，距离从后摄镜头量到文字平面。用
`tests/camera/camera-rear-manual-focus.cpp` 的 `scan <镜头子设备> <每档帧数> <DAC> ...`
在本机有效 AF 行程内粗扫，再反向细扫。取中心清晰度中位数的峰值。
至少测两个距离，按物距从远到近写入 `/var/lib/libcamera/liuqin/s5kjn1-focus.json`。
顶层字段为 `schema`（整数 1）、`model`（`s5kjn1`）、`eeprom_sha256`（本机完整后摄
EEPROM 的 SHA-256）和 `points`。每个点包含 `distance_cm` 和整数 `dac`。
更换模组后需重新测量。物距估测的误差也会进入屈光度换算。

重启 `liuqin-rear-calibration.service` 并重新打开相机。管线只公布实测区间，
区间内按屈光度分段线性插值。镜头驱动接受位置且经过 100 ms 稳定时间后，
对应采集帧的元数据才报告该位置。在近景与远景分别运行
`manual <镜头子设备> 60 <近点屈光度> <远点屈光度> <近点屈光度>`，
对照清晰度、驱动设定值和帧元数据，检查一次设定后能否保持。

安装匹配的 libcamera 开发包后，可在平板上编译标定工具：

```sh
mkdir -p out
c++ -std=c++20 -O2 tests/camera/camera-rear-manual-focus.cpp \
    $(pkg-config --cflags --libs libcamera) -pthread -o out/camera-rear-manual-focus
```

`cam -c "$REAR_ID" --list-controls` 可查看本机范围。应用进入手动模式后可只在
目标对焦距离变化时发送 `LensPosition`。例如要对准约半米处，发送 2.0 屈光度，
前提是该值位于公布的区间内。重新开启连续自动对焦可发送 `AfModeContinuous`。

### Fedora Snapshot

Fedora Plasma 使用发行版提供的 `org.gnome.Snapshot.desktop`，通过 D-Bus 启动相机。
前后摄预览使用 GTK 默认渲染器。镜像通过 GSettings 默认启用硬件录像编码。

#### 桌面相机硬件录像

定制 Kamoso 保存 Matroska（`.mkv`），视频使用 `v4l2h264enc`，声音使用 Vorbis。
录像编码分支按 30 fps 处理 PipeWire 的可变帧率输入，使编码器能按时间分配码率。
后摄预览和录像共用 GStreamer 单调系统时钟。板载后摄的 PipeWire 缓冲头含有
V4L2 的单调采集时间，Kamoso 将它换算为管线运行时间，避免首张预览帧的交付延迟叠加到后续画面。
按下录像快门时，Kamoso 记下一次
管线运行时间，摄像头和麦克风都按这个起点写入文件，两路首次采样的时间差得以保留。
当预览已经协商出 4080×3060 NV12 时，Kamoso 在源端保留整组格式，包括内存类型、
色彩信息和输入帧率，避免切入录像时让传感器反复配置同一模式。拍照前解除格式限制。

Fedora 官方 Snapshot 51 保存 H.264/MP4。默认镜像包含 `gstreamer1-plugin-openh264`，
供 Snapshot 检测 H.264 支持。系统默认打开 `org.gnome.Snapshot enable-hardware-encoding`，
Snapshot 随后优先选择 Iris。用户可以在 Snapshot 的录像首选项里切换硬件编码。
具体选择逻辑见 [Snapshot 的格式检测](https://github.com/GNOME/snapshot/blob/51.0/aperture/src/utils.rs)
和 [编码器优先级](https://github.com/GNOME/snapshot/blob/51.0/aperture/src/viewfinder.rs)。

验证时先用 `v4l2-ctl --list-devices` 找到 `qcom-iris-encoder`，再在录像期间用
`ls -l /proc/$(pidof -s kamoso)/fd` 或 `ls -l /proc/$(pidof -s snapshot)/fd`
核对进程实际打开的编码节点。停止录像后检查文件的编码、尺寸、级别和音轨。

```sh
ffprobe -v error -count_frames \
  -show_entries 'stream=codec_name,width,height,level,nb_read_frames,sample_rate:format=duration' \
  recording.mp4
```

同一命令也适用于 MKV。前摄 2592×1952、后摄 4080×3060 在 30 fps 下分别需要
至少 5.1 和 6.0 级。SM8450 Iris 驱动按实际编码尺寸和帧率更新 H.264 级别下限。
这两款应用的普通录像与[三曝光 HDR 录像](CAMERA-ICP.md)使用独立的录像管线。

验证后摄音画同步时，在镜头前做两次间隔数秒且能看清接触瞬间的拍手动作。逐帧圈出手掌接触的
时间范围，再与同一成片中相应的声音瞬态比较。用 `journalctl --user -u wireplumber`
按录像快门时间核对 `configuring streams` 的次数。在同一平板上用 mpv 完整回放，
分别读取 `frame-drop-count`（显示丢帧）和 `decoder-frame-drop-count`（解码丢帧）。

#### 默认拍照尺寸

IMX596 的硬件 NV12 输出有 2592×1952 和宽屏 2592×1472 两档。
PipeWire 1.6.11 的 libcamera 插件在枚举离散尺寸时先公布最接近 640×480 的一档，
随后列出全部尺寸。Snapshot 51 在每档帧率下优先选高度不超过 1080 的模式，
两个候选均超出时使用第一档。此时宽屏裁切发生在传感器选模式时。
见 [PipeWire 的格式枚举](https://github.com/PipeWire/pipewire/blob/1.6.11/spa/plugins/libcamera/libcamera-source.cpp)
和 [Snapshot 的尺寸选择](https://github.com/GNOME/snapshot/blob/51.0/aperture/src/utils.rs)。

`liuqin-pipewire-libcamera` 提供单独的 SPA 插件。WirePlumber 从同一插件加载摄像头
监视器、设备和视频源，共用一个 CameraManager。板级规则用 `api.libcamera.preferred-size` 为前摄指定 2592×1952、后摄指定 4080×3060。当该尺寸出现在当前像素格式的离散列表中，插件将它公布为首选。应用仍可显式请求前摄宽屏或后摄 4K 模式。
源码包固定上游归档摘要，可用 Fedora 1.6.11-1 源码 RPM 中的同名归档构建。
更新 PipeWire 摄像头后端时，应一并核对这个独立包的上游版本。
见 [RPM 配方及规则](../build/pipewire-libcamera/)。

安装该 RPM 后重启当前用户的 `wireplumber.service` 并重新打开 Snapshot。
用 `wpctl status -n` 找到前摄节点 ID。`pw-cli enum-params "$ID" EnumFormat` 的首项应为
2592×1952，列表中应同时包含 2592×1472。切到前摄后，在预览运行时用
`pw-cli enum-params "$ID" Format` 检查已协商尺寸，在 IMX596 子设备上用
`v4l2-ctl -d "$SUBDEV" --get-subdev-fmt pad=0` 检查实际 RAW 输出，最后解码快门保存的 JPEG 核对像素尺寸。

#### 在定制 Kamoso 点选后摄焦点

在后摄预览中轻点带纹理的物体，黄色方框标记所选位置。Kamoso 根据取景器的黑边和像素宽高比换算触点，
再通过 GStreamer 的导航事件让旋转、镜像和缩放按实际协商的管线反向换算。黑边不接受点选，
会扭曲几何形状的特效会暂停点选。前摄为固定焦距。

Kamoso 随包提供的辅助程序在同一个 PipeWire 请求中发送 `AfModeAuto`、`AfTriggerStart`
和两个归一化的 libcamera 控件 `liuqin::AfPointX`、`liuqin::AfPointY`。
坐标相对于当前 IFE 输出。后摄在触点周围约六分之一画宽、六分之一画高的区域测量亮度梯度，
等镜头稳定后比较各位置的清晰度。扫描期间点另一个物体会重新测量。单次扫描结束后保持焦点，
重新开流默认恢复中央连续自动对焦。黄色框只表示选区，光线不足或目标缺少纹理时可能无法合焦。

复测时让近处文字和远处纹理同时入镜，依次点选两者并观察各自的清晰度和 GT9764 的
`focus_absolute`。可在预览运行时用 `pw-dump` 核对后摄节点公布的两个浮点坐标控件。

#### 在 Snapshot 后摄预览期间控制焦点

Snapshot 51 的取景器没有把点按或长按转换为 libcamera 对焦请求。Snapshot 默认使用后摄中央区域的连续自动对焦。核验触摸时应记录
同一次预览的输入事件、镜头位置和触发时间，以区分触点选区与连续 AF 自行重扫。

后摄预览开启时，桌面用户可从终端运行同一 RPM 提供的命令：

```sh
liuqin-camera-focus lock
liuqin-camera-focus set 2.0
liuqin-camera-focus auto
```

`lock` 只切到手动模式，保留镜头当前位置，适合在预览清晰时使用。
`set` 在同一个 PipeWire 请求中发送手动模式和目标屈光度，2.0 对应约半米。
允许的区间来自当前模组已公布的校准表。`auto` 恢复连续自动对焦。
这些控制针对当前打开的后摄流，重新打开相机时默认连续对焦。
命令按 `PropInfo` 解析控制编号，并用 `node.name` 向视频源发送
`Props`。PipeWire 设备和视频源可共享相同的 `object.path`，按该路径发送可能命中设备。

## 前摄录像时序与基础调校

`0003` 在每次配置模式后刷新 IPA 的传感器信息，按实际行长和 VT 像素时钟
计算 `ExposureTime`。全分辨率模式的行时长与两个合并模式不同，不能沿用
相机枚举时的时序。IMX596 彩色管线配置后公布 `FrameDurationLimits`，
GStreamer 的帧率请求经它转换为 VBLANK；启动控制在第一帧前写入。
控制名称在枚举阶段也可发现，配置模式后更新实际范围；进入 IPA 前统一补齐
libcamera 控制类型表，兼容应用创建的无类型启动列表和隔离运行的 IPA。
曝光上限随帧长收紧，始终预留原厂的 24 行裕量。帧期间的变更通过延迟控制队列
写入，VBLANK 先于曝光，元数据的 `FrameDuration` 取自对应帧的延迟控制值。
帧长按行量化，名义 30/60 fps 与实际值有微小误差。
固定录像优先采用请求范围内的最短帧时长，暗光不会自行降帧率；此时需要提高增益。
这项控制针对软件 ISP 彩色管线，RAW 单流仍使用传感器 V4L2 控制。
启动时读取当前快门和增益，在新帧长的曝光上限内重新分配二者，优先使用快门时间。
在传感器增益范围允许时保留曝光量，避免重开时单独重置快门、却留下高增益而放大噪声。
启动时也先独立写入 VBLANK，让内核更新曝光范围，再写曝光和增益；
否则切换到更长帧时，快门可能被旧范围静默截断。
缩短帧长后若已到最高增益，仍可能欠曝；这不等于取消固定帧率限制。

停止软件 ISP 时先阻止输入缓冲重新入队，并取消仍在等待异步元数据的请求。
仅清空帧信息表会遗留已完成图像但未完成元数据的请求，录像 EOS 时可能触发
`queuedRequests_.empty()` 断言；回归需覆盖连续录像、正常 EOS 和关闭后重开。
IPA 在停止后也拒绝迟到的异步请求与统计，避免访问已清空的帧上下文。

`imx596.yaml` 与增益 helper 使用原厂 AAC 模组的四通道 RAW10 黑电平 64，
换算成 libcamera 16-bit 标度为 4096，避免纯色或缺少暗部的场景改变黑电平。
基础色彩校正采用原厂 IPE CC13 中较保守的一组五种色温矩阵，保留每个色温区间
的两端锚点与零偏移；白平衡更新采用 0.25 的平滑系数，首个有效统计直接收敛，
黑场不更新增益。平滑系数是本项目的视频策略，不是声称解出了原厂 AWB 算法。

色彩参数来自固定 ROM 的 `com.qti.tuned.liuqin_aac_imx596_front.bin`
（SHA256 `d7f013bef1c515e57f8c8d6d12488e0dc8018794792223d8ace2bf6e83b5bd08`），
CC13 IPE 模块 ID 60，所取路径为 DRC 1–100、自定义选择器 1–64、LED 0、
lux 220–350，对应 region 1576/1578/1580/1582/1584。原厂自定义选择器 1001
及多级场景切换未移植，此组矩阵是基础值，不能称为完整原厂调校。
布局和 RGB 行列次序可对照 Qualcomm 的
[CC13 参数定义](https://github.com/bjzhou/CamxChi/blob/9f17f1b568626b393e785b369c5dc94543774d7a/chi-cdk/api/chromatix/XSD/isp/cc_1_3_0.xsd)
及 [CC13 寄存器转换](https://github.com/bjzhou/CamxChi/blob/9f17f1b568626b393e785b369c5dc94543774d7a/camx/src/hwl/iqsetting/cc13setting.cpp)。
不分发原厂二进制；`tests/camera/camera-oem.py --libcamera /path/to/libcamera`
会追踪上述引用并逐项核对矩阵和黑电平。

前摄单机 EEPROM 白平衡/镜头阴影校准已接入硬件路径；软件路径尚未使用它。
`0005` 在可用的 IFE CC131 上执行上述 IPE 分支的 D50 矩阵，使用 Q10 系数；
`0006` 扩展为五组原厂色温矩阵，结合原厂相对白点轨迹与本机 EEPROM 进行基础 AWB。
只有内核矩阵控制匹配时才公布 `ColourCorrectionMatrix` 和 `ColourTemperature`。
关闭 AWB 可手动覆盖；恢复 AWB 后自动选择矩阵，重新启动从单机 D50 白点开始。
完整场景选择器、混合光源分区校正及标准色卡验收仍缺失，详见 [硬件 ISP](CAMERA-ISP.md)。
矩阵元数据沿用硬件路径的三帧延迟估算，尚不是逐帧精确控制。
可选 IPE 已接入原厂低增益区间的单层时域降噪；桌面预览使用单曝光。独立 HDR 录像命令直接读取 IFE，在三档曝光间做运动配准和硬件融合。完整噪声模型与人脸曝光仍待适配。普通白纸和实景只能检查明显偏色、闪动和成像是否正常，不能替代
标准色卡、多照明光源及暗场/平场标定。

`0008-liuqin-awb-neutral-selection.patch` 改进白点候选选择：仍使用原厂
相对白点轨迹与单机校准排除非中性色，对有效候选按中央区域及亮度加权，先选主要
光源，再成对合并红蓝增益。暗区域、高光和无效值不参与，候选不足时保持上一白点；
没有给所有场景添加固定的暖色偏置。算法属于本项目，不能称为原厂 AWB。
`tests/camera/camera-isp-awb.py --libcamera /path/to/source` 检查暖光/日光中性色、混合光
暗背景、高光、暗场和无效输入，可增加 `--sanitize` 检查内存与算术。
此补丁已进入配方；普通白纸对比不能替代混合光源和标准色卡验收。

后摄硬件路径用原厂 Qtech 模组的独立色彩矩阵和白点，曝光和增益按 S5KJN1
控制单位转换；对焦改从 NV12 亮度平面取样。`0010` 接入后摄单机白平衡、阴影和
AF 范围；内核通过独立能力控制声明后摄 LSC 几何。缺失或无效校准会退回模组参考值。
同一补丁为具备 4080×3060 profile 的 `qcom-ipe` converter 接通后摄实时帧；旧的
前摄专用桥接模块不会被误用于后摄。桥接模块须独立构建，可选择恒等处理或使用各模组原厂参数的 TF21 单层硬件时域降噪；
可选本机服务按内核和启动目标严格校验后加载。实时预览使用单曝光。独立拍照工具另支持三曝光 HDR。详见 [后摄硬件 ISP](CAMERA-ISP.md#后摄参数与对焦)。

`0011` 保持阴影网格的裁剪偏移为有符号数。网格第一行允许略早于有效图像，
若将负坐标与无符号裁剪量相加，会回绕为极大值，插值得到的增益随即被夹到极限，
形成顶部亮带或暗带。后摄及前摄 1952 行模式都需要这一修复。
`tests/camera/camera-lsc-coordinates.py --libcamera /path/to/source` 执行实际采样代码，
检查三种硬件几何与四种翻转下的边缘范围和对称性。实拍验收还需检查整条上缘，
不能仅以四角平均亮度评价镜头阴影校正。

## 构建与安装

默认 Fedora 相机配方自动构建这组 RPM。也可独立执行：

```sh
devbox run build -- --target libcamera
```

构建根独立于桌面镜像。输出 `rpms/` 含 `libcamera`、`libcamera-ipa`、
`libcamera-tools`、`libcamera-gstreamer`；`extra-rpms/` 保留同版 devel、qcam、
v4l2 和 Python 子包。镜像装配校验包名称、架构、版本、摘要，保留修订版 SRPM，
并锁定四个基础运行包及桌面配方选中的同版附加子包。修改配方或补丁会使构建缓存失效。

对已有系统更新时，在同一 DNF 事务中安装四个基础包，以及本机已安装的任何
上述附加子包。不要用 `rpm --nodeps`。完成真机采集和对焦验证后再设置版本锁：

```sh
sudo dnf versionlock add libcamera libcamera-ipa libcamera-tools libcamera-gstreamer
```

同样锁定已安装的附加子包。锁定会暂缓上游安全和功能更新；升级来源时需要重新
审查补丁和验证硬件。回到官方包时先解除这组锁，再对整组已安装包执行
`dnf distro-sync`，保留其他软件的版本锁。

## 检查

在应用补丁并启用 `-Dtest=true` 的 libcamera 构建目录运行：

```sh
meson test -C /path/to/build --print-errorlogs frame-duration contrast-af software-contrast
```

帧时长测试覆盖原厂 30/60/10 fps、模式上限、曝光裕量及异常请求边界；
对焦测试检查搜索、失败、旧帧、取消与重试；统计测试检查 RAW8、RAW10、RAW12 的
打包/非打包统计、中央窗口、暗场噪声和无有效统计的帧。可用
`-Db_sanitize=address,undefined` 检查内存访问与算术。
打包边界检查为 `devbox run -- python3 tests/camera/fedora-libcamera.py`。

这些测试不替代真实镜头的近远景清晰度、前后摄切换和应用兼容性检查。

以下 RGB 示例适用于软件 ISP；硬件 NV12 路径的格式见 [硬件 ISP](CAMERA-ISP.md)。
前摄无声录像可按以下方式复现；先关闭其他相机应用，并从 `cam -l` 获取前摄 ID：

```sh
gst-launch-1.0 -e libcamerasrc camera-name="$FRONT_ID" \
  ! video/x-raw,format=RGB,width=2584,height=1472,framerate=30/1 \
  ! queue max-size-buffers=3 max-size-time=0 max-size-bytes=0 \
  ! videoconvertscale n-threads=4 add-borders=true \
  ! video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1,colorimetry=bt709,pixel-aspect-ratio=1/1 \
  ! v4l2h264enc ! h264parse ! mp4mux ! filesink location=front-1080p30.mp4
```

按 Ctrl+C 正常发送 EOS，等待封装结束。H.265 可将编码器和解析器换为
`v4l2h265enc`、`h265parse`。两处帧率一起改为 `60/1` 可请求 60 fps；
传感器时序支持不代表软件 ISP、转换与编码全程无丢帧。需检查完整文件可解码、
帧时间戳单调、实测帧率、启动间隙及反复关闭重开，不能只看 MP4 的名义帧率。
此流程没有录音分支，不能证明音画同步或 Kamoso 录像界面的兼容性。

检查单次对焦时，可为 `cam --script` 创建以下 YAML；先用 `cam -l` 确认后摄 ID。
让中央区域朝向有光且静止的文字，等曝光稳定后再触发；以下示例留出 300 帧，
实际等待时间取决于帧率和光照：

```yaml
frames:
  - 0:
      AfMode: 1
  - 300:
      AfTrigger: 0
```

```sh
LIBCAMERA_LOG_LEVELS=IPASoft:DEBUG cam -c "$REAR_ID" \
  --capture=510 --metadata --script=/path/to/single-af.yaml
```

日志会记录镜头目标位置、原始对比度统计与拍摄时间戳；请求元数据报告
`AfMode` 和 `AfState`。检查扫描是否遍历位置、返回对比度峰值，并用实际图像
确认清晰度。无纹理或遮挡场景应报告失败，不能仅以马达移动认定对焦成功。

## 固定曝光的 HDR 输入采集

`tests/camera/camera-hdr-capture.cpp` 使用 libcamera 的 `Camera::start(controls)`，先让
AE/AWB 和后摄对焦运行，再停流，分别采集 2、8、32 ms 三档曝光。每档开流前关闭
AE/AWB，固定同一增益、白平衡和颜色矩阵，后摄停止连续对焦；两个传感器驱动均在
写入开流寄存器前执行控制初始化。IPE 会话历史随停流清除，避免混入上一档曝光。
libcamera 的固定手动流不会因空请求或仅对焦请求重复写入成像参数。

每档记录 16 帧，并保存首帧、末帧 NV12。`frames.jsonl` 分别记录采集代次、
传感器时间戳、转换器帧号及实际量化的曝光/增益。IPE 帧号是处理序号，不能当成
传感器帧号；输出时间戳经 V4L2 的微秒精度传递后必须与传感器时间戳相符。
`camera-hdr-validate.py` 拒绝混代、缺帧、重复/倒序、时间戳不符和段内参数变化，
并按实际曝光乘增益计算倍率。格式版本 2 在同一请求的 DMA 读取期间计算并记录图像
SHA-256。校验器复核每个首、末帧文件与原始记录相符，检查文件名内的采集代次和
转换器帧号，并拒绝硬链接复用。同一校验器可读取版本 1，其摘要只代表校验时的内容。

```sh
c++ -std=c++17 -O2 tests/camera/camera-hdr-capture.cpp -o /tmp/camera-hdr-capture \
  $(pkg-config --cflags --libs libcamera libcrypto) -pthread
/tmp/camera-hdr-capture front /path/to/new/front-bracket
python3 tests/camera/camera-hdr-validate.py /path/to/new/front-bracket
python3 tests/camera/camera-hdr-validate.py --self-test
```

后摄将 `front` 换为 `rear`。输出目录必须不存在，完整成功才产生 `COMPLETE`。
分档采集工具也可作为独立 HDR 拍照命令的输入。前摄 2592×1952 和后摄 4080×3060、4080×2296、3840×2160 的拍照流可直接在常驻 IPE 上进行三曝光硬件融合，
保存为同尺寸的 SDR JPEG。桌面通用曝光元数据仍采用估算延迟。独立录像命令在每次开录时检验实际曝光延迟，
逐组核对 IFE 帧号，并以中曝光的采集时间写入录像。详见[运动配准与 HDR 录像](CAMERA-ICP.md#运动配准与-hdr-录像)。命令和硬件对照测试见[三曝光硬件 HDR 拍照](CAMERA-ICP.md#三曝光硬件-hdr-拍照)。
