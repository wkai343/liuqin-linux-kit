# Adreno GPU 统计

为 KDE KSystemStats 补充高通平台设备，继续提供默认系统监视器使用的 `gpu/all/usage`。
单设备传感器为 `gpu/gpu0/usage`；内核同时提供 devfreq 频率时还注册
`gpu/gpu0/coreFrequency`，单位 MHz。设备编号避开已有 GPU，兼容概览与历史页的默认选择规则。

通过设备树兼容标识识别 Adreno，读取 `gpu_busy_percent` 和 `devfreq/*/cur_freq`。
不固定平台设备地址和 DRM 卡号，不需要 root，不读取 debugfs，不更改调频策略。
缺少或无效的数据保持不可用；仅当 GPU 提供真实显存数据时才注册汇总显存传感器。
Adreno 共享系统内存，本适配不伪造独立显存及其百分比、温度或功耗。

默认 Fedora 镜像使用官方 `ksystemstats`，本项目按固定的 KDE 6.8.2 源码只编译
`ksystemstats_plugin_gpu` 目标，生成 `liuqin-ksystemstats-gpu-6.8.2-1.liuqin.aarch64.rpm`。
该包只安装私有目录中的 GPU 插件、统计服务的 drop-in 以及许可/源码记录。
守护进程、CPU、内存、网络和其他统计插件由 Fedora 官方包提供。
概览使用 `gpu/all/usage`，历史页通过 `gpu/gpu` 加数字匹配单 GPU，原有页面配置继续适用。

源码与包名、版本、校验值见 `build/rootfs/fedora-stats-source.json`；构建代码见
`build/lib/fedora_stats.py`，阶段缓存名为 `fedora-adreno-gpu`。组件在目标 Fedora ARM64 根内编译，
从实际 ELF 提取共享库依赖，并记录构建时 Qt、libksysguard、glibc 与 libstdc++ 的最低版本要求。
包只要求 `ksystemstats >= 6.8.2`，不提供或替代 `ksystemstats`，不锁定官方后端的精确版本。
桌面装配会安装官方后端，并检查私有 GPU 插件与服务配置存在、共享库能够加载。

包内 `/usr/share/licenses/liuqin-ksystemstats-gpu/` 包含上游许可证、适配源码、
带补丁的完整源码归档、打包脚本、`SOURCE.json` 和 `BUILD.json`；后者记录插件哈希及实际依赖。
构建阶段的 `stats-packages.tsv` 保留完整构建包版本清单。
同一上游版本内调整适配或 ABI 构建环境后，应增加锁文件中的 `package_release` 再发布新包；
升级上游 `version` 时，release 可从 `1.liuqin` 重新开始。
该锁文件约束 GPU 插件源码与版本，不固定发行版的官方后端版本。

## 已安装系统迁移

已有系统若仍安装原来的 `ksystemstats-6.7.5-102.liuqin`，先安装新构建的 GPU 插件 RPM，
再将后端同步到发行版软件源。不要通过删除 `ksystemstats` 来迁移，以免连带移除桌面软件。

```sh
sudo dnf install ./liuqin-ksystemstats-gpu-6.8.2-1.liuqin.aarch64.rpm
sudo dnf distro-sync ksystemstats
systemctl --user daemon-reload
systemctl --user restart plasma-ksystemstats.service
```

本地自编译包按自己的签名策略安装；测试环境可对该次安装使用 `--setopt=localpkg_gpgcheck=False`。
不更改全局校验设置。迁移后用 `rpm -q ksystemstats liuqin-ksystemstats-gpu` 确认两个独立包，
用 `rpm -V ksystemstats` 确认官方文件完整，再检查 GPU 传感器。

## GPU 插件独立部署测试

已从锁定的 6.8.2 源码重新编译 ARM64 插件，并在 liuqin 平板部署验证：

| 组件 | 当前验证版本 |
| --- | --- |
| 官方后端 | `ksystemstats-6.8.2-1.fc45.aarch64` |
| 独立 GPU 插件 | `liuqin-ksystemstats-gpu-6.8.2-1.liuqin.aarch64` |
| 运行库 | Qt `6.11.2`、libksysguard `6.8.2`、KF6 CoreAddons `6.31.0` |

