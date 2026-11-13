# GPU、GMU 与 OpenCL

[硬件支持](HARDWARE.md) ｜ [设备树对照](DEVICE-TREE.md) ｜ [构建指南](BUILD.md)

SM8475 使用 Adreno 730、DRM MSM 和 Mesa Freedreno/Turnip。Fedora 配方另安装
`mesa-libOpenCL`、`ocl-icd`、`clinfo`，通过 Rusticl 在同一 GPU 上执行 OpenCL。

## 当前方案与验证范围

Fedora Mesa 保持发行版官方 RPM。项目另外提供内核补丁、early preamble 环境配置和
Mesa libclc SPIR-V 包；这几项分别处理不同问题，不能相互替代。

| 问题 | 当前处理 | 作用范围与维护要求 |
| --- | --- | --- |
| 原厂 GPU 电源档位不完整 | `0061` 补齐 220–900 MHz 的 13 档 OPP、GX/CX 投票及 ACD | SM8475 GPU；GMU 频率与既有带宽表继续沿用原厂一致的值 |
| 图像读写、复制后地址映射错误 | `0062` 补齐 SP/TPL1 非上下文寄存器恢复项 | A730；保持图像平铺与 UBWC |
| 宽向量跨步复制结果错误 | `IR3_SHADER_DEBUG=noearlypreamble` 与独立缓存目录 | 继承环境的 IR3 图形和计算应用；Mesa 更新后需复测再决定是否移除 |
| Mesa libclc 兼容性警告 | 打包固定的 Mesa libclc 正式 SPIR-V 发布文件 | 替换 `libclc22-spirv`；需随 Fedora LLVM/libclc 基线维护 |
| 长计算被误判为 GPU 挂起 | `0063` 将 `hangcheck_period` 设为 5000 ms | Liuqin A730 的整个 GPU；真正挂起时的软件恢复会变慢 |

图像、矩形复制和双向跨步复制的定向 CTS 已通过。长计算的完整
`full_3d_explicit_local` 单项在 5000 ms 运行时策略下通过；含 `0063` 的内核另经
临时启动与默认内核普通重启，验证原故障计算路径、默认周期和图形/OpenCL 回读，
临时内核还验证了受控卡死后的恢复。该完整单项尚未在固化策略的新内核上重跑。
**完整 OpenCL CTS 尚未完成验收**，长期稳定性及其他设备批次仍待验证。

