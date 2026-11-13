# 构建指南

[项目首页](../README.md) ｜ [安装助手](INSTALLER-GUI.md) ｜ [内核更新](KERNEL-UPDATES.md)

默认构建 Linux 7.3.4 + Fedora 45 KDE Plasma 的配套 boot/rootfs。安装环境使用独立的原生 ADB Rec。硬件实现与验收范围见[内核适配总清单](HARDWARE.md)。

## 构建步骤

### 准备环境

在 Linux 主机安装 Devbox/Nix，以普通用户进入项目根目录：

```sh
devbox install
devbox run doctor
devbox run init
```

Devbox 提供交叉编译器、QEMU、镜像和打包工具。完整构建还需要网络、Linux 6.7+、Podman、`newuidmap` / `newgidmap`，以及当前用户至少 65535 个从属 UID/GID。默认 rootless 构建使用独立的用户、挂载、PID 和 binfmt_misc 命名空间，保留目标所有权、ACL 和文件能力，不改变宿主 binfmt 注册。

`init` 创建不提交 Git 的本机配置 `build.json`，模板见 [build.example.json](../build/devbox/build.example.json)。已有有效配置会保留；无效配置报错且不覆盖。编辑输入路径后继续，字段见[配置说明](#配置说明)。相对路径均从项目根目录解析。

需要另一份配置时运行 `devbox run init -- --config build.new.json`，后续命令也传入 `--config build.new.json`。脚本参数放在 Devbox 的 `--` 之后。

### 准备原厂资料

```sh
devbox run fetch-rom
```

下载并校验 **OS2.0.6.0.VMYCNXM Fastboot ROM**，只提取 `vendor_boot.img`、`dtbo.img`、`super.img`、`NON-HLOS.bin` 到 `inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images`。已有完整包可复用：

```sh
devbox run fetch-rom -- --archive /path/to/liuqin-fastboot-rom.tgz
```

也可将 `rom_images` 指向已解压的同版 `images` 目录。来源与完整包校验值见 [stock-rom-source.json](../build/data/stock-rom-source.json)，单份镜像校验值见 [liuqin-rom-images.json](../shared/liuqin-rom-images.json)；Recovery ZIP 或其他版本不能替代这些构建输入。下载包保存在 `inputs/downloads/stock-rom/`，损坏缓存会保留并停止。

原厂资料提供硬件适配资源，操作系统来自官方 Fedora；本机地址和校准数据在安装时导入。构建不连接平板，也不执行 ROM 刷机脚本。

### 构建与交付

```sh
devbox run plan
devbox run build -- --export-dir out/releases/my-fedora-build
devbox run outputs
```

`build` 默认生成配套 boot/rootfs；不带 `--export-dir` 时只保存在 `out/build/from-source/`。导出目录必须尚不存在，包含 `boot.img`、`rootfs.tar.gz`、`native-root.contract` 和 `SHA256SUMS`。核验配套标识与完成记录摘要后才发布目录，不附带 Rec、构建配置或独立登录信息；CI 使用同一入口。

`outputs` 只列出已完成缓存，不联网、不构建，也不保证对应当前配置。交付本次构建应使用 `--export-dir`。

### 当前构建产物

| 产物 | 内容 |
| --- | --- |
| `boot-liuqin-native.img`（导出为 `boot.img`） | 目标内核、DTB、固件、启动脚本与模块身份 |
| `rootfs.tar.gz` | 官方基础系统、桌面、设备包及同版内核模块 |
| 原生 `recovery.img` | 独立内核、运行载荷、UI、触控与 ADB；使用[原生 Rec 构建入口](NATIVE-RECOVERY.md#构建与交付) |

目标 boot 与 rootfs 模块必须配套。更换内核时可复用已完成的桌面装配，再打包根归档。

对已有系统使用 Fastboot 临时启动前，还需沿用安装时的启动目标绑定：分区与文件系统 UUID、
`native-root` 路径及 `boot_generation`。构建输出是未绑定的发布镜像；先用
[`liuqin_boot_target.bind`](../shared/liuqin_boot_target.py) 和已核实的现有绑定生成测试副本，
并核对新版本模块及启动契约。仅复制新模块不会自动给发布镜像补上目标绑定。
默认内核更新会生成新的 `boot_generation`；更新后再次临时启动须重新读取当前目标并绑定，
不能复用更新前的测试镜像。完整流程见[已安装系统更新](KERNEL-UPDATES.md#已安装系统更新)。

| 命令 | 用途 |
| --- | --- |
| `devbox run build -- --target kernel` | 编译 Image 与 DTB |
| `devbox run build -- --target modules` | 构建同版内核模块 |
| `devbox run build -- --target boot` | 构建引导镜像，不装配桌面 |
| `devbox run build -- --target packages` | 构建配套 RPM/DEB 内核升级包 |
| `devbox run build -- --target kernel --configure-only` | 生成最终 `.config`，不编译 |
| `devbox run build -- --refresh` | 重新装配系统软件阶段，保留旧缓存 |
| `devbox run help` / `devbox run test` | 查看入口说明 / 运行离线检查 |

各构建目标自动补齐依赖。安装导出的配套文件：

```sh
uv run --project installer installer/install-gui.py --bundle out/releases/my-fedora-build
```

Rec 准备、备份与写入范围见[安装助手](INSTALLER-GUI.md)和[安装指南](FLASHING.md)。日常软件更新用 DNF，内核更新用[配套升级包](KERNEL-UPDATES.md)；系统内自动写 boot 默认停用。

## 配置说明

### 本机构建配置

`build.json` 选择内核与板级输入，`rootfs_config` 指向的软件配方选择发行版、应用、overlay 和账号初始化方式。所有构建目标均要求 `rootfs_config`，过时字段会报错。

```json
{
  "kernel_source": "sources/linux-liuqin-7.3.4",
  "kernel_fragments": ["build/kernel/configs/custom-zram.config"],
  "kernel_localversion": "-liuqin-fedora-v734",
  "rootfs_config": "build/rootfs/fedora-plasma.json",
  "usb_manufacturer": "Xiaomi",
  "usb_product": "Pad 6 Pro",
  "rom_images": "inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images",
  "package_version": "0.3.20"
}
```

| 配置项 | 说明 |
| --- | --- |
| `kernel_source` | 内核 Git 目录；不存在时获取来源锁中的固定提交。已有源码须为干净的 Git 提交 |
| `kernel_source_lock` | 可选，默认 `build/kernel/source.json`；锁定目标内核，与 Rec 来源分开维护 |
| `kernel_fragments` | 自定义配置片段，在项目板级配置之后应用 |
| `kernel_localversion` | 内核版本后缀；修改源码或配置后更换，避免同名内核对应不同模块 |
| `rootfs_config` | `build/rootfs/fedora-plasma.json`，或基于它定制的 Fedora Plasma 配方 |
| `usb_manufacturer`、`usb_product` | 目标引导 USB 救援模式的显示名称，默认 `Xiaomi`、`Pad 6 Pro` |
| `rom_images` | 指定版本原厂 ROM 的 `images` 目录 |
| `package_version` | 内核升级包版本，每次发布提高 |

升级默认内核时同步更换 `kernel_source` 路径，避免旧目录被当成自定义源码；`init` 不迁移已有配置。USB 名称须为非空文本、不含控制字符、最多 126 个 UTF-8 字节，重建并安装 boot 后生效；Rec 的 USB 身份独立配置。

### 发行版与桌面配方

| 配方 | 软件包与首次设置 | 登录与管理员组 |
| --- | --- | --- |
| Fedora 45 Plasma | DNF/RPM、官方 Plasma Setup | Plasma Login、`wheel` |

当前只提供 Fedora Plasma 桌面配方，ARM64 用户态组件也在 Fedora 环境构建。Rec 的 Python、BusyBox、分区及文件系统工具由独立的 [Buildroot + musl 配方](../build/runtime/README.md) 从源码构建。Fedora initramfs 和板级包的静态 BusyBox 也由[固定上游源码](../build/busybox/README.md)交叉编译；指纹库直接取上游归档，版本和 SHA256 保持不变。rootfs 须提供 systemd、设备服务、`fastrpc` 账号/组及同版内核模块和元数据。新增发行版需固定官方 ARM64 基础系统，并适配包管理、账号、服务和登录流程；板级与桌面文件边界见 [overlay](STRUCTURE.md#板级与桌面-overlay)。

### 内核与启动配置

[source.json](../build/kernel/source.json) 固定内核版本、基础提交、配置和 `target_patches`，构建核对 `expected_source_tree`。补丁顺序以清单为准，不按编号补齐空缺。固定基础提交的补丁应用在构建缓存中，不改写用户源码；使用其他提交须自行合入适用补丁。`tested_source_commit` / `tested_source_tree` 仅是历史验收基线，不能证明当前产物已通过实机测试。

Fedora 与 Rec 共用 50 个补丁；TDDI 首帧唤醒与校准恢复集中在 `0011`，DSI 命令调度、四档模式与在线切换集中在 `0024`，笔精度、报文诊断与唤醒故障隔离集中在 `0031`，`0035` 使用原厂 Cape 的 CPU 能力描述区分大小核，`0036` 补齐 CPU 功耗系数和两级带宽监测，验证范围见[设备树对照](DEVICE-TREE.md)。`0037` 补入原厂 M81 电量计刷新请求，见[充电](FASTCHARGE.md#电量刷新)；`0038` 对齐 M81 功率属性和厂商状态事件；`0039` 导出当前 Type-C 对端的供电能力，供自动认证区分纯充电来源与 USB 数据设备；`0040` 修正 UCSI 的 PPS 协商电压、电流和合同变化通知；`0041` 同步认证后变化的 Type-C 供电能力目录；`0042` 修正 UCSI 注册失败标志与注销后的连接器指针；`0043` 补齐固件恢复后的通知订阅与 UCSI 初始化重试、代次隔离及清理。`0044` 校验 UCSI GLINK 读写应答的完整长度。`0045` 按 Type-C 分配限制 DP 通道数。`0046` 单独处理 AUX 空闲时已确认的 DP PHY PLL 失锁中断。`0047` 连接 DP0 音频后端与显示控制器。`0048` 在停流前同步关闭 DPCM 音频后端，并在 DPU 首次显示提交完成后重配、启动仍然打开的播放流；CPU DAI 关闭 DSP 后，component close 再关闭 DP 音频引擎，独立于物理插拔状态。`0049` 补齐四路模拟麦克风接线与 TX 生命周期，`0050` 允许未接外屏时探测其播放前端，`0051` 保留四路原始录音的连续 DSP 通道标识，`0052` 对齐 WCD938x 的 TX 中断归属并限制异常处理循环，`0053` 在 SoundWire 关时钟前停用并等待 IRQ，`0054` 为 SoundWire 1.7 配对 CPU1 命令 FIFO 与中断；`0055` 在 ADSP 传输暂时中断时保留 M81 最近有效电量，`0056` 补齐整卡睡眠、播放队列重置和 ADSP 音频服务恢复，`0057` 修正多路 ADC 共用时钟、高通滤波时序及异常离线收尾，`0058` 在已确认离线时回收音频图，见[音频](AUDIO.md)。`0059` 在 DPU 关闭显示管线前清除旧帧的待提交状态，见[显示](DISPLAY.md)；`0060` 持续处理磁吸键盘清醒期间的状态中断，并保留睡眠唤醒开关，见[硬件](HARDWARE.md#键盘状态中断与睡眠)。

`0061` 对齐 SM8475 原厂 GPU 电源档位，`0062` 恢复 A730 图像地址相关的 SP/TPL1
非上下文寄存器，`0063` 将 Liuqin A730 的挂起检查周期设为 5000 ms，给长计算保留
更长预算。该策略同时影响图形与计算的超时恢复，作用范围和代价见
[GPU 与 OpenCL](GPU.md#大规模计算的超时预算)。

合并补丁时须保持最终源码树一致，并同步 Rec 锁中的补丁摘要及公共源码锁摘要。
补丁文件或顺序改变会更新构建身份。

修改源码后先提交，再更换 `kernel_localversion`。用 `--target kernel --configure-only` 检查自定义片段；zram 选项只提供内核能力，容量和算法由 rootfs 服务配置。Fedora 额外应用 `fedora.config` 启用 SELinux，并将 `ath11k` / `ath11k_pci` 编为模块；Rec 保留原有内置配置。

### 启动与设备适配

目标引导使用 [native-bootargs.txt](../device/native-bootargs.txt) 和 [native-display.config](../build/kernel/configs/native-display.config)，四档刷新率默认配置见[显示文档](DISPLAY.md#默认配置)。原生 Rec 同样使用 Linux 7.3.4 公共源码和补丁，配置、专用补丁与构建产物独立，见 [Rec 内核配方](../build/recovery/kernel/README.md)。

- **图形启动**：Fedora 启用 Plymouth 时装配独立启动运行包，应用 `firmware-splash.config`，使用 `CONFIG_BACKLIGHT_KTZ8866=m`、`CONFIG_FB_SIMPLE=y`、`CONFIG_SM_DISPCC_8450=m`，由固件帧缓冲过渡到原生 DRM；未启用 Plymouth 的目标仍内置 KTZ8866。动画准备与 UFS 校验并行，在最终存储证明前等待准备结束；动画失败不跳过存储检查。
- **压缩与驱动等待**：目标启用 `CONFIG_RD_ZSTD=y` 时用 Zstd，否则用 gzip；独立安装环境用 gzip。`deferred_probe_timeout=60` 允许等待根系统模块，依赖就绪即继续，不固定等待 60 秒。
- **存储保护**：两轮 UFS、双份 GPT 和分区节点证明一致后提前结束等待；就绪证明未通过时，保留约 10 秒的有界等待及后续独立只读检查。桌面沿用 Rec 对原厂 LUN 3/5 自指备份头的有限兼容，仍核对 GPT 校验和、主备内容及 sysfs 几何，不写盘修表。持久化桌面仅封存内部 UFS，外接磁盘保持原权限；安装、充电和失败回退封存全部 SCSI 设备。
- **配套核验**：boot/rootfs 的 `LIUQIN_DESKTOP_UFS_ONLY_V1` 策略与设备支持包须匹配；新根交接前后重验节点身份，失败则拒绝桌面启动，不写盘修表。
- **只读复查**：`liuqin-storage-seal --verify-flags` 在每个原有检查点重新打开完整节点快照，批量读取 `BLKROGET`，核对设备号、节点身份、UFS 归属和前后命名空间。写保护操作、读写挂载前后、移动内核文件系统前及 `switch_root` 前的独立复查均保留，桌面 guard 同样使用批量复查；失败仍进入原有拒绝或回退路径，不沿用先前的校验结果。
- **网络**：Fedora 由 [早期模块列表](../device/board-overlay/etc/modules-load.d/liuqin-wlan.conf) 加载 WLAN 电源控制和 `ath11k_pci`，与 RTC 采样并行，避免固件启动阻塞进入 `/init`。须提供同版根系统模块；仅依赖稍后的 udev 自动加载会把等待转移到网络准入。设置本机 Wi-Fi MAC 时仍等待目标网卡完成 udev 初始化、核对并设置原厂身份，再启动 NetworkManager；原有失败处理和用户会话顺序保留。桌面不启动诊断 USB 网络；RAM 故障救援与 Rec ADB 分开维护。

登录设备等待、PowerDevil 探测范围和启动耗时对照见[显示与登录](DISPLAY.md#登录与会话启动)；
引导写入、回退与系统内更新器的验收限制见[内核更新](KERNEL-UPDATES.md)。

### 根文件系统格式

同一 rootfs 归档支持安装为 ext4 或 Btrfs。Btrfs 要求目标内核和 Rec 均内置 `CONFIG_BTRFS_FS=y`、安装 runtime 提供 ARM64 Btrfs 工具；Fedora 配方安装 `btrfs-progs`。GUI 核对实际能力，不支持时只开放 ext4；更改支持范围须重建配套 boot/rootfs 并使用兼容 Rec。

Btrfs 使用单设备、元数据 DUP、Zstd 三级压缩和 `native-root` 子卷，`/home` 位于其中，不自动创建快照。启动检查使用 `ro,rescue=nologreplay,subvolid=5` 避免日志重放。安装、桌面启动与正常重启已验证。

### rootfs 软件与 overlay

完整软件清单见 [Fedora 配方](../build/rootfs/fedora-plasma.json)。

图形使用 Fedora 官方 Mesa Freedreno/Turnip；配方包含 Rusticl OpenCL、ICD 加载器和
`clinfo`。选用 `mesa-libOpenCL` 时，构建会校验固定的 Mesa libclc 发布文件并打包
兼容的 SPIR-V RPM，不重新编译 Mesa 或 libclc。该包的路径与依赖限定于锁定的 Fedora
LLVM/libclc 基线，升级基线时需重新核对。

板级 overlay 为用户会话和登录 shell 设置 `IR3_SHADER_DEBUG=noearlypreamble` 及独立
缓存目录，规避 A730 的跨步复制错误。此设置也影响继承环境的 IR3 图形应用；已有系统
安装配置后需重启应用或重新登录。安装、更新维护与计算检查见 [GPU 与 OpenCL](GPU.md)。

| 字段 | 用途 |
| --- | --- |
| `distribution`、`desktop` | 发行版与桌面，如 `fedora`、`plasma` |
| `packages` / `remove` | 额外安装 / 裁剪软件；保留启动及设备运行依赖 |
| `overlay` | 合并到根目录的配置目录，只允许普通文件、目录和不离开目标根的相对链接，不可写运行时挂载目录 |
| `first_boot_setup` | `true` 首次开机创建账号；`false` 构建时预置账号 |

Fedora 桌面安装推荐依赖，`remove` 清单阻止相应弱依赖引入并在装配结束时移除。裁剪时保留登录、网络、电源管理、存储和所需语言组件。SSH 服务端预装但停用，需要时在平板执行 `sudo systemctl enable --now sshd`，主机密钥在设备上生成。

传感器组件使用固定上游源码与补丁，在目标发行版中编译；Adreno 监测使用官方 `ksystemstats` 加独立 [GPU 插件](../device/ksystemstats/README.md)，Qt/libksysguard ABI 更新后仍须验证；[指纹组件](FINGERPRINT.md)按独立来源锁构建，私有库不替换系统 libfprint。Fedora 使用官方 KWin 和欢迎中心，`device/kwin/` 不参与构建；固件来源与许可见 [CREDITS](../CREDITS.md)。

### 首次设置与输入法

- `first_boot_setup: true`：不预置普通用户或密码，也不生成 `out/login/` 凭据。开机由 Plasma Setup 设置语言、网络、时区、设备名和账号，完成标记为 `/etc/plasma-setup-done`，不要在已有系统上删除此标记重做初始化。
- `first_boot_setup: false`：填写 `user`，可选 `hostname`、`password_file`。密码文件须为权限 0600 的单行文件；省略时生成随机密码，存入权限 0600 的 `out/login/<发行版>-plasma-<内容标识>.txt`。旧配方仅含 `user` 时仍按预置账号处理。
- 默认使用 Plasma 屏幕键盘，保留 Fcitx5、移除重复的 IBus/Maliit 主程序；不要用 `langpacks-zh_CN` 元包重新引入 IBus 拼音。
- Wayland 可在“系统设置 → 键盘 → 虚拟键盘”切换 Plasma 键盘与 Fcitx 5；后者是输入框架启动器，不提供屏幕按键。两者不能同时占用 KWin 输入法接口。
- 共用 `XMODIFIERS=@im=fcitx`，清除全局 `QT_IM_MODULES`；Wayland 不全局设置 `GTK_IM_MODULE`、`QT_IM_MODULE`、`SDL_IM_MODULE`，X11 将三项设为 `fcitx` 并自动启动 Fcitx5。旧 Qt/XWayland 应用可单独用 `QT_IM_MODULE=fcitx 程序名`。

## 已知问题

- 基础系统归档固定 SHA256，但 DNF 仓库尚未整体锁定快照；实际软件版本记录在 `packages.tsv`，GPU 插件构建依赖另见 `stats-packages.tsv`。相同配方重新装配可能得到不同包版本。
- rootless 依赖不足或仅有单 UID 映射时构建停止，不自动转为 sudo。已有 rootful 环境可显式设置 `LIUQIN_ROOTFS_MODE=sudo`；旧缓存所有者不会随模式切换而改变。
- 构建和离线检查通过不等于新内核完成硬件验收；实机范围与待解决项见[适配清单](HARDWARE.md)。

## 排查方法

### 配置、输入与缓存

先检查实际使用的配置及输入；采用其他配置文件时替换下列 `--config` 路径：

```sh
devbox run -- python3 build/devbox/build.py show-config --config build.json
devbox run -- python3 build/devbox/build.py check-inputs --config build.json
```

`prepare` 可预先准备依赖。修改 Devbox 工具后重新执行 `devbox install` 和 `devbox run doctor`。离线检查入口与覆盖范围见[测试说明](../tests/README.md)。

- 完成阶段位于 `out/build/from-source/`，按源码、配置、输入及构建器内容区分并校验复用；`.attempt-*` 或缺少完成记录的目录不会作为缓存。
- 失败后修复原因并重跑原命令，保留 `out/`；只在主动更新仓库软件时用 `--refresh`。x86 主机上的 ARM64 软件安装与 SELinux 标记需经 QEMU，首次耗时较长。
- 清理按完整阶段删除，不只移除部分产物；保留配套 boot/rootfs、内核升级包、Rec/回刷基线及设备外备份。
- Fedora SELinux 标签由目标策略校验后写入归档，无需关闭宿主 SELinux 或加载平板策略；装配隔离宿主工具与库，并阻止目标服务自动启动。

### 常见故障

| 现象 | 检查与处理 |
| --- | --- |
| `init` 提示配置已存在 | 有效配置会保留，继续 `plan`；无效配置先修正，或用 `--config` 新建 |
| rootless 映射或命名空间失败 | 检查 Linux 6.7+、Podman、`newuidmap` / `newgidmap` 和 subuid/subgid 范围，不使用单 UID 映射绕过 |
| Fedora ARM64 `systemctl` 崩溃 | 重新安装 Devbox 工具并运行 `doctor`；静态 QEMU 已包含 [64 位挂载标识 ABI 修复](https://github.com/qemu/qemu/commit/3b68c2db2392eadfa89c64647e96a69d8490477a)，装配会检查目标工具并在失败时停止 |
| 基础归档下载 `The read operation timed out` | 核对日志中的下载地址。Fedora 默认清华镜像，官方地址在 [fedora-source.json](../build/rootfs/fedora-source.json) 的 `upstream_url`；可换为可达 HTTPS 镜像，保留 `sha256`，重试原命令 |
| 输入或源码校验失败 | 核对来源锁与本地修改，保留缓存定位问题，不跳过校验 |
| mkbootimg 缓存被改动 | 核对固定 AOSP 源码树；已验证模块的字节码会自动清理，未知源码改动仍被拒绝 |
| DNF 找不到包 | 核对 Fedora 包名与配方，如 `plasma-milou`、`elisa-player`、`kde-partitionmanager` |
| 离线装配中 UPower 无法连接服务 | 用 `upower --help` 检查程序；`--version` 需要运行中的服务 |
| 屏幕键盘显示但无法输入或收起 | 检查全局 `QT_IM_MODULES` 及 Wayland 输入法变量，避免覆盖 Plasma 键盘自身模块 |
