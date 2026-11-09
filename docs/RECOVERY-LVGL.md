# Recovery 图形维护界面

[原生 Rec](NATIVE-RECOVERY.md)

原生 Recovery 已接入 LVGL 9.6.0，使用中文界面和经过校验的静态 ARM64 程序。显示继续使用Bootloader simplefb，不接管 DRM 显示输出，不启用外接显示或 3D GPU。

## 已提供的操作

- 系统列表显示已登记的 Android 和 Linux、根分区、文件系统及当前默认系统。点选其他系统后，Rec 核对引导镜像和系统登记，再显示原默认与目标供确认。
- 切换通过后回到首页，选择「启动默认系统」即可退出 Rec 并开机。写入和回读过程中，界面显示处理状态。
- 首页将系统启动与设备维护分组。「维护工具」汇总设备信息、分区查看、运行日志、日志导出与屏幕亮度。
- 顶部结合 PMIC 的 USB 输入供电与控制器状态显示已连接、未连接、连接中或准备中，USB 供电已消失时不会沿用控制器残留的“已配置”状态。独立采集和刷新在弹窗、系统检查期间继续运行；数据超过 5 秒未更新时显示状态未知。USB 已连接不代表电脑已建立或授权 ADB 会话。
- 设备信息：电量、内核、槽位、构建标识、可用内存和存储保护状态。
- 分区查看：分页显示分区名称、容量及只读标志，只提供查看。
- 运行日志：查看内核和 UI 日志，支持翻页及刷新。
- 日志导出：在 RAM 中准备诊断包，再由电脑安装器“导出 Recovery 日志”保存到选定目录；包内不包含 Android 用户文件或 Linux rootfs 内容。
- 屏幕亮度：按 10% 步长调节，范围 10–100%。
- 电源菜单：重启到默认系统、返回 Bootloader Fastboot、关机。均需在独立确认窗口确认，默认选中取消。

平板端切换需要带有 `tablet_boot_switch_version=1` 的 Rec、已登记且可核验的当前默认引导，以及至少 30% 电量。确认后仅为 `boot_a` 与 `linux_boot` 开放本次写入，写入前保存旧引导快照。提交时核验回读并保存旧登记，旧登记指向已核验的旧引导镜像。安装 ROM、配置或格式化分区由电脑安装器负责。

## 输入方式

屏幕触控已接入，支持用手指点按菜单和按钮。音量/电源键和键盘仍可操作。界面按单指处理；第二根手指不会把当前点击拖到另一处，也不提供多指手势。手写笔输入设备已识别，但尚未进行实物点按验收。

可使用音量上/下选择按钮、短按电源键确认。键盘方向键/Tab 切换，Enter 确认，Esc 返回。相对鼠标/触控板设备也有输入通道；已确认实体按键和手指触屏操作，未单独验收触控板。不要把长按电源当作界面确认，长按仍可能触发硬件强制重启。

光标默认隐藏，仅在鼠标或触控板实际移动时显示；使用导航按键或直接触屏输入后隐藏。系统枚举出鼠标接口不会使屏幕中央显示光标。

## 触屏启动

原来的 Novatek 驱动等待 DRM panel follower，但 Recovery 保留 Bootloader scanout，真实 DSI 面板驱动没有启动。专用 `liuqin_bootloader_panel.ko` 仅在准确 liuqin 板号、MDSS/DISPCC 停用且 simplefb 已驱动 1800×2880 framebuffer 时注册“面板已经准备好”的状态；不接管面板的 GPIO、pinctrl、供电、时钟或 DSI。现有 NVT follower 随后使用禁止工程复位的硬件 CRC 路径加载触控 RAM 固件。内置 CSOT/TM 固件，由已有板级驱动选取。

加载器先检查设备身份、存储只读、RAM 根文件系统、内核及模块/脚本/固件 SHA256，再加载模块并等待触屏就绪。LVGL 只有读到绑定本次 boot ID 的成功状态才接管直接触控设备；失败时保留实体按键和电脑控制。模块随 RAM 会话存在，重启后消失，不提供运行中卸载或面板断电操作。

输入按 Linux 的 [Type B 多点触控协议](https://docs.kernel.org/input/multi-touch-protocol.html)在 `SYN_REPORT` 边界提交坐标、保留快速点击的按下/松开，并处理事件丢失和启动时手指已按住的情况。面板状态衔接沿用内核 [panel follower 接口](https://docs.kernel.org/gpu/drm-kms-helpers.html#panel-helper-reference)。

## 退出与日志保护

电源和引导请求在主线程执行，使 BCB 清理能够设置中断处理；后台线程监控 LVGL 退出并刷新 USB 状态。回归通过真实 Unix socket 检查 BCB 清理及只读保护。

图形程序通过 root 专用的本地 Unix socket 调用固定操作。电源和引导切换使用 60 秒有效的单次确认令牌，绑定本次 boot ID。切换与电脑安装共用设备操作锁。电脑检查或写入期间，退出和切换都需要等操作结束。日志和 UI 状态保存在 RAM，最多保留最近三个导出包，重启或关机后清除。

电脑导出前核验设备、启动会话和运行时哈希，只允许读取固定 RAM 导出目录中命名有效、最多 2 MiB 的日志包，下载后再次检查大小和 SHA256。主机保存为新文件，不覆盖已有文件。

## 构建

```sh
# 项目 Devbox 工具链；首次缺少依赖时获取固定版本的源码。
python3 build/recovery/build-ui.py --out out/build/recovery-ui-new

python3 build/recovery/build-native.py \
  --kernel-source /path/to/clean/kernel/source \
  --kernel-out /path/to/kernel/build \
  --runtime /path/to/arm64/tools \
  --board /path/to/prepared/board \
  --ui-runtime out/build/recovery-ui-new \
  --out out/build/native-recovery-lvgl-new
```

上述是基础原型的构建入口；当前 ADB 安装版另由 build-native-installer.py 组合 UI、adbd 与专用内核组件，见[构建方式](NATIVE-RECOVERY.md#构建与交付)。基础原型带 --ui-runtime 时会编译触屏衔接模块，要求对应维护内核提交 `fe81794b5e1b8a3f11a165f3acbfdb8fa671dbb3`；内核更换后需重新适配和验证。模块源码与 GPLv2 文本在 `device/recovery/touch/`。

依赖版本和提交固定在 `build/recovery/ui-sources.json`。中文字体由 Noto Sans SC 生成子集，生成记录和 OFL 许可证在 `device/recovery/ui/fonts/`；普通构建直接使用这些准备好的字体源文件。LVGL、cJSON 和字体许可证随镜像与分发包提供。

使用 `build-ui.py --host` 可生成不接触设备的预览程序。程序必须同时使用 `--demo --preview FILE` 才能输出离线预览，不能把预览当作设备截图。测试菜单导航和确认窗口时可启用 CMake 的 `UI_EVENT_TESTS`，主机测试还可配合 AddressSanitizer/UndefinedBehaviorSanitizer。

新版临时镜像可先从 Fastboot 加载。平板端选用当前 ROM 安装记录中匹配的 Recovery 摘要。若设备绑定的固定镜像独立于 ROM 安装，构建临时镜像时可用 `build-native-installer.py --qualified-fixed <匹配固定资格的 recovery.img>` 绑定该设备与固定镜像的摘要。新版固定镜像会直接检查盘上的签名、运行中的构建身份和完整摘要。固定版候选需单独通过真机冷启动和回退验收。
