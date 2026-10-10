# 中文文档索引

[项目首页](../README.md) ｜ [English overview](../README.en.md)

常用文档默认中文，英文版本以 `.en.md` 结尾。原 `.zh-CN.md` 路径保留兼容。

| 内容 | 文档 |
| --- | --- |
| 安装包、设备要求与恢复准备 | [安装指南](FLASHING.md) |
| QML 界面、独立备份与后端协议 | [QML 安装助手](INSTALLER-GUI.md) |
| 命令行布局与安装步骤 | [安装步骤](INSTALL-TESTING.md) |
| 内核、设备组件和镜像装配 | [构建指南](BUILD.md) |
| 持续集成与发布材料 | [CI](CI.md) |
| 指纹使用与验证范围 | [指纹](FINGERPRINT.md) |
| 相机 | [相机](CAMERA.md) |
| 快充 | [快充](FASTCHARGE.md) |
| Android 应用容器 | [Waydroid](WAYDROID.md) |
| 源码位置、测试和贡献要求 | [参与贡献](../CONTRIBUTING.md) |

## 安装器源码

安装器与设备适配在同一仓库，无需另找独立前端仓库：

- `tools/install-gui.py`：电脑端入口。
- `tools/liuqin_gui/qml/Main.qml`：QML 向导。
- `tools/liuqin_gui/controller.py`：Qt 与独立后端通信。
- `tools/liuqin_gui/engine.py`、`backups.py`、`worker.py`：安装、备份和事件协议。
- `tools/install-liuqin.py`、`tools/lib/`：共用布局、通信及 RAM 安装脚本。

默认演示不接触设备；选择真机方案后，写入仍须用户确认。源码仓库不包含
个人备份、GUI 运行状态或完整安装镜像。
