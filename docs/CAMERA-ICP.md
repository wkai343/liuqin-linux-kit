# ICP / IPE 帧处理验证

[硬件 ISP](CAMERA-ISP.md) ｜ [已安装系统的临时启动](BUILD.md#已安装系统的临时启动)

`tests/camera-icp/` 是独立的 SM8475 测试组件，未加入发行内核。默认构建没有
视频节点；可选视频桥接、经过本机校验的启动服务及配套 libcamera
补丁见下文。它能认证加载原厂 ICP 固件，建立 HFI 队列，执行 SYS_INIT、
ping、IPE 实时会话 CREATE/DESTROY、固定 640×480 NV12 CONFIG_IO，以及有界的
FRAME_PROCESS 合成图像及固定尺寸实拍帧回放的 DMA 输入输出，再停机释放资源。单帧及同一会话连续帧均可
检查原厂固件实际完成回复和输出数据。单帧验证不能作为实时视频、时域降噪或 HDR
已经通过验收的依据。

## 对照来源

- 小米 `liuqin-t-oss`，提交 `eb49e10028e27fa98856cb5e458c0f0ec5b4554e` 的
  `drivers/clk/qcom/camcc-waipio.c`；固定 ROM 的设备树和 `camera.ko`。
- Qualcomm GPL LX7 / HFI 参考实现：OnePlus SM8450 Android 13 相机模块，提交
  `81e710f9ad5b18539db69fe7cbe217de8e7115a5`，
  `vendor/qcom/opensource/camera-kernel/drivers/cam_icp/`。
- 用户自行提取的 `firmware/CAMERA_ICP.mbn`，SHA256
  `1dfc57b560935614742fe4dc90e03bc510b9b7bf67f9a01e13740bd71d4a4edc`。
  不分发该固件或原厂 HAL。
- 同一 ROM 的 `camera.qcom.so` 和 `libipebpsstriping.so`，摘要固定在
  `make-frame-fixture.py`。条带结果由用户私有二进制离线解释生成，输出只放 `out/`，
  不随源码、内核模块发布包或 rootfs 分发。

原厂 `camera.ko` 的 MDT 加载和 PAS 认证使用外设 ID 33。平台上的 PAS-supported
查询可能返回 false，而原厂直接执行认证加载；测试仍要求 MDT 加载和 PAS 认证均
成功，不跳过签名验证。使用 PAS context 保留认证 metadata，认证后按原厂顺序初始化 HFI。

## 资源和顺序

LX7 CSR/CIRQ/WD 分别是 `0xac01000` / `0xac01800` / `0xac04000`，主机中断是
GIC SPI 463。固件加载到设备树中已有的 `0x9f500000`、8 MiB、`no-map` 保留区。
通用寄存器位于 CSR + `0x20`，HFI 寄存器编号再乘 4。

必须包含原厂 `camcc_debug_clk` 对应的 **`CAM_CC_QDSS_DEBUG_XO_CLK`**。
只开启 ICP 核心和 AHB 时钟不足以读取 CSR；遗漏这一路会导致访问停滞和异常重启，
表现得像固件握手失败。不要用延长轮询超时掩盖这个依赖。

ICP、BPS、IPE 的 SM8475 RCG 还需匹配原厂安全切换和硬件时钟控制。
内核配方的 `0094-camcc-sm8475-ipe-shared-roots.patch` 在已有 SM8475 分支选择 shared RCG ops 和 hw_clk_ctrl，
保留 SM8450 及已有板级识别逻辑。单独构建测试内核时，应先确认这项时钟配置已生效。
测试按 TOP → BPS → IPE 上电，启用相关总线/核心时钟，建立 IOMMU 映射，准备队列，
认证启动 ICP，再执行 HFI。结束时先 PAS shutdown，随后停时钟和电源，最后解绑和
释放映射；shutdown 失败时保留 DMA、时钟及模块引用，禁止冒险释放仍可能被固件访问的内存。

队列表包含 24 字节头和四个 956 字节队列头，启用 CMD/MSG/DBG 三个队列。
FW uncached IOVA 范围为 `0x13600000`、7 MiB，QDSS 为 `0x13d00000`、1 MiB；
该映射使用独立 IOMMU domain，不复用 CAMSS 的 SIDs。SYS_INIT 返回成功、ping
返回原 cookie、CREATE 返回非零 handle、DESTROY 返回成功才算控制通路通过。
固件版本字及中断计数也应记录在忽略的 `out/` 中。

## 构建与检查

先在平板执行 `prepare.py snapshot --output out/icp-resources.json`，保存实际资源
phandle、运行内核和当前启动目标。ABL 可能把 `__symbols__` 重写到统一 sink，
因此不能直接套用静态 DT label overlay。快照根据节点路径及 compatible 检查资源，
构建器只使用该次快照的数值 phandle；安装或启动目标变化后必须重新采集。

在工作机执行（所有输出放在 `out/`）：

```sh
python3 tests/camera-icp/prepare.py build \
  --snapshot out/icp-resources.json --firmware /path/to/CAMERA_ICP.mbn \
  --kernel-source /path/to/matching/kernel/source \
  --kernel-build /path/to/matching/kernel/build \
  --cross-compile /path/to/aarch64-toolchain-prefix- \
  --output out/icp-modules
```

可用 `python3 tests/camera-icp-config.py --vendor /path/to/vendor` 离线核对实际
测试驱动的 CONFIG_IO 命令及 NV12 描述符与原厂 HAL 的描述符构造（需要 Unicorn、
pyelftools 和 C 编译器）。这只核对字节布局，不能替代第 5 级实机检查。

测试图像前，先生成固定私有数据，再向上述构建命令增加
`--frame-fixture out/ipe-fixture`：

```sh
python3 tests/camera-icp/make-frame-fixture.py \
  --vendor /path/to/vendor --output out/ipe-fixture
python3 tests/camera-ipe-programs.py --vendor /path/to/vendor
```

生成器解释实际 HAL 的 CDM 数组构造，分别在两个主机地址执行原厂条带库，
要求只有两处已知指针发生地址差分，核对目标落在条带缓冲区内，再替换成 IOVA。
不会把任意形似地址的寄存器字重定位。构建器额外固定三个数据块的摘要及大小，
拒绝修改过的帧地址和配置；C 头由已核验数据重新生成。未提供这些数据时，第 6/7 级
在上电前返回 `EOPNOTSUPP`（实拍回放第 8 级也一样）。

构建器只生成模块和清单，不装模块、不改启动镜像。必须使用匹配配置及 Module.symvers，
并先在独立测试内核中加入时钟前置补丁。按[临时启动步骤](BUILD.md#已安装系统的临时启动)绑定并核对测试镜像。在线卸载或重载 camcc 后，genpd 仍可能保留旧的父子链接。

关闭相机客户端，核验清单、固件、内核、实时 DT 与快照一致后，手动加载 overlay 和
测试驱动。视频桥接先用 `modprobe videobuf2-vmalloc`、`modprobe v4l2-mem2mem`
加载同版内核依赖；直接 `insmod` 外部测试模块不会自动解析这些依赖。
`/sys/bus/platform/devices/ac01000.icp/selftest` 只接受八种固定操作：

| 值 | 操作 |
| --- | --- |
| 1 | 上电、启用时钟、停机 |
| 2 | 认证固件、HFI 握手、停机 |
| 3 | 加上 SYS_INIT、ping |
| 4 | 加上 IPE CREATE、DESTROY |
| 5 | 会话内配置固定 640×480 NV12 FULL 输入和 DISPLAY 输出，再销毁会话 |
| 6 | 加上单帧合成色带、FRAME_PROCESS、输出检查 |
| 7 | 同一会话处理八帧，交替方向/亮度、递增 requestId 和 HFI cookie，逐帧检查 |
| 8 | 处理完整上传的 640×480 NV12 像素帧，使用固定命令、IQ 和地址；未上传完整帧时不上电 |

逐级检查返回值与 `status`，重复第 4/5 级检验会话和配置生命周期；每轮须 `result=0`、
`unsafe=0`，新日志不得出现 SMMU fault、时钟超时或固件错误。卸载顺序为测试驱动、
overlay，恢复默认内核后再核验前后摄和桌面音频。不提供用户可写的寄存器地址或 DMA 命令接口。

## 固定帧的地址与验收

ROM 中带 DWARF 的 `CAMERA_ICP.elf` 与签名 `.mbn` 的代码段大小、数据及 BSS 布局不同，
不是同一固件构建。其类型信息可作线索，不能直接作为实际固件的帧 ABI；其他机型的
`ipe_frame_process_data` 也不能无验证套用。

已经用实际配套 HAL 的描述符写入代码核对 NV12 格式号 20、96 字节 image slot、
尺寸和两个平面布局；640×480 CONFIG_IO 的实际固件回复为成功，并给出 4496 字节
scratch、524288 字节 CDM 需求。此配置使用 8336 字节主体及 8 字节 userArg，
通过单独的 shared IOVA 映射传递，不让主机指针进入固件命令。完成匹配核验
HFI 消息头的 user_data1 cookie；实际固件不回显这里试写的配置体尾部 userArg，
不得用该字段假定逐请求关联。

FRAME_PROCESS 使用同一 INDIRECT packet type、opcode 6。测试构造 35112 字节
帧结构及其后的 CDM 数组，IQ 数据 6180 字节，条带最大空间 64872 字节。
采用默认 YUV 拓扑，保留 ICA 普通 zoom window、8 位对齐和无效像素插值设置，
**IFE zoom window 保持零**。将普通窗口误写到 IFE 专用窗口会使固件在
`CreateIcaPasses` / `ConfigICA1Mapping` 返回 `0x000a0003`，不会产生有效图像；
这不是增加超时可以修复的问题。

| IOVA | 用途 | 物理页来源 |
| --- | --- | --- |
| `0x800000` | CONFIG_IO | heap + 6 MiB |
| `0x804000` | 帧命令及紧随其后的 CDM 数组 | 同上 |
| `0x810000` | IQ | 同上 |
| `0x820000` | 条带和已核对的内部地址 | 同上 |
| `0x840000` | 512 KiB 固件 CDM 工作区 | 同上 |
| `0x13e10000` | FULL 输入，Y/UV 共 460800 字节 | heap + 5 MiB + `0x10000` |
| `0x13e88000` | DISPLAY 输出，Y/UV 共 460800 字节 | heap + 5 MiB + `0x88000` |
| `0x13ef9000` | 4496 字节 scratch | heap + 5 MiB + `0xf9000` |

shared 与 image 各映射 1 MiB 独立 IOVA 别名，仍指向测试自己分配的页；SFR 的前
8 KiB 不作为图像使用。输出先填 `0xa5`，未使用区域填 `0x5a`。每帧完成后检查
opcode、cookie、两层返回码、输入 CRC、亮度平面逐字节匹配和边界哨兵。
第 7 级使用四种合成图案循环两遍，能检测请求错配或重复旧帧；它不是时域降噪。

`status` 给出 `completed_frames`、`frame_done`、`input_changed`、`guard_changed`
及输出 CRC。只有 `result=0`、`unsafe=0`、两项 changed 为零且帧数符合操作等级才算
通过。固件停机、时钟关闭后才保存最后一帧到只读、root 可读的 `output_nv12` 属性；
失败请求的快照仅供诊断，不能依据“能读到文件”判定出图。写测试命令应使用一次
无缓冲 `os.write()`，避免缓冲文件对象在错误后关闭时重复提交。日志与图片存入 `out/`。

### 实拍帧回放

从前摄现有 IFE / libcamera NV12 通路保存一帧，再取偶数坐标的 640×480 中央裁剪，
或明确记录缩放步骤后生成同尺寸 NV12。保持 Y 平面 307200 字节、交错 UV 平面
153600 字节；不得把 RGB 图片、带 stride padding 的数据或完整分辨率帧直接上传。

加载配套测试模块后，在平板以 root 运行：

```sh
python3 tests/camera-icp/replay.py \
  --input out/front-640x480.nv12 --output out/ipe-photo-result
```

`input_nv12` 是 root 只写、长度固定的像素暂存区，不接受配置或 DMA 地址。上传从
偏移零开始，严格按顺序写满 460800 字节才可执行第 8 级；重新上传会使旧输出失效，
不完整或乱序输入被拒绝。上传和帧执行共用互斥锁，运行期间无法更换输入；PAS 停机
失败后同样禁止上传。`replay.py` 处理 sysfs 短写，检查完整性、返回码、亮度一致性和
边界哨兵，保存输出与摘要。未上传、部分上传和乱序写入均应在上电前拒绝。

这条链路是“传感器 → IFE 出图 → 保存/裁剪或缩放 → IPE 硬件回放”，并非 libcamera
实时 IPE 流。当前配置要求亮度平面逐字节一致；色度存在小量硬件重采样差异，应分别
统计 UV 误差、检查颜色通道及边缘，不能要求整个 NV12 文件散列相同。白平衡偏色、
高光裁剪及噪声应同时对照 IPE 输入与输出，避免把前级已有的问题归到 IPE。

固件 DBG 队列也可读取有界日志，帮助区分 ABI、拓扑、CDM 和硬件错误。

## 可选实时视频桥接

`ipe-video.h` 增加 V4L2 M2M 节点和 media controller，保持同一 ICP/IPE 会话处理
连续请求，支持固定的 640×480、2592×1472、2592×1952、4080×3060 NV12 恒等处理。
每个打开的文件独立维护队列，硬件只允许一个活动会话。输入与输出均采用多平面
API 的单个连续 NV12 内存平面，兼容 CAMSS 导出的同一 DMA-BUF 内 Y/UV 两段。

私有配置须另外生成，并显式传给构建器：

```sh
python3 tests/camera-icp/make-video-fixture.py \
  --vendor /path/to/vendor --output out/ipe-video-fixture
# 在前述 prepare.py build 参数中增加：
# --video-fixture out/ipe-video-fixture
```

四个尺寸的条带配置都由配套原厂库生成，逐一核对两次地址差分和固定摘要。
前摄条带占 104004 字节，后摄占 138788 字节，各自内部地址位置不同于 640×480，
不能仅替换尺寸常数。视频配置的条带从 `0x820000` 开始，后摄延伸至 `0x841e24`，
因此视频 CDM 工作区移到 `0x880000`；上文独立 640×480 回放仍使用 `0x840000`。
构建器校验所有数据文件和 C 头，视频数据同样不随源码或发行包分发。

第一版通过 vb2-vmalloc 导入 DMA-BUF，显式同步 CPU 访问，再复制到驱动拥有的
IOMMU 图像页；输出在匹配 FRAME_PROCESS cookie 和成功状态后复制给应用。
它不是零拷贝。输入、输出使用独立固定 IOVA，只映射当前尺寸所需页及边界哨兵，
视频输入、输出基址分别为 `0x14000000`、`0x16000000`，每个槽上限 32 MiB，
后摄按实际 18727200 字节及保护页分配，避免旧间距造成输入、输出重叠。
不接受应用传入寄存器或固件命令。时间戳沿用输入，视频帧序号按会话递增。
失败后将队列置为错误；停止和关闭等待有界工作完成，先认证停机、关闭时钟，再
释放私有图像页与 HFI 映射。停机失败则保留资源和模块引用，禁止重新启动。

`0007-liuqin-ipe-video-converter.patch` 为 simple 管线登记 `qcom-ipe` converter，
在转换前处理 IFE 统计和请求元数据。`0010` 为后摄增加 4080×3060 profile 检查，
前后摄均可使用该通路；没有符合后摄尺寸的 profile 时不启用 converter。
即使输入输出格式相同也强制经过 converter；排队失败会先停止转换器再取消请求。
该补丁已列入 `build/libcamera/source.json`，但视频桥接模块仍需手动准备和加载；
仅升级 libcamera 不会自动提供 IPE。运行时应核对驱动的 completed_frames 与应用
帧时间戳，不能只凭 libcamera 枚举到 converter 判定已经使用。

ICP 认证和初始化必须在 IFE STREAMON 之前完成，再启动传感器连续采集；
已观察到相反顺序令 IFE 不再产生图像中断。当前支持前后摄 30/60 fps 单曝光连续请求，
直接视频测试也检查四种固定尺寸的像素和关闭重开。一次性超时注入后，驱动须完成
PAS 停机与私有 DMA 清理，下一会话可重新启动；libcamera 必须停止 converter、
取消未完成请求，不能留下退出断言。该检查不等于真实固件崩溃的完整恢复验收。
Linux 7.3 的 V4L2 ioctl 回调须由 `file_to_v4l2_fh(file)` 获取上下文，不能假设
旧式 `priv` 参数非空。

实机验收须包括：

- 使用独立、正确绑定的测试内核提供时钟修正，不能在线重载 camcc 代替。
- 四种尺寸的 `video-smoke.c` 输入、输出、时间戳检查，以及多次停止/重开。
- 同时运行 IFE 与 IPE 的 libcamera/GStreamer 30/60 fps 采集和桌面应用拍照。
- 队列占用、异常参数、停止中断、进程退出及固件失败清理。
  root 可设置一次性 `video_timeout_once` 参数，在提交命令后触发超时处理，检查
  驱动没有提前释放 DMA；它不等同于注入了真实的固件硬件故障。
- 新内核日志无 SMMU fault、超时或异常，验证后恢复默认内核。

恒等配置不启用 TF、运动估计或 HDR。另有下述原厂 TF 配置与可选本机启动服务；
私有固件和调参数据仍不随发行包自动安装。

## 原厂命令与地址接口

`tests/camera-ipe-programs.py` 解释本机 HAL 的命令构造，在 PacketBuilder 写入前
记录目标和数据范围：TF211 为每级 404 字节、四级，每级拆成 `0x5868` 起 94 个
寄存器和 `0x59fc` 起 7 个寄存器，写入 NPS 命令缓冲槽 9；IPE HDR401 为
`0x15964` 起 62 个寄存器，写入槽 5。旧 TF20 的 396 字节布局不适用于此包。
这些检查没有将复位默认值或标记测试值提交硬件，也不验证实际噪声模型。

`frame-layout.h` 定义当前帧、参考帧、显示/视频和参考输出端口，以及有边界检查的
帧地址槽计算。原厂实际 `frameSet` 步长为 1608 字节，首地址偏移为 188；
不能用旧版本公开结构体的大小推测。`camera-ipe-history.py` 执行固定摘要 HAL 的
构造代码，对照 16 批、62 端口、两平面和元数据的全部 3968 个槽，并核对四级
当前帧到参考帧端口的正反映射。视频桥接用此函数写 FULL/DISPLAY 地址，
可选 TF 配置还使用 FULL_REF 输入/输出地址。三曝光拍照使用下文独立的 MFHDR 配置。

```sh
python3 tests/camera-ipe-history.py --vendor /path/to/vendor
python3 tests/camera-ipe-programs.py --vendor /path/to/vendor
```

实时桥接已有 DMA-BUF 与 libcamera 请求生命周期；单层 TF 已接入 FULL 当前/参考
图像、原厂调参/CDM 和会话历史重置，仍需 DS4/DS16/DS64 金字塔、配准与曝光历史管理。HDR 的固定曝光输入工具和帧记录校验见 [libcamera](LIBCAMERA.md#固定曝光的-hdr-输入采集)。
拍照逐档停流。独立录像工具使用连续三档曝光，见[运动配准与 HDR 录像](#运动配准与-hdr-录像)。
传感器模式表只描述单曝光读出。
这些功能及多光源实景画质必须单独验收，不能由单帧色带通过替代。


### 原厂 TF21 硬件时域降噪

可选 TF 配置支持上述四种尺寸及后摄 4080×2296、3840×2160。后两档使用 S5KJN1 调校，条带库分别产生 138788 和 134440 字节，并单独核对两次地址差分。每种尺寸均增加 FULL_REF 输入与输出，首帧令
`invalidMctfpImage=1`，后续帧使用上一帧硬件生成的参考图像。两个历史槽分别位于
`0x18000000`、`0x1a000000`，按完成顺序交替读写；它们与当前输入和显示输出独立，
均按实际图像尺寸加保护页分配。TF CDM 位于 `0x860000`，与后摄条带和固件 CDM
工作区互不重叠。重新打开、改变尺寸或异常退出都会结束原会话，下一会话从无历史
首帧开始。超时路径在认证停机成功后才释放四个图像槽。

```sh
python3 tests/camera-icp/make-temporal-fixture.py \
  --vendor /path/to/vendor --output out/ipe-temporal-fixture
# 以 temporal-fixture 替代 video-fixture：
# prepare.py build ... --temporal-fixture out/ipe-temporal-fixture
```

生成器执行匹配版本的原厂 Chromatix 解析器、TF21 参数转换、Titan680 寄存器打包、
CDM 写入和条带库，并核对首帧/参考帧命令及两次地址差分。前摄使用 AAC IMX596
调校，后摄使用 Qtech S5KJN1 调校；私有原厂文件与派生命令不提交或随包分发。
构建器固定校验全部派生文件，不能用任意配置替换它们。
4K60 TF21 每帧处理 8812800 个条带像素，选择原厂 600 MHz IPE 档。
CAMNOC SF 按当前帧、参考帧的两读两写申请 2985984 kB/s，停机时释放。
帧缓冲复制使用设备自己的内核工作线程；在有不同容量 CPU 的机器上，4K 优先安排在较大核，
其他档使用默认 CPU 集合。HFI 回复中断唤醒等待线程，并保留有界轮询以覆盖合并通知。

当前为单层 FULL 分辨率时域滤波，使用各模组默认视频树的首个低增益区间。
尚未实现随增益插值的完整噪声模型、DS4/DS16/DS64 历史金字塔和 ICA 运动配准，
不能称为原厂完整 MCTF。应用仍通过同一个 libcamera IPE converter 使用这条通路。
验收应同时检查硬件完成数、静态噪声下降、移动边缘、场景突变和超时后重开；
不能仅根据输出文件存在判定降噪生效。HDR 需要独立的多曝光输入和融合配置，
不会因 TF 启用而自动启用。


`video-smoke --temporal /dev/videoN 640 480 30` 可验证该固定 TF 配置：首帧需
与输入一致，随后检查静态噪声误差下降、中性 UV、不超过阈值的亮度偏差与拖影。
第 10–19 帧移动亮块，第 20 帧切换整幅场景；再次运行应重新从无历史状态开始。
测试只加载像素，不上传任意命令；传入 `--temporal` 不会自行开启硬件 TF。

如需本机开机加载，可将已核验的 `manifest.json` 和两个模块安装到 root 所有且
非组/其他用户可写的 `/usr/local/lib/liuqin-ipe/`，将 `load.py` 安装为
`/usr/local/libexec/liuqin-ipe-load`，再安装本目录的 `liuqin-ipe.service`。
服务在桌面用户会话前加载，图像处理仍按需上电。加载器校验模块摘要、vermagic、
原厂固件、实时 phandle、保留内存以及完整启动目标；内核或 `boot_generation`
变化时拒绝加载，需在新默认系统重新生成快照并核验匹配模块。
服务不包含私有调参数据，不会在内核升级后自动编译或盲目复用旧模块。

Fedora SELinux enforcing 下，本机私有模块也需要正确的模块文件标签。仅为这两个
文件登记 `modules_object_t` 并恢复标签后再启动服务，不关闭 SELinux：

```sh
sudo semanage fcontext -a -t modules_object_t \
  '/usr/local/lib/liuqin-ipe/(icp-overlay|qcom-icp-test)\.ko'
sudo restorecon -v /usr/local/lib/liuqin-ipe/*.ko
sudo systemctl enable --now liuqin-ipe.service
```

## HDR 曝光拓扑检查

`tests/camera-ipe-hdr-topology.py` 在离线解释器内执行同一原厂 HAL 的
`SetupMFHDRExposureMap` 和 `FillMFHDRIQSettings` 对应代码段。它验证两曝光映射
`[1, 0, 2]`、三曝光映射 `[1, 3, 2]`，以及相邻曝光 sensitivity 比值是否严格大于
16 的两个路由分支。四曝光被原厂函数拒绝。倍率应由已校验的实际曝光时间与增益
计算，不能直接用请求值或固定延迟推测。

```sh
python3 tests/camera-ipe-hdr-topology.py --vendor /path/to/vendor
```

### IPE 多曝光拍照

本机前、后摄 SAT 拍照配置使用 ArcSoft HDR。高通 CHI 的另一组拍照图定义了
`MFHDRTwoFrameSnapshotFusion` 和 `MFHDRThreeFrameSnapshotFusion`。可以从同版
`com.qti.chiusecaseselector.so` 读取节点属性、曝光变体、内部连线和最终输出。
这些定义提供 Linux 硬件适配的依据，原厂相机是否在运行时选择它们须另行验证。

每档 NV12 先经过单独的 IPE，生成全分辨率的 UBWC TP10 和四分之一尺寸的 PD10。
三曝光的短、中、长输入分别使用固件端口 `0/1`、`14/15`、`4/5`；两曝光使用
`0/1`、`4/5`。融合节点的 profile/processing 均为 9，`ConfigureIPE` 选择
`TOPOLOGY_TYPE_MFHDR`（4）。融合输出走 `10/11`，由最后一个 IPE 读取并生成
NV12。两曝光图有 8、9 两个输出端口，三曝光图为 8。CHI 图另外包含用于配准的
EVA 节点。两曝光图的控制通路可单独核对，三档采集的中间曝光元数据不能直接
充当独立两档采集时的原厂 AEC 状态。

`tests/camera-ipe-hdr-snapshot.py` 将格式版本 2 的实拍输入按采集时记录的摘要绑定至原厂图并执行
`IQSetupTriggerData`、DSX101、HDR401、Titan680 寄存器打包和原厂条带库。
DS4 的宽、高分别按 `2 * ceil(dimension / 8)` 对齐，由 DSX 设置和相应端口共同描述。
脚本分别在两个地址计算条带并核对内部指针，比较 HDR 开/关及两、三曝光模式。
两颗模组的 HDR401 默认关闭。离线开启 MAC 后，Titan680 的 `CreateCmdList`
为 62 个寄存器生成 256 字节直接写命令。虽然 HDR401 声明了两张表且打包器生成
512 字节表数据，提供有效 DMI 缓冲区后，该函数实际写入的 DMI 命令仍为零字节。
原厂 `FillCDMProgramArrays` 在 pre-LTM 组生成类型 72 的直接写程序，查找表描述符为
跳过类型 255。拍照分支在调用描述符构造函数时直接传入该常数，对应 LUT 索引 4。

实际加载的 `CAMERA_ICP.mbn` 在 Titan680 的 IPE 初始化阶段写入 MAC 权重。
`tests/camera-icp/hdr-firmware.py` 从签名镜像的 PT_LOAD 段读取数据，以相关调试
ELF 的 `ipeInitMfhdrMacLut_48` 符号交叉核对全部 512 字节。签名镜像
`0xec0e8` 处是表地址 `0xecb30` 和长度 512。初始化代码从 `ipeInfo + 216`
取表，检查 Titan680 芯片描述符的 `mfhdrMacLutSingleBuffered` 标志，再向
`0x1590c` 依次写入表号 1、2。两个循环分别从偏移 0、256 读取 64 个 32 位字，
经 `0x15910` 写入硬件。离线报告列出固件表与 Chromatix 打包器所产表的独立摘要。
每帧的曝光倍率和 MAC 参数仍通过 pre-LTM 直接写程序提交。

```sh
python3 tests/camera-ipe-hdr-snapshot.py --vendor /path/to/vendor \
  --capture /path/to/front-bracket --output out/front-stages-640.json
python3 tests/camera-ipe-hdr-snapshot.py --vendor /path/to/vendor \
  --capture /path/to/rear-bracket --native --output out/rear-stages-native.json
```

向前述 `prepare.py build` 增加 `--hdr-config-probe` 可构建单独的 640×480 测试模块。
selftest 9 为预处理，10、11 为两、三曝光融合，12、13 为三、两曝光后处理。
每一级建立独立固件上下文，发出 CONFIG_IO，按固件返回的长度分配实际 scratch
页，并映射所选端口的像素和 UBWC 元数据。测试逐页核对 IOMMU 映射及两端保护页，
在固件停机后回收。图像和 scratch 分开分配，各档输入没有共用地址。640×480
离线检查还逐字节比对 C 驱动生成的五份端口配置与 P010 后处理配置，以及原厂的平面尺寸和偏移。

若要提交实际图像，先用同版原厂库生成固定命令，再向上述构建命令传入
`--hdr-still-fixture out/ipe-still-fixture`。构建器核验每份命令的长度与摘要。

```sh
python3 tests/camera-icp/make-snapshot-fixture.py --vendor /path/to/vendor \
  --output out/ipe-still-fixture
```

测试接口位于 ICP 设备的 sysfs 目录。表内每张线性输入使用 640×480、与 NV12 相同的双平面布局，
共 460800 字节。DSX 直接写程序和四张缩放系数表来自匹配的 Titan680 实现。

| selftest | 可复现的检查 |
| --- | --- |
| 14 | 将一张图写入 `input_nv12` 后运行。从 `snapshot_pre_output` 读取 749568 字节 UBWC TP10 和随后的 49152 字节 DS4 PD10。 |
| 15 | 读取上一级成功产生的中间图并输出 `output_nv12`。与原始 NV12 分别比较亮度和色度，可独立检查一次曝光的编码与解码。 |
| 16、17、18 | 在每一级前分别写入已核验的短、中、长曝光。驱动按顺序保留三组硬件中间图，仅完整输入集可以进入下一步。 |
| 19、20 | 在 MAC 关闭时分别提交三曝光和两曝光的融合节点对照帧。19 绑定全部六张中间图，20 只绑定同组的短、长曝光。 |
| 21、23 | 分别使用后摄、前摄的三曝光 MAC 配置，将三组中间图在 IPE 内融合。每个配置含实际曝光倍率对应的 62 个寄存器、IQ 和匹配条带。 |
| 22 | 将融合中间图转换为线性 10 位结果。`snapshot_p010` 包含 921600 字节，小端 16 位字的高十位有效。 |

融合节点同时检查 `FRAME_PROCESS` 回复、输出平面和输入完整性。对同一组三曝光
和 DMA 绑定，关闭 MAC 的 MFHDR 帧在 CDM 等待时超时，日志为
`CdmWaitStatus=0x10001`、`frame_error=9`、`frame_rc=0xa0007`。签名固件中的
错误映射将该返回码对应到 `timed out`。启用 MAC 并配套提交 IQ、寄存器和条带
后，请求返回 `frame_error=0`、`frame_rc=0`、`completed_frames=1`。
这个对照定位到融合帧的配置组合，未把超时归因于单个寄存器位。

### 三曝光硬件 HDR 拍照

同一份 IPE 模块支持桌面单曝光预览、TF21 和静物三曝光 HDR 拍照。前摄 IMX596
的拍照流为 2592×1952，后摄 S5KJN1 默认为 4080×3060，另可选两档宽屏。每颗模组都需要与该模式
相符的三档固定曝光记录。使用原厂 IPE 条带库和实际曝光倍率生成对应配置。宽屏模式使用后摄原尺寸传感器流独立取样。
4080×2296 的短、中、长实际曝光为 1997、8001、31994 µs；3840×2160 为 1002、4000、16003 µs。
4K 档使用独立的 HDR401 寄存器，使长曝光落在 60 Hz 帧周期内。每档都校验像素摘要和实际曝光。

```sh
# 平板采集；将两份完整 bracket 目录复制到能读取原厂 vendor 的构建主机
camera-hdr-capture rear out/bracket-wide 4080x2296
camera-hdr-capture rear out/bracket-uhd 3840x2160
for mode in wide uhd; do
  python3 tests/camera-icp/make-snapshot-fixture.py --vendor /path/to/vendor --native \
    --capture "out/bracket-$mode" --output "out/ipe-$mode"
done
# 在含原有前后摄 native、MAC 配置的 prepare.py build 命令中增加
# --hdr-wide-fixtures out/ipe-wide out/ipe-uhd
```

```sh
for sensor in front rear; do
  python3 tests/camera-icp/make-snapshot-fixture.py --vendor /path/to/vendor \
    --capture "/path/to/${sensor}-bracket" --output "out/ipe-mac-${sensor}"
  python3 tests/camera-icp/make-snapshot-fixture.py --vendor /path/to/vendor --native \
    --capture "/path/to/${sensor}-bracket" --output "out/ipe-native-${sensor}"
done
# 在前述 prepare.py build 命令中加入以下选项
# --hdr-still-fixture out/ipe-mac-front --hdr-mac-fixtures out/ipe-mac-front out/ipe-mac-rear
# --hdr-native-fixtures out/ipe-native-front out/ipe-native-rear --temporal-fixture out/ipe-temporal-fixture
```

按上节的开机加载说明，将匹配当前内核和启动目标的组合模块与 manifest 安装到
`/usr/local/lib/liuqin-ipe/`。常驻模式下，HDR 命令和 libcamera 预览使用同一模块，
拍摄期间 WirePlumber 可以继续运行。先关闭正在采集的相机应用，再运行 HDR 命令。
旧桌面模块配独立 VGA HDR 包的安装仍可沿用 `hdr-bundle.py` 的按需加载路径。

在 Linux 驱动中，关闭采集流后传感器和 CCI 可短暂保持活动。此时紧接离线融合，
再从后摄切到前摄，可以在 CCI master 0 的第一笔 IMX596 写入 `0x0136` 等待
REPORT 时超时。原厂 CCI 对每个 master 记录活动客户端，并按引用计数处理资源启停。
常驻模块在第一档离线预处理及下一次实时 IPE 会话启动前，通过运行时电源管理同步
挂起空闲的传感器和共享 CCI。这两个时点分别覆盖拍照与返回预览的切换。实时路径
保留其他活动客户端的引用。离线路径在采集仍占用设备时返回错误。可重复执行
“后摄 HDR 拍照、后摄预览、前摄预览”，逐路检查帧数和内核的 CCI 超时记录。
[原厂 CCI 资源管理](https://github.com/MiCode/vendor_qcom_opensource_camera-kernel/blob/6c6099a85cb3154fc8a744c17b6b9f5fd361dea8/drivers/cam_sensor_module/cam_cci/cam_cci_soc.c)

拍照使用 Python、FFmpeg 和按 [libcamera 说明](LIBCAMERA.md#固定曝光的-hdr-输入采集)
编译的 `camera-hdr-capture`。在平板上编译下面的像素转换工具，或使用对应的交叉编译器。
安装时将它与 `camera-hdr-capture`、`camera-hdr-photo.py` 和 `camera-hdr-validate.py`
放在同一目录，子目录放 `camera-icp/hdr-bundle.py` 和 `camera-icp/check-hdr-photo.py`。
将 `liuqin-hdr-photo` 链接到拍照脚本。每次拍摄使用新的输出文件名。

```sh
cc -std=c11 -O2 -ffp-contract=off tests/camera-icp/hdr-pixels.c -lm -o hdr-pixels
sudo liuqin-hdr-photo --sensor rear --output rear-hdr.jpg
sudo liuqin-hdr-photo --sensor front --output front-hdr.jpg
sudo python3 /usr/local/libexec/liuqin-hdr/camera-icp/check-hdr-photo.py --sensor rear --native
```

拍照工具核验每档实际曝光、帧时间戳和像素摘要。默认保留拍照流整幅有效像素，
按行移除 NV12 填充。前后摄的 IFE Gamma151 已对图像应用 sRGB 曲线。用户态先用模组的
`cst12_ipe` 矩阵还原颜色，再逐通道逆转 sRGB 曲线，得到与曝光量成比例的 8 位输入。
G 保留全分辨率，B/R 在线性光下按 2×2 平均后交错存放。运动区域回退也使用同一份线性中曝光。
IPE 按当前尺寸的固件配置、IQ 和硬件条带，完成三路预处理、HDR401 融合及端口 8
的 10 位读回。每个图像端口独占带保护页的 64 MiB IOVA 槽，驱动按实际平面布局
映射图像，按固件回报分配工作区。10 位样本按 P010 布局读回；用户态在线性域以三个通道的
最大值决定高光压缩比例，再逐通道应用 sRGB 输出曲线，生成 SDR JPEG。
前摄 JPEG 还参考自动曝光预热末尾 8 帧的“曝光时间 × 模拟增益”中位数，与固定中曝光比较。
自动曝光较亮时，在融合和运动回退完成后提亮暗部和中间调，最多两档，也不超过长曝光的倍数。
三个颜色通道使用相同的线性倍率；越亮的区域补偿越少，线性峰值达到 100（中曝光白点为 255）
后沿用原有曲线。后摄拍照和 HLG 录像不使用这项补偿。
`--resolution vga` 可显式选择 640×480。原厂最终色调标定仍待适配。

`check-hdr-photo.py --native` 在图像底部跨条带布置两像素宽的明暗条纹，核验原尺寸
细节。受控渐变测试逐次只改变一档曝光，检查三档对融合结果各自的贡献，以及中曝光
饱和后仍能区分的高光层次。桌面 TF21 使用上文的 30 帧时域测试独立验收。

下面的主机测试不打开相机或 IPE。`--tools` 指向为主机编译的像素工具和配套拍照脚本；
不提供原图时，只运行已知曝光灰阶、彩色采样、运动回退和 JPEG 高光测试。提供已有的 schema-2
三曝光目录时，还可检查每个传感器原尺寸及 VGA 的实际融合输入，`--align` 需要同目录的 `hdr-motion`。

```sh
python3 tests/camera-hdr-still-colour.py --tools out/hdr-tools --align \
  --capture /path/to/front-bracket --capture /path/to/rear-bracket
```

### 运动配准与 HDR 录像

`liuqin-hdr-photo` 在安装 `hdr-motion` 时默认先配准短、长曝光，再进行原尺寸
硬件融合。`--no-align` 可用于同组像素的静态对照。原厂 CHI 三曝光图含两个 EVA
配准节点。Linux 当前在 CPU 上提取 ORB 特征、用 RANSAC 估计平移、旋转和统一缩放，
将亮度及色度平面变换到中曝光坐标。局部曝光响应不一致或移出边界的区域采用同一张
中曝光的像素。低纹理、视差、滚动快门和快速运动会限制可融合的面积。该版本尚未调用 EVA。

`liuqin-hdr-video` 录制无声的 VP9 Profile 2 Matroska。前后摄默认从原尺寸 NV12 等比裁剪到 640×480。后摄指定宽屏时保留对应传感器模式的整幅图像。录制进程设置 `LIBCAMERA_LIUQIN_IFE_ONLY=1`，
直接从 IFE 取帧。后摄各档使用系统安装的项目 libcamera，前摄 VGA 支持工具目录中配套的 libcamera。
桌面预览仍走原有 TF21 管线。录像先用 AE/AWB 和后摄 AF 预热，
再固定增益和白平衡。后摄在取得已稳定的镜头位置时，将该位置交给手动锁焦，避免录像期间每四帧计算一次对比度而错过曝光写入窗口。每三帧构成一组短、中、长曝光，
30/60 fps 输入在逐帧交替曝光时分别最多产生 10/20 组三曝光。工具分别报告实际成片帧率和 IPE 五阶段处理吞吐。

每次开录都用非周期的曝光序列测量控制延迟。程序在画面中均匀取样，用前一段亮度的中位数和中位绝对偏差区分三档，
再用后一段 28 帧独立检查候选延迟。前摄和后摄 4080×2296 须对应后第 2 帧，后摄 4080×3060 和 3840×2160 须对应后第 3 帧。
交替曝光期间还会核对每张入选图像的亮度类别。记录器同时检查 IFE 源帧号、单调时钟、曝光行数读回及写入完成时间。后摄驱动按活动模式更新只读行消隐值，两档分别为 1808 和 256 个像素时钟。写入超过已验证
窗口（30 Hz 为 12 ms，60 Hz 为 6 ms）会丢弃受影响的整组。连续半秒无法取得完整组时转入逐档保持。场景亮度无法区分三档或校验不通过时，改用逐档
保持曝光的连续流，跳过过渡帧。编码时间戳取自每组的中曝光，录制时长和运动速度由原始采集时间决定。

后摄 4K 启动时，标准错误输出记录 68 帧的曝光写入延迟、测光网格亮度总和，以及每个候选延迟下三档的样本数、中位数和中位绝对偏差。
`separated` 的两个值分别对应短／中和中／长曝光；`scored=0` 表示该候选未进入后 28 帧的独立评分。
`HDR startup` 汇总在 6 毫秒内完成的写入次数并给出模式选择原因。要专门验证逐帧交替录像，可用
`sudo env LIUQIN_HDR_REQUIRE_INTERLEAVED=1 liuqin-hdr-video --sensor rear --resolution 3840x2160 --seconds 180 --output uhd.mkv`。
此时启动校验失败或录制中失去配对会停止采集，并放弃该次输出文件。
同一命令加上环境变量 `LIUQIN_HDR_TRACE_STARTUP=1` 可检查后摄 4K 开录前两秒的缓冲等待。
采集和录像进程都在内存中记录事件，结束时写到标准错误。`HDR capture_event` 依次给出事件、IFE 帧号、槽号、传感器时间和事件时间。
`HDR stage_event packet` 给出中曝光时间、事件时间、输出组号、短曝光帧号和槽号。用短曝光帧号关联采集端领取缓冲的记录，
用中曝光时间关联配准、归还缓冲、IPE 读写、HLG 转换和编码器入队事件。两种时间均为单调时钟纳秒数。

排查热机丢组时，设置 `LIUQIN_HDR_TRACE_TAIL=1` 并把标准错误保存到文件。采集端和录像进程在请求时长的最后 62 秒记录事件，
留出两秒来匹配跨过末分钟起点的缓冲。可以同时启用启动跟踪。下列命令用成片的时间戳计算末分钟每十秒的帧数，并把各次 IPE 提交间隔
拆成写入、读回、等待上一项编码任务、等待下一组配准以及调度开销。并行执行的等待只计一次。输出另外列出拿不到缓冲的次数，以及三处缓冲都能匹配时各自的状态。
启用两段跟踪后，`--startup` 还会列出首秒内成片间隔最长的两组、两张中曝光帧的时间差、间隔内的采集事件，以及各事件发生时正在执行的处理阶段。
在同一命令中加上 `LIUQIN_HDR_TRACE_ENCODER=1`，可以按中曝光时间跟踪 appsrc 出队、VP9 输入和输出、Matroska 输入，以及该时间戳首次到达 filesink 的时刻。
分析结果的 `encoder_tail_ms` 给出末分钟各段的平均和第 95 百分位耗时，并分别统计关键帧和预测帧。`appsrc_queued_frames_at_vp9_output` 统计每个编码包输出时，appsrc 中还有几帧等待编码。队列可容纳两帧，可用这些计数和配准等待时间一起判断送帧是否受阻。filesink 的入口时间表示数据已送交写入组件。核对实际文件写入耗时可另用 `strace -f -T -yy -e trace=write,writev,pwrite64,fsync,fdatasync` 跟踪输出文件。

```sh
mkdir -p out/camera-uhd
sudo env LIUQIN_HDR_TRACE_STARTUP=1 LIUQIN_HDR_TRACE_TAIL=1 LIUQIN_HDR_REQUIRE_INTERLEAVED=1 \
  liuqin-hdr-video --sensor rear --resolution 3840x2160 --seconds 180 \
  --output out/camera-uhd/uhd.mkv 2>out/camera-uhd/uhd.log
python3 tests/camera-hdr-video-timing.py --startup out/camera-uhd/uhd.log out/camera-uhd/uhd.mkv
```

录像在融合前按 sRGB 逆传递函数线性化颜色。两档后摄宽屏使用 root 专用的
`/dev/liuqin-rear-hdr`。录制器以读写方式独占打开设备，每次写入一整组三路已配准的线性像素，再读出一张同尺寸的 P010 图像。
录制器对 3840×2160 在采集前调用 `LIUQIN_HDR_PREPARE_UHD`（`_IO('H', 0x70)`），建立固件上下文及各阶段缓冲区。重复调用复用同一组上下文。旧驱动返回 `ENOTTY` 时，录制器沿用首次写入长度选择模式的接口。
驱动把每档像素直接复制到预处理输入。每组须完成三次预处理、一次 HDR401 融合和一次输出，读取 P010 后才能提交下一组。
同一段录像复用已启动的 ICP 固件和各阶段的 IOMMU 映射。各阶段缓冲区就绪后，驱动核对 UBWC、PD10 的格式、尺寸和行跨度，
让上一级直接写入下一级使用的物理页。写入端使用独立的 IOVA，读取端保留只读映射。每次固件完成后核验输入完整性，两级之间核对 CRC。
3840×2160 在三次预处理期间借用输出处理阶段空闲的输入页，给中曝光建立按 NV12 长度限定的只读映射，前后各留保护页。
IPE 预处理当前曝光期间，CPU 复制下一档输入并计算 CRC。短、中、长三帧依次提交，收到各自的固件回复后逐一复核 CRC。
后续融合阶段会重新初始化借用的页。某档复制失败时，驱动先收回在途命令的回复，终止整组并关闭固件会话。
清理成功后允许在同一描述符重新提交。

原厂 ICP 管理器在申请硬件上下文时配置 I/O，在释放上下文时销毁对应的固件句柄。[原厂 ICP 上下文管理](https://github.com/MiCode/vendor_qcom_opensource_camera-kernel/blob/6c6099a85cb3154fc8a744c17b6b9f5fd361dea8/drivers/cam_icp/icp_hw/icp_hw_mgr/cam_icp_hw_mgr.c)
Linux 为 4K 录像的预处理、HDR401 融合、P010 输出各创建一个固件句柄，
每个句柄保留独立的 CONFIG_IO 描述和固件返回的缓冲区尺寸。预配置后的每组提交五条帧处理命令，逐条等候完成。
正常关闭录像时按创建顺序的逆序销毁三个句柄。4080×2296 录像仍按阶段创建和销毁句柄。
每个端口保留独立的保护页。关闭描述符或进程退出时，驱动先确认固件停机，再回收 DMA 映射。VGA 和旧模块继续通过 `video_fuse` 逐组提交。

在平板上运行 `sudo python3 tests/camera-icp/check-hdr-record-contexts.py`，可用两组不同亮度的合成输入核对独立开流与交替融合的 P010 像素。
脚本同时核对预配置后首次读取得到 `ENODATA`、首组及后续每组五个固件回复，以及关闭时三个销毁回复。该测试直接使用 IPE 录像设备。

后摄宽屏采集与配准共用三个预分配的三曝光缓冲区。采集端在一组开始时取得空位，直接写入三路像素，配准读完后才归还空位。
没有空位时跳过该组的像素复制，继续设置并核对曝光。配准完成的组按原顺序送入 IPE。
录像在两个工作线程上分配三档曝光的亮度图构建和 ORB 特征提取。4K 配准通过 12×12 区域均值从原图取得 320×180 亮度图。AArch64 宽屏录像对小幅运动使用 NEON 做全尺寸双线性采样，
沿用 OpenCV 的 1/32 像素网格和边界复制规则，大幅运动走 OpenCV 原路径。输入线性化按完整色度行分工。[OpenCV 仿射重采样实现](https://github.com/opencv/opencv/blob/4.13.0/modules/imgproc/src/imgwarp.cpp)
4K 录像提交 IPE 时优先使用进程允许的高算力 CPU。融合后转换到 BT.2020 色域和 HLG 传递函数，输出有限范围的 10 位 YUV。
转换器对三个通道均低于 64 的线性 RGB 码值按需建立查找表。每个 2×2 块合计四个带 16 位小数的定点色度贡献，
接近半码舍入边界时按原有双精度顺序计算，得到与标量转换相同的 10 位输出。其余码值沿用三元组缓存或直接计算。拍照命令继续输出 SDR JPEG。
[ITU-R BT.2100](https://www.itu.int/rec/R-REC-BT.2100/en)

后摄 4K 默认使用四个 VP9 线程、四列瓦片和行并行编码。测试八列编码时，在同一条录像命令中设置 `LIUQIN_HDR_UHD_EIGHT_TILES=1`。
这组设置将 VBR 目标码率调至 288000 bit/s，并让配准线程从第一组三曝光起就使用进程允许的高算力 CPU，以便及时归还缓冲。两种设置的关键帧间距均至多 12 帧。
编码器的 `tile-columns` 参数取瓦片列数以 2 为底的对数，四列和八列分别对应 2 和 3。[GStreamer vp9enc](https://gstreamer.freedesktop.org/documentation/vpx/vp9enc.html)

后摄 4K 的四列和八列配置默认都由系统调度 VP9 线程。仅在设置 `LIUQIN_HDR_UHD_VP9_FAST_CPU=1` 时，录制器才按队列深度调整主线程。每个编码包输出时，若 appsrc 已积满两帧，主线程在其允许范围内选用高算力 CPU（SM8475 上为 CPU 4–7）。队列降至一帧或清空时恢复原范围。首包输出时 libvpx 已建好三个辅助线程，它们继续由系统调度。结束、取消或下游报错时，流线程在返回 GStreamer 线程池前恢复原范围。[GStreamer 流线程通知](https://gstreamer.freedesktop.org/documentation/additional/design/stream-status.html)

可在装有 Python GStreamer 绑定和 10 位 `vp9enc` 的 Linux 上运行 `python3 tests/camera-hdr-video-scheduling.py` 做离线回归。测试通过录像器编码内存帧，检查默认设置、显式开关和适用分辨率。可控队列用于验证线程的 CPU 切换与恢复，并覆盖 CPU 容量不可读或修改亲和性被拒绝时的回退。
对照时固定输入、瓦片配置和跟踪选项，交替安排两种调度方式的测试顺序。用 `encoder_tail_ms` 核对关键帧和预测帧耗时，并比较送帧等待、`wait_for_registration` 和末分钟每十秒的实际保存帧数。
宽屏录像的成功命令默认只更新驱动状态。需要逐条核对 CONFIG_IO 和 FRAME_PROCESS 回复时，可在测试前把
`/sys/module/qcom_icp_test/parameters/hdr_log_acks` 设为 `1`，结束后恢复为 `0`。固件错误仍会写入内核日志。

需要带 `0013-liuqin-direct-ife-capture.patch` 的项目 libcamera、OpenCV、libyuv、
Python GStreamer 绑定，以及带 10 位支持的 GStreamer VP9 编码器。Fedora 的 `python3-gstreamer1` 提供可写缓冲区映射，
允许 HLG 转换直接填充编码器使用的帧。仅有基础 GI 绑定时通过复制填充。构建前述含两颗
模组原尺寸配置的 IPE 模块，使用匹配的内核和本机 manifest。两档宽屏另需上述宽屏配置。用户态可在平板上构建。

```sh
tests/build-camera-hdr-tools.sh out/hdr-tools
sudo liuqin-hdr-photo --sensor rear --resolution 3840x2160 --output uhd.jpg
sudo liuqin-hdr-video --sensor rear --resolution 4080x2296 --seconds 15 --output wide.mkv
sudo liuqin-hdr-video --sensor rear --resolution 3840x2160 --seconds 15 --output uhd.mkv
sudo python3 out/hdr-tools/camera-hdr-video.py --sensor front --seconds 10 --output front-hdr.mkv
ffprobe -v error -count_frames -select_streams v:0 \
  -show_entries stream=width,height,profile,pix_fmt,color_space,color_transfer,color_primaries,nb_read_frames:format=duration front-hdr.mkv
python3 tests/camera-hdr-motion.py --motion out/hdr-tools/hdr-motion --pixels out/hdr-tools/hdr-pixels
python3 tests/camera-hdr-video-pixels.py out/hdr-tools
c++ -std=c++17 -O3 tests/camera-hdr-warp.cpp -o out/hdr-tools/check-warp $(pkg-config --cflags --libs opencv4)
out/hdr-tools/check-warp
sudo python3 out/hdr-tools/camera-icp/check-hdr-photo.py --sensor rear --size 3840x2160 --recording
```

产物应解码为 `yuv420p10le`、`bt2020nc`、`arib-std-b67`、`bt2020`。
用解码帧数除以实际时间检查帧率。用 `ffprobe -v error -select_streams v:0 -show_entries frame=pts_time -of csv=p=0 uhd.mkv` 导出逐帧时间，
检查相邻帧间隔的第 95 百分位和最大值。运动测试使用已知平移、独立移动物体和显露背景，
同时检查遮挡区回退到中曝光及 HLG 黑白端点。EVA 硬件配准、视频速率优化和
HDR 模式的桌面应用选择仍待接入。
