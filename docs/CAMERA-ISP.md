# IFE680 硬件 ISP 路径

[相机支持](CAMERA.md) ｜ [libcamera 适配](LIBCAMERA.md)

`0085`–`0090` 为完整 IFE680 增加前后摄 RAW10 → NV12 通路。前摄经过
CSIPHY2，后摄经过 CSIPHY3；二者选择 CSID0 IPP → IFE0 PIX，由两个写通道
分别输出 Y 和 UV。两颗传感器不能同时占用同一 IFE。
去马赛克、镜头阴影、白平衡和色彩转换在 ISP 硬件完成；CPU 读取低分辨率
Bayer 统计，执行基础自动曝光和白平衡，不处理整幅 Bayer 图像。
不运行 Android HAL，也不依赖 GPU 软件 ISP。

## 支持范围

| 项目 | 实现与限制 |
| --- | --- |
| 输入 | IMX596 / S5KJN1 RAW10，支持四种 Bayer 排列；libcamera 根据传感器安装方向设置翻转 |
| 尺寸 | 前摄 2592×1952 / 2592×1472，后摄 4080×3060 / 4080×2296 / 3840×2160。每档输出尺寸等于传感器输入尺寸 |
| 处理 | LSC411、WB201、BLS121、Demosaic361、CC131、Gamma151、CST12、MNDS23、RoundClamp12 |
| 输出 | 连续 NV12，sRGB 传递函数，BT.601 全范围；Y 后紧接 UV |
| 自动控制 | 32×24 Bayer 统计；中心加权 AE、50 Hz 快门步进、原厂相对白点轨迹与单机 D50 校准约束的 AWB |
| 校准 | 前后摄分别读取本机 EEPROM，双读及分区校验，应用白平衡与 17×13×4 阴影表；后摄另使用单机 AF 范围 |
| 帧率 | 前摄 30/60 fps、后摄 30 fps；固定帧时长，暗光达到曝光和增益上限后仍可能欠曝 |
| 并发 | 一个 NV12 流；PIX 独占完整 IFE，不支持与同一 IFE 的 RDI 同时采集 |

后摄的 4080×2296 和 3840×2160 使用原厂模式寄存器表，按传感器有效区域的中央裁剪
重采样本机阴影网格，通过 IFE 的 PIX 队列直接输出 NV12。与之配套的 libcamera
按输入尺寸核对 IPE 能力。加载宽屏配置后，三档后摄 NV12 均可走 IPE TF21；查询输出行宽前先在探测文件描述符上选择同尺寸输入。原厂 VFE680 的
`CAM_VFE_BUS_VER3_VFE_OUT_FULL` 单路上限为 4096×4096。前摄 5184×3904、
后摄 8160×6120 超出这条输出的上限；目前全尺寸原始图走 RDI。
可用 `tests/camera-hwisp-libcamera.py --camera "$ID" --size 3840x2160 --fps 60 --output out/uhd`
复核 NV12 缓冲区大小、逐帧时间戳和实测速率。

AE/AWB 是本项目的基础策略，不是原厂闭源算法。白平衡每六帧更新并平滑，
仅接受未饱和、接近经过单机校准的原厂白点轨迹的统计区域；纯色场景仍可能误判。
RAW10 黑电平 64 在 IFE 的 18-bit 域中为 16384，LSC/WB 保留该基准，
随后 BLS 扣除并归一化，避免放大黑电平引起暗部偏色。
Gamma 使用标准 sRGB 曲线。CC131 在白平衡之后、Gamma 之前执行原厂 IPE CC13
的色温矩阵，区间内保持系数、区间间线性插值。配方保存原厂五组参数；当前
2856–7500 K 控制范围使用其中四组，2000–2450 K 一组仅保留作原厂参考。原厂 IFE 自身是单位矩阵，
直接照抄不会补上 IPE 的色彩修正。这仍是选定 DRC/custom/LED/lux 分支的基础值，
不是完整原厂场景选择器。前后摄可选实时 IPE 恒等处理已经接入 libcamera；
原厂 TF21 单层时域降噪已接入可选 IPE 通路；连续三曝光 HDR 录像已通过独立命令接入，完整噪声模型、TF 运动补偿及人脸曝光仍待适配，见[ICP / IPE 验证](CAMERA-ICP.md)。

