# 测试说明

[参与贡献](../CONTRIBUTING.md) ｜ [构建指南](../docs/BUILD.md) ｜ [硬件支持与限制](../docs/HARDWARE.md)

在仓库根目录使用 Devbox，避免宿主编译器、BusyBox 或 Python 依赖不一致：

```sh
devbox run test
devbox run -- sh tests/persistent-root.sh
devbox run -- python3 tests/docs-links.py
```

`devbox run test` 覆盖构建、安装器、引导更新、存储保护、显示服务等离线回归。
测试使用临时目录、合成镜像或模拟设备，不刷写平板；QML、源码阶段验证和真机验收另行执行。
CI 的实际测试列表以 `.github/workflows/` 为准，不能把本地入口通过等同于全部 CI 作业通过。

`fedora-stats.py` 用真实 ARM64 共享库生成 GPU 独立 RPM，检查文件清单只包含私有插件、
服务 drop-in 和许可/源码记录，不占用官方守护进程或标准插件路径；同时检查共享库与 Qt/KDE
版本依赖、错误架构和符号链接拒绝，以及构建流程的独立包产物路由。需要 ARM64 C 编译器、
RPM 工具和 pyelftools，已接入 Devbox 测试与 CI。
`adreno-stats.py` 另用 Qt6Core 编译真实 sysfs 读取函数，并在固定源码归档存在时验证补丁。
这两项离线检查不代替设备上的插件加载、实际 GPU 负载和睡眠恢复测试。

`device-overlays.py` 检查板级/GNOME 层的组合、缺失层、同名及文件/目录冲突、已有目标文件和父路径保护，
并使用合成编译输入构建、解包 GNOME DEB，核对两层覆盖变量、schema、依赖和维护脚本。
它不验证 GNOME 控制中心或 ARM64 辅助程序的编译；`source-build.py` 另覆盖通用 DEB
无需 GNOME 输入，以及 Fedora/Ubuntu 通用包的板级内容和装配代码缓存失效。

`nanosic-folio.py` 按默认构建配方重建驱动并执行实际 C 函数，以模拟 GPIO/HID 检查
连接、背折与合盖组合、修饰键/鼠标/三触点释放、屏蔽期间的状态报告、恢复采样和异常帧。
使用 ASan/UBSan；`--source /path/to/kernel` 可检查准备好的内核源码。
这不替代真实 HID/libinput、霍尔电平和 Plasma 合盖动作的设备验收。

`fingerprint-release.py` 以合成 ROM 和模拟 `mcopy` 检查默认 Fedora rootfs 的指纹固件装配：
十个文件进入归档、root/0600 权限、首次启动复用缓存，以及原厂镜像或分段损坏时停止。
测试不需要原厂固件或平板；与其余 root 文件所有权测试一样，在 root 或映射为 root 的隔离环境执行。

## 显示与启动回归

| 测试 | 检查范围 |
| --- | --- |
| `display-clock.py` | 补丁中的时钟取整、频率边界和 CCF 约束 |
| `display-refresh-rates.py` | 从默认 7.3 配方重建并执行面板模式枚举；默认 90/120/144 Hz、诊断开关后的七档时序、唯一首选、机型/单链路回退、分配失败、扫描命令失败不重放及命令扫描行边界；可追加主机时钟及固定 ROM 检查 |
| `display-brightness-minimum.py` | 两个内核配方的最低非零亮度、原厂曲线其余档位、直接写零与关屏后的恢复 |
| `display-policy.py`、`display-recovery.py` | CABC 配置、活动会话、恢复限流、失败与退出处理 |
| `backlight-default.py`、`boot-splash.py` | 亮度量程、初始化等待、背光/显示时钟装载顺序，以及充电与动画退出路径 |
| `splash-handoff.py`、`splash-logo.py`、`splash-orientation.py` | 帧缓冲边界、绘制进程隔离与回退、四向特征识别、关机方向缓存及 systemd 直接入口 |
| `storage-seal.py`、`storage-ready.py`、`storage-paths.py` | 批量只读设置、UFS/GPT 就绪证明、路径替换及回退 |
| `wlan-mac.py` | 目标网卡 udev 初始化、命名竞态与设备身份 |
| `rtc.py` | 真实 helper 的离线时间推算、计数器重置/停走、过期和损坏校准、chrony 只读协议的响应身份/长度/时间和同步判定、原子保存与 RPM 板级载荷；不改变主机时间，不代替关机/重启验收 |
| `fedora-startup.py` | 登录界面 Powerdevil 仅在合成器和 Wayland 套接字就绪时启动、退出后拒绝再激活；普通桌面不受该条件限制。检查音频 PCM 探测前以静音状态准备保护 DSP |
| `mipps-startup.py` | 普通 PD 跳过适配器认证前仍校验电芯；校验失败不会继续 VDM 或 PD 门控 |
| `storage-scope.py` | 运行真实 C 选择/封存逻辑与 init 交接检查；验证内部控制器及驱动、USB 原权限与晚分区保持、救援时全盘封存、移动 `/dev`/`/sys` 后核验；ARM64 musl/QEMU 覆盖 glob 内存归属，升级清理保留修改文件和符号链接 |

