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
```

`build` 默认将配套 boot/rootfs 保存在 `out/build/from-source/`。本地和 CI 均用 `--export-dir` 交付本次构建，目录须尚不存在。导出时先核对 boot/rootfs 的配套标识及完成记录中的文件摘要。要查看以往完成的缓存，可运行 `devbox run outputs`，其中可能包含其他配置的产物。

### 当前构建产物

| 系统导出文件 | 内容 |
| --- | --- |
| `boot.img` | 目标内核、DTB、固件、启动脚本与模块身份。缓存文件名为 `boot-liuqin-native.img` |
| `rootfs.tar.gz` | 官方基础系统、桌面、设备包及同版内核模块 |
| `native-root.contract` | boot 与 rootfs 共用的内核及模块标识 |
| `SHA256SUMS` | 上述三个文件的摘要。在导出目录执行 `sha256sum -c SHA256SUMS` 可复核 |

安装所需的 `recovery.img` 由[原生 Rec 构建入口](NATIVE-RECOVERY.md#构建与交付)独立生成。更换目标内核时可复用已完成的桌面装配，再打包同版模块和根归档。要在已有多系统 Linux 上试用发布镜像，请从未绑定的 `boot.img` 生成[临时启动副本](#已安装系统的临时启动)。

| 命令 | 用途 |
| --- | --- |
| `devbox run build -- --target kernel` | 编译 Image 与 DTB |
| `devbox run build -- --target modules` | 构建同版内核模块 |
| `devbox run build -- --target boot` | 构建引导镜像，不装配桌面 |
| `devbox run build -- --target packages` | 构建 Fedora 内核升级 RPM，见[软件包清单](KERNEL-UPDATES.md#构建升级包) |
| `devbox run build -- --target libcamera` | 单独构建前后摄硬件 ISP 控制与后摄对焦的配套 RPM，无需内核或原厂 ROM |
| `devbox run build -- --target obs-v4l2` | 单独构建 OBS V4L2 插件 RPM，自动补齐 FFmpeg，无需内核或原厂 ROM |
| `devbox run build -- --target kernel --configure-only` | 生成最终 `.config`，不编译 |
| `devbox run build -- --refresh` | 重新装配系统软件阶段，保留旧缓存 |
| `devbox run help` / `devbox run test` | 查看入口说明 / 运行离线检查 |

各构建目标自动补齐依赖。安装导出的配套文件：

```sh
uv run --project installer installer/install-gui.py --bundle out/releases/my-fedora-build
```

Rec 准备、备份与写入范围见[安装助手](INSTALLER-GUI.md)和[安装指南](FLASHING.md)。日常软件更新用 DNF，内核更新用[配套升级包](KERNEL-UPDATES.md)；系统内自动写 boot 默认停用。

### 已安装系统的临时启动

在要测试的已安装多系统 Linux 仍在运行时，确认同批内核模块、设备包和启动契约已就位，将设备上当前的 `/etc/liuqin-boot-target` 原样保存到电脑本次测试的 `out/` 子目录，然后再重启到 Bootloader。每次测试重新读取，默认内核更新后 `boot_generation` 会变化。

在电脑的仓库根目录执行。三个参数依次是本次保存的目标、未绑定的发布镜像和新的测试副本路径。示例使用 Devbox 的 Python 环境，电脑需能运行 Fastboot。[绑定工具](../shared/liuqin_boot_target.py)会保留发布镜像供更新器使用。[镜像解析器](../shared/liuqin_artifacts.py)在启动前读取同一份副本，解析失败、绑定缺失或任一字段不符都会中止。

```sh
devbox run -- env PYTHONPATH=shared python3 - out/boot-test/current-target \
  out/releases/my-fedora-build/boot.img out/boot-test/trial.img <<'PY'
from pathlib import Path
import subprocess
import sys
from liuqin_artifacts import boot_interface
from liuqin_boot_target import bind, parse

