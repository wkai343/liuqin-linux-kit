# 构建指南

[项目首页](../README.md) ｜ [安装助手](INSTALLER-GUI.md) ｜ [内核更新](KERNEL-UPDATES.md)

从官方 ARM64 基础系统装配 KDE Plasma，生成 `installer.img`、`boot.img`、`rootfs.tar.gz`。
默认使用 Fedora 44，保留 Ubuntu 26.04 配方。内核、桌面和安装环境分别配置与缓存；
设备适配沿用原项目并加入本项目补丁。构建成功与硬件验收分别记录，见 [硬件说明](HARDWARE.md)。

## 1. 准备环境与配置

在 Linux 主机安装 Devbox/Nix，以普通用户进入项目根目录执行：

```sh
devbox install
devbox run doctor
devbox run init-build
```

Devbox 提供交叉编译器、QEMU、镜像和打包工具，三个文件共用这套环境。
完整构建还需要网络、宿主 sudo 和支持 binfmt 的 Linux 内核，构建器按阶段提权。
命令和配置中的相对路径均从项目根目录解析，也可填写绝对路径。

主要维护两份配置：

- `build.json`：本机内核与板级输入，不提交 Git；模板为 `build/devbox/build.example.json`。
- `rootfs_config` 指向的 JSON：发行版、软件、overlay 和首次初始化方式。

已有 `build.json` 时，`init-build` 保留原设置并正常退出。
需要另一份配置可运行 `devbox run init-build -- --config build.new.json`，后续例如 `devbox run plan -- --config build.new.json`。
脚本参数放在 `--` 之后，与 Devbox 自身的选项分开。

`build.json` 示例：

```json
{
  "kernel_source": "sources/linux-liuqin-7.3",
  "kernel_fragments": ["build/kernel/configs/custom-zram.config"],
  "kernel_localversion": "-liuqin-fedora-v73",
  "rootfs_config": "build/rootfs/fedora-plasma.json",
  "usb_manufacturer": "Xiaomi",
  "usb_product": "Pad 6 Pro",
  "rom_images": "inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images",
  "package_version": "0.3.1"
}
```

| 配置项 | 用途 |
| --- | --- |
| `kernel_source` | 目标内核 Git 源码目录；不存在时获取 `build/kernel/source.json` 中的固定提交 |
| `kernel_source_lock` | 可选，安装到平板的目标内核来源；默认 `build/kernel/source.json`（7.3）。安装环境自身运行的内核由 `source-6.17.json` 独立锁定 |
| `kernel_fragments` | 叠加的内核配置片段，示例启用 zram |
| `kernel_localversion` | 自定义内核版本后缀；修改源码或配置时更换，避免同名内核对应不同模块 |
| `rootfs_config` | Fedora 或 Ubuntu 的 rootfs 配方路径 |
| `usb_manufacturer`、`usb_product` | USB 显示名称，默认为 `Xiaomi`、`Pad 6 Pro` |
| `rom_images` | 指定版本原厂 ROM 的 `images` 目录 |
| `package_version` | 内核升级包版本，每次发布提高 |
| `kernel_reclaim_mte` | 可选，默认 `false`；清理旧版设备树中的 512 MiB MTE 预留，当前 7.3 无需设置，见下文 |

已有内核源码必须是干净的 Git 提交，修改后先提交再构建。
切换到 Ubuntu 时，将 `rootfs_config` 改为 `build/rootfs/ubuntu-plasma.json`；其他发行版需要另行适配。

## 2. 准备原厂资料

```sh
devbox run fetch-rom
```

