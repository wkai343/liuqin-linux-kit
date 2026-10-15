# 小米平板 6 Pro 相机

[项目首页](../README.md) ｜ [硬件说明](HARDWARE.md) ｜ [rootfs 软件配置](BUILD.md)

## 当前支持

后摄 S5KJN1 已有 CAMSS/libcamera 适配，使用 CPU 软件 ISP，尚无自动对焦或校准调校。
Fedora 已验证普通用户采集、Kamoso 预览与拍照，以及 QRCA 实时扫码；录像未验证。
前摄没有枚举到，尚未启用。默认高分辨率预览偏慢，不代表全部场景都有约 30 fps。

2026-10-14 的基础采集测试使用 Fedora libcamera 0.7.1 和
`6.17.0-rc1-liuqin-fedora-gf1039a48a289`：1280×720 ABGR8888 连续 30 帧约 30 fps，
关闭后重新采集成功。Kamoso 26.08.1 默认协商 3840×2400，用户确认预览和照片保存可用，
但预览帧率偏低。默认应用没有预览分辨率选项，需要应用适配才能降低取流分辨率。
这些结果不自动作为后续新内核、新镜像或软件版本的验收。

## Kamoso

从应用列表打开 Kamoso。默认 Fedora 配方包含它及需要的 libcamera/GStreamer 组件：
`libcamera-gstreamer`、`libcamera-ipa`、`gstreamer1-plugins-good`、
`gstreamer1-plugins-good-qt6` 和 `gstreamer1-plugins-bad-free`。
缺少 Qt6 GStreamer 插件时，预览界面可能无法加载。

软件 ISP 尚无专用调校，画质和性能不能与原厂相机相比。安装 FFmpeg 命令行工具不会补齐对焦、调校或加速。

## QRCA

默认菜单入口为 QRCA 选择 GStreamer 后端，通过 libcamera 插件发现后摄。
在已测试的 Qt 6.11.2 / QRCA 26.08.1 组合中，彩色实时预览可见但扫码失败；
图片文件识别正常，应用专用灰度转换后实时扫码已由用户确认通过。

命令行对应设置为：

```sh
QT_MEDIA_BACKEND=gstreamer \
QT_GSTREAMER_OVERRIDE_VIDEO_CONVERSION_ELEMENT='videoconvert ! capsfilter caps=video/x-raw,format=GRAY8' \
qrca
```

此兼容设置只用于 QRCA，预览为黑白，不改变其他应用。

## 诊断

关闭相机应用后运行：

```sh
cam -l
cam -c1 --capture=3
media-ctl -p
```

预期枚举到一个 Internal back camera。视频节点编号会变化，不能固定使用 `/dev/video0`。
相机运行期间避免同时枚举或启动另一应用；检查模块和内核日志时关注 `s5kjn1`、`qcom_camss`、`camcc` 与 `cci`。
当前软件 ISP 的静态属性与调校仍不完整，相机电源域关闭及休眠恢复需要进一步验证。

## 上游参考

上游 Ubuntu/GNOME 用 Snapshot 验证过预览、拍照、录像和回放，采用相机专用 DMA-BUF 启动包装。
这些记录及 `liuqin-camera`/Snapshot 兼容组件保留来源，不作为 Fedora 默认应用或录像验收声明。
Fedora 使用 Kamoso、QRCA 与发行版组件，不要求安装 GNOME Snapshot。

## 来源

相机支持由 reisa 贡献。S5KJN1 驱动改编自 Vladimir Zapolskiy（Linaro）的上游驱动，提交 [e38fd0933c75](https://github.com/torvalds/linux/commit/e38fd0933c759bfd51aafbee07f11e8d69da4366)，保留了原版权声明和模块作者。liuqin 的 19.2 MHz 模式数据根据设备厂商的相机模组配置重建，不分发任何厂商二进制文件。CSIPHY v2.1.3 通道参数参照高通 GPL-2.0 下游相机驱动，保留了其版权声明。贡献者说明板级适配部分借助了 AI 辅助开发。