构建只执行 GPU 目标，保留 6.8.2 的 Xe 及真实显存百分比支持，并追加 Adreno 采集。
两包的 `rpm -V` 检查通过，文件清单没有重叠；进程映射确认 GPU 模块来自私有目录，
CPU、内存和网络插件来自官方标准目录。默认 GPU 传感器路径继续适用，
Adreno 没有伪造的 `totalVram`、`usedVram` 或 `usedPercent` 传感器。
CPU、内存和网络传感器读取正常。

使用无窗口 GLES 负载验证，硬件渲染器为 `FD730`，对应 Adreno 730。
采样同时覆盖系统监视器传感器与内核 GPU busy 数据：

| 阶段 | GPU 使用率 | 核心频率 |
| --- | --- | --- |
| 空闲 | 0% | 220 MHz |
| GPU 负载 | 峰值 72% | 最高 818 MHz |
| 负载结束后 | 恢复到 0% | 恢复到 220 MHz |

### 挂起与唤醒

同一新版组合在默认 `SuspendState=freeze` 策略下，通过 RTC 定时唤醒完成一次有效测试。
内核日志确认 `PM: suspend entry (s2idle)` 与 `PM: suspend exit`，
`CLOCK_BOOTTIME` 与 `CLOCK_MONOTONIC` 的经过时间差为 29.999 秒；
挂起成功计数从 2 增至 3，失败计数保持 0，boot ID 不变。

统计守护进程 PID、启动时间、插件映射与原采样客户端跨挂起保持不变，没有重启服务。
唤醒后重新施加 GLES 负载时，GPU 使用率为 48–65%、频率 818 MHz；
负载结束后恢复到 0%、220 MHz。CPU、内存采样继续更新，Wi-Fi 自动恢复连接。
测试结束后已清理临时 RTC 闹钟和采样任务。

这些数据覆盖当前版本组合的一次 30 秒 s2idle 周期，不代表 deep、长时间或多轮待机验收。
此前 6.7.5 测试曾出现一次提前返回，原因未确认；本次新版测试按预定时间完成，
不能将其解释为 GPU 插件修复了系统提前唤醒问题。6.7.5-2.liuqin 旧包保留作回退基线。

### 插件加载与维护边界

插件安装在 `/usr/lib64/liuqin/qt6/plugins/ksystemstats/`，通过独立包拥有的
`/usr/lib/systemd/user/plasma-ksystemstats.service.d/50-liuqin-gpu.conf`
只为该统计服务设置 `QT_PLUGIN_PATH=/usr/lib64/liuqin/qt6/plugins`。
保持原插件文件名与 ID，利用 KCoreAddons 对同 ID 插件的优先路径选择；
官方 GPU 插件文件保留，其他 Qt 程序的插件路径沿用原配置。
独立包没有替代或冲突声明，只依赖 `ksystemstats >= 6.8.2` 和所需运行库，
不锁定官方后端的精确版本。网络断线提示按官方后端原有行为处理。

后续 Qt/libksysguard ABI 更新仍须重新验证加载、采样与睡眠恢复；
定制 GPU 模块还需同步上游 GPU 修复。长期方向仍是向 KDE 上游合入 Adreno 支持。

参考：[KSystemStats 插件装载](https://github.com/KDE/ksystemstats/blob/master/src/daemon.cpp)、
[KCoreAddons 插件选择](https://github.com/KDE/kcoreaddons/blob/master/src/lib/plugin/kpluginmetadata.cpp)、
[Qt 插件部署与兼容规则](https://doc.qt.io/qt-6/deployment-plugins.html)。

验证真实读取函数与补丁：

```sh
python3 tests/adreno-stats.py
devbox run -- python3 tests/fedora-stats.py
```

宿主测试需要 C++ 编译器、pkg-config 和 Qt6Core 开发文件。ARM64 构建依赖在隔离 Fedora 环境内安装。
原有 KDE 文件继续适用其作者与 SPDX 声明。本目录的新增采集代码使用
`GPL-2.0-only OR GPL-3.0-only`，不改动项目根目录许可证。
