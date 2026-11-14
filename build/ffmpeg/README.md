# Fedora FFmpeg 的 Iris 接口修复

默认 Fedora rootfs 构建使用这个配方重建 `ffmpeg` 源码包，保留发行版补丁、编解码器白名单、
构建选项和拆包方式。它不是 Fedora 官方签名的二进制包，也不修改 Mesa 或内核。
镜像预装修复后的 FFmpeg 与七个配套运行库，并用 DNF5 锁定整组版本。

当前固定输入为 `ffmpeg-9.0.2-3.fc45.src.rpm`，SHA-256 和 spec 摘要见
[source.json](source.json)。配方仅增加：

- H.264、H.265 的 V4L2 stateful 硬解 wrapper；原生软件解码器继续按 Fedora 配置禁用。
- 线性 NV12/NV21 输入的可见区域拷贝：按驱动 stride 放置行，UV 位于对齐后的 Y 平面之后，
  按 AVFrame 的实际高度读取，并检查目标容量、负 stride 和拷贝偏移。
- 解码端读取只读的可见区域：即使驱动拒绝 `S_SELECTION`，仍查询 `G_SELECTION`，
  校验边界并传递 AVFrame crop 字段，避免把 H.265 的底部填充作为画面输出。
  每次 `SOURCE_CHANGE` 都刷新可见区域，包括存储尺寸未变化的情况；在重新设置
  CAPTURE 格式前缓存结果，避免驱动重置裁剪矩形或初始化占位尺寸覆盖真实视频头信息。
- 编码后将压缩数据复制到独立 AVPacket，及时归还硬件 CAPTURE 缓冲区。
  否则异步封装器保留的 packet 引用可能耗尽默认四个缓冲区，排空代码会误认为已经结束，
  丢失仍待编码的尾帧。额外复制仅针对压缩码流，不改变解码图像帧的引用路径。
- 解码通过 `get_format()` 协商线性 NV12/NV21 的 `DRM_PRIME` 输出，并公布内部硬解配置。
  每帧导出 DMA-BUF，描述真实 stride、存储高度和图像平面偏移；最后一个帧引用释放后才
  关闭导出 FD、归还 CAPTURE 缓冲区。保留帧引用时关闭解码器也不会提前释放硬件资源。
  未选择 DRM PRIME 的应用仍可接收普通映射图像帧。
  同时接受调用方的 DRM device context，使 FFmpeg 命令行也能显式选择 DRM PRIME。
- 解码时间戳使用正数 cookie 穿过 V4L2，返回时恢复原始 packet PTS。
  负数、B 帧重排、重复及未知时间戳不会因无符号转换或微秒取整而改变。
  有限历史表以完整 cookie 校验过期项，过期时返回未知时间戳，不冒用其他包的 PTS。
- 实现解码 flush：停止并分离旧硬件会话，下一次接收帧时重新创建会话，清除旧队列、
  EOS 和时间戳历史。渲染器仍持有的帧继续引用旧缓冲区，不会进入新会话。
  避免视频结束后拖回开头仍停在末帧；重开错误通过接收接口返回。

Iris 的 1280×720 输入可以协商成 1280×736 存储布局。上游 9.0.2 用存储高度读取
AVFrame，可能越界；仅把读取高度改成 720 会让 UV 偏移仍然错误。
本地修复保留驱动要求的平面布局，清空行尾及底部填充。
改动范围限于连续存储的线性 NV12/NV21，不能据此声称其他 V4L2 格式都已修复。

## 构建

正常执行 `devbox run build` 即可，不需要先手工编译或安装 FFmpeg。
默认 [Fedora 配方](../rootfs/fedora-plasma.json) 的 `packages` 包含 `ffmpeg-free`，
以此启用独立的 `fedora-ffmpeg` 阶段：

- 下载并校验固定 SRPM，缓存到 `inputs/ffmpeg/`；
  `devbox run -- python3 build/devbox/build.py prepare` 也会预取该输入。
- 在独立 Fedora ARM64 构建根中安装编译依赖，应用七个补丁并执行 `rpmbuild -ba`。
  x86_64 主机沿用项目的 QEMU/rootless 环境；编译依赖不进入桌面镜像。
- 校验八个运行库的名称、架构、版本和摘要，与桌面软件在同一 DNF 事务中安装。
  核对 V4L2 编解码接口，再生成 `/etc/dnf/versionlock.toml` 中的八条版本锁。
- 构建包的 `devel-rpms/` 另保留同次编译的 libavcodec、libavutil、libswresample
  三个开发包，供 [OBS 插件](../obs-v4l2/README.md) 在隔离根中编译；桌面只取 `rpms/`
  的运行包。清单采用 schema 2，缺少开发包的旧缓存会重新构建。
- 将修订版 SRPM、配方、构建脚本及摘要清单保留到镜像的
  `/usr/share/licenses/liuqin-ffmpeg/`；最终 rootfs 装配再检查运行库、版本锁与源码。
- 对已安装的 mpv、Haruna 添加 `v4l2m2m,auto` 默认配置，保留用户设置和已有系统配置。
  Qt GStreamer 后端存在时安装 `liuqin-qt-video`，只为指定应用启用已验证的播放设置。

该阶段有独立构建缓存；源码、补丁或构建脚本变化会使缓存失效。
首次构建需要编译 FFmpeg，后续输入不变会复用校验过的 RPM。
自定义配方不选 `ffmpeg-free` 时不构建这组修复包；其他应用仍可能依赖仓库中的 FFmpeg 库。

也可在独立 Fedora 45 aarch64 环境中单独重建。以下脚本仅产生 RPM，不负责安装和版本锁：

