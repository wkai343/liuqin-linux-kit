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
