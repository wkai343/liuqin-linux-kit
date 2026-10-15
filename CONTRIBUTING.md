# 参与 Liuqin Linux Kit

[项目首页](README.md) ｜ [仓库与问题反馈](https://github.com/wkai343/liuqin-linux-kit/issues)

设备集成、安装器、构建工具和文档放在本仓库。
设备内核来源由 `build/kernel/source.json` 选择；本项目的默认增量补丁位于
`build/kernel/patches/`。自定义内核可使用独立、已提交的源码目录和配置片段，
不要求先向原作者的内核仓库提交，也不要将内核源码检出或构建缓存放入本仓库。

提交应围绕明确的问题，说明最终行为、修改原因和验证结果。适配第三方代码时保留原作者与许可证声明。修改后构建、测试受影响的组件，再提出变更。

记录真机结果时注明型号、源码版本、镜像版本和复现步骤。区分构建成功、模拟测试和实际硬件验收。共享日志前删除凭据、设备地址、序列号和个人信息。

不要改写已发布的版本标签。发行版需标明源码与构建输入，安装和恢复说明必须与验证过的镜像一致。除用户明确选择重装外，内核与根文件系统更新应保留账号和文件。

## 安装器开发

QML、Python 后端及备份逻辑在 `installer/liuqin_gui/`；公共接口与设备脚本在 `shared/` 和 `device/installer/`。
源码说明和后端协议见 [安装器文档](installer/README.md)。先运行演示与离线测试，再安排真机验收。

演示和自动 GUI 回归：

```sh
uv run --project installer installer/install-gui.py --demo
uv run --project installer python tests/gui-qml.py
```

演示不访问平板，DEMO 备份不可用于恢复；离线结果不能替代真机验收。

设备备份、系统镜像、运行状态及私有校准材料不要放入源码提交。

新增发行版适配时区分板级共用代码和发行版/桌面专用依赖，并记录实际验收范围。原作者及第三方 Copyright、SPDX、许可证和来源文件不能作为清理内容删除；见 [来源与致谢](CREDITS.md)。