### 刷新率模式

`display-refresh-rates.py` 已加入 `devbox run test` 和 CI，不依赖内核源码或 ROM。
默认枚举 90/120/144 Hz；低频实验开关额外枚举 30/48/50/60 Hz，默认拒绝低频扫描命令。
覆盖回退、双路档位不一致、内存分配失败和部分传输禁止重放；本测试不代替真机验收。
它执行配方中实际的面板 C 函数及命令调度计算，使用 UBSan 检查边界。
可追加准备好的完整源码与原厂 `dtbo.img`，执行实际双 DSI/DSC 时钟计算，
并核对固定 ROM 中两个 M81 面板的模式列表、基准时序和 PHY 字节：

```sh
devbox run -- python3 tests/display-refresh-rates.py \
  --source /path/to/kernel-with-0013 \
  --rom-dtbo /path/to/OS2.0.6.0.VMYCNXM/images/dtbo.img \
  --oem-module /path/to/msm_drm.ko
```

`--oem-module` 需要 pyelftools，核对固定模块内的 M81 专用时序跳转表和常量。
ROM 校验限定 SHA256 `5e0377c7781e57f65c474f20edf3d7fb61f03f6030ca170e4edc29467a7993dd`。
离线测试不代表新档位已在面板上显示，也不验证切换期间的触摸、手写笔或功耗。

### Plymouth 主题运行检查

`tests/splash-theme.py` 使用实际的 Fedora script 插件、图片和文字库，显示对象在内存中模拟；
不启动守护进程，也不访问屏幕或输入设备。需在已安装 `plymouth-plugin-script` 和
`plymouth-theme-spinner` 的 Fedora 环境执行，每个组合使用独立进程：

```sh
python3 tests/splash-theme.py --width 900 --angle 90
```

覆盖宽度 `900`、`1800` 和方向 `0`、`90`、`180`、`270`，检查缩放、像素旋转、提示与密码遮罩。
这项组件测试不属于无需 Fedora 环境的 `devbox run test`。
方向识别测试只包含固定 ROM 的 224 点分类样本，不包含完整图像或设备截图。
用 `build/scripts/generate-boot-logo-signature.py --help` 查看从已校验 `imagefv.elf` 重建特征表的方法。

## 内核源码阶段验证

其余 `display-*.py` 多数执行从指定内核源码提取的 C 函数，检查单项补丁前后行为。
它们需要对应阶段的 `--source`、`--baseline`（部分脚本为 `--baseline-source`），
部分还需要固定 ROM 提取的命令或时序证明 JSON。先用 `--help` 查看所需参数。

基线与候选应是应用对应补丁前后的独立源码快照；可用 `build/lib/kernel_adaptation.py`
的 `assemble()` 按来源清单构造，不能用最终源码代替所有阶段，也不要修改正在使用的构建缓存。
ROM 证明文件及具体调用记录由开发者保存在本机 `out/`；这些测试不属于当前 CI 的开箱即用入口。
公开可再生的素材准备流程仍是待办，现有开发记录需与源码、补丁及 ROM 校验值一起保留。

