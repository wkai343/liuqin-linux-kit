# 视频编解码

[硬件支持](HARDWARE.md) ｜ [内核更新](KERNEL-UPDATES.md)

SM8475 使用 Iris2 视频单元，通过 `qcom-iris` 的 V4L2 stateful 接口访问。
当前配方支持 H.264、H.265 和 VP9 解码，并补齐 H.264、H.265 的安全 ARP 编码路径。
使用缺少安全非像素上下文的旧设备树时，只注册解码设备。
GPU 的 Mesa/OpenCL 支持与这个视频驱动是独立路径。

## 验证范围

| 路径 | 有限验证结果 | 限制 |
| --- | --- | --- |
| H.264 解码 | 以 Fedora 官方 GStreamer 解码 8-bit 1280×720、30 fps、60 帧素材，帧数及逐帧像素摘要与 FFmpeg 软件解码一致 | 未覆盖所有 profile、分辨率和损坏码流 |
| H.265 解码 | 同上，60 帧逐帧一致 | Main10、HDR 和更高分辨率待测 |
| VP9 解码 | 同上；修复前丢失可见帧，修复后 60 帧逐帧一致；提前停止后可重新解码 | 10-bit、动态分辨率及长片待测 |
| H.264 编码 | 完整内核通过 8-bit 720p 60 帧，以及 1080p、854×480 各 30 帧；默认 Baseline 和显式 High，独立解码帧数及 PSNR/SSIM 校验通过 | 其他 profile、4K 及长时间运行待测 |
| H.265 编码 | Main profile，同上三种尺寸与完整帧数校验通过 | Main10、HDR、4K 及长时间运行待测 |

以上是单台设备的短测，已覆盖完整内核临时启动及更新后的默认内核普通重启，
不能外推为性能、功耗或浏览器硬解验收。临时启动时用户确认画面和触摸正常；
两种启动方式均复测 H.264/H.265 编码和三种解码，并核对配套模块、音频拓扑及四路麦克风采集。
两种编码并发、提前停止后重开、自动休眠及编解码后的模块卸载/重载通过。
视频空闲时短暂进入 RTC 唤醒的 s2idle，恢复后重新编码及 VP9 解码通过；
系统睡眠期间仍保持打开的视频会话尚未验证。
默认更新经项目更新器回读并确认，保留旧内核及电脑上的启动备份，自动写引导恢复为关闭。
GStreamer 元件存在、V4L2 节点存在或驱动列出格式，都不能单独证明实际编解码成功。
Fedora 的 FFmpeg 构建可能缺少部分软件解码器；参考结果需由具备对应编解码器的主机生成。

## 驱动处理

- VP9 的隐藏帧及部分初始响应会返回没有有效图像的 CAPTURE 缓冲区。
  Iris2 固件有时没有设置 `NOSHOW` 标志。把这些缓冲区当作错误帧交给用户态，
  会丢弃同时间戳对应的可见帧。驱动内部回收并重新排队这些缓冲区，
  保留真实错误、EOS 和分辨率变化的 `LAST` 标志；停止 CAPTURE 时不重新送回固件。
- 真实错误缓冲区保留原时间戳，便于用户态匹配丢弃的帧。
- 驱动断电前排空中断线程。等待期间释放 `core->lock`，并序列化其他电源操作，
  避免卸载线程和 IRQ 线程互相等待；看门狗的 IRQ 禁用计数在恢复供电时配平。
- SM8450 的帧率、工作速率和宏块速率上限按原厂 Waipio 数据填写。
  上限属于能力描述，不能当作实测吞吐。编码端补充四条处理管线，避免频率计算使用零管线数。

### 编码重启与安全 ARP

Iris2 固件即使处理非安全编码会话，也要求 ARP 内部缓冲区位于安全非像素上下文。
原厂 `msm_vidc_get_buffer_region()` 将 ARP 固定归到 `MSM_VIDC_SECURE_NONPIXEL`，
不随会话的安全标志改变。把它作为普通 DMA 内存提交，会在实际送帧后触发整机挂起和重启。
只补能力值、参考帧缓冲区或切换电源管理模式不足以修复这个路径。