## libcamera 与桌面

配套 libcamera `5.liuqin15.fc45` 的 simple 管线在 IQ ABI 1/2 匹配且
`/run/libcamera/liuqin-imx596.json` 有效时优先选择前摄 PIX；后摄要求 ABI 2。
条件不满足时保留 RAW/CPU 软件 ISP。硬件路径只公布单流 NV12，
应用需要缩放或 RGB 时由下游转换，不把软件转换误称为 ISP 硬件缩放。

Fedora overlay 用 `qcom_camss.ife680_pix=1` 开放通路，并在 display-manager
启动前执行 `liuqin-front-calibration.service` 和 `liuqin-rear-calibration.service`。
两个服务各自等待对应传感器的 NVMEM provider，可并行执行，
最长 5 秒，含读取和解码的总启动超时为 8 秒；不拉入 `systemd-udev-settle.service`，
避免等待无关设备而阻塞整个 sysinit。provider 尚未出现时不能用启动条件直接跳过，
否则会错过稍晚完成的传感器探测。前摄要求 `0086`，后摄要求 `0089`；
旧内核不得单独安装此模块选项文件。已有 Fedora 系统手动复制配置、unit 和 helper 后，
需对这些目标路径执行 `restorecon`，再重载 systemd；保留 SELinux enforcing。
服务不复制别台机器的校准数据，也不导出模组序列号。
`AeEnable`、`AwbEnable`、曝光、增益、白平衡和帧时长可通过 libcamera 控制。
控制是异步应用的，元数据使用三帧延迟估算，尚不保证某一请求严格对应某一曝光帧。
`0086` 提供的矩阵控制通过类型、尺寸和范围检查后，libcamera 才公布 `ColourCorrectionMatrix`。
关闭 AWB 后可写 RGB 行优先的 3×3 矩阵，基础系数范围为 −2 至 2047/1024，
`0088` 的宽范围控制存在时扩大为 −4 至 4095/1024。
`0006` 同时公布 `ColourTemperature`（2856–7500 K）。关闭 AWB 后，色温可换算为
红蓝增益；只给增益时估算色温；显式矩阵优先。重新启用 AWB 后恢复自动矩阵选择，
重新启动从单机 D50 白点开始；元数据报告色温估计及实际量化后的矩阵。
原厂 `refPtV1` 的 A/TL84/D50/D65/D75 比值用于相对白点轨迹，先相对 D50 归一化再
乘本机 EEPROM 比值。2856/4000/5000/6500/7500 K 是本项目采用的光源标称温度，
并非从原厂解出的完整色温映射。AWB 在对数比值平面筛选中性区域，色温按倒数插值；
不支持混合光源分区校正，也不代表已通过各类 LED/荧光灯与标准色卡验收。
没有该扩展的旧内核继续使用原有硬件路径，不公布矩阵控制。

```sh
cam -l
# 从输出选择前摄 ID，不固定 camera 编号。
cam -c "$FRONT_ID" -s role=viewfinder,width=2592,height=1952 --capture=180 --metadata
gst-launch-1.0 -e libcamerasrc camera-name="$FRONT_ID" \
  ! video/x-raw,format=NV12,width=2592,height=1472,framerate=60/1 \
  ! fakesink sync=false
```

检查日志中的 `IFE680 NV12, hardware Bayer statistics and calibrated LSC`，
以及实际格式、帧数、时间戳和新内核错误。可运行
`python3 tests/camera-hwisp-libcamera.py --camera "$FRONT_ID" --fps 60 --frames 600 --output out/ife-libcamera`
核验系统 GStreamer 路径的帧数、完整 NV12 大小与实际帧率。GStreamer 的 `libcamerasrc` 和设备
提供器使用同一 libcamera 路径；Kamoso 前摄预览与拍照可使用此通路；其他桌面应用兼容性仍需分别验证。
测试桌面应用时保留 WirePlumber 的音频功能；暂停整个服务可能让 Kamoso 的
同步快门音等待到超时，即使 JPEG 已经保存。底层模块重载需要临时停服务时，
恢复服务和音频输出后再验收拍照交互。

