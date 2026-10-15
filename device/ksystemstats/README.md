# Adreno GPU 统计

为 KDE KSystemStats 补充高通平台设备，继续提供默认系统监视器使用的 `gpu/all/usage`。
单设备传感器为 `gpu/gpu0/usage`；内核同时提供 devfreq 频率时还注册
`gpu/gpu0/coreFrequency`，单位 MHz。设备编号避开已有 GPU，兼容概览与历史页的默认选择规则。

通过设备树兼容标识识别 Adreno，读取 `gpu_busy_percent` 和 `devfreq/*/cur_freq`。
不固定平台设备地址和 DRM 卡号，不需要 root，不读取 debugfs，不更改调频策略。
缺少或无效的数据保持不可用；仅当 GPU 提供真实显存数据时才注册汇总显存传感器。
Adreno 共享系统内存，本适配不伪造独立显存、温度或功耗。

当前 Fedora 路径从固定的 KDE 6.7.5 源码构建 `ksystemstats` RPM，并在 rootfs 装配时安装。
2026-10-13 已在 liuqin 的 Fedora 上验证：系统概览和历史页均可显示变化的 GPU 使用率，
用户态还可读取 GPU MHz 频率。概览使用 `gpu/all/usage`，历史页通过 `gpu/gpu` 加数字匹配单 GPU。
源码来源和校验值见 `build/rootfs/fedora-stats-source.json`；构建代码见 `build/lib/fedora_stats.py`。
软件包版本使用较高的 `101.liuqin` release 区分 Fedora 原包。同一版本的普通 DNF 更新会保留它；
将来升级到更高版本的官方 KSystemStats 会替换适配，需要同步源码锁与补丁重新构建。

验证真实读取函数与补丁：

```sh
python3 tests/adreno-stats.py
```

宿主测试需要 C++ 编译器、pkg-config 和 Qt6Core 开发文件。ARM64 构建依赖在隔离 Fedora 环境内安装。
原有 KDE 文件继续适用其作者与 SPDX 声明。本目录的新增采集代码使用
`GPL-2.0-only OR GPL-3.0-only`，不改动项目根目录许可证。