驱动按原厂配置使用独立的 SMMU SID `0x2184`、掩码 `0x400`、VMID 11
（`CP_NON_PIXEL`），ARP 的 IOVA 限定在 `0x01000000` 到 `0x257fffff`。
每个会话独立分配 ARP，普通输入帧和输出码流仍使用非安全内存。
页表保留 HLOS 读写权限，并授予 VMID 11 只读权限；ARP 数据页交给 VMID 11 读写。
这不是受保护视频播放功能，也不要求应用创建安全编码会话。

安全授权通过可睡眠的 SCM 调用完成。页表在原子上下文释放时，将权限回收交给工作队列；
部分数据段授权失败时撤销已完成的授权。若撤销权限失败，保留对应内存到重启，
并记录错误，不能把仍受保护的页交回系统分配器。没有安全上下文时保留仅解码模式。

Iris2 的编码参考帧按图像分别分配，数量包含当前重建帧，并采用原厂 128 字节宽度对齐。
编码裁剪只发往码流端口，COMV 数量属性只用于解码；会话明确设置为非安全会话。
这些配置与安全 ARP 配合使用，不能把其他平台关闭 ARP 的方法直接用于本机固件。

### 编码结束时的最后一帧

Iris2 编码固件的 `DRAIN` 确认可能返回零标志，不包含 `HFI_FW_FLAGS_SUCCESS`。
原厂驱动接受这种回复。若把它判为会话错误，用户态重新排队的 CAPTURE 缓冲区会
立即以空错误帧返回；GStreamer 可能据此结束读取，使硬件已经完成的最后一帧丢失。
修复仅允许 SM8450 编码会话的零标志确认，继续处理后续输出和 `LAST`；
显式错误标志、解码会话和其他平台仍按严格规则处理。
验收同时记录送入编码器和读出的帧数，并用独立解码器确认完整帧数，不能只检查 EOS。

## 可复现短测

先用 `v4l2-ctl --list-devices` 确认 `qcom-iris-decoder`，再准备两秒、60 帧的
8-bit 720p H.264、H.265 与 VP9/IVF 文件。VP9 素材应包含隐藏帧或 super-frame。
以下命令强制使用硬件解码元件，不能替换成可能回退软件解码的 `decodebin`：

```sh
gst-launch-1.0 -v filesrc location=sample.h264 ! h264parse ! v4l2h264dec ! fakesink sync=false
gst-launch-1.0 -v filesrc location=sample.h265 ! h265parse ! v4l2h265dec ! fakesink sync=false
gst-launch-1.0 -v filesrc location=sample.ivf ! ivfparse ! vp9parse ! v4l2vp9dec ! fakesink sync=false
```

这些命令只检查管线是否运行到 EOS。正确性验收还需用 `appsink` 收集输出，
按每平面的 stride 和 offset 去除行填充，转换为连续 I420 后与软件参考逐帧比较，
同时要求输出帧数等于输入可见帧数。仅看 EOS 或第一个画面会漏掉 VP9 丢帧。

编码检查先确认 `qcom-iris-encoder`，可用以下两秒短测生成码流：

```sh
gst-launch-1.0 -e videotestsrc num-buffers=60 ! 'video/x-raw,format=NV12,width=1280,height=720,framerate=30/1,colorimetry=bt601' ! v4l2h264enc ! 'video/x-h264,profile=high,level=(string)3.1' ! h264parse ! 'video/x-h264,stream-format=byte-stream' ! filesink location=encoded.h264
gst-launch-1.0 -e videotestsrc num-buffers=60 ! 'video/x-raw,format=NV12,width=1280,height=720,framerate=30/1,colorimetry=bt601' ! v4l2h265enc ! 'video/x-h265,profile=main,level=(string)3.1' ! h265parse ! 'video/x-h265,stream-format=byte-stream' ! filesink location=encoded.h265
```