## 后摄参数与对焦

S5KJN1 使用独立的原厂 Qtech CC13 IPE 参数及 `refPtV1` 白点，来源为
`com.qti.tuned.liuqin_qtech_s5kjn1_wide.bin`，SHA256
`ab4c7b74b4f4d88c65b2c545994e4c90314be3fee20b2b05042ca7e9114147ab`。
矩阵分支为 module 60、DRC 1–100、custom 1–64、LED 0、lux 226–316，
CCT 表 2053，叶子 2055/2057/2059/2061/2063；白点来自 module 197 的
7/6/3/2/1 项。两颗传感器原厂 RAW10 黑电平均为 64。
后摄从独立 S5KJN1 NVMEM provider 读取 GT24P128E 的 18432 字节，只使用本机
模组、AF、白平衡和阴影区校验通过的结果。`liuqin-front-calibrate --sensor s5kjn1`
沿用公共解码入口，输出 `/run/libcamera/liuqin-s5kjn1.json`。原厂测量阴影表位于
`0x65`，17×13 点按 R/Gr/Gb/B 交错存储；白平衡的 Gr/Gb 方向按后摄镜像标志处理，
不能套用前摄固定反转规则。阴影增益为 `reference × golden / measured`，参考值来自
原厂 LSC41 的 D50 分支（region 1745，lux 230–330、CCT 4700–9000），golden 为
region 1767。这是固定基准校准，尚未移植完整的光照、色温与增益分支插值。

后摄阴影网格采用原厂 4080×3060 LSC411 几何（level 3、subgrid 16×16、delta 4×3），
随实际传感器翻转重采样。内核必须公布 `BASE + 6` 几何能力才应用后摄网格；旧内核
仍可使用单机白平衡和 AF 范围。校准缺失或无效时退回模组白点、全镜头控制范围及
单位阴影网格；不能复用前摄或上一次配置的网格。

