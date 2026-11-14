# OBS 的独立 Iris/V4L2 编码器

`liuqin-v4l2` 是独立的 OBS 插件，注册 `liuqin_v4l2_h264` 和 `liuqin_v4l2_hevc`。
它通过系统 FFmpeg 的 `h264_v4l2m2m` / `hevc_v4l2m2m` 调用 Iris Encoder，使用 OBS
标准录像的封装器，不覆盖官方 OBS、libobs 或 obs-ffmpeg 文件，也不需要 VA-API。
依赖本项目的 [FFmpeg 行距和 packet 生命周期修复](../ffmpeg/README.md)。

## 使用范围

- 8-bit NV12、SDR，H.264 High / H.265 Main，VBR 目标码率和关键帧间隔可设置。
  不提供 CBR、CQP、动态码率、B 帧、HDR 或 GPU 纹理直传；原始图像仍经过 CPU 拷贝。
  若推流服务强制覆盖成 CBR，插件会明确拒绝，不把 VBR 冒充成 CBR。
- 录像使用「输出 → 高级 → 录像 → 标准」，选择带 `V4L2 / Iris` 的编码器。
  推荐 MKV；MP4 和 TS 也有短测覆盖。该插件不会修复或替换旧的「自定义输出（FFmpeg）」。
- Iris 的首个输出可能只有 SPS/PPS 或 VPS/SPS/PPS。插件缓存这些参数集供
  `get_extra_data()` 使用，不把这个包计为视频帧，也不要求它有图像时间戳。
  同时接受参数集与首帧合并的包；普通图像包保留 PTS/DTS，并识别实际关键帧。
- OBS 的标准封装器在收到首个图像包后才获取头信息，因而 MKV/MP4 初始化时参数集已经
  可用。正常停止时，OBS 继续编码直到输出时间戳抵达停止点，写完停止点之前的帧。
  解除输出回调后，插件再排空并释放停止点之后的硬件流水线帧。不能在 `destroy()` 中
  向已经停止的输出补写数据；强制退出或强制停止也不保证完整尾帧。

已验证环境为 Fedora ARM64 OBS `32.2.2-3.fc45`、FFmpeg `9.0.2-3.liuqin3.fc45`
和本项目 SM8475/Iris2 内核。720p30 的 H.264/H.265 × MKV/MP4/TS，以及同一编码器对象
停止后重新录像均需通过下面的帧数检查。另覆盖 852×480 行距、1920×1080 可见高度、
30000/1001 帧率和帧率除数。OBS 将请求的 854 像素输出宽调整为 852，验收以实际尺寸为准。
直播服务、长时间录制、麦克风与主观音画同步不在这组短测范围内。

## 构建和安装

默认 `devbox run build` 会安装 Fedora 官方 `obs-studio`，并通过独立的
`fedora-obs-v4l2` 阶段构建、安装 `obs-studio-plugin-liuqin-v4l2` RPM。
已有系统只需要插件包时，在仓库根目录运行：

```sh
devbox run build -- --target obs-v4l2
```

此目标不需要原厂 ROM、内核源码或连接平板。它自动准备 Fedora ARM64 构建根和
修复版 FFmpeg；x86_64 主机使用项目的 QEMU/rootless 环境。首次无缓存时会先编译
FFmpeg，后续复用已校验的缓存。命令最后显示产物目录：
`out/build/from-source/fedora-obs-v4l2-<摘要>/assembled/`，包含插件 RPM、
`manifest.json` 和 `build-packages.tsv`。构建会运行包适配回归和共享库/API 加载检查。
源码、回归程序、FFmpeg 构建包或构建脚本改变会使对应缓存失效；`--refresh` 可刷新仓库依赖。

RPM 安装插件到 `/usr/lib64/obs-plugins/`，数据到
`/usr/share/obs/obs-plugins/liuqin-v4l2/`，源码和来源记录到
`/usr/share/licenses/obs-studio-plugin-liuqin-v4l2/`。不会替换官方 OBS 文件，
也不增加 OBS 版本锁；更新改变 OBS/FFmpeg ABI 时需要重新构建、短测。
FFmpeg 仍沿用已有整组版本锁和升级规则。构建依赖与匹配的三个 FFmpeg 开发包只进入
隔离构建根，不随插件进入桌面镜像。自定义配方不选 `obs-studio` 时跳过默认插件阶段；
选择 OBS 时须同时选择 `ffmpeg-free`。单独构建目标不受此自动选择条件限制。