编码验收需将码流交给独立软件解码器，核对尺寸、帧数和画面，并与同一输入做 PSNR/SSIM 比较。
原始 I420/NV12 文件没有色彩元数据，`rawvideoparse` 的 `colorimetry` 必须与素材一致；
错误的默认矩阵会让 `videoconvert` 改变色彩，使画质对照失真。
宽度未按四字节对齐时，还应明确指定 `rawvideoparse` 的 `plane-strides` 和
`plane-offsets`，与原始文件的紧密排列一致，避免解析器把行填充误算为有效输入。
同时运行两个编码会话可检查 ARP 隔离；提前停止后重开用于检查资源释放。

再检查提前停止、重新打开、自动休眠及解码后的模块卸载/重载；
保存测试区间内的内核日志，确认无固件超时、IRQ 警告或 SMMU 故障。
旧驱动存在卸载死锁，不能用反复卸载旧模块作为通用测试方法。
素材、输出、精确版本及实机日志放在忽略的 `out/` 中。

## FFmpeg 短测

Fedora `ffmpeg-free` 的编译能力列表不等于设备实际可用的硬件能力。
`9.0.2-3.fc45` 未启用 H.264/H.265 的 V4L2 解码 wrapper，且线性 NV12 编码输入
存在对齐高度越界读取问题。修复源码、构建方法和更新规则见
[Fedora FFmpeg 配方](../build/ffmpeg/README.md)。
默认 Fedora 镜像会构建并安装这组修复包，同时锁定 FFmpeg 与七个配套运行库的版本；
升级维护时需要整组审查、验证并更新版本锁。

修复配方的有限验收包括：H.264/H.265/VP9 各 60 帧与独立软件参考的 I420 摘要一致，
H.264/H.265 的 MP4 输入同样逐帧一致；两种编码的完整帧数与画质检查通过。
mpv 在禁用软件回退、使用空视频输出的条件下，三种格式均报告 V4L2 硬解并正常到达 EOF。
显示输出及浏览器的单独验证范围见下方应用表；所有 profile、长片和网络流仍未覆盖。

配方同时修复两条用户态路径：解码时读取驱动的只读可见区域，避免把 H.265 的
736 行存储高度当成 720 行显示高度；编码后及时归还硬件输出缓冲区，避免异步封装器
保留压缩包引用时触发提前结束、少尾帧。编码输出因此增加一次压缩码流复制，
没有改变解码图像帧的引用路径；性能和功耗影响尚未单独测量。

解码必须明确选择硬件 wrapper，并要求帧数及 I420 摘要与独立软件参考一致：

```sh
ffmpeg -c:v h264_v4l2m2m -i sample.h264 -an -pix_fmt yuv420p -fps_mode passthrough -f framehash -hash sha256 h264.framehash
ffmpeg -c:v hevc_v4l2m2m -i sample.h265 -an -pix_fmt yuv420p -fps_mode passthrough -f framehash -hash sha256 hevc.framehash
ffmpeg -c:v vp9_v4l2m2m -i sample.ivf -an -pix_fmt yuv420p -fps_mode passthrough -f framehash -hash sha256 vp9.framehash
```

编码用紧密排列的 8-bit I420 输入，明确转换为硬件支持的 NV12；例如 30 帧短测：

```sh
ffmpeg -f rawvideo -pixel_format yuv420p -video_size 1280x720 -framerate 30 -i source.i420 \
  -frames:v 30 -an -pix_fmt nv12 -c:v h264_v4l2m2m -b:v 4M -g 30 -f h264 encoded.h264
ffmpeg -f rawvideo -pixel_format yuv420p -video_size 1280x720 -framerate 30 -i source.i420 \
  -frames:v 30 -an -pix_fmt nv12 -c:v hevc_v4l2m2m -b:v 4M -g 30 -f hevc encoded.h265
```