电池单位修正的验证示例：

```sh
mkdir -p out/test-reports
devbox run -- python3 tests/battery-readings.py \
  --baseline /path/to/kernel-before-battery-patch \
  --source /path/to/kernel-with-battery-patch \
  --report out/test-reports/battery-readings.json
```

该测试在临时目录应用补丁，执行实际 getter，覆盖机型、单位、溢出及错误返回；
不修改输入源码。它验证软件换算，不证明电量计刷新及时或待机测量准确。

跨版本迁移到 7.3 时，将上例的 `--source` 换成 `--rebased-source`：执行新版 getter，
保留原有属性换算和固件传输约束，允许主线新增属性。首帧背光另用
`tests/kernel73-first-frame.py --source /path/to/kernel-7.3 --report out/test-reports/first-frame.json`，
检查新版 DRM follower 锁、回调、延迟背光和取消竞态。
`tests/kernel73-probe.py --source /path/to/kernel-7.3` 执行 GPIO 元数据与相机电源域选择代码，
检查 GPIO 分类及所有父域均属于待注册集合；可用 `--baseline` 指向首版失败源码，验证反例。
`tests/kernel73-pll-bias.py --source /path/to/fixed-kernel --baseline /path/to/before-fix`
执行 PHY 偏置引用与 MMIO 代码，覆盖嵌套时钟查询、开关循环及旧版状态不一致的反例。
`tests/kernel73-device-lifecycle.py` 使用相同的 `--source`、`--baseline` 参数，
检查扬声器格式约束、USB 独立电源循环和共享时钟的失败清理，以及新版工作队列声明。
`tests/kernel73-dp-lifecycle.py` 使用相同参数，检查未连接端口、PHY 初始化失败和已被其他路径
使用时的探测清理，以及 DP 上电失败后的状态；旧版反例需复现。
`tests/nanosic-caps-led.py` 使用相同参数，执行键盘 LED 报文转换、合并状态、有限重试及
接回、睡眠、复位、移除路径，检查原有描述符和只读请求接口保持一致。
`tests/display-panel-margins.py --source /path/to/candidate --baseline-source /path/to/before`
执行面板 prepare 与复位函数，覆盖三个等待开关、机型与身份限制及失败清理；
同时检查原厂命令表和其他面板代码未变；默认引导通过三个独立参数省去额外等待。
这些测试需要实际源码，不在普通离线入口中。

### 显示与触摸初始化

两项测试对应不同的源码阶段，均提取实际驱动 C 函数，在带 UBSan 的宿主编译器中执行：

| 测试 | 输入源码阶段 | 覆盖 |
| --- | --- | --- |
| `tddi-calibration-recovery.py` | 应用到 `0010` 的快照，不包含 `0011` | 54 项：稳定校准门限、一次恢复、传输错误、关屏取消、锁顺序和已有告警保留 |
| `tddi-initial-wake.py` | 包含 `0011` 的最终配方 | 70 项：上述恢复保护、首次显示就绪依赖、原厂唤醒报文与等待、部分失败禁止重发、周期内参数切换和恢复开关独立性 |

最终配方的运行示例：

```sh
mkdir -p out/test-reports
devbox run -- python3 tests/tddi-initial-wake.py \
  --source /path/to/kernel-with-0011 \
  --report out/test-reports/initial-wake.json
```

