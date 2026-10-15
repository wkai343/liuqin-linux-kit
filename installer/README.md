# 电脑端安装器

支持 Linux、Windows 10/11 x64，读取三个安装文件，不编译内核或 rootfs。
使用步骤、Windows 驱动与网络设置见 [安装助手](../docs/INSTALLER-GUI.md)。

```sh
uv run --project installer installer/install-gui.py
uv run --project installer installer/install-gui.py --bundle /path/to/images
python3 installer/package.py
```

`pyproject.toml`、`uv.lock`、`.python-version` 定义独立的 managed Python 3.12 环境，
依赖仅包含 PySide6-Essentials。Windows 可运行 `installer/start-windows.cmd`。
源码包包含 `installer/`、`shared/` 和 `device/installer/` 所需文件，不包含镜像或构建环境。

## 源码与离线测试

| 位置 | 用途 |
| --- | --- |
| `liuqin_gui/qml/Main.qml` | 六页 QML 向导 |
| `liuqin_gui/controller.py` | Qt 与 worker 的进程通信 |
| `liuqin_gui/engine.py` | 不依赖 Qt 的安装引擎与设备传输 |
| `liuqin_gui/backups.py` | 备份校验、索引及旧备份导入 |
| `liuqin_gui/worker.py`、`demo.py` | 串行 JSON 协议与模拟实现 |
| `install-liuqin.py` | CLI、设备通信与布局辅助 |
| `../device/installer/`、`../shared/` | 设备安装脚本与公共接口 |

```sh
uv run --project installer installer/install-gui.py --demo
python3 tests/gui-engine.py
python3 tests/gui-worker.py
uv run --project installer python tests/gui-qml.py
```

演示不访问设备，界面显示“演示模式 · 不连接设备”；DEMO 备份不可用于恢复。
QML 测试使用 offscreen/software 渲染；独立源码包不包含 `tests/`，测试在完整仓库执行。
正常启动进入设备安装流程，不显示模式标签。

## 后端协议

单独启动模拟 worker：

```sh
python3 installer/install-gui.py --worker --demo --state-dir /tmp/liuqin-demo
```

stdin 每行一个 JSON 请求，含 `action` 和可选请求 `id`；stdout 每行一个 JSON 事件。
事件含 `schema_version`、`seq`、`type`，安装事件另带 `session_id`、`request_id`；模拟事件标记 `simulation=true`。
当前事件 schema 为 2，备份描述 `backup.json` 为 1。

| action | 参数 |
| --- | --- |
| `devices` | 无 |
| `validate` | `bundle` |
| `find_backup` | `serial`、`backup_parent` |
| `select_backup` | `serial`、`backup_dir` |
| `backup` | `serial`、`bundle`、`backup_parent`；可选 `force_new=true` |
| `prepare` | `serial`、`bundle`、`backup_dir`；`layout` 仅接受 `linux-single`，`filesystem` 可选 `ext4` 或 `btrfs`，省略时为 `ext4` |
| `install` | `session_id`、`plan_id`、`confirmation` |
| `verify` | `session_id`、`desktop_and_reboot_verified=true` |
| `cancel`、`shutdown` | 无 |

`prepare` 必须使用已有备份，不隐式创建备份；`backup` 默认复用，`force_new` 明确另建。
`confirmation` 要求匹配的 `serial`，以及严格为 `true` 的 `personal_data_backed_up`、`erase_userdata`；
未验收包还需 `allow_experimental=true`。方案绑定设备、RAM 会话、文件、布局、格式和备份 ID。

并发请求会被拒绝，准备完成后只接受 `install` 或 `cancel`；更换方案须重新准备。
准备/备份进行中可请求取消，写入期间拒绝取消。stdin 断开时 worker 等待当前操作结束，
清理准备会话并保存记录；进程被强制杀死、断电或断线仍可能中断写入。