独立软件解码输出码流，检查完整 30 帧、可见尺寸及 PSNR/SSIM。
只检查进程退出码或生成文件不足以排除 UV 偏移错误、裁剪错误及最后一帧丢失。
另选 1920×1080 和 854×480 输入可覆盖底部填充及行间距对齐。

mpv 可以通过 `--hwdec=v4l2m2m-copy --hwdec-software-fallback=no` 请求这条路径，
用 `--msg-level=vd=debug,ffmpeg=debug` 核对实际解码器。`--vo=null --untimed` 仅用于
短测解码至 EOF，不证明屏幕显示、零拷贝、长片稳定性或浏览器硬解。

### DRM PRIME 输出与时间戳

修复配方可协商 `AV_PIX_FMT_DRM_PRIME`，描述线性 NV12/NV21 的 DMA-BUF。
支持这个接口的应用可以将解码缓冲区直接导入 GPU，避免 CPU 图像回读和上传；
这里的零拷贝指解码器到渲染器的图像传递，仍会执行色彩转换、缩放和桌面合成。
普通 FFmpeg 图像输出和 `v4l2m2m-copy` 路径继续可用。
除无外部设备的内部配置外，也接受应用提供的 DRM device context，供 FFmpeg 命令行
`-hwaccel drm -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format drm_prime` 选择。
诊断时可接 `-vf hwdownload,format=nv12` 做像素校验；该过滤器会主动回读，不能用于衡量
零拷贝播放性能。

V4L2 对负数 `tv_usec` 的无符号转换可能把负 PTS 变成约 4294 秒，造成显示等待。
配方使用正数 cookie 保留原始 PTS，包括 B 帧重排、重复和未知时间戳。
H.264/H.265 的有效 MP4 短片已同时检查 60 帧像素摘要及 PTS，与独立软件解码一致。
素材应在编码时直接写入 MP4，保留正确的 PTS/DTS；给含 B 帧的裸码流简单补顺序时间戳
再封装，可能产生错误显示顺序，不能作为播放器时序参考。

解码 flush 会停止并分离旧硬件会话，下一次取帧时重新打开，清除旧的 EOS 和队列。
渲染器保留的末帧继续引用旧缓冲区，释放时不会混入新会话。
已用三种格式检查播完后两次跳回开头，均重新输出 DRM PRIME 帧并正常到达 EOF。
可用 [播放器回归](../tests/ffmpeg-v4l2-playback.py) 重复此检查；它不替代长片任意位置跳转测试。

### 应用播放与自动硬解

以下范围为 Fedora 本机安装的应用、Wayland 桌面、8-bit 720p/30 fps 两秒素材。
实际窗口播放与解码器日志一起检查，不能用“能打开文件”替代硬解判定。

| 应用路径 | H.264 / H.265 | VP9 | 图像传递与设置 |
| --- | --- | --- | --- |
| mpv 0.41、Haruna 1.8 | 自动 V4L2 硬解 | 自动 V4L2 硬解 | `hwdec=v4l2m2m,auto`，DRM PRIME；Haruna 的 libmpv/OpenGL 路径也可导入 |
| Fedora Firefox 159/160，经兼容检查的本地启动入口 | 自动 V4L2 硬解 | 自动 V4L2 硬解 | 私有挂载视图使用修复版系统 FFmpeg、DRM PRIME；官方 RPM 文件和沙箱设置保留 |
| Fedora Chromium 156，临时开启原生 V4L2 | H.264 播放与重播通过；H.265 路径未实现 | 硬解出帧，但结束和重播存在异常 | 默认未启用；不加入日常启动配置 |
| Qt 6.11 FFmpeg 后端 | 自动选择 V4L2 wrapper | 当前选择软件解码 | 普通 NV12 图像上传，不能算作零拷贝 |
| Qt 6.11 GStreamer 后端 | 自动 V4L2 硬解 | 自动 V4L2 硬解 | 默认仍可能上传图像；下述启动入口允许 DMA-BUF 协商 |

