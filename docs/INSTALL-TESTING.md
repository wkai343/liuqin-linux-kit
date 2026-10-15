# CLI 安装与验收

[项目首页](../README.md) ｜ [GUI 操作](INSTALLER-GUI.md) ｜ [安装要求与恢复](FLASHING.md)

日常安装使用 GUI。本页供命令行测试与安装后验收；设备、布局和备份要求见 [安装指南](FLASHING.md)。
电脑需要 Fastboot；CLI 需要 Python 3.11+。GUI 使用 uv 管理独立环境，Windows USB 设置见 [安装助手](INSTALLER-GUI.md)。

## 命令行安装

先检查三个本地文件，不访问平板：

```sh
python3 installer/install-liuqin.py --bundle /path/to/images --check
```

平板进入 Fastboot 后执行：

```sh
python3 installer/install-liuqin.py --bundle /path/to/images --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --layout linux-single \
  --filesystem ext4 --allow-unverified
```

将 `DEVICE_SERIAL` 和路径替换为实际值。Btrfs 使用 `--filesystem btrfs`，三个文件都须支持。
当前构建包尚未完成完整新安装验收，测试时需显式 `--allow-unverified`，写入前仍交互确认清空数据。
CLI 创建新备份，`--backup` 必须是镜像目录以外的新目录；已有备份复用使用 GUI。

安装临时启动 RAM 环境，读取真实分区表、备份启动分区和 persist/GPT，
格式化 userdata、解压系统、导入本机校准，最后写入 boot_a、回读并选定 A 槽。
`/home` 位于同一文件系统，重装会清空其中的文件，不能用 `--keep-home` 保留。

已有拆分布局时先使用本机原厂 GPT 备份恢复边界。分区表恢复是独立操作，
不会恢复个人文件或操作系统；Android 恢复准备见 [安装指南](FLASHING.md#数据与恢复)。

## 安装后验收

1. 完成初始化并进入桌面，检查中文、触控、存储和联网。
2. 检查 HiFi 声音、屏幕键盘，以及键盘接上/取下时的输入与旋转。
3. 正常重启，确认仍能登录；按需验证相机、指纹和其他外设。
4. 记录设备容量、源码提交、三个文件的校验值、测试结果与未通过项。

本机 256GB 已安装系统的结果见 [硬件说明](HARDWARE.md)，不替代新版 GUI、新镜像从头安装或其他容量验收。

失败时保留备份、安装记录和错误文字，先核对已完成阶段，再决定恢复或重试，避免重复格式化。
公开日志前移除个人信息；设备备份、校准和指纹私态不上传。