脚本从[指定的 cpidMi 页面](https://www.cpidmi.com/xiaomi-pad-6-pro-liuqin-os2-0-6-0-vmycnxm-15675)
对应镜像源下载 **OS2.0.6.0.VMYCNXM Fastboot ROM**，校验完整包后只提取
`vendor_boot.img`、`dtbo.img`、`super.img`、`NON-HLOS.bin`。
默认输出为 `inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images`。

已有完整包可复用，或直接在配置中填写已解压的同版 `images` 路径：

```sh
devbox run fetch-rom -- --archive /path/to/liuqin_images_OS2.0.6.0.VMYCNXM_20250318.0000.00_15.0_cn_c29f6de5df.tgz
```

下载来源和完整包校验值在 `build/data/stock-rom-source.json`，单份镜像校验值在
`shared/liuqin-rom-images.json`。Recovery ZIP、其他 ROM 版本不能替代。
损坏缓存会保留并停止，不覆盖用户文件；完整下载包保存在 `inputs/downloads/stock-rom/`。

原厂资料用于设备树、传感器配置、音频保护程序及通用调音等硬件适配；
操作系统本身来自官方 Fedora 或 Ubuntu。构建不连接平板，也不运行 ROM 内的刷机脚本。
每台平板的设备地址和校准数据在安装时从本机导入，不放进通用镜像。

## 3. 构建与输出

```sh
devbox run plan
devbox run build
```

`plan` 只显示配置和缺失材料。`build` 自动补齐依赖；也可先运行 `devbox run prepare`，
仅准备 ROM 输入、官方基础系统、BusyBox、mkbootimg 和内核源码。

成品位于 `out/releases/<发行版>-plasma-<内容标识>/`，只包含：

```text
installer.img
boot.img
rootfs.tar.gz
```

报告放在 `out/reports/`，包版本清单随构建缓存保存。
成品状态为 `BUILT_NOT_DEVICE_TESTED`，实际安装后的验收另行记录。

### 三个文件的输入

| 文件 | 构建输入 | 何时需要更新 |
| --- | --- | --- |
| `installer.img` | 安装环境的运行内核（默认 6.17）、最小 ARM64 runtime、RAM 安装脚本及板级资料 | 安装能力、安装环境内核或通道改变时 |
| `boot.img` | 安装后运行的目标内核（默认 7.3）、设备树、固件、启动脚本与模块身份 | 目标内核、设备树或启动配置改变时 |
| `rootfs.tar.gz` | 官方基础系统、桌面/软件配方、设备包、目标内核包及同版模块 | 用户态改变，或为新安装打包另一版内核时 |

默认构建中，`installer.img` 临时运行 6.17，负责将 7.3 系统写入平板；安装完成后从目标 `boot.img` 启动。
安装环境与目标系统的内核版本可以不同，但目标 boot 与 rootfs 中的内核模块必须配套；
更换目标内核只需重打包最终 rootfs，已完成的桌面装配可以复用。
已安装系统使用内核升级包，见 [内核更新](KERNEL-UPDATES.md)。

### 分别构建

| 命令 | 输出或范围 |
| --- | --- |
| `devbox run kernel -- --configure-only` | 生成最终 `.config` 预览，不编译 |
| `devbox run kernel` | 目标内核 Image 与 DTB |
| `devbox run modules` | 同版内核模块 |
| `devbox run boot` | 目标引导镜像，不装配桌面 |
| `devbox run kernel-packages` | 按配方生成版本化 RPM/DEB 内核升级包 |
| `devbox run rootfs` | 装配用户态、设备包与目标内核，生成根归档 |
| `devbox run build-installer` | 安装环境自身的运行内核与 RAM 安装镜像 |
| `devbox run build -- --refresh` | 重新装配 APT/DNF 软件阶段，保留旧缓存 |

各命令自动补齐依赖，结束时打印实际产物路径。
首次准备完整安装目录使用 `devbox run build`。

### 启动与设备适配

目标内核包含键盘、指纹、显示和电池接口补丁，顺序以 `build/kernel/source.json` 为准。
正常系统使用 `device/native-bootargs.txt` 和 `build/kernel/configs/native-display.config`；
独立安装环境使用自己的内核与启动参数，不随这些目标补丁改变。
显示策略、CABC 用法和调试参数见 [显示与画质](DISPLAY.md)，未完成事项见 [待改进与验证](DEVELOPMENT-STATUS.md)。

电池补丁修正容量单位并停止提供固件固定的 10 W 占位值，已在临时内核核对读数。
它不改变充电或供电策略；容量刷新仍有延迟，不能用短时容量差计算可靠的待机功耗。

正常引导还设置 `deferred_probe_timeout=60`，让驱动能等到根系统装配完毕并加载模块；
依赖就绪便继续，不会强制等待 60 秒。

Fedora 配方包含 `plymouth` 时，构建会从官方 Fedora 基础系统生成并缓存独立的
Plymouth 运行包（含官方 Spinner 素材和 script 插件），再将它与匹配的显示模块放入 `boot.img`。
此时额外应用 `firmware-splash.config`，将 KTZ8866 改为模块，先绘制早期画面再接管背光。
首次生成运行包需要与根系统装配相同的 root/ARM64 执行环境；内核更新可复用该运行包。
桌面根归档不参与运行库收集，`installer.img` 保持独立。
运行库中内容和权限相同的 ELF 文件在 CPIO 中共享数据，全部路径保持可用，配置文件仍独立。
正常引导在目标内核启用 `CONFIG_RD_ZSTD=y` 时使用 Zstd 压缩，否则使用 gzip；
独立安装环境继续使用 gzip。安装器与引导更新工具均可读取这两种格式。
开机方向从保留的小米标识别，关机方向跟随退出前的显示方向；原生驱动接手仍会短暂黑屏。
方向规则、回退与验证范围见 [开关机画面](DISPLAY.md#开关机画面)。
早期显示设备就绪后即继续，检查间隔为 50 毫秒，等待上限为 5 秒。
可在正常引导参数中加入 `plymouth.enable=0` 停用动画。
正常启动会核对 UFS 逻辑单元、双份 GPT 与内核分区节点，连续两轮完整就绪后提前结束存储等待；
无法证明就绪时保留约 10 秒等待上限，随后仍执行独立只读校验。安装和充电模式保留原等待流程。
初始只读操作使用静态批量工具，工具缺失或失败时退回 BusyBox。
存储节点快照通过 BusyBox 批量解析规范路径，保留每个交接阶段的完整节点和只读状态核对。
兼容 ABL 留下旧备用数组校验值的情况：主表必须有效、主备条目必须完全相同；检查不改写 GPT。
Fastboot 刷写或设置槽位后的首次启动仍可能走完整等待；正常重启已验证可以提前结束。
根系统配置原厂 Wi-Fi MAC 时只等待目标网卡完成 udev 初始化，不等待全局设备队列；
配置成功后才启动 NetworkManager，已通过真机正常重启验证。

## 4. 自定义内核

默认安装目标为保留现有设备适配的 Linux 7.3；安装环境自身的内核独立配置。
迁移范围、已知问题和旧版构建方式见 [7.3 内核说明](KERNEL-7.3.md)；不能直接使用未适配的主线源码替换。

通过 `kernel_fragments` 添加配置片段，例如复制 `build/kernel/configs/custom-zram.config` 后修改。
项目板级配置先应用，自定义片段最后应用；用 `kernel --configure-only` 检查最终结果。
zram 内核选项只提供能力，容量与算法由 rootfs 的服务配置决定。

修改内核代码后提交 Git，并更新 `kernel_localversion`。默认固定基础提交会在构建缓存中自动应用
`build/kernel/source.json` 记录的板级补丁，不改写源码目录。
改用其他提交时，需自行核对并合入 `build/kernel/patches/` 中适用的补丁。
Fedora 目标还叠加 `build/kernel/configs/fedora.config`，启用 SELinux。
Fedora 图形启动构建使用 `CONFIG_BACKLIGHT_KTZ8866=m`、`CONFIG_FB_SIMPLE=y` 和
`CONFIG_SM_DISPCC_8450=m`，按帧缓冲绘制、背光、显示时钟的顺序接手。
未启用 Plymouth 的目标仍使用内置 KTZ8866；独立安装内核使用自己的配置。

### 可选：回收 MTE 预留

当前默认 7.3 设备树已不含这块 512 MiB 预留，**无需设置 `kernel_reclaim_mte`**。
保留 `true` 也不会额外增加可用内存：节点不存在时，清理过程不改动 DTB，日志显示回收 `0 MiB`。

使用仍含该预留的旧版或自定义设备树时，可加入 `"kernel_reclaim_mte": true`，
从目标 boot 的 DTB 移除已核对的预留节点。
该选项不改变 Image、模块或独立 installer；节点地址、大小、属性不符或被引用时构建停止。
仅改变此项时可以保留内核 release，但仍须提高 `package_version` 并安装同批升级包。

此前在 6.17 上，一台 8GB / 256GB、HW16.9.0 设备已通过内存读写、启动、重启及桌面主要功能检查，
可管理内存从约 6.3 GiB 增至约 6.8 GiB。其余 116 MiB 由固件排除，保留原节点；
MPSS 回收试验失败。此结果不覆盖其他批次，因此该选项默认关闭。

### USB 显示名称

`usb_manufacturer`、`usb_product` 同时用于系统引导与安装镜像。
每项为非空文本，可含中文，不含控制字符，最多 126 个 UTF-8 字节。
修改后运行 `devbox run boot`；安装环境也要改名时再运行 `devbox run build-installer`。
安装新引导后生效，仅重装 rootfs 或修改系统主机名不会改变 USB 产品名。

### 根文件系统格式

在安装器中选择 ext4 或 Btrfs，同一份 rootfs 归档可用于两者。
Btrfs 必须在目标内核和安装内核中内置（`CONFIG_BTRFS_FS=y`，不能为模块），
安装 runtime 还需提供 ARM64 Btrfs 工具；两个发行版均安装 `btrfs-progs`。
GUI 根据三个文件的实际能力开放选项，自定义配置关闭 Btrfs 时仅开放 ext4。

Btrfs 使用单设备、元数据 DUP、Zstd 三级压缩，系统在 `native-root` 子卷中，`/home` 位于其中。
不增加 GPT 分区、独立 home 子卷或自动快照策略。
启动检查使用 `ro,rescue=nologreplay,subvolid=5`，避免只读挂载仍重放日志；
见 [Btrfs 挂载说明](https://btrfs.readthedocs.io/en/latest/Administration.html#btrfs-specific-mount-options)。

首次启用须重新构建三个文件。旧镜像的 Btrfs 驱动为模块；
修复后的路径已在本机 256GB 上通过 GUI 安装、桌面启动及正常重启。

## 5. 自定义根文件系统

编辑 `rootfs_config` 指向的配方，完整软件清单见
[Fedora 配方](../build/rootfs/fedora-plasma.json)或 [Ubuntu 配方](../build/rootfs/ubuntu-plasma.json)。

| 字段 | 用途 |
| --- | --- |
| `distribution`、`desktop` | 发行版及桌面，例如 `fedora`、`plasma` |
| `packages` | 额外安装的软件包 |
| `remove` | 裁剪软件；启动必需组件不可移除 |
| `overlay` | 合并到根目录的配置目录，例如 `build/rootfs/plasma-overlay` |
| `first_boot_setup` | `true` 为首次开机创建账号；`false` 为构建时预置账号 |

overlay 只允许普通文件、目录及不离开目标根的相对链接，不可写入运行时挂载目录。
可以扩展现有 overlay，或复制后改为自己的路径。
Fedora 桌面装配会安装弱依赖（`Recommends`、`Supplements`），补齐推荐的插件和集成功能；
`remove` 中的包不作为弱依赖自动引入，装配结束时仍按清单移除。
构建工具、基础运行环境及独立启动运行包保持精简；Ubuntu 配方仍使用 `--no-install-recommends`。
配方省略构建脚本已声明的 `konsole`、`dolphin`、`gdisk`、`e2fsprogs` 和 `glibc-langpack-zh`；
`unzip`、`kio-extras` 由强依赖带入，`kate-plugins` 和 Discover 的 PackageKit／Flatpak 后端由推荐依赖带入。
必需功能继续显式列包，裁剪时须保留所需运行组件；常见依赖包括：

| 功能 | Fedora 组件 |
| --- | --- |
| 登录与网络 | `systemd-pam`、`dbus-daemon`、`iproute`、Wi-Fi 后端及监管数据库 |
| 中文界面 | `glibc-langpack-zh`、Noto CJK 字体、`qt6-qttranslations`；保留软件自身翻译 |
| Kate 与 KRunner | `kate`、`kwrite`（含共用翻译）、`plasma-milou`（搜索 QML 组件） |
| 电池与电源管理 | `upower`、`powerdevil` |
| 移动存储 | `udisks2`、`dosfstools`、`exfatprogs`、`ntfs-3g` |
| 终端与手册 | `procps-ng`（含 `free`）、`ncurses`（含 `clear`）、`man-db`、`man-pages`、`mandoc` |
| 浏览器 | `firefox`、`firefox-langpacks`；中文包使用本地资源 |
| Flatpak | `flatpak`、`plasma-discover-flatpak`；注册系统级 Flathub，不预装应用或放宽全局权限 |

默认还包含 KDE 常用应用、分区工具、KDE Connect、Elisa、Haruna、MPV 和相机组件。
媒体使用 Fedora 官方仓库组件，具体编码和硬件解码能力需分别确认。
SSH 服务端预装但停用；需要时运行 `sudo systemctl enable --now sshd`，主机密钥在平板上生成。
Bash 模板来自 `/etc/skel`，提示符和别名由系统配置提供，构建时检查模板非空。

### 输入法与屏幕键盘

Fedora 保留 Plasma 屏幕键盘和 Fcitx5，移除 IBus 主程序、拼音引擎及重复的 Maliit 键盘。
Qt Virtual Keyboard 是 Plasma 键盘的依赖。`ibus-libs` 可因其他软件依赖保留；
不安装会重新引入 IBus 拼音的 `langpacks-zh_CN` 元包。

Wayland 下，触屏使用 Plasma 键盘及其拼音引擎；需要 Fcitx5 时，在
“系统设置 → 键盘 → 虚拟键盘”切换到 Fcitx 5。它是输入框架启动器，不提供屏幕按键；
两者不能同时占用 KWin 原生输入法接口。

环境配置放在 Plasma 会话和 shell 配置目录：

| 会话 | 输入法变量 |
| --- | --- |
| 共用 | `XMODIFIERS=@im=fcitx`，清除全局 `QT_IM_MODULES` |
| Wayland | 不全局设置 `GTK_IM_MODULE`、`QT_IM_MODULE`、`SDL_IM_MODULE`，保留 KWin 文本输入通道 |
| X11 | 上述三项设为 `fcitx`，自动启动 Fcitx5 |

全局 `QT_IM_MODULES` 会覆盖 Plasma 键盘自身模块，可能导致键盘显示却无法输入或收起。
旧 Qt/XWayland 应用可单独以 `QT_IM_MODULE=fcitx 程序名` 启动，兼容性按应用判断。
参考 [Fcitx 的 Plasma Wayland 接入说明](https://fcitx-im.org/wiki/Using_Fcitx_5_on_Wayland#KDE_Plasma)。

### 首次设置

`first_boot_setup: true` 时不预置普通用户或密码，也不生成 `out/login/` 凭据。
首次开机进入 Plasma Setup，设置语言、网络、时区、设备名和账号，完成后进入登录界面。
Fedora 使用官方 Plasma Setup / Plasma Login，管理员加入 `wheel` 组；
Ubuntu 使用项目打包的 Plasma Setup、SDDM 和 `sudo` 组。
Fedora 使用 Plasma 屏幕键盘，Ubuntu 使用 Maliit。

初始化完成标记为 `/etc/plasma-setup-done`；不要在已有系统上删除它重做初始化。
首次登录后的欢迎中心使用官方包与默认欢迎文案，项目只扩展“设置你的平板”导航页。
可随时关闭，日后从菜单重新打开；显示记录保存在用户的 `~/.config/plasma-welcomerc`。
Fedora 登录管理器默认接键盘横屏、平板模式自动旋转，其保存的显示设置优先于镜像默认值；
这项配置不改写普通用户的桌面显示设置。

需要预置账号时设 `first_boot_setup: false`，填写 `user`，可选 `hostname`、`password_file`。
密码文件须为权限 0600 的单行文件；未提供时生成随机密码，保存到权限为 0600 的
`out/login/<发行版>-plasma-<内容标识>.txt`。旧配方仅包含 `user` 时仍按预置账号处理。

### 项目编译的设备组件

除内核与项目设备包外，主要自编译组件如下：

| 组件 | 构建方式与说明 |
| --- | --- |
| 传感器 | 固定上游源码与补丁，在目标发行版 ARM64 根中编译，使用该发行版运行库 |
| Adreno 监测 | 按 `fedora-stats-source.json` 构建带平台适配的 KSystemStats RPM；官方更高版本可能替换它 |
| 指纹 | 按 `device/fingerprint/oem/SOURCE_LOCK.json` 构建专用 RPM，私有库不替换系统 libfprint；见[指纹说明](FINGERPRINT.md) |
| Ubuntu 初始化向导 | 按 `plasma-setup-source.json` 编译，保留完成状态与失败账号回退补丁；Fedora 直接用官方包 |

默认 Fedora 使用官方 KWin、欢迎中心及其余桌面软件。`device/kwin/` 为研究与回归材料，不参与镜像构建。
设备包提供稳定的传感器服务、HiFi 音频配置和本机校准导入；固件来源与许可见 [CREDITS](../CREDITS.md)。

## 6. 缓存与排障

完成阶段保存在 `out/build/from-source/`，按源码、配置、输入及构建器内容区分并校验后复用。
失败目录为 `.attempt-*`，缺少完成记录的目录不会作为缓存；修复后重新运行原命令即可。
首次 x86 主机上的 ARM64 软件安装与 SELinux 标记需经 QEMU，耗时较长。
重试时保留 `out/`，避免使用 `--refresh`；只有主动更新仓库软件时才用该选项。

根归档优先通过写时复制准备，使用 `pigz` 最多 8 线程压缩，无 `pigz` 时回退到 gzip。
阶段与耗时会输出到终端。基础归档固定 SHA256，APT/DNF 实际包版本另有记录，尚未锁定整套仓库快照。
构建在私有挂载/PID 命名空间中运行，隔离宿主 Nix 工具与库，并阻止目标软件自动启动服务。

Fedora SELinux 标签由 ARM64 目标策略校验后写入归档，保留所有权、ACL、文件能力和扩展属性。
不在宿主文件系统上写入目标标签，因此无需关闭宿主 SELinux 或加载平板策略。

| 现象 | 处理 |
| --- | --- |
| `init-build` 提示配置已存在 | 保留即可，继续 `plan` 和 `build` |
| 输入或源码校验失败 | 核对固定来源与本地修改；保留缓存定位问题，不跳过校验 |
| mkbootimg 缓存被改动 | 工具校验固定 AOSP 源码树；自动忽略并清理已验证模块的字节码，未知源码仍会拒绝 |
| DNF 提示找不到包 | 核对 Fedora 包名，例如 `plasma-milou`、`elisa-player`、`kde-partitionmanager` |
| 构建环境中 UPower 无法连接服务 | 离线装配用 `--help` 检查程序；`upower --version` 需要运行中的服务 |

## 7. 安装与验证

```sh
uv run --project installer installer/install-gui.py --bundle out/releases/fedora-plasma-实际内容标识
```

安装会清空 userdata；备份、确认和恢复准备见 [安装助手](INSTALLER-GUI.md)及[安装指南](FLASHING.md)。
已有系统的日常软件更新使用 DNF/APT，内核更新使用 [配套升级包](KERNEL-UPDATES.md)。

离线验证覆盖来源校验、ARM64 装配、缓存、软件包、SELinux 归档、三文件接口及存储保护。
本机 256GB 已通过 Linux GUI 单根 Btrfs 安装、桌面启动及正常重启。
Windows GUI 安装也已验证成功，范围见 [硬件说明](HARDWARE.md)；其他容量及系统内自动写 boot 仍需单独验收。

基础系统来源：[Fedora Container Base](https://fedoraproject.org/en/misc/#container)、
[Ubuntu Base](https://cdimage.ubuntu.com/ubuntu-base/releases/26.04/release/)。
来源锁分别在 `build/rootfs/fedora-source.json`、`ubuntu-source.json`；
初始化组件见 [KDE Plasma Setup](https://invent.kde.org/plasma/plasma-setup)，
欢迎页扩展使用 [Plasma Welcome QML 接口](https://invent.kde.org/plasma/plasma-welcome/-/blob/Plasma/6.6/README.md)。
