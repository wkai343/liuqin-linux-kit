# QML 安装助手 v0.2.1

电脑端安装向导，基于 Qt Quick / QML 和 PySide6，后端为独立 Python JSON worker。
第一版支持 Linux 主机、Xiaomi Pad 6 Pro（liuqin）、配套单根 ext4 安装包和 A 槽引导。

v0.2 将设备备份拆成独立操作。准备安装必须选择已核验备份，不再自动备份分区；原始备份与本次运行日志分开保存。

v0.2.1 修正原始 persist 恢复备份与当前分区整分区哈希不同就阻止复用的过严判定。快照差异只作提示；仍检查当前校准输入，并绑定准备时的设备状态，确认后再核验。

## 启动

需要 Python 3.11+、PySide6、Fastboot，以及能访问平板的 USB 与 USB 网络环境。
已有 PySide6 时可以直接运行；没有时建议在虚拟环境安装：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-gui.txt
.venv/bin/python tools/install-gui.py --demo
```

演示模式使用内置模拟材料与模拟平板，完全不调用 Fastboot、设备 socket 或块设备。生成的备份带有 DEMO 标记，不可用于恢复平板。

真机界面：

```sh
python3 tools/install-gui.py --bundle /path/to/matching/bundle
```

打开真机界面只枚举设备并查找本地已有备份，不自动开始安装。在“连接平板”页单独创建设备备份，或选择已有备份；之后点击“检查安装方案”。写入前还需检查方案、勾选确认并输入所选设备的序列号。已有 Linux 的 userdata 也会在重装时全部清空。

旧上游 v0.6.0 安装包未声明单根能力，会被拒绝。必须选择包含匹配 installer.img、boot.img、rootfs.tar.gz 和 bundle.json 的配套单根包。OFFLINE_ASSEMBLED 包会要求显式接受实验状态。

可以用 `--state-dir DIRECTORY` 指定运行记录位置；默认位于 Qt 用户数据目录。新备份位置也可在界面更改，必须位于安装包目录以外。程序不要求整个 GUI 以 root 运行，USB 权限由电脑环境提供。

## 独立备份与复用

- “创建设备备份”：先查找并核验已有备份。已有合格备份时直接复用，不连接 RAM、不重复生成分区镜像；没有备份时临时启动 RAM，读取 boot_a、boot_b、persist 和 GPT，完成后返回 Fastboot。
- “选择已有备份”：支持此前命令行安装器和 v0.1 真机流程保存的目录，不改写这些目录。
- “查找已有备份”：读取按序列号保存的索引及所选备份位置，重启程序后仍能找到已选备份。
- “另建备份”：明确创建新目录，保留全部旧备份；用于按需保存另一个时间点的设备快照。
- “检查安装方案”：仅核验已有备份、当前设备和实际分区布局，不再调用分区备份传输。

本地复用检查包括文件 SHA256、备份序列号、GPT 报告及几何信息。准备和执行安装时还检查当前分区边界、boot 分区大小以及 persist 全分区 SHA256。Bootloader 切槽引起的 GPT 类型 GUID / 属性变化不影响物理分区边界，单根安装后 boot 内容变化也不会使原始备份失效；不要求当前 boot 与原始 boot 一致。

persist.img 是创建备份时的原始恢复材料，不是要求当前分区永远逐字节相同的镜像。整分区哈希包含文件系统元数据，差异本身不能证明具体校准文件改变或损坏。v0.2 的“当前校准分区与备份不同，请另建备份”阻断在 v0.2.1 改为可见提示，不改写原始文件，也不自动新备份。

当前 persist 以 ro,noload 方式挂载，在 RAM 中临时目标运行安装镜像自带的 provision.sh，检查其当前输入能按项目规则导入（WLAN/蓝牙地址、音频校准和传感器注册表）。这里只是输入规则检查，不等于硬件功能验收。临时目录与挂载随后清理，原始分区不写入。准备时记录当前 persist 哈希并绑定方案，确认后若当前状态再次变化，仍在格式化前停止。

新备份在全部文件完成后发布 backup.json。安装方案 ID 同时绑定备份 ID；确认后再核验。设备备份保存在所选备份目录，receipt.json / events.jsonl 改存 state-dir/runs/SESSION_ID，避免覆盖原始备份的记录。

设备备份不包含个人 userdata 文件，也不等于完整 Android 恢复包。备份保存的是创建时的设备状态；重装后的新快照不能替代此前的原厂启动备份。

## 已实现

- 六页向导：准备、连接、方案、确认、进度、结果。
- 包 SHA256 检查、单根能力检查和内核版本核验。
- Fastboot 序列号绑定、机型/解锁/槽位/boot 大小/下载上限检查。
- RAM UUID 与序列号核对；读取真实 GPT，不使用 256GB 示例作为真实方案。
- 独立设备备份、已有备份复用与原始材料保留；备份操作不要求满足整个系统归档的 RAM 暂存门槛。
- 安装时检查电量及 RAM 暂存空间，核验已有备份和当前设备匹配情况。
- 方案 ID 绑定设备、RAM 会话、包、真实分区信息和 A 槽引导；安装前重新校验。
- 每设备锁，阻止同一用户启动的两份程序同时操作同一平板。
- 单根格式化、系统解压、本机校准配置与根系统启动文件校验。
- boot_a 整分区写入后再次临时启动 RAM，回读 SHA256；通过后由 Bootloader 选择 A 槽并启动。
- 准备阶段有序取消；写入期间拒绝取消和正常关闭窗口；重复安装请求被拒绝。
- JSON 阶段事件、真实校验/发送字节进度、不定进度阶段，以及持久 receipt.json / events.jsonl。
- 进程写入完成与首次桌面验证分开；桌面和正常重启由用户实际确认。
- 导出诊断报告，不复制私有分区备份，并替换报告中的序列号和已知私人路径。

主机暂时提供只绑定平板网络地址的文件服务，路径有会话令牌和允许列表，备份不在服务范围内。安装脚本由当前应用提供，在 RAM 中校验后运行，保留项目根系统启动校验规则。安装后不新增持久管理员监听通道。

## 事件协议

后端单独启动：

```sh
python3 tools/install-gui.py --worker --demo --state-dir /tmp/liuqin-demo
```

stdin 每行一个 JSON 请求，stdout 每行一个 JSON 事件。事件含 schema_version、seq、type，安装事件另外带 session_id 和 request_id；所有演示事件标记 simulation=true。

请求 action：devices、validate、find_backup、select_backup、backup、prepare、install、cancel、verify、shutdown。

find_backup 参数：serial、backup_parent。
select_backup 参数：serial、backup_dir。
backup 参数：serial、bundle、backup_parent，另有可选 force_new=true。默认复用已有合格备份，force_new 明确创建另一个目录。
prepare 参数：serial、bundle、backup_dir、layout（目前仅 linux-single）。backup_dir 是必填项；旧的 backup_parent 不再触发隐式备份。
install 参数：session_id、plan_id、confirmation。confirmation 必须包含匹配序列号 serial，以及值严格为 true 的 personal_data_backed_up、erase_userdata；实验包还需 allow_experimental=true。
verify 参数：session_id、desktop_and_reboot_verified=true。

并发操作会被拒绝。准备完成后仅接受 install、cancel 或有序退出；方案变化必须重新准备。安装期间丢失 stdin 不会主动终止正在执行的写入，worker 会等待操作结束并保存记录。操作系统强制杀进程、电脑掉电或 USB 断线仍可能打断安装，不具备任意阶段自动恢复能力。

## 验证与边界

```sh
python3 tests/gui-engine.py
python3 tests/gui-worker.py
python3 tests/gui-qml.py
```

前两项只需 Python 标准库。QML 测试需 PySide6，使用 offscreen/software 渲染并运行完整演示向导。

本次已验证 30 项后端测试、3 项 worker 协议测试，以及独立备份→复用→方案→确认→安装→首次启动的 QML 完整演示，包含快照差异提示、只读校准检查与清理、以及确认后的状态变化阻断。另对本任务实际原始 CLI 备份执行了只读导入检查，目录未修改，未访问平板。原 CLI 与设备端根安装回归在 v0.1 开发阶段已通过，本次没有更改其安装逻辑。

新 GUI 真机端完整安装尚未执行。此前本任务验证成功的是脚本安装和引导修复流程，不能代替新 GUI 全流程真机验收。

第一版不提供任意多分区、双系统、Fedora/Plasma 选择、zram 内核构建、自动 Android 恢复或断点重装。失败时保存备份和执行记录，检查实际状态后决定后续操作；不要自动重跑格式化。

## 源码位置

- tools/liuqin_gui/engine.py：Qt 无关的 prepare/commit 引擎和真机传输。
- tools/liuqin_gui/backups.py：不可变备份的校验、旧备份导入、身份与布局兼容检查。
- tools/liuqin_gui/worker.py：串行 JSON 进程协议。
- tools/liuqin_gui/demo.py：不接触硬件的模拟实现。
- tools/liuqin_gui/controller.py：QProcess 与 QML 属性/方法桥接。
- tools/liuqin_gui/qml/Main.qml：六页界面。
- tools/install-liuqin.py：复用的设备通信、备份与布局辅助函数。
- tools/lib/install-root.sh：设备端安装与阶段标记。

事件协议/schema 当前为版本 2；backup.json 描述格式为版本 1。v0.1 客户端需改为先选择或创建备份，再发送带 backup_dir 的 prepare 请求。
