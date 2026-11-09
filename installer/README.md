# 电脑端安装器开发

[GUI 操作](../docs/INSTALLER-GUI.md) ｜ [Rec 后端](../docs/NATIVE-RECOVERY-INSTALLER.md)

当前产品使用五步、分区优先的 QML 向导。安装环境为配套原生 ADB Rec；Linux 材料为配套 boot/rootfs，Android 材料为受支持的完整 ROM。安装器不调用编译器或 Devbox，运行环境使用配套 Recovery 镜像。

## 运行和源码边界

```sh
uv run --project installer installer/install-gui.py
uv run --project installer installer/install-gui.py --recovery-image /path/to/temporary-rec/recovery.img
uv run --project installer installer/install-gui.py --demo
```

`pyproject.toml`、`uv.lock` 和 `.python-version` 管理 Python 3.12、PySide6-Essentials 及镜像处理依赖。Windows 可运行 start-windows.cmd，ADB/Fastboot 由 Platform-Tools 提供，不配置 USB 网卡。

| 位置 | 职责 |
| --- | --- |
| `liuqin_gui/qml/Main.qml`、`wizard.py` | 当前五步 UI、选定目标、材料和统一确认 |
| `controller.py` | Qt/worker 通信、日志、报告脱敏、Ctrl+C 与关闭保护 |
| `worker.py` | 串行 JSON 请求、取消边界及后端生命周期 |
| `engine.py`、`workflow.py`、`workflow_capacity.py` | 不依赖 Qt 的安装、组合任务、备份及容量核对 |
| `boot_switch.py`、`boot_store.py` | 独立默认切换、中断检查/恢复与清理预览 |
| `native_adb.py`、`native_installer.py` | ADB、环回材料传输与绑定本次 Rec 的会话 |
| `backups.py`、`fixed_recovery.py` | 备份索引/复用和本机固定 Rec 保护 |
| `../device/installer/`、`../device/recovery/`、`../shared/` | 设备执行、能力/分区范围及共用接口 |

连接只识别，点击读分区才按需进入环境。确认计划绑定材料、设备、当前启动、布局、备份和旧引导，写入阶段拒绝取消/关闭。

## 当前 worker 协议

```sh
python3 installer/install-gui.py --worker --demo --state-dir /tmp/liuqin-demo
```

stdin 每行一个 JSON 对象，含 action 和可选 id；stdout 每行一个事件，含 schema_version=2、seq、type、请求/会话标识及 simulation。模拟数据不连接平板，DEMO 备份不能用于设备恢复。

| action | 主要输入/作用 |
| --- | --- |
| devices | 枚举设备 |
| inspect_environment | serial、runtime_path；只读识别与镜像匹配 |
| wizard_scan | serial、runtime_path；读取真实分区与系统登记 |
| wizard_prepare | snapshot_id、actions、partition_config、install_targets、材料、default_target、backup_parent、固定 Rec 策略；准备统一计划 |
| wizard_switch_prepare | system_id、snapshot_id；准备默认引导切换 |
| boot_store_inspect | 只读检查事务、默认引导和空间 |
| boot_store_prepare | snapshot_id、operation、transaction；准备恢复或清理计划 |
| wizard_resume_prepare | 原失败 receipt.json；准备严格限定的新布局初始化续接 |
| wizard_install | session_id、plan_id、确认序列号及 accept_operations/allow_experimental |
| rom_preview | 材料/基线、策略和当前设备；生成独立只读预览 |
| native_export_logs | serial、runtime_path、destination；导出经过核验的 RAM 日志包 |
| cancel、shutdown | 仅在允许的准备/空闲阶段结束任务 |

预览没有可执行的 workflow 确认资格。默认切换和维护也走独立准备/确认，不复用任意旧 plan_id。当前向导始终使用 recovery_mode=off，不附加“其他 Recovery”末尾操作。

请求并发会被拒绝。stdin 断开不会强杀正在写入的线程；已准备而未执行的会话可取消清理。真实断线、进程崩溃或断电的恢复依赖持久记录与实际设备核验，不是简单重放整个请求。

## 底层兼容协议

worker 继续提供 validate、backup/find_backup/select_backup、prepare/install、scan_layout、scan_partitions、verify 等兼容动作。linux-single、multi-root、partition-reset、partition-only、linux-managed 和 android-manage 是低层引擎类型，当前 GUI 不把它们显示为操作模式。

兼容 prepare 使用显式备份、材料、layout 及对应配置；install 继续核对其 session_id、plan_id、serial 和针对性的清除/保留确认。`install-liuqin.py` 提供 GUI 共用辅助接口；日常安装使用上述向导协议。

分区/Android/Linux 的详细保护和中断边界分别见[多系统设计](../docs/MULTIBOOT-DESIGN.md)、[Android ROM](../docs/FULL-ROM-FLASH.md)及[Rec 安装后端](../docs/NATIVE-RECOVERY-INSTALLER.md)。

## 测试与打包

```sh
python3 tests/gui-engine.py
python3 tests/gui-worker.py
uv run --project installer python tests/gui-qml.py
uv run --project installer python tests/gui-device-first.py
uv run --project installer python tests/gui-combined-install.py
```

所有 GUI 回归使用当前 Main.qml、真实模拟 worker 和 offscreen/software 渲染；窗口关闭/信号保护、报告、ROM 预览及引导维护分别覆盖。完整项目的测试说明位于 tests/README.md；独立分发包不包含 tests/。

`python3 installer/package.py` 默认导出不带镜像的源码包。使用 `--native-recovery`、`--adbd-sources` 可内置临时原生 Rec 与所需来源；`--fixed-installer` 附带固定版的校验镜像，不自动刷入它。分发包包括必要主机/设备源码、共用接口、文档和许可，排除 .venv、构建缓存及设备备份。

带镜像的包以随包 STATUS.txt、SHA256、构建 ID 和硬件记录说明资格；全新构建须独立验收。