saved, release, trial = map(Path, sys.argv[1:])
target = parse(saved.read_bytes())
bind(release, trial, target)
if boot_interface(trial).get("boot_target") != target:
    sys.exit("测试镜像与本次保存的启动目标不一致")
subprocess.run(["fastboot", "boot", str(trial)], check=True)
PY
```

临时验证完成后普通重启，回到已安装的默认内核，再按[内核更新流程](KERNEL-UPDATES.md#已安装系统更新)核对运行内核和 `boot_a`、预览持久更新。

## 配置说明

### 本机构建配置

`build.json` 选择内核与板级输入，完整默认值见 [Devbox 初始化模板](../build/devbox/build.example.json)。所有构建目标都需要 `rootfs_config`，它指定发行版、软件、overlay 和账号初始化方式。

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

[source.json](../build/kernel/source.json) 固定内核版本、基础提交、配置和 `target_patches`，应用这些补丁时，清单必须用 `expected_source_tree` 指定预期的 40 位小写十六进制 Git 树 ID，构建会核对补丁应用后的源码树。补丁顺序以清单为准，不按编号补齐空缺。固定基础提交的补丁应用在构建缓存中，不改写用户源码；使用其他提交须自行合入适用补丁。`tested_source_commit` / `tested_source_tree` 仅是历史验收基线，不能证明当前产物已通过实机测试。

[Rec 内核清单](../build/recovery/kernel/source.json)按同一顺序应用公共补丁，再应用 Rec 专用补丁。仅整理补丁时须保持最终源码树一致。修改源码内容时重新核对目标树。补丁文件或顺序变化都会改变构建身份，须同步 Rec 清单内的补丁摘要和公共清单摘要。用 `python3 tests/recovery/recovery-source.py` 检查两份清单。各项改动的原厂依据和实测范围见[硬件适配清单](HARDWARE.md)。

修改源码后先提交，再更换 `kernel_localversion`。用 `--target kernel --configure-only` 检查自定义片段；zram 选项只提供内核能力，容量和算法由 rootfs 服务配置。Fedora 额外应用 `fedora.config` 启用 SELinux，并将 `ath11k` / `ath11k_pci` 编为模块；Rec 保留原有内置配置。

### 启动与设备适配

目标引导使用 [native-bootargs.txt](../device/native-bootargs.txt) 和 [native-display.config](../build/kernel/configs/native-display.config)，四档刷新率默认配置见[显示文档](DISPLAY.md#默认配置)。原生 Rec 同样使用 Linux 7.3.4 公共源码和补丁，配置、专用补丁与构建产物独立，见 [Rec 内核配方](../build/recovery/kernel/README.md)。

- **图形启动**：Fedora 启用 Plymouth 时装配独立启动运行包。运行包的 `runtime.json` 必须用布尔值 `firmware_splash` 声明是否支持固件帧缓冲交接。默认配方应用 `firmware-splash.config`，使用 `CONFIG_BACKLIGHT_KTZ8866=m`、`CONFIG_FB_SIMPLE=y`、`CONFIG_SM_DISPCC_8450=m`，由固件帧缓冲过渡到原生 DRM；未启用 Plymouth 的目标仍内置 KTZ8866。动画准备与 UFS 校验并行，在最终存储证明前等待准备结束；动画失败不跳过存储检查。
- **压缩与驱动等待**：目标启用 `CONFIG_RD_ZSTD=y` 时用 Zstd，否则用 gzip；独立安装环境用 gzip。`deferred_probe_timeout=60` 允许等待根系统模块，依赖就绪即继续，不固定等待 60 秒。
- **存储保护**：两轮 UFS、双份 GPT 和分区节点证明一致后提前结束等待；就绪证明未通过时，保留约 10 秒的有界等待及后续独立只读检查。桌面沿用 Rec 对原厂 LUN 3/5 自指备份头的有限兼容，仍核对 GPT 校验和、主备内容及 sysfs 几何，不写盘修表。持久化桌面仅封存内部 UFS，外接磁盘保持原权限；安装、充电和失败回退封存全部 SCSI 设备。
- **配套核验**：boot/rootfs 的 `LIUQIN_DESKTOP_UFS_ONLY_V2` 策略与设备支持包须匹配；新根交接前后重验节点身份，失败则拒绝桌面启动，不写盘修表。
- **只读复查**：`liuqin-storage-seal --verify-flags` 在每个原有检查点重新打开完整节点快照，批量读取 `BLKROGET`，核对设备号、节点身份、UFS 归属和前后命名空间。写保护操作、读写挂载前后、移动内核文件系统前及 `switch_root` 前的独立复查均保留，桌面 guard 同样使用批量复查；失败仍进入原有拒绝或回退路径，不沿用先前的校验结果。
- **网络**：Fedora 由 [早期模块列表](../device/board-overlay/etc/modules-load.d/liuqin-wlan.conf) 加载 WLAN 电源控制和 `ath11k_pci`，与 RTC 采样并行，避免固件启动阻塞进入 `/init`。须提供同版根系统模块；仅依赖稍后的 udev 自动加载会把等待转移到网络准入。设置本机 Wi-Fi MAC 时仍等待目标网卡完成 udev 初始化、核对并设置原厂身份，再启动 NetworkManager；原有失败处理和用户会话顺序保留。桌面不启动诊断 USB 网络；RAM 故障救援与 Rec ADB 分开维护。

登录设备等待、PowerDevil 探测范围和启动耗时对照见[显示与登录](DISPLAY.md#登录与会话启动)；
引导写入、回退与系统内更新器的验收限制见[内核更新](KERNEL-UPDATES.md)。

Fedora 原生 boot 的充电环境使用目标系统的 ARM64 Python、服务所需扩展及其动态库。
标准构建会自动准备该环境。启用 Plymouth 时两者来自同一次 Fedora 软件包事务。
单独调用 `build-liuqin-native-boot.sh` 时，`CHARGER_RUNTIME` 指向同版构建收集的充电目录，
`KERNEL_MODULES_DIR` 指向对应内核的模块产物。默认 Fedora 将温控依赖的 SPMI ADC5 内置。
自定义配置编为模块时，构建会从已校验的模块包中取出 ADC 及其依赖。[运行环境收集器](../build/lib/charger_runtime.py) · [关机充电](FASTCHARGE.md#关机充电)

### 根文件系统格式

同一 rootfs 归档支持安装为 ext4 或 Btrfs。Btrfs 要求目标内核和 Rec 均内置 `CONFIG_BTRFS_FS=y`、安装 runtime 提供 ARM64 Btrfs 工具；Fedora 配方安装 `btrfs-progs`。GUI 核对实际能力，不支持时只开放 ext4；更改支持范围须重建配套 boot/rootfs 并使用兼容 Rec。

Btrfs 使用单设备、元数据 DUP、Zstd 三级压缩和 `native-root` 子卷，`/home` 位于其中，不自动创建快照。启动检查使用 `ro,rescue=nologreplay,subvolid=5` 避免日志重放。安装、桌面启动与正常重启已验证。

### rootfs 软件与 overlay

完整软件清单见 [Fedora 配方](../build/rootfs/fedora-plasma.json)。

图形使用 Fedora 官方 Mesa Freedreno/Turnip；配方包含 Rusticl OpenCL、ICD 加载器和
`clinfo`。选用 `mesa-libOpenCL` 时，构建会校验固定的 Mesa libclc 发布文件并打包
兼容的 SPIR-V RPM，不重新编译 Mesa 或 libclc。该包的路径与依赖限定于锁定的 Fedora
LLVM/libclc 基线，升级基线时需重新核对。

默认配方还包含 `ffmpeg-free`，会在独立 Fedora ARM64 构建根中重建固定的官方 SRPM，
应用 Iris 的输入对齐、解码裁剪及编码缓冲区修复。镜像只安装八个运行库包与 DNF5
版本锁工具，并保留对应的修订版源码包；不会带入 FFmpeg 编译依赖。
首次构建会增加编译耗时，后续复用独立缓存；修改来源、补丁或脚本会使缓存失效。
整组运行库默认锁定，升级需审查新版、验证后更新锁；这也会暂缓该组安全更新。
构建、解锁与恢复官方包的步骤见 [Fedora FFmpeg 配方](../build/ffmpeg/README.md)，
硬件验证见 [视频接口](VIDEO.md)。

默认配方也安装官方 `obs-studio`，自动构建并安装独立的 H.264/H.265 V4L2 编码器
RPM。插件使用同次 FFmpeg 构建的开发包，在独立 ARM64 根中编译、运行包适配测试，
并检查真实共享库/API；开发包不进入桌面镜像。它不覆盖或锁定官方 OBS。
单独构建、安装与缓存规则见 [OBS 插件](../build/obs-v4l2/README.md)。

同时提供 [Firefox 系统 FFmpeg 启动适配](../build/firefox/README.md)：默认配方安装
Bubblewrap，选用修复 FFmpeg 时添加 `/usr/local` 下的独立入口与兼容名单。
不重建或锁定 Firefox，不改写官方 RPM 文件；未知版本自动回到原版媒体库，
VP9 会暂时使用软件解码。已有 overlay 的本地启动器保留，维护兼容名单后重新装配。

板级 overlay 为用户会话和登录 shell 设置 `IR3_SHADER_DEBUG=noearlypreamble` 及独立
缓存目录，规避 A730 的跨步复制错误。此设置也影响继承环境的 IR3 图形应用；已有系统
安装配置后需重启应用或重新登录。安装、更新维护与计算检查见 [GPU 与 OpenCL](GPU.md)。

| 字段 | 用途 |
| --- | --- |
| `distribution`、`desktop` | 发行版与桌面，如 `fedora`、`plasma` |
| `packages` / `remove` | 额外安装 / 裁剪软件；保留启动及设备运行依赖 |
| `overlay` | 合并到根目录的配置目录，只允许普通文件、目录和不离开目标根的相对链接，不可写运行时挂载目录 |
| `first_boot_setup` | 必填布尔值。选 `true` 在首次开机时创建账号，选 `false` 在构建时预置账号 |

Fedora 桌面安装推荐依赖，`remove` 清单阻止相应弱依赖引入并在装配结束时移除。裁剪时保留登录、网络、电源管理、存储和所需语言组件。SSH 服务端预装但停用，需要时在平板执行 `sudo systemctl enable --now sshd`，主机密钥在设备上生成。

传感器组件使用固定源码与补丁，在目标发行版中编译。Adreno 监测使用官方 `ksystemstats` 加独立 [GPU 插件](../device/ksystemstats/README.md)，Qt/libksysguard ABI 更新后须复测。[指纹组件](FINGERPRINT.md)使用独立来源锁和私有库。Fedora 使用官方 KWin 和欢迎中心，[KWin 排查补丁](../device/kwin/README.md)仅供单独复核。固件来源与许可见 [CREDITS](../CREDITS.md)。

### 首次设置与输入法

- `first_boot_setup: true`：不预置普通用户或密码，也不生成 `out/login/` 凭据。开机由 Plasma Setup 设置语言、网络、时区、设备名和账号，完成标记为 `/etc/plasma-setup-done`，不要在已有系统上删除此标记重做初始化。
- `first_boot_setup: false`：填写 `user`，可选 `hostname`、`password_file`。密码文件须为权限 0600 的单行文件；省略时生成随机密码，存入权限 0600 的 `out/login/<发行版>-plasma-<内容标识>.txt`。
- 首次设置期间合盖只熄屏，开盖可继续向导。进入新账号后，合盖默认锁屏并熄屏，用户可在电源设置中调整。
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