默认镜像为 mpv 和 Haruna 安装系统级偏好，不覆盖已有配置或用户显式设置。
mpv 的 `auto` 安全列表未包含 `v4l2m2m`，因此需要先列出它，再保留普通自动回退。
Haruna 对应设置为 `/etc/xdg/haruna/haruna.conf` 中的
`[Playback] HWDecoding=v4l2m2m,auto`。两者都是配置调整，不重建播放器。

Firefox 使用独立的[系统 FFmpeg 启动适配](../build/firefox/README.md)。默认构建安装
`/usr/local/bin/firefox`，兼容名单内的版本才启用私有媒体库映射；官方浏览器文件不改写，
但适配进程中的媒体库会变化。未验证的浏览器或 FFmpeg 更新自动使用原版媒体库，
此时 VP9 恢复软件解码，不阻止 Firefox 更新。已有浏览器须完全退出后重新启动。
短测覆盖播放、重播、Opus/AAC 音轨、AV1 软件解码、WebAudio FFT、主动禁用硬解和
未知版本回退；不包含扬声器主观听音验收。关闭方法与兼容名单维护见上述说明。

Qt 播放器可单独通过以下入口启动：

```sh
liuqin-qt-video PROGRAM [ARGUMENT ...]
```

入口设置 `QT_MEDIA_BACKEND=gstreamer`、`QSG_RHI_BACKEND=opengl` 和
`QT_GSTREAMER_DISABLE_PIXEL_ASPECT_RATIO=1`。最后一项移除 Qt 默认的普通内存、
固定像素比例 caps 过滤器，否则 DMA-BUF 会被排除；视频仍保留自身的像素比例元数据。
这三个设置仅作用于该进程及子进程，不作为桌面全局环境，也不替换 QRCA 的专用摄像头设置。
Qt 的这些环境接口属于私有配置点，升级 Qt 后应复测。

可用以下命令核对真实显示路径；不要加入 `--vo=null` 或 `--untimed`：

```sh
mpv --hwdec=v4l2m2m --hwdec-software-fallback=no --msg-level=vd=debug,vo=debug sample.mp4
QT_LOGGING_RULES='qt.multimedia.*=true' GST_DEBUG=GST_ELEMENT_FACTORY:4 liuqin-qt-video PROGRAM
MOZ_LOG='PlatformDecoderModule:5,FFmpegVideo:5' firefox
```

mpv/Haruna 日志应同时出现 `Using hardware decoding (v4l2m2m)` 和 `drm_prime`；
Firefox 应出现相应 `V4L2 codec`、DRM PRIME 协商和实际帧输出；
Qt/GStreamer 应确认 `v4l2h264dec`、`v4l2h265dec` 或 `v4l2vp9dec` 及实际 sink 的
`video/x-raw(memory:DMABuf)`，而非只看探测到的 caps。需要进一步核对时，跟踪
`VIDIOC_EXPBUF` 与 `DRM_IOCTL_PRIME_FD_TO_HANDLE`，同时检查画面、裁剪、播放时间和 EOS。
避免对性能测量启用详细跟踪，它会增加开销。
浏览器短测仍可记录到少量显示丢帧，应另查 `video.getVideoPlaybackQuality()`；
解码输出逐帧正确不代表每帧都按时呈现。这些短片结果不构成长片流畅度或功耗验收。

### Chromium 的原生 V4L2 路径

Fedora ARM64 Chromium `156.0.8078.79-1.fc45` 的默认短测中，VP9 使用
`VpxVideoDecoder` 软件解码，H.264 初始化失败，H.265 被判为不支持。
图形合成和 WebGL 启用不代表视频硬解启用。

该版本的 Linux 视频总开关名为 `AcceleratedVideoDecoder`；只启用
`AcceleratedVideoDecodeLinuxGL` 不足以打开它。独立临时 profile 中组合使用
`--ozone-platform=wayland --use-gl=angle --use-angle=gl` 和
`--enable-features=AcceleratedVideoDecoder,AcceleratedVideoDecodeLinuxGL,AcceleratedVideoDecodeLinuxZeroCopyGL,PreferV4L2VideoAcceleration`
后，H.264 和 VP9 实际选择 `V4L2VideoDecoder`，无需 VA-API 或 Firefox 的媒体库映射。
以上仅用于复现，不作为默认配置；测试不修改已有 profile 或官方 RPM。