脚本自带桩代码，不依赖私有日志、固件或旧诊断脚本，也不修改输入源码。
这些源码阶段检查不属于普通 `devbox run test` 的自动构建入口；真机结果见
[显示验证](../docs/DISPLAY.md#首次唤醒顺序)。

真机测试分别记录临时启动、固定引导后的重启、拔线冷启动及睡眠唤醒；
故障后自动恢复必须单独记录，不能写成“首次显示正常”。

## 多系统安装与引导

`multiboot.py` 检查容量/UUID、真实 boot 重打包、准备与提交边界、文件/方案变更拦截和默认引导失败恢复；`multiboot-device.py` 在稀疏普通文件上运行真实 sgdisk，执行设备端分区脚本并验证保留原厂前缀、userdata 身份及错误输入拒绝；`multiboot-store.py` 验证实际 ext4 镜像格式化、引导存储校验及默认项提交。它们均不访问真实块设备。

`boot-store.py` 对普通文件注入引导切换写入前、半写入、登记前后断电，检查恢复旧引导、补交目标登记、重复快照回收及再次中断后的重试。`transaction-recovery.py` 使用真实暂存算法与文件系统格式化，检查 Android/Linux 安装回读后的登记续接、损坏材料拒绝及不重复写入系统分区。`gui-boot-store.py` 在实际 QML 中检查维护入口、事务选择和独立确认。维护范围、失败会话接管和载荷哈希分别由 `native-installer.py`、`native-recovery.py` 覆盖；均不访问真实设备。

`install-root-single.py` 另覆盖绑定 ext4/Btrfs 的安装与错 UUID 拒绝；`persistent-root.sh` 覆盖绑定定位、文件系统 UUID、系统 ID、重复分区和错误绑定拒绝。QML 验证单独执行：

```sh
uv run --project installer python tests/gui-combined-install.py
```

这些检查不替代真实分区重建、冷启动与断电恢复验收。

## Android 管理

`android-installer.py` 检查保留/重装、独立清除确认、受保护分区、源文件变更与失败阻断；`android-device.py` 对普通文件执行真实设备端算法和 F2FS/ext4 格式化，比较所有受保护 Linux 文件与引导存储，并注入错误目标、身份变化和中断。`android-rom.py` 覆盖完整 ROM 选择性导入、错误成员与 sparse/零填充；`android-runtime.py` 用 ARM64 ELF 样本核对 Python/标准库/扩展动态依赖打包，不执行目标程序。

```sh
uv run --project installer python tests/gui-full-rom.py
```

完整系统构建的 rootfs 阶段默认使用用户命名空间；Android 首次启动和加密初始化仍须真机验收。

## 分区与系统分步安装

`staged-install.py` 验证同一设备先分区、只装 Android、没有 userdata 时分次装 Linux，以及 Android 后再装 Linux；检查旧备份拒绝、已安装目标拒绝、方案篡改和安装失败不登记。
`managed-linux-device.py` 用普通文件执行实际设备端引导事务，检查旧 Android/其他 Linux/镜像保护、未完成根安装拒绝提交和默认项回退。
`multiboot-device.py` 也覆盖只有 userdata 与只有 rootfs 的真实 GPT 生成；`install-root-single.py` 检查根分区起始位置变化在格式化前停止。

```sh
python3 tests/staged-install.py
python3 tests/managed-linux-device.py
uv run --project installer python tests/gui-device-first.py
```

最后一项使用当前 QML、controller 和 worker 检查分区方案、选定 rootfs 安装、重装取消及默认引导切换确认，不访问硬件。以上不替代完整安装环境构建及真机启动验收。

## 读取实际分区与保留追加

`partition-discovery.py` 检查实际 GPT 的空闲区、原厂满盘拒绝、userdata 锁定、未知布局只读、已有布局追加、晚于 rootfs 创建 userdata 和方案变更拒绝。
`partition-device.py` 在普通稀疏文件上执行真实 sgdisk 追加，并在已有 userdata 的首部、中间和尾部放置数据哨兵，验证连续两次追加后内容、所有旧 GPT 属性、旧引导文件和默认引导保留；也覆盖写入中断后拒绝自动接管。
`gui-device-first.py` 使用当前 QML/controller/worker 检查先读分区、容量边界、使用全部未分配空间、统一确认与再次读取。

```sh
devbox run -- python3 tests/partition-discovery.py
devbox run -- python3 tests/partition-device.py
uv run --project installer python tests/gui-device-first.py
```

## 设备优先向导与 Recovery

`workflow.py` 覆盖先读设备、删除/新建后安装 Linux、重装已安装 rootfs、单 Android 无引导存储安装、受保护目标拒绝、材料与计划变化拒绝，以及移除所有 rootfs 后删除引导存储。
`partition-editor-device.py` 使用真实 sgdisk/mkfs 普通文件夹具验证删除与格式化事务、UUID 更新和保留数据；不访问块设备。
`recovery-installer.py` 验证 v3/v4 结构、机型拒绝、gzip/LZ4、临时启动零 flash、仅固定 Recovery 两槽写入和部分写入回退。Recovery 镜像为合成夹具，不证明第三方镜像实际可启动。

```sh
uv run --project installer python tests/gui-device-first.py
uv run --project installer python tests/recovery-installer.py
devbox run -- python3 tests/partition-editor-device.py
```

所有 GUI 回归均加载产品正在使用的 `installer/liuqin_gui/qml/Main.qml`。`gui-qml.py` 覆盖确认按钮、已准备方案冻结、真实模拟任务期间的 Ctrl+C/关闭保护、报告脱敏和准备状态下取消后退出。`gui-device-first.py` 覆盖无需 OS 材料的扫描、分区选择路由、统一确认执行和重装取消；组合、完整 ROM、ROM 预览及引导维护分别由对应的当前 GUI 测试覆盖。兼容 worker 协议由引擎与设备端测试验证。

`paired-recovery.py` 覆盖配套 Recovery 的身份/完整性检查、ADB 通道与一体打包；不代替真机启动及分区写入验收。

`recovery-compatibility.py` 覆盖系统识别、vendor 指纹与内核精确匹配、多镜像选择、未知固件/镜像不重启、短期运行依据绑定与过期、Android 目标固件拦截、返回 Android 的诊断。`gui-device-first.py` 还验证连接只识别、点击后才读分区。适配测试中的验收项均为合成夹具，不会写入生产适配目录，也不代表真机支持。

`native-recovery.py` 检查原生镜像的内核/DTB/启动与状态程序身份、篡改拒绝、只读传输限制、串号/槽位/解锁/存储保护核验、状态屏幕的数据生成，以及独立原生测试包。使用合成文件和模拟传输，不操作平板；真实屏幕和 RNDIS 仍需真机验收。

`native-ui-service.py` 覆盖固定操作集合、一次性电源确认、过期/会话变化/忙碌拒绝、亮度边界和 RAM 日志导出范围。`native-ui-events.c` 使用真实 LVGL 的离线显示检查页面导航、确认窗口焦点隔离及默认取消，可在 UI 的 CMake 构建中启用 `UI_EVENT_TESTS`，并配合 ASan/UBSan。测试的电源调用使用替身，不会重启测试电脑或平板。

`native-bcb.py` 使用普通文件验证启动快照、仅消费无参数请求、OTA/清除数据与阶段请求保留、未完成安装拒绝清理，
以及部分写入、回读损坏和中断后的原扇区恢复及只读复位。

`recovery-bootstrap.py` 执行实际网关的 RAM 检查；设置 `LIUQIN_TEST_RECOVERY_IMAGE` 并在含 qemu-aarch64 的 Devbox 中运行，会验证镜像内 BusyBox 的真实路径、applet 和运行时解包。`block-identity.py` 验证 GPT 信息与内核边界匹配、每次读取的缓存和带连字符固件分区的保护。

`partition-reread.py` 验证磁盘忙时在 GPT 写入前停止、清理错误不覆盖原始错误，以及动态设备号变化后的受限节点刷新。`recovery-session.py` 覆盖 qseecomd 精确服务识别、仅卸载内部 UFS 的日志映射、未知占用拒绝；不访问真实磁盘。`partition-editor-device.py` 还验证从精确匹配且全部未格式化的新 Linux 布局继续初始化，并拒绝重复执行。`staged-install.py` 验证默认引导失败只恢复仍保留且镜像匹配的旧系统。

`miku-rom.py` 覆盖完整 OTA 导入与动态分区镜像生成，`miku-workflow.py` 覆盖单 Android / 多系统安装路径的 Miku 适配选择与确认绑定。

`partition-names.py` 检查 16/17 ASCII 字节边界、旧 20 字符标签拒绝、设备写入前拦截、旧布局只读且不自动重启，以及已知旧 boot 解析器的定点更新。`workflow.py` 还覆盖中断记录续接、篡改拒绝和重复初始化拒绝；这些都是离线测试，不操作真实设备。

`native-touch-service.py` 检查触控衔接的只读环境、simplefb、模块/固件哈希门槛及模块失败处理。`native-touch-input.c` 覆盖快速点击、非零触点槽、多指隔离、事件丢失、启动时已按住及手写笔悬停；可独立编译或通过 UI 的 `UI_EVENT_TESTS` 运行，不接触真实输入设备。

`recovery-kernel-components.py` 用主机 C 夹具执行内核 GPT 兼容谓词，拒绝其他磁盘和不一致的镜像表；有本地基线镜像及 DTC 时检查电池传输、USB 角色、触控面板及原版显示时钟/电源依赖的保留。有 Unicorn 和 ARM64 工具链时，在四种装载地址执行对齐入口，验证重叠复制及 FDT 参数。缺少可选构建输入时只跳过对应项目，不代替真机启动验收。

`kernel-common-fixes.py` 检查正式内核 Image 对齐封装的完整性、重复发布与无效布局，并模拟执行四种装载地址。传入 `--source <已适配的 7.3 源码>` 时，另外编译执行真实 CPU/GPT/OF/触控/键盘函数，验证板型和元数据边界、活跃设备依赖、有限重试及超时日志；音频夹具覆盖 APM 回调未注册、probe 失败、错误/短回包、保留其他板型耳机检测等情况。时钟夹具在注册过程中同步准备和释放新父时钟，覆盖速率与投票两类回调及注册失败；恢复旧的数据发布顺序会实际触发空指针检查。

`rootless-build.py` 检查完整 UID/GID 映射要求、映射中间缺口、普通阶段不提权、rootfs 阶段默认选择用户命名空间，以及仅显式选择 sudo 时才进入原有 rootful 路径。测试不挂载文件系统；真实 rootless 的 DNF、ARM64、ACL/capability 和数值所有权另作集成验收。

`fixed-recovery.py` 用临时 RSA2048 密钥构造离线镜像，核验 v0 / gzip-DTB 边界、AVB 自签名与载荷哈希、禁用验证标志拒绝、候选角色，以及现有 GUI 仍不接受固定候选镜像。不会连接设备；需要 OpenSSL。实际 ABL 的设备树和 AVB 链模拟证据见固定 Recovery 调查文档。

`native-firmware.py` 检查 A/B 固件只读采集、节点替换/启动会话变化拒绝、DTBO 边界、未知/部分匹配、精确镜像选择、槽位验收范围、固定候选拒绝及主机记录权限。测试不连接设备。

`universal-recovery.py` 检查 v2 内嵌 DTBO、AVB 自签名、偏移与载荷篡改、MIUI/HyperOS/Miku 条件矩阵、缺失校验元数据、未知 ABL 及候选镜像与自动启动/安装入口的隔离。`native-firmware.py` 另检查 AVB 元数据读取上限、会话帧长度和分区摘要变化。两者均不连接设备；镜像测试需要 OpenSSL。原始 ABL 的离线验证范围见 [固定启动条件](../docs/RECOVERY-FIRMWARE-PROFILES.md#固定启动条件)。

`rom-preview.py` 检查 ROM 目录/TAR/完整 OTA 的身份和完整性、AVB 元数据冲突、修改 flags 的观测、材料变化、未知 ABL、安装后固件组合、只读后端和取消。预览不创建可执行 plan_id；`gui-worker.py` 验证预览结果不能提交给刷写入口。使用 `uv run --project installer python tests/gui-rom-preview.py` 验证 GUI 的三个 ROM、两种固件策略、禁止执行及旧结果失效，可加 `--screenshots /path/to/output` 保存界面。

`native-installer.py` 检查安装候选与只读版的隔离、作用范围与启动会话绑定、GPT/分区 UUID 核对、防回滚观测缺失、失败后封存、旧/新引导存储的编辑范围、boot 全量传输后再写入和回读，以及模拟设备上的分区、Android、Linux/default-boot 完整流程。真实写入上下文用模拟 ioctl 检查异常时恢复只读；boot 写入测试只使用临时普通文件。测试禁止安装流程调用 Fastboot/网络，不接触真实平板。范围见 [原生安装候选](../docs/NATIVE-RECOVERY-INSTALLER.md)。

`fixed-recovery-preserve.py` 检查设备绑定的冷启动资格与签名镜像、底层固件基线、安装后的 ABL/AVB 条件、同一固定镜像对新 ROM 元数据的适配、旧运行时拒绝、仅 recovery_a 的确认范围、RAM 传输损坏、恢复回读及失败回退。普通文件替代块设备，另验证 Android 写入器跳过 ROM Recovery、完成记录保存实际固定 Rec 摘要、恢复后不丢失 write_started 记录，以及读取 ROM 时不额外要求安装令牌。全部不访问 USB 或真实分区。

`native-adb.py` 检查 FunctionFS 启动前的存储/设备门槛、USB-only 环境、ADB 镜像身份、boot ID 绑定及禁止回退 USB 网络。真实 ARM64 adbd 的主机环回测试使用独立 ADB server，隐藏 USB 节点与用户密钥；与平板 USB 枚举分开验收。实现与回归见 [原生 ADB Rec](../docs/NATIVE-RECOVERY-INSTALLER.md#adb-连接与传输)。

`tests/generic-ota.py` 验证通用 OTA 的分区边界、固件/DTBO/回滚索引拒绝、super 空白区域与会话材料绑定；不连接设备。

`gui-combined-install.py` 使用当前 QML/controller/worker，验证一个 Android + 两个 Linux 的多选、每个 rootfs 独立材料、统一确认与执行、最后切换默认引导，以及无 ROM 的多个 Linux 计划。`workflow.py` 与 `native-installer.py` 还验证全部材料在编辑前检查、失败保留已完成子任务、新旧 Rec 能力隔离、每个归档摘要和目标绑定；`full-rom.py` 验证完整 ROM + Linux 默认引导的范围及越界拒绝。全部使用临时文件和模拟设备，不访问平板。

`boot-retention.py` 用普通文件验证三个现用系统保护、连续 12 次替换后版本数量有界、删除系统的孤立引导回收、仅保留一个可核验的旧 Linux 内核、未完成事务阻止清理，以及移动/删除/完成记录阶段断电后的续接。清理失败不把已提交系统标为失败；中断清理阻止新安装。`workflow.py` 还检查仅删除当前默认系统时先要求切换默认。

带 `--source`、`--baseline` 或 ROM 证明参数的显示测试是手工运行的内核回归入口，不因未列入默认 CI 就视为废弃。供电顺序、部分使能失败、投票释放及其他面板隔离使用 `display-retained-power.py` 的状态机夹具。

构建路由回归检查 `build --target` 的统一流程、旧命令/隐藏参数/旧配置拒绝，以及 `outputs` 配套选择。配置检查不自动迁移旧路径或加载素材清单。`project-structure.py` 检查 Rec RAM 装配独立性和安装器源码包边界。

`--export-dir` 回归覆盖本次构建配套选择、文件缺失/损坏、同名内核的不同载荷、导出失败不发布目录，以及不附带登录信息或其他缓存。

指纹录入提供者使用假 TA 和临时私有目录检查多账号隔离、同账号凭据复用、UID 重分配拒绝及取消清理。`devbox run test` 在用户命名空间中运行该检查；CI 另用 Node 执行 Polkit 授权规则。测试不访问传感器、TEE 或真实指纹数据。

`fingerprint-gallery.py` 用假 TA 与真实私有文件检查多指追加、旧单指格式拒绝、删除/未发布引用失效、ID 复用隔离及取消/提交恢复；`fingerprint-gallery-client.py` 执行实际 C 协议和匹配代码，检查精确删除、单次采集、HAT 和返回 ID 验证。两者均不访问硬件。

`fingerprint-abi.py` 从锁定的公开源码构建 libfprint/TOD SDK，在真实 Enroll/Verify/Identify/序列化 API 上运行合成进程测试，并编译修改后的 fprintd。CI 安装开发依赖后运行；它只启动测试设备类和假助手，不运行系统守护进程或访问传感器、TEE。源码构建依赖与测试输入见脚本 `--help`。