下文先说明实现、配置与代价，再给出[定向回归](#图像和复制定向回归)、
[长计算验证](#长计算与故障恢复)和[完整 CTS](#完整-opencl-cts)的运行方法。

## 原厂频率与电源参数

补丁 [`0061`](../build/kernel/patches/linux-7.3/0061-sm8475-gpu-operating-points-and-cx-votes.patch)
删除继承自 SM8450 的 GPU OPP 表，以固定原厂 ROM 中的 Cape 表替换，恢复 900 MHz：

| GPU MHz | GX RPMh 等级 | CX 最低等级 | ACD 控制字 |
| --- | --- | --- | --- |
| 220 | 48 | 64 | `0xc8285ffd` |
| 285 | 56 | 64 | `0xc02c5ffd` |
| 324 | 64 | 64 | `0xc02a5ffd` |
| 364 | 80 | 128 | `0xc0285ffd` |
| 439 | 96 | 128 | `0xc0285ffd` |
| 515 | 128 | 128 | `0x882e5ffd` |
| 580 | 144 | 192 | `0x882e5ffd` |
| 645 | 192 | 192 | `0x882d5ffd` |
| 710 | 224 | 256 | `0x882c5ffd` |
| 765 | 256 | 256 | `0x882c5ffd` |
| 815 | 320 | 320 | `0x882b5ffd` |
| 862 | 384 | 384 | `0x882b5ffd` |
| 900 | 416 | 416 | `0x882b5ffd` |

这些值是电压等级，不是毫伏或微伏。GX/MX 投票沿用驱动的 RPMh 转换；新增的
`qcom,opp-cx-level` 经本机 `cx.lvl` 命令数据库向上匹配为 CX 硬件索引，再写入
GEN7 HFI 性能表的第二个字段。原先该字段名为 `acd`，固定发送 `0xffffffff`；
ACD 实际使用独立的 HFI ACD 表。只有 A730 且每个 GPU OPP 都有 CX 属性时才启用
新转换；不完整表和不可用等级会终止初始化，没有该属性的平台保持既有行为。

GMU 的 200 MHz / LOW_SVS（64）和 500 MHz / SVS（128）与原厂一致，继续继承。
`a730_build_bw_table()` 的 12 档 DDR 表也已对应原厂；缺少普通 GPU `interconnects`
属性不能作为缺少 GMU 带宽投票的依据。本改动不移植 Android 的完整功耗调度策略。
实际模拟电压由 AOP/电源固件、芯片校准及电源依赖决定，未提供每档毫伏读回。

来源可交叉核对：

- 固定 [OS2.0.6.0.VMYCNXM ROM](../build/data/stock-rom-source.json) 的 `vendor_boot`
  DTB 02，SHA256 为 `527344cd52497d0b8b9311f1ab82ac3418b86b95c25e7786890f38f40054e467`，
  节点 `/soc/qcom,kgsl-3d0@3d00000/qcom,gpu-pwrlevels`。
- [小米 Cape GPU 设备树](https://github.com/MiCode/kernel_devicetree/blob/1c507816ba8aa6117dcec41f577ef7a66d2091f7/qcom/cape-gpu.dtsi)。
- [小米 GEN7 RPMh 驱动](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/gpu/msm/adreno_gen7_rpmh.c)
  的 `setup_gx_arc_votes()`、`to_cx_hlvl()` 和 GMU 性能表构造。

## A730 图像地址恢复

补丁 [`0062`](../build/kernel/patches/linux-7.3/0062-a730-restore-image-bank-modes.patch)
在 A730 的上电/抢占寄存器恢复列表中补充 `TPL1_NC_MODE_CNTL` 和
`SP_NC_MODE_CNTL`，保存驱动初始化后的实际值，同时更新固件列表计数。
[原厂 GEN7 列表](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/gpu/msm/adreno_gen7.c)
包含这两项；遗漏它们会造成图像写入与纹理读取采用不同的地址映射。
故障表现为图像部分行/块错位，不能通过只关闭 UBWC 解决。
改动按 A730 硬件型号限定，保留其他 A7xx 的列表和 A750 的独立恢复路径。

复测需覆盖 GPU 写入后 CPU 回读、GPU 写入后 GPU 读取、CPU 写入后 GPU 读取、
GPU 图像复制，以及其他图形负载和空闲后的重复运行。内核修复不需要关闭图像平铺或
UBWC；它也不能替代下面的共享内存计算规避。已有系统使用配套内核 RPM，按
[内核更新流程](KERNEL-UPDATES.md)完成临时启动、返回默认内核和正式更新。

## OpenCL

### 软件包与 libclc

Fedora 的 OpenCL 运行时使用官方软件包：

```sh
sudo dnf install mesa-libOpenCL ocl-icd clinfo
```

默认 rootfs 另外安装固定的 Mesa libclc SPIR-V 库。打包使用上游已发布的二进制文件，
不在本地重新编译 libclc 或 Mesa。已有系统先在构建主机生成 RPM：

```sh
devbox run -- python3 build/scripts/build-mesa-libclc.py --out out/packages/mesa-libclc
```

将生成的 RPM 复制到平板后，在平板执行：

```sh
sudo dnf install ./libclc22-spirv-22.1.8.3-1.liuqin.aarch64.rpm
rpm -V libclc22 libclc22-spirv
clinfo
python3 tests/opencl-smoke.py
```

测试要求枚举到 GPU 类型的 `FD730`，编译并执行向量加法、局部内存归约与 barrier、
`sqrt`，逐项回读校验结果；不会以 CPU 设备替代 GPU。Mesa 26.2.6 默认自动启用
Freedreno Rusticl，无需全局设置 `RUSTICL_ENABLE`。软件版本、OpenCL 版本与扩展应以
设备上的 `clinfo` 为准，驱动启用规则见 [Mesa Rusticl 文档](https://docs.mesa3d.org/rusticl.html)。

Fedora 原版 `libclc22-spirv` 不含 Mesa 分支修正，会触发 “Patched Mesa libclc not
detected”。项目使用 [Mesa libclc 22.1.8.3](https://gitlab.freedesktop.org/karolherbst/mesa-libclc/-/releases/22.1.8.3)
的正式 SPIR-V 发布文件，包含内建函数和数学函数修正；不通过隐藏警告或伪造版本标记处理。
来源提交、原始源码、发布文件 URL 与 SHA256 固定在
[`mesa-libclc-source.json`](../build/rootfs/mesa-libclc-source.json)。构建校验每份输入，RPM
携带原始源码、许可证、来源记录和打包脚本。

该包替换 `libclc22-spirv`，将两份库放在 Fedora Mesa 编译时固定的 `/usr/lib64/clc22/`；
不替换其他架构的 LLVM bitcode。由于同基线 Fedora `libclc22` 精确依赖
`libclc22-spirv(aarch-64) = 22.1.8-3.fc45`，补丁包显式提供这一兼容能力，同时保留
实际版本 `22.1.8.3-1.liuqin`。兼容关系限于此 LLVM 22.1.8 基线；更换 Fedora
libclc/Mesa 基线时需重新核对路径、依赖和 CTS，不能任意扩展这个精确版本声明。
回退可用 `sudo dnf downgrade libclc22-spirv`，或安装事先保留的同版 Fedora RPM。

这条警告不会阻止 OpenCL 初始化或禁用 GPU，它表示 Rusticl 未检测到 Mesa 分支的
libclc 标记，不能据此判断某个应用一定失败或一定正确。保留 Fedora 原版库可继续使用
发行版更新，但仍需验证目标应用；消除警告本身不是修复图像或复制错误的证据。
项目 RPM 也需要随 Fedora 的 LLVM/libclc 基线维护，不能承诺它与发行版升级永久兼容。
警告消失和短测通过不等于 Khronos CTS 或任意 OpenCL 应用兼容性通过。
发行版更换 Mesa/LLVM/libclc 后应重新检查编译、数学函数及目标应用。

### 保留 Fedora Mesa 的共享内存规避

A730 上的宽向量跨步复制可能出现局部内存数据串组或清零；使用显式复制循环或
GLES compute 也可复现，因此不能把问题归结为 libclc 的 `async_work_group_copy`。
禁用 GPU 抢占、降低 GPU 频率也不能消除该现象。关闭 IR3 early preamble 可以规避
已复现的错误，但底层根因尚未完整定位。

板级配置使用 Fedora Mesa 已提供的
[`IR3_SHADER_DEBUG=noearlypreamble`](https://docs.mesa3d.org/envvars.html#ir3-shader-debug-environment-variables)，
保留发行版软件包及更新路径：

- [`60-liuqin-a730.conf`](../device/board-overlay/etc/environment.d/60-liuqin-a730.conf)
  应用于新的 systemd 用户会话。
- [`liuqin-a730.sh`](../device/board-overlay/etc/profile.d/liuqin-a730.sh)
  应用于登录 shell，包括 SSH 登录 shell。

两处配置都把 Mesa 缓存放到用户缓存目录下的 `liuqin-a730-noearly-v1`，避免继续使用
旧配置生成的 OpenCL 程序，同时保留正常磁盘缓存。现有应用需退出重启，桌面会话需
重新登录。SSH 直接执行的非登录命令、系统服务或自行清理环境的应用需要显式传入变量。
对单个进程复测可使用：

```sh
IR3_SHADER_DEBUG=noearlypreamble \
MESA_SHADER_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/liuqin-a730-noearly-v1" \
  your-opencl-program
```

该配置关闭继承环境的 IR3 应用的 early preamble 优化，影响范围包含图形与计算；
并非只修改异步复制指令。性能代价取决于工作负载，不能据此宣称更快。
Mesa 更新后应在独立缓存中去掉开关，复测两个 `async_strided_copy*` 用例和重复数据
回读，再决定是否移除两处配置；仅更新成功或警告消失不能作为移除依据。
本规避与 libclc RPM 的版本兼容维护是独立事项。

相关上游资料：

- [Mesa #15568](https://gitlab.freedesktop.org/mesa/mesa/-/issues/15568) 报告
  A725/A730 的 early preamble 导致 Vulkan 游戏画面异常或 GPU 挂起。
  这是相同芯片和优化路径的相关报告，尚不能认定与本机 OpenCL 错误同根因。
- [Mesa !34746](https://gitlab.freedesktop.org/mesa/mesa/-/merge_requests/34746)
  修正 A7xx 常量和局部内存共用 Local Buffer 的容量限制；它解释了相关硬件资源，
  不能直接作为本次问题已修复的证据。
- [Mesa !42153](https://gitlab.freedesktop.org/mesa/mesa/-/merge_requests/42153)
  针对跳过绘制时错误执行 early preamble 的 Turnip 规避，也不是已经验证的
  Freedreno OpenCL 修复。

### 大规模计算的超时预算

`full_3d_explicit_local` 的已知失败组合是 `1024×1024×1024` 的全局调度，
以 512 MiB 缓冲区分八次校验。`1×1024×1` 的跨步原子访问可以让单次计算持续数秒，
此时命令处理器等待着色器完成，间接命令缓冲区位置保持不变。原配置在支持进度探测的
A730 上采用 250 ms 检查周期，可能提前重置正常任务，随后 Rusticl 在结果映射时返回
`CL_OUT_OF_RESOURCES`。该错误不一定表示物理内存耗尽；关闭 early preamble
也不能解决这种超时。

补丁 [`0063`](../build/kernel/patches/linux-7.3/0063-liuqin-a730-compute-hangcheck-period.patch)
在 GPU 初始化完成后，将 Liuqin A730 的 `hangcheck_period` 设置为 5000 ms。
板型通过 `liuqin_board_quirks()` 识别，兼容 ABL 改写后的标识。其他设备沿用默认值。
这是整块 GPU 的检查周期，同时影响图形与计算；不是只对 CTS 或某个 OpenCL 进程生效。

5000 ms 是已验证可支持该长计算的保守设置，尚未验证最小可用周期。它不是硬件规定值，
也不是精确的单任务总时限；fence 状态、进度判断、有限重试和定时器调度共同决定何时恢复。
修改保留硬件故障中断、抢占和恢复，但不保证任意时长的计算都能运行。

正常任务仍由完成中断和 fence 驱动返回；本修改不改变 GPU 频率、电压或着色器指令。
主要代价出现在异常路径：软件判定真正挂起需要更长时间，相关应用可能等待更久，
影响桌面的故障也可能造成更长卡顿。`msm_gpu.c` 的 `hangcheck_handler()` 还承担
丢失完成中断时的任务回收补偿，这条兜底路径同样会延后。短测通过不能证明长期没有
副作用；后续优化应同时验证长计算正确性和真实故障恢复延迟。

可只读检查当前内核的实际值，不依赖包版本推断：

```sh
sudo cat /sys/kernel/debug/dri/ae01000.display-controller/hangcheck_period_ms
```

当前板级策略应返回 `5000`。节点要求已挂载 debugfs；不同 DRM 节点命名时，应先确认
所选节点属于本机 MSM 驱动。此接口用于诊断，临时改值不替代内核修复或重新启动验证。

## OpenCL CTS 验证

### 构建 CTS

使用 Khronos [OpenCL-CTS](https://github.com/KhronosGroup/OpenCL-CTS) 发布标签
`v2026-10-21-00`（提交 `16b197d191903abad0047e6bcb113167b589ecb4`），并回移
官方矩形复制检查修正
[`acdacf6b4522ba7a7c51413039c7b2a76256a13e`](https://github.com/KhronosGroup/OpenCL-CTS/commit/acdacf6b4522ba7a7c51413039c7b2a76256a13e)。
旧版用例未按实际 row/slice pitch 判断同一缓冲区的重叠，可能把驱动正确返回的
`CL_MEM_COPY_OVERLAP` 误报为失败。该修正保留合法复制操作和数据校验。
在平板原生编译，启用 GL 测试；运行目录、编译器、驱动版本和日志保留在忽略的 `out/` 目录。
构建与运行均在平板的仓库根目录开始。完整清单的 GL 测试需在桌面终端继承实际
XWayland 的 `DISPLAY` / `XAUTHORITY`。只检查单个故障时，构建后选择相应定向命令：

```sh
sudo dnf install git cmake ninja-build gcc-c++ ocl-icd-devel opencl-headers \
  spirv-tools spirv-headers-devel libglvnd-devel freeglut-devel glew-devel mesa-libGLU-devel
git clone --depth 1 --branch v2026-10-21-00 \
  https://github.com/KhronosGroup/OpenCL-CTS.git out/OpenCL-CTS
git -C out/OpenCL-CTS fetch --depth 1 origin acdacf6b4522ba7a7c51413039c7b2a76256a13e
git -C out/OpenCL-CTS show --format= --binary acdacf6b4522ba7a7c51413039c7b2a76256a13e \
  | git -C out/OpenCL-CTS apply --check
git -C out/OpenCL-CTS show --format= --binary acdacf6b4522ba7a7c51413039c7b2a76256a13e \
  | git -C out/OpenCL-CTS apply
cmake -S out/OpenCL-CTS -B out/opencl-cts-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCL_INCLUDE_DIR=/usr/include \
  -DSPIRV_INCLUDE_DIR=/usr -DCL_LIB_DIR=/usr/lib64 \
  -DSPIRV_TOOLS_DIR=/usr/bin -DOPENCL_LIBRARIES=OpenCL -DGL_IS_SUPPORTED=ON
cmake --build out/opencl-cts-build --parallel 4
```

### 图像和复制定向回归

图像和复制修正应先运行下面的定向回归。在仓库根目录使用修正后的 CTS，确认实际 GPU 是 FD730：

```sh
CL_DEVICE_TYPE=CL_DEVICE_TYPE_GPU CL_PLATFORM_INDEX=0 CL_DEVICE_INDEX=0 \
IR3_SHADER_DEBUG=noearlypreamble \
MESA_SHADER_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/liuqin-a730-noearly-v1" \
  out/opencl-cts-build/test_conformance/basic/test_basic \
  writeimage writeimage_int16 writeimage_fp32 imagedim_pow2 imagedim_non_pow2 \
  image_multipass_integer_coord image_multipass_float_coord imagenpot \
  bufferreadwriterect immutable_bufferreadwriterect \
  async_strided_copy_global_to_local async_strided_copy_local_to_global
```

这 12 项包含图像、矩形复制和双向跨步复制；应重复运行，覆盖冷缓存和缓存复用。
验证内核图像修复本身时，还应去掉 early preamble 开关，以独立缓存运行前 10 项。
它们通过不代表完整 OpenCL 兼容，尤其不能覆盖超大 `full_3d_explicit_local` 调度。
超大调度的超时处理见[大规模计算的超时预算](#大规模计算的超时预算)。结果映射返回
`CL_OUT_OF_RESOURCES` 也可能来自队列或 fence 等待失败，不能单凭名称判定物理内存不足。

定位时保留失败输入、实际错误码、驱动版本和生效的环境变量，按用例单独复现。
改变编译器调试开关时使用独立缓存或临时设置 `MESA_SHADER_CACHE_DISABLE=true`，
防止旧缓存掩盖编译变化。禁用缓存仅用于对照，不是日常默认设置。
完整清单仍须单独执行和验收。

### 长计算与故障恢复

在仓库根目录使用原始 CTS 程序和默认规模，只选择这一项。保留完整尺寸，测试期间阻止
自动睡眠，并为结果使用独立日志目录：

```sh
mkdir -p out/opencl-thread-dim
CL_CONFORMANCE_RESULTS_FILENAME="$(realpath out/opencl-thread-dim)/results.json" \
CL_DEVICE_TYPE=CL_DEVICE_TYPE_GPU CL_PLATFORM_INDEX=0 CL_DEVICE_INDEX=0 \
IR3_SHADER_DEBUG=noearlypreamble \
MESA_SHADER_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/liuqin-a730-noearly-v1" \
systemd-inhibit --what=sleep --mode=block \
  out/opencl-cts-build/test_conformance/thread_dimensions/test_thread_dimensions \
  full_3d_explicit_local >out/opencl-thread-dim/cts.log 2>&1
```

同一路径重复运行会覆盖结果，需先归档或更换目录。确认 JSON 将该项标为 `pass`，并核对
程序退出状态和原始日志。同时保存内核日志、内存状态及出现故障时的 GPU 转储，确认
正常长计算期间没有重置或结果校验错误。
用相同计算、不同超时预算作对照可以区分误判与真实执行故障；临时修改 debugfs 后
必须恢复原值。故障转储可按 [Mesa Freedreno 文档](https://docs.mesa3d.org/drivers/freedreno.html)
使用 `crashdec` 解码，关注计算派发、命令处理器等待位置和访问故障。受控无进展任务的
故障注入需单独记录，核对它触发的预期恢复，并在恢复后再次验证普通图形与 OpenCL
计算；其预期错误日志不能与正常负载的新错误混在一起。

### 完整 OpenCL CTS

完整清单另行执行，耗时明显长于定向回归。先回到仓库根目录，在桌面终端运行：

```sh
mkdir -p out/opencl-cts-logs
cts_source=$(realpath out/OpenCL-CTS)
cts_build=$(realpath out/opencl-cts-build)
cts_logs=$(realpath out/opencl-cts-logs)
cd "$cts_build/test_conformance"
IR3_SHADER_DEBUG=noearlypreamble \
MESA_SHADER_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/liuqin-a730-noearly-v1" \
systemd-inhibit --what=sleep --mode=block python3 \
  "$cts_source/test_conformance/run_conformance.py" \
  "$cts_source/test_conformance/opencl_conformance_tests_full.csv" \
  CL_DEVICE_TYPE_GPU "log=$cts_logs"
```

该清单包含 62 组 GPU 命令；另外三行线性图像过滤只适用于 CPU，按清单自身的
设备条件排除。保留 `full*`、最大图像/内存分配和穷举数学等原始参数，不设置
`CL_WIMPY_MODE`，不以 quick 清单替代。未宣告的可选功能由 CTS 自身判定 skip；
失败、崩溃、未完成和缺失可执行文件均不能记作通过。检查最终汇总及原始日志，
不能仅凭官方 Python 调度脚本的退出码判断成功。需要逐项 JSON 时，可为单个测试
设置 `CL_CONFORMANCE_RESULTS_FILENAME`，每个命令使用独立路径，避免覆盖。

## 可复现检查

验收覆盖 13 档双向切换、900 MHz GLES 负载、图像与计算回读、Turnip Vulkan 展示、
FD730 OpenCL 编译及计算回读，以及自动调频、空闲运行时休眠和 RTC s2idle 恢复。
冷却设备提供 13 个状态。GPU 超时预算另以长计算和受控无进展任务验证，恢复后需再次
检查图形与 OpenCL 回读。高温限频、长时压力、其他类型的 GPU 故障恢复、完整 OpenCL
CTS 和其他设备批次仍需独立验收。

在仓库根目录使用候选源码及其编译出的 DTB：

```sh
devbox run -- python3 tests/gpu-sm8475.py \
  --source /path/to/kernel \
  --dtb /path/to/sm8475-xiaomi-liuqin.dtb \
  --stock-base /path/to/oem-dtb-02.dtb \
  --report out/test-reports/gpu-sm8475.json
```

离线测试执行实际 GMU CX 转换和 HFI 构造 C 函数，覆盖原厂映射、线协议布局、
旧平台行为、缺项/错误返回和引用释放；同时核对原厂 DTB 摘要、最终 GPU 13 档与
GMU 200/500 MHz 表。它不代替真机初始化与高频负载检查。

寄存器恢复列表另执行：

```sh
devbox run -- python3 tests/gpu-reglist.py --source /path/to/kernel
```

该检查编译实际列表构造函数，验证 A730 与其他型号的恢复项、固件计数和动态项边界。

真机验收应分别记录临时启动与普通重启，检查 devfreq 可用频率、实际负载下的
900 MHz、降频与空闲休眠、温度和 GMU/IOMMU 错误，并在 s2idle 恢复后再次执行
GLES、Vulkan 和 OpenCL 计算。临时启动与固定更新遵守[内核更新流程](KERNEL-UPDATES.md)。
精确镜像、模块、固件、启动绑定及日志保存在忽略的 `out/` 目录。