H.264 High 的 720p 两秒素材播放和重播通过。VP9 可输出 60 帧，但存在正常结束事件
缺失或重播不收尾的情况；使用支持 HTTP Range 的源和不含音轨的素材仍出现结束异常。
日志中的 `POLLERR` 是进一步定位线索，尚不能单独认定根因。
H.265 则在初始化后明确返回不支持：该版本的 stateful 解码器对 HEVC 分支保留
`NOTIMPLEMENTED()`。因此 Chromium 尚未达到与已验证 Firefox 路径相同的可用范围。
验证须同时检查 CDP Media 或 `chrome://media-internals` 的解码器、实际帧输出、画面、
结束和重播；`chrome://gpu` 的单个状态或能力列表不能代替这些检查。

实现来源：[156 的视频开关](https://github.com/chromium/chromium/blob/156.0.8078.79/media/base/media_switches.cc)、
[stateful 解码器](https://github.com/chromium/chromium/blob/156.0.8078.79/media/gpu/v4l2/v4l2_stateful_video_decoder.cc)。

### OBS 的编码接入

[独立 V4L2 编码器插件](../build/obs-v4l2/README.md)在官方 Fedora OBS 中增加
H.264 / H.265 的 Iris 硬编选项，不替换 OBS、libobs 或官方插件，也不依赖 VA-API。
使用本项目修复版系统 FFmpeg，支持 8-bit NV12/SDR、VBR 目标码率和关键帧间隔。
入口是「输出 → 高级 → 录像 → 标准」，编码器名称含 `V4L2 / Iris`。

官方 OBS `32.2.2-3.fc45` 的自带 FFmpeg 插件只注册了本机可用的软件视频编码器。
其「自定义输出（FFmpeg）」虽能调用硬件，却在 H.264/MKV 写头时失败，TS 短测也有
60 帧输入仅保留 58 帧的问题。该旧路径仍存在，不能与独立插件的标准输出路径混用。

Iris/FFmpeg 会先返回一个仅含参数集的包。独立插件缓存 H.264 SPS/PPS 或 H.265
VPS/SPS/PPS，供标准封装器初始化；参数集包不计为视频帧。正常停止采用 OBS 标准
输出按时间戳收尾的机制，等待停止点之前的图像全部输出，再关闭和排空硬件会话。
插件还处理行距、可见高度、发送端 EAGAIN 重试、分数帧率时间戳和关键帧请求。

| 验证范围 | 检查内容 |
| --- | --- |
| H.264 / H.265 × MKV / MP4 / TS | 720p30，每段目标 60 帧；独立软件解码核对完整帧数、尺寸和连续时间戳 |
| 停止后重新录像 | 复用同一编码器对象，检查重新初始化和有效容器头 |
| 行距和高度 | 852×480、1920×1080；按 OBS 实际输出尺寸核对，不能把对齐填充当画面 |
| 分数帧率和帧率除数 | 30000/1001 及隔帧编码，检查时间戳与播放时长 |
| 包适配回归 | 参数集单独/合并输出、重复头信息和短缓冲区，含 ASan/UBSan 检查 |

不提供 CBR/CQP、动态码率、B 帧、HDR 或 GPU 纹理直传；直播服务、长时间录制和
实际音频尚未验收。官方 OBS 更新不受插件锁定，但 OBS/FFmpeg ABI 变化需要重建验证。
默认 rootfs 配方安装官方 OBS 和独立插件 RPM，也可用
`devbox run build -- --target obs-v4l2` 单独构建；安装、撤销和可复现短测见
上述插件说明。日志和录像保存在忽略的 `out/` 目录。

### 尚未补齐的应用路径

GStreamer 的 V4L2 元件和 FFmpeg 的 V4L2 wrapper 是两套独立用户态实现。
GStreamer `decodebin` 和 FFmpeg 消费者的自动选择策略不同，需分别验证。

当前 Fedora Mesa 的本机运行检查没有发现可用的 `msm_drv_video.so` VA-API 后端，
Turnip 也没有公布 Vulkan Video 扩展。安装 `libva` 或看到 `ffmpeg -hwaccels` 中
列出 VAAPI/Vulkan，不能补出这些后端。

[Firefox 的 V4L2 实现](https://searchfox.org/firefox-main/source/dom/media/platforms/ffmpeg/FFmpegVideoDecoder.cpp)
要求 `AV_PIX_FMT_DRM_PRIME`。系统 FFmpeg 已满足这条接口，不依赖 VA-API；但原版
Firefox 159/160 优先选择内置 FFVPX 处理 VP9，并在该模块内回退软件解码，没有继续试
系统 FFmpeg 的 V4L2 wrapper。`media.prefer-non-ffvpx` 会把另一软件模块提前，不能解决
此问题。上面的启动适配是有限兼容的过渡方法，不是 Mozilla 的正式配置接口。
从适配环境外直接调用 `/usr/bin/firefox`，仍使用原版 VP9 选择策略；长期应修复
Firefox 对系统硬件解码模块的选择逻辑。

QtWebEngine、在线视频网站的 MSE/DRM 路径、10-bit/HDR、动态分辨率、
长片拖动跳转及长时间播放仍需单独验证。Qt FFmpeg 后端还会排除 V4L2 编码器，
命令行硬编可用不等于 Qt 录制接口已接通。

## 来源

- [Vishnu Reddy 的 IRQ 断电等待修复](https://lkml.iu.edu/hypermail/linux/kernel/2611.1/10620.html)
  和[看门狗 IRQ 计数修复](https://lkml.iu.edu/hypermail/linux/kernel/2611.1/10610.html)：本地适配的待合入补丁。
- [Xilin Wu 的 Iris VA 修复系列](https://lore.landau.one/lkml/20261029-iris-va-fixes-v1-0-44a2783f782b@radxa.com/T/)：
  时间戳及隐藏帧处理的基础；本地补充未标记的空缓冲区和停止期间处理。
- [原厂 Waipio 能力表](https://github.com/LineageOS/android_kernel_qcom_sm8450-modules/blob/lineage-20/qcom/opensource/video-driver/driver/platform/waipio/src/msm_vidc_waipio.c)。
- [另一台 SM8475 设备的编码重启报告](https://github.com/royka1/diting-ubuntu/blob/main/ubuntu-diting/docs/KNOWN_ISSUES.md#video-encoder-resets-the-phone-sm8475-iris-r35)：
  提供相似现象的独立记录，不足以证明两者根因相同。

- [原厂缓冲区所属内存域](https://github.com/LineageOS/android_kernel_qcom_sm8450-modules/blob/lineage-20/qcom/opensource/video-driver/driver/vidc/src/msm_vidc_driver.c)
  和[原厂 Iris2 缓冲区尺寸与数量](https://github.com/LineageOS/android_kernel_qcom_sm8450-modules/blob/lineage-20/qcom/opensource/video-driver/driver/variant/iris2/inc/hfi_buffer_iris2.h)。
- [原厂 SMMU 安全页表管理](https://github.com/LineageOS/android_kernel_qcom_sm8450/blob/lineage-20/drivers/iommu/arm/arm-smmu/arm-smmu.c)：
  页表同时保留 HLOS 权限并授予目标 VMID 读取权限；SID、VMID 和地址范围另与本机原厂 DTB 对照。
- [原厂 HFI 回复处理](https://github.com/LineageOS/android_kernel_qcom_sm8450-modules/blob/lineage-20/qcom/opensource/video-driver/driver/vidc/src/venus_hfi_response.c)：
  `handle_session_drain()` 允许不带成功标志的确认。