后摄曝光使用实际行时长、8 行下限和 22 行裕量。当前驱动的模拟增益为 1–64
整数控制；AE 保留小数目标，元数据报告实际整数增益，并按半个增益步长增加迟滞，
避免取整导致欠曝停滞或两个码值之间反复跳动。后摄默认连续对焦，沿用已确认
镜头写入和单调时间戳的对比度扫描，在硬件 NV12 的中央亮度平面采样；
CPU 只计算低密度梯度，去马赛克与色彩处理仍由 IFE 完成。
有效单机 AF 数据按原厂宏距、无穷远端点及各 20% 行程裕量限定扫描范围。
单机物距表通过 EEPROM 摘要校验后，管线按实测区间公布屈光度 `LensPosition`。
只有 EEPROM 端点时保留自动对焦。测量方法见[后摄手动对焦标定](LIBCAMERA.md#后摄手动对焦标定)。
该路径不使用 PDAF/BF 硬件统计，速度和暗场行为仍需继续调校。

```sh
python3 tests/camera-hwisp-libcamera.py --camera "$REAR_ID" \
  --size 4080x3060 --fps 30 --frames 600 --output out/rear-ife
python3 tests/camera-isp-multilight.py --sensor s5kjn1 \
  --libcamera /path/to/patched-libcamera --vendor /path/to/vendor
python3 tests/camera-isp-format.py --kernel /path/to/patched-linux
python3 tests/camera-rear-calibration.py --vendor /path/to/vendor
python3 tests/camera-rear-lsc.py --kernel /path/to/patched-linux --vendor /path/to/vendor
python3 tests/camera-lsc-coordinates.py --libcamera /path/to/patched-libcamera
python3 tests/camera-vfe-stop.py --kernel /path/to/patched-linux
python3 tests/camera-stream-unwind.py --kernel /path/to/patched-linux
```

后摄连续 NV12 需要 18727200 字节。PIX 的逻辑行数上限必须按内存平面的纵向
布局换算，NV12 同一内存平面含 Y 和 UV，不能将总字节数截为 `stride × 4096`。
格式回归直接执行内核协商函数，检查前后摄 NV12、NV12M、RAW 及额外 stride。
后摄 LSC 回归在仿真中执行固定摘要 HAL 的网格计算函数，对照真实内核 MMIO
写入的后摄几何，并验证禁用、跨尺寸旧网格及不支持的尺寸不会启用阴影校正。

## 内核接口与复现

这是本项目的实验 ABI，不是通用主线 CAMSS UAPI：PIX 子设备上
`V4L2_CID_USER_BASE + 0x3000` 为 ABI 版本（`0088` 为 2），后续依次为 Q10 红/蓝增益
`U32[2]`、只读帧统计 `U32[15362]`、停止采集时可写的阴影网格 `U16[884]`。
统计前两项为帧号、有效标志，其余是每区域 80 字节的原厂 AWBBG17 布局。
用户态核对帧号，忽略过期统计；接口不允许任意寄存器或 DMA 地址写入。
`0086` 在 `BASE + 4` 提供 `U16[9]` 色彩矩阵控制。每项是 RGB 矩阵的有符号 Q10
系数加 2048，取值 0–4095。默认使用单位矩阵，内核转换成 CC131 的 GBR 顺序和
12-bit 补码，在与 WB 相同的下一次寄存器更新中写入。该扩展不改变原有 ABI 1
控制的含义。`0088` 另提供 `BASE + 5` 的 `U16[9]`，偏置 4096、范围 0–8191；
矩阵超出有符号 12-bit Q10 范围时，整组改用 Q9 并同步改变 qfactor，避免截断
原厂后摄暖光系数。旧 libcamera 不识别 ABI 2 时会回退软件路径，升级须配套。
`0089` 增加只读 `BASE + 6` 整数能力，值 1 表示支持后摄 LSC 几何；ABI 版本仍为 2。
阴影写入记录当前宽高，并允许重复写入同值，防止切换相机时误用另一尺寸的网格。

停止输出必须在同一 `output_lock` 临界区将状态设为 OFF，再停写通道并清空活动计数。
缓冲完成中断忽略已停止或没有活动缓冲的输出，避免停止与解除 WM 映射之间的迟到
中断将计数减为负数，继而在重新排队时越界。`camera-vfe-stop.py` 执行实际驱动
函数，覆盖该窗口、空队列重启及负索引；硬件仍需交替前后摄、连续采集及反复启停。

`0090` 补齐 STREAMON 失败的回退：若传感器或其他上游节点启动报错，先停止此前
已经接受启动的子设备，再释放媒体管线和退回缓冲。失败节点负责自身回退，不能再
无条件停止它而重复释放电源引用。原先仅释放媒体管线会留下活动 VFE，后续配置
持续返回 EBUSY。`camera-stream-unwind.py` 执行实际启动与回退函数，逐节点注入
错误，核验没有残留启动状态、没有重复停止，并保留原始错误及缓冲状态。

关闭所有相机客户端和枚举服务后，可使用 `tests/camera-hwisp.py` 验证直接 V4L2
采集。模块必须与内核匹配；不要绕过版本检查，也不要用 `modprobe -r` 递归卸载
时钟和 VB2 依赖。CAMSS 的异步绑定会持有传感器模块引用；确认采集已停止、没有
客户端占用后，先卸载 CAMSS，再按需卸载传感器，不能强制卸载仍被引用的模块。
重新加载后等待 udev 完成，再读取校准。快速重载发生 CCI 超时时应保存日志，
不能将 STREAMON 回退修复视为底层 CCI 超时的根因修复。

```sh
sudo python3 tests/camera-hwisp.py --output out/ife-30 --fps 30 --frames 300
sudo python3 tests/camera-hwisp.py --output out/ife-60 --fps 60 --frames 300
```

脚本检查实际 DQBUF 数量、连续帧号、字节数、时间戳与新增错误。`--save` 保存
全部 NV12，空间为宽×高×3/2×帧数。校准还需分别核验：不采集时重复读取与 PM
释放；采集时读取返回 EBUSY；固定曝光下 unity/2× 阴影表的线性响应；翻转和裁剪
后平场均匀度；白纸中性与彩色物品实拍。普通白纸不能替代色卡和多照明标定。

临时启动前按[已安装系统的临时启动步骤](BUILD.md#已安装系统的临时启动)绑定并核对测试镜像，测试结束后返回已安装的默认内核。

## 原厂对照与校准边界

CSID、写通道与中断布局参考高通 GPL 相机代码的
[`cam_ife_csid680.h`](https://github.com/MiCode/vendor_qcom_opensource_camera-kernel/blob/6c6099a85cb3154fc8a744c17b6b9f5fd361dea8/drivers/cam_isp/isp_hw_mgr/isp_hw/ife_csid_hw/cam_ife_csid680.h)
及同一提交的 VFE680/680_110 定义。
IQ 模块寄存器地址、位域和写入顺序与本机固定 ROM
OS2.0.6.0.VMYCNXM 的 `vendor/lib64/hw/camera.qcom.so` 离线对照：
SHA256 `91146496f299c62ed2e79b7d7920cb525e8862e716dfbee7a857fffa9f7125d5`。
使用的是其中的 **Titan680** 实现；该库同时含有 Titan480，二者 IQ 地址不能混用。
仓库不分发原厂库，也不在设备上加载它完成这条路径。

`0080` 在前摄模组的设备树中描述了位于 `0x51` 的 EEPROM，独立 at24 节点默认禁用。
`0086` 让 IMX596 注册只读 NVMEM provider。读取时，驱动为整个模组供电，
持有传感器控制锁，并按 CCI 支持的最大读长分块传输。
采集中返回 EBUSY。注册 provider 本身不访问 EEPROM，避免独立 at24 探测与
传感器电源时序交叉。卸载先撤销 provider，再释放传感器状态。

EEPROM 的模块、白平衡和镜头阴影区使用原厂校验。17×13×4 阴影值是测量值，
转换为 `golden / EEPROM × reference` 后才是增益，不能直接写入 IFE。
LSC411 使用 14-bit 通道打包、Q10 增益，Gr/Gb 单机差异并入网格；
默认图像翻转和视频中心裁剪均在全传感器坐标中重采样。
本机 EEPROM 不进入源码或发行镜像，日志和测试记录只留在忽略的 `out/`。

色彩矩阵取值和分支来源见 [libcamera 调校](LIBCAMERA.md#前摄录像时序与基础调校)。
可使用主机 C 编译器、`unicorn` 和 `pyelftools` 复核真实内核写寄存器函数：

```sh
python3 tests/camera-isp-colour.py --kernel /path/to/patched-linux \
  --libcamera /path/to/patched-libcamera --vendor /path/to/extracted-vendor
```

测试在隔离的仿真地址空间调用固定摘要的 Titan680 packer，对照单位矩阵、
负数/边界系数及随机矩阵；不访问设备 MMIO。真机需固定曝光/WB，对照单位、
0.5 倍、红蓝交换和原厂矩阵，在线性域核验亮度比例，另外覆盖连续 30/60 fps。

## 时域降噪与 HDR 的硬件边界

原厂调校包含 `tf21_ipe_v2`（模块 80）、`hdr40_ipe_v2`（70）和
`hdr10p10_ipe_v2`（69）；库中对应 Titan680 TF211、HDR401 实现。
这些条目的存在不能证明某个前摄模式启用了它们。

| 功能 | 原厂接口证据 | 当前缺口 |
| --- | --- | --- |
| 时域降噪 | IPE TF211，FULL/DS4/DS16/DS64 当前帧及 REF 输入/输出 | 已接入原厂低增益区间参数、FULL_REF 双历史槽、首帧失效及会话恢复；仍缺增益插值、历史金字塔与运动配准 |
| 传感器多曝光 HDR | AAC 模组四个模式的 HDRExposureType、ZZHDRInfo、HDR3ExposureInfo、QHDRInfo 均为空 | 没有经过原厂模式验证的交错或同步多曝光输入，不能凭猜测新增传感器寄存器 |
| 多帧 HDR 拍照 | 两颗模组的 SAT 配置使用 ArcSoft。高通 CHI 另有两曝光和三曝光 IPE 拍照图 | 前后摄可分档锁定曝光，并将量化曝光、传感器时间戳和帧文件摘要逐帧对应。前摄 2592×1952、后摄 4080×3060、4080×2296 和 3840×2160 的静物拍照流已接通 IPE HDR401 整帧三曝光融合、10 位读回和 SDR JPEG 保存，另可选 640×480。拍照支持 CPU 运动配准和局部去重影，原厂最终色调仍待标定。 |
| 多曝光 HDR 录像 | 原厂 CHI 定义短、长曝光的 FULL/DS4 路由，静态三曝光图有两个 EVA 配准节点 | 独立录像工具使用三档连续采集、CPU 配准和 IPE HDR401。默认输出 640×480，后摄可选 4080×2296 或 3840×2160 的 10 位 HLG。输出按中曝光时间戳计时，EVA 未接入。 |

IPE 参考帧端口可对照高通 GPL 的
[`cam_icp.h`](https://github.com/MiCode/vendor_qcom_opensource_camera-kernel/blob/6c6099a85cb3154fc8a744c17b6b9f5fd361dea8/include/uapi/camera/media/cam_icp.h)，
固件会话及 CONFIG_IO/FRAME_PROCESS/ABORT/DESTROY 命令见
[`hfi_session_defs.h`](https://github.com/MiCode/vendor_qcom_opensource_camera-kernel/blob/6c6099a85cb3154fc8a744c17b6b9f5fd361dea8/drivers/cam_icp/fw_inc/hfi_session_defs.h)。
IPE 是独立于 CAMSS PIX 的固件通路；可选前后摄实时桥接已有 DMA-BUF 复制和连续图像会话，
可选 TF21 配置另接入 FULL_REF 端口和原厂调参命令。向 IFE 写入 TF 参数不能建立
时域处理。单层 TF 的静止降噪、移动边缘、场景切换与会话历史重置应分别验收；
后续仍需历史金字塔、运动估计、增益插值和曝光变化的历史管理。
静态 HDR 可在中曝光已饱和的受控灰阶上验证短曝光保留的高光。实景动态范围、
暗部噪声和移动物体的重影仍需定量验收。详见[三曝光拍照](CAMERA-ICP.md#三曝光硬件-hdr-拍照)。


`tests/camera-ipe-hdr-tuning.py --vendor /path/to/vendor --output out/hdr-audit.json`
使用匹配的原厂解析器核验前后摄 HDR401 树，并以合成的两/三曝光元数据执行参数
转换。两颗模组的 HDR401 默认 enable 均为零，转换后也保持关闭。
这项检查证明参数布局与调用链可复现；它不证明硬件不支持 HDR，也不能代替
真实曝光输入、融合和色调映射的验收。

多光源参数和本地估计器可离线核验（需要 C++ 编译器及用户提取的原厂 vendor）：

```sh
python3 tests/camera-isp-multilight.py --libcamera /path/to/patched-libcamera --vendor /path/to/vendor
```

此检查比对五组原厂 CCM 区间、五个白点，检查两个单机响应下的正反映射、矩阵连续性和
系数范围。硬件验收还须在固定场景切换暖光、日光及不同 LED，检查白纸中性、肤色、
收敛与闪动；数学检查不能代替这些实拍，也不能据此宣称与原厂画质相同。
