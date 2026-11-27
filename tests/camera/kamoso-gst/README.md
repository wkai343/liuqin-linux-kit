# 官方 Kamoso 的独立快门插件验证

这个测试不替换 Kamoso，也不把插件装到系统目录。测试插件注册一个
`GstDeviceProvider`，仅在指定 `LIUQIN_PROOF_NODE` 时向应用提供名为
“Liuqin shutter proof”的附加摄像头。选中它后，预览使用指定 PipeWire 节点的真实 NV12 帧。
单纯打开官方 Kamoso、选用原来的摄像头，不会启用该能力。

官方 Kamoso 向 GStreamer 的 `camerabin` 发出 `start-capture`。
在测试的 GStreamer 1.28 分支中，`wrappercamerabinsrc` **在准备拍照时**
调用所选源的 `GstPhotography.prepare_for_capture`，然后把源缓冲区送往拍照分支。
插件在这个回调中分配编号并确认协商的格式，再用新分配的 NV12 缓冲区替换对应帧。
插件不读取拍照目录，也不编码或写入 JPEG。保存位置由 Kamoso 选择，JPEG 由原应用管线生成。
接口和行为见 [camerabin](https://gstreamer.freedesktop.org/documentation/camerabin/camerabin.html)
及 [wrappercamerabinsrc 实现](https://github.com/GStreamer/gstreamer/blob/1.28/subprojects/gst-plugins-bad/gst/camerabin2/gstwrappercamerabinsrc.c)。

在平板上准备 GStreamer 开发环境（包括 photography 和 video）、C 编译器、系统桌面会话、
Python 的 pyatspi 和 GdkPixbuf。将 Fedora 仓库的 Kamoso 二进制 RPM 验签后解包，使用包内的
`usr/bin/kamoso`；Qt/KDE 运行库应匹配。下例从仓库根目录执行，输出目录须为新目录。
确认 RPM 的签名来自预期 Fedora 发布密钥；不要用下游同名 RPM 代替。

```sh
work="$PWD/out/kamoso-proof"
mkdir -p "$work/official" "$work/plugins"
rpmkeys --checksig --verbose "$rpm"
rpm -qp --qf '%{NEVRA}\n' "$rpm"
rpm2cpio "$rpm" | (cd "$work/official" && cpio -id --quiet ./usr/bin/kamoso)
cc -std=c11 -Wall -Wextra -O2 -shared -fPIC tests/camera/kamoso-gst/shutterproof.c \
  $(pkg-config --cflags --libs gstreamer-photography-1.0 gstreamer-video-1.0) \
  -o "$work/plugins/libgstliuqinshutterproof.so"
# 从 wpctl status -n / wpctl inspect 取得实际后摄的 node.name。
tests/camera/kamoso-gst/run-proof.sh "$work/official/usr/bin/kamoso" "$work/plugins" \
  "$node_name" "$work/run1"
```

验收顺序为单拍一次、启动连拍、收到第二次连拍请求时停止。第三次总请求在测试插件中人为延迟
600 ms，检查停止操作已返回而最后一帧尚未交付；再等待超过一个 2.5 秒连拍周期。
测试解码三张 JPEG，从四个方向读出同心矩形编码 `B1`、`B2`、`B3`，同时核对请求、
返回和停止时序。窗口退出或测试超时后只关闭此次启动的临时服务。

该测试证明**单窗口**下插件能识别快门并提供应用实际保存的帧。它不实现闪光或 HDR，
也不验证多窗口争用、闪光曝光时序、长耗时 HDR 或拍摄期间撤销相机。GStreamer 或 Kamoso
更换拍照管线后，应重跑该测试。
