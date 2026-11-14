# Firefox 的系统 FFmpeg 启动适配

Fedora Firefox 159/160 的 VP9 优先进入内置 FFVPX。该库没有 V4L2 VP9 wrapper，
会在模块内回退软件解码；已修好的系统 FFmpeg 因此没有获得解码机会。
H.264/H.265 已能通过系统 FFmpeg 的 V4L2/Iris、DRM PRIME 路径播放，
不需要额外实现 VA-API。

本适配是有兼容范围的过渡方案，不是 Mozilla 提供的正式配置开关：
使用 Bubblewrap 建立私有挂载视图，把匹配的系统 `libavcodec` 和 `libavutil`
映射到 Firefox 的两个内置媒体库路径。Firefox 的可执行文件、`libxul`、
宿主 RPM 文件及原有沙箱设置不变，但进程中使用的媒体库发生了变化。
适配也会影响音频和软件解码，不能只测试 VP9 后就扩大兼容名单。

## 入口与默认构建

默认 Fedora 配方安装 `bubblewrap`。选用了修订版 FFmpeg，且 Firefox 和所需库
均已安装时，构建器安装以下内容：

- `/usr/local/bin/firefox`、`/usr/local/bin/liuqin-firefox`：指向独立启动器；
- `/usr/local/libexec/liuqin-firefox`：兼容检查与私有挂载逻辑；
- `/usr/local/share/liuqin-firefox/compatibility.json`：已验证的组合；
- `/usr/local/libexec/liuqin-firefox-host/{bin,firefox}`：空挂载点。

Fedora 桌面文件中的 `Exec=firefox` 沿用，Plasma 的 PATH 优先找到 `/usr/local/bin`。
不改写发行版桌面文件；已有 overlay 的本地启动器保留。
显式调用 `/usr/bin/firefox` 或其他直接调用浏览器二进制的入口，在适配环境外仍走原版。
新配置需要关闭已有 Firefox 进程后再启动；向旧进程转发 URL 不会替换其已加载的库。

可从终端检查实际选择：

```sh
LIUQIN_FIREFOX_MEDIA_DEBUG=1 firefox
MOZ_LOG='PlatformDecoderModule:5,FFmpegVideo:5' firefox
```

VP9 应出现 `V4L2 codec vp9_v4l2m2m`、`IsHardwareAccelerated=true` 和实际帧输出。
仅出现 `FFVPX` 日志前缀不能判定软件解码，因为该名称标识 Firefox 的调用模块，
私有视图中该模块已加载系统库。

## 更新与回退

不锁定 Firefox，也不重建它的软件包。启动器核对浏览器四个 ELF 的 GNU build ID、
官方启动脚本摘要，以及两项 FFmpeg 包的精确发行号；这些信息用于兼容识别，
不能代替 RPM 签名或完整性验证。未知组合、缺少库或不能创建挂载命名空间时，
使用原版媒体库。因此浏览器更新可以正常进行，未知版本的 VP9 会暂时恢复软件解码。
Firefox 内部重启也重新检查：私有视图保留原始目录，必要时先恢复原版库，
避免浏览器更新后仍沿用旧的媒体库映射。

`compatibility.json` 目前限定 Fedora ARM64 Firefox `159.0.1-1.fc45`、
`160.0-1.fc45` 和 FFmpeg `9.0.2-3.liuqin3.fc45`。其他平台或构建不会因版本号近似
就自动获准。升级 FFmpeg 仍遵循其整组版本锁和源码维护流程。

单次关闭适配：

```sh
LIUQIN_FIREFOX_SYSTEM_FFMPEG=0 firefox
```

撤销日常入口时，先关闭浏览器，删除本适配创建的两个 `/usr/local/bin` 符号链接即可。
不要删除 Fedora 的 `/usr/bin/firefox` 或内置媒体库。

## 验证与维护

新组合应先下载并校验 Fedora RPM 签名，解包至 `out/`，在隔离视图和独立 profile 中
测试后再加入名单。至少覆盖 VP9 播放与重播、H.264/H.265 回归、Opus/AAC 音轨、
AV1 软件解码、WebAudio FFT、主动禁用硬解、未知版本回退及适配内再次启动。
核对真实解码器、帧输出、画面、沙箱状态和测试区间的内核日志。
没有验证的 10-bit/HDR、网站 MSE/DRM、WebRTC 和长片播放不包含在短测结论中。

`python3 tests/firefox-media.py` 用合成 ELF 和临时根目录检查兼容识别、未知组合拒绝、
参数传递、嵌套回退路径和安装时保留用户配置，不启动浏览器或访问视频设备。

## 来源

- [Firefox 159 的解码器选择](https://github.com/mozilla-firefox/firefox/blob/FIREFOX_159_0_1_RELEASE/dom/media/platforms/PDMFactory.cpp)。
- [Firefox 160 的解码器选择](https://github.com/mozilla-firefox/firefox/blob/FIREFOX_160_0_RELEASE/dom/media/platforms/PDMFactory.cpp)。
- [Firefox 的 FFVPX 版本和定制项](https://github.com/mozilla-firefox/firefox/blob/FIREFOX_159_0_1_RELEASE/media/ffvpx/README_MOZILLA)。
- [Bubblewrap 挂载选项](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)。

长期修复应在 Firefox 中让有可用 V4L2 硬件接口的系统模块获得机会，保留正常软件回退；
本入口不替代上游的解码器选择修复。
