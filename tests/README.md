# 测试说明

[参与贡献](../CONTRIBUTING.md) ｜ [构建指南](../docs/BUILD.md) ｜ [待改进与验证](../docs/DEVELOPMENT-STATUS.md)

在仓库根目录使用 Devbox，避免宿主编译器、BusyBox 或 Python 依赖不一致：

```sh
devbox run test
devbox run -- sh tests/persistent-root.sh
devbox run -- python3 tests/docs-links.py
```

`devbox run test` 覆盖构建、安装器、引导更新、存储保护、显示服务等离线回归。
测试使用临时目录、合成镜像或模拟设备，不刷写平板；QML、源码阶段验证和真机验收另行执行。
CI 的实际测试列表以 `.github/workflows/` 为准，不能把本地入口通过等同于全部 CI 作业通过。

## 显示与启动回归

| 测试 | 检查范围 |
| --- | --- |
| `display-clock.py` | 补丁中的时钟取整、频率边界和 CCF 约束 |
| `display-policy.py`、`display-recovery.py` | CABC 配置、活动会话、恢复限流、失败与退出处理 |
| `backlight-default.py`、`boot-splash.py` | 亮度量程、初始化等待、背光/显示时钟装载顺序，以及充电与动画退出路径 |
| `splash-handoff.py`、`splash-logo.py`、`splash-orientation.py` | 帧缓冲边界、绘制进程隔离与回退、四向特征识别、关机方向缓存及 systemd 直接入口 |
| `storage-seal.py`、`storage-ready.py`、`storage-paths.py` | 批量只读设置、UFS/GPT 就绪证明、路径替换及回退 |
| `wlan-mac.py` | 目标网卡 udev 初始化、命名竞态与设备身份 |

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

真机测试分别记录临时启动、固定引导后的重启、拔线冷启动及睡眠唤醒；
故障后自动恢复必须单独记录，不能写成“首次显示正常”。