```sh
mkdir -p out/ffmpeg-source
dnf download --source --destdir out/ffmpeg-source ffmpeg-free-9.0.2-3.fc45
sudo dnf install rpm-build redhat-rpm-config gpgverify cpio dnf5-plugins
sudo dnf builddep out/ffmpeg-source/ffmpeg-9.0.2-3.fc45.src.rpm
python3 build/scripts/build-fedora-ffmpeg.py \
  --srpm out/ffmpeg-source/ffmpeg-9.0.2-3.fc45.src.rpm \
  --out out/ffmpeg-rpm --jobs 4
```

`--out` 必须为尚不存在的目录。`--prepare-only` 可在其他架构上只准备 rpmbuild 输入。
输入摘要不同会停止；升级来源时需要重新审查 spec、补丁和硬件验证，不能只更新摘要。
输出包括 `RPMS/aarch64/` 下的二进制包与 `SRPMS/` 下含修复源码的 SRPM。
安装时应在同一事务中更新 FFmpeg 和配套库，以及本机已经安装的对应 devel 子包，
保持 Fedora spec 的精确版本依赖。不要使用 `rpm --nodeps`。

## 更新与撤销

发行号为 `3.liuqin3.fc45`，高于当前官方 `3.fc45`，低于下一官方 `4.fc45`。
默认镜像自动设置 versionlock，保留 overlay 中无关软件的锁；同名冲突锁会停止构建。
单独使用 `build-fedora-ffmpeg.py` 不设置锁。未锁定时，后续 Fedora 更新可以覆盖
这组包；如果官方仍缺少修复或硬解白名单，相关问题会重新出现。

对已有系统手动安装修复包时，在完成
[视频短测](../../docs/VIDEO.md#ffmpeg-短测)后，用 DNF5 锁定当前版本的整组运行库：

```sh
sudo dnf versionlock add ffmpeg-free libavcodec-free libavdevice-free \
  libavfilter-free libavformat-free libavutil-free libswresample-free libswscale-free
dnf versionlock list
```

若安装了对应 devel 子包，也应一起锁定。检查列表中的版本确为已验证版本；
DNF5 默认将规则保存到 `/etc/dnf/versionlock.toml`。
版本锁也会阻止这组包的安全更新，不会自动将本地修复移植到新版。
升级时需要先审查新版源码与补丁、准备候选包并完成短测，再解除这组锁、
在同一事务中升级配套库，最后锁定新版本。其他软件仍可更新；依赖新版 FFmpeg
的应用更新可能因依赖约束被暂缓。

解除这组锁时使用包名删除，保留其他软件的版本锁：

```sh
sudo dnf versionlock delete ffmpeg-free libavcodec-free libavdevice-free \
  libavfilter-free libavformat-free libavutil-free libswresample-free libswscale-free
```

恢复当前仓库官方包时，先解除上述锁，再对以下整组运行 `dnf distro-sync`，
并一并同步已安装的对应 devel 包：

```sh
sudo dnf distro-sync ffmpeg-free libavcodec-free libavdevice-free \
  libavfilter-free libavformat-free libavutil-free libswresample-free libswscale-free
```

若启用了其他 FFmpeg 软件源，先检查事务来源；以上撤销方法假定使用 Fedora 官方源。

## 离线回归

在应用本配方补丁、配置并构建了 `libavutil/libavutil.a` 和 `libavcodec/packet.o`
的主机 FFmpeg 树上：

```sh
python3 tests/ffmpeg-v4l2.py --source /path/to/configured-ffmpeg --cc clang
```

另需 `pkg-config` 和 libdrm 开发头文件；也可用 `--drm-include /path/to/libdrm/include`
明确指定头文件目录。测试不要求实际 DRM 设备。

测试直接编译实际 `v4l2_buffers.c`，用 ASan/UBSan 检查 720p → 736 行、
1080p → 1088 行、854 像素宽、独立/负行间距、单/多平面 API、UV 偏移、
填充清零、无效缓冲区拒绝及解码裁剪后的平面地址。
另用模拟 QBUF 检查保留八个 packet 引用时仍立即归还硬件缓冲区，数据独立且尾部填充为零。
事件回归执行实际 `v4l2_handle_event()`，覆盖仅裁剪变化、重新配置时保留真实可见区域、
不支持 selection 的旧驱动和非法矩形拒绝。
DRM 回归使用真实 FD 模拟导出，覆盖单/双内存平面、736 行存储裁剪、帧克隆、先关闭
解码器后释放帧、部分导出失败后的 FD 回收、错误尺寸拒绝，以及重排和过期 PTS。
未修补的 9.0.2 在第一项出现越界读取。
这不代替硬件的完整帧数、画质和内核日志检查。

真机解锁桌面后，可运行 `tests/ffmpeg-v4l2-playback.py`，使用时间戳正确的两秒素材
检查可见 DRM PRIME 播放，以及保留末帧时连续两次跳回开头。
参数和输出说明见 [测试入口](../../tests/README.md)。

## 来源

- [FFmpeg 9.0 的 V4L2 缓冲区实现](https://ffmpeg.org/doxygen/9.0/v4l2__buffers_8c_source.html)。
- [同类 Iris 越界报告 PR 24742](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24742)：
  作为问题对照；本地补丁另外处理平面偏移及行间距，不是原样应用该 PR。
- [V4L2 平面 YUV 格式约定](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/pixfmt-yuv-planar.html)。
- [V4L2 DMA-BUF 导出接口](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-expbuf.html)。
- [FFmpeg 的 DRM 帧布局](https://ffmpeg.org/doxygen/9.0/hwcontext__drm_8h_source.html)。