将 RPM 复制到已安装本项目修复版 FFmpeg 的平板，退出 OBS 后使用
`sudo dnf install /path/to/obs-studio-plugin-liuqin-v4l2-0.1.0-1.liuqin.aarch64.rpm`。
如曾用下面的手工方式安装，先移走自己的 `obs-studio/plugins/liuqin-v4l2/` 目录，
避免用户插件遮蔽系统 RPM。卸载使用 `sudo dnf remove obs-studio-plugin-liuqin-v4l2`，
不会删除录像文件。

### 手工 CMake 构建

需要 CMake、C 编译器、pkg-config，以及与运行库匹配的 `obs-studio-devel`、
`simde-devel`/`hedley-devel`、FFmpeg libavcodec/libavutil 开发包。
Fedora 的 libavcodec pkg-config 还依赖 libswresample 开发包；本地修复版 FFmpeg 的
devel RPM 应从同一次 FFmpeg 构建产物取得，不混装官方不同版本或使用 `rpm --nodeps`。
也可把这些 RPM 解包到隔离目录，通过 `PKG_CONFIG_SYSROOT_DIR` 和
`PKG_CONFIG_LIBDIR` 提供头文件，链接当前安装的运行库，不安装开发包到系统。

在 ARM64 系统中执行：

```sh
cmake -S build/obs-v4l2 -B out/obs-v4l2 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build out/obs-v4l2
ctest --test-dir out/obs-v4l2 --output-on-failure
# 关闭 OBS 后，以桌面用户安装，不使用 sudo。
cmake --install out/obs-v4l2 \
  --prefix "${XDG_CONFIG_HOME:-$HOME/.config}/obs-studio/plugins"
```

安装内容限于 `obs-studio/plugins/liuqin-v4l2/`；重新启动 OBS 后加载。
升级插件也应先退出 OBS。移走该目录即可撤销，已有录像文件不受影响。
插件不锁定官方 OBS 的更新；OBS/FFmpeg ABI 变化时仍需重新编译和短测。
现有 FFmpeg 版本锁保持原来的维护规则。手工 CMake 默认仍采用用户插件目录布局；
RPM 构建使用 `-DOBS_INSTALL_SYSTEM=ON -DCMAKE_INSTALL_PREFIX=/usr`。

## 验证

`obs-v4l2-packets` 执行真实的包适配代码，覆盖参数集单独/合并输出、重复参数集、
首帧时间戳和短缓冲区。可用 `-fsanitize=address,undefined` 编译同一个测试，核对内存边界。

真机 Wayland 会话中运行以下脚本，使用自己生成的测试图片，输出目录必须尚不存在：

```sh
python3 tests/obs-v4l2-record.py \
  --plugin out/obs-v4l2/liuqin-v4l2.so --data build/obs-v4l2/data \
  --image /path/to/generated-test.png --output out/obs-h264-mkv \
  --codec h264 --container mkv --repeat 2
```

分别选择 `h264` / `hevc` 和 `mkv` / `mp4` / `ts`。默认录制 60 帧，约两秒；
脚本使用真实 libobs、图像源、AAC 静音轨和标准 `ffmpeg_muxer`，不会采集桌面或麦克风。
它在第 59、60 帧之间请求停止，检查此前包的 PTS 连续性，并保存实际停止时间。
OBS 的总包数可能包含用于触发停止的边界包，不能据此直接判断文件是否完整。

把录像复制到具备软件 H.264/H.265 解码器的机器上，用 `ffprobe -count_frames`
或 `-show_frames` 独立核对：每段应有 60 个可解码视频帧，PTS 连续、实际尺寸正确、
首帧为关键帧，MKV/MP4 有有效 extradata。不要在此平板上仅用自动选择的硬件解码器
作唯一判据。还应检查测试期间 Iris/SMMU/GPU 日志及 `rpm -V obs-studio obs-studio-libs`。

## 实现参考

- [OBS 编码器接口](https://github.com/obsproject/obs-studio/blob/32.2.2/libobs/obs-encoder.h)
- [标准 FFmpeg 封装器的头信息与停止时序](https://github.com/obsproject/obs-studio/blob/32.2.2/plugins/obs-ffmpeg/obs-ffmpeg-mux.c)
- [OBS Linux 用户插件搜索路径](https://github.com/obsproject/obs-studio/blob/32.2.2/frontend/widgets/OBSBasic.cpp)

插件与 C 回归测试采用 GPL-2.0-or-later，Python 录像测试采用 MIT。
