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
| `backlight-default.py`、`boot-splash.py` | 亮度量程、初始化等待和启动显示交接 |
| `storage-seal.py`、`storage-ready.py`、`storage-paths.py` | 批量只读设置、UFS/GPT 就绪证明、路径替换及回退 |
| `wlan-mac.py` | 目标网卡 udev 初始化、命名竞态与设备身份 |

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

真机测试分别记录临时启动、固定引导后的重启、拔线冷启动及睡眠唤醒；
故障后自动恢复必须单独记录，不能写成“首次显示正常”。
