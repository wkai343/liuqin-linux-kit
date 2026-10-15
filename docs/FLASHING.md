# 安装要求与恢复

[项目首页](../README.md) ｜ [安装助手](INSTALLER-GUI.md) ｜ [CLI 与验收](INSTALL-TESTING.md)

## 设备与安装文件

仅支持已解锁 Bootloader 的 Xiaomi Pad 6 Pro（liuqin，SM8475）。
使用接口相容的 `installer.img`、`boot.img`、`rootfs.tar.gz`，电脑端安装器单独提供。
256GB 单根已实测，128GB / 512GB 仅通过容量规划离线检查。
新版 GUI 从头安装、Btrfs、Windows USB 安装和 Android 恢复仍待各自验收。

安装器读取真实 GPT，要求 userdata 唯一、位于第一颗 UFS（sda）、至少 16 GiB，
逻辑块为 4096 字节，并符合原厂 35 分区布局。编号、边界不套用其他容量的数值；
身份或布局不能确认时停止。其他型号、任意多分区和修改过的原厂布局不在当前支持范围。

## 单根布局

`linux-single` 使用整个 userdata，格式可选 ext4 或 Btrfs，系统位于 `native-root`，
`/home` 位于其中，从 A 槽启动。Btrfs 的 `native-root` 为子卷；不创建独立 home 或额外 GPT 分区。
userdata 边界保留，Bootloader 切槽仍可能改变 GPT 的启动属性。

**首次安装与重装均会格式化 userdata。** 安装前把个人文件备份到平板以外，
另存本机原 boot、persist 和 GPT。设备备份不含 userdata 文件，也不是完整 Android 恢复包；
不能使用其他平板的校准或设备地址。备份复用见 [安装助手](INSTALLER-GUI.md#备份复用)。
日常软件和内核升级无需重装 rootfs，见 [内核更新](KERNEL-UPDATES.md)。

## 数据与恢复

恢复 Android 需要适配本机、满足防回滚要求的原厂 Fastboot ROM，并重新准备 Android userdata。
仅刷回 boot 不能撤销 Linux 安装或恢复个人文件。若旧系统曾拆分 userdata，
先按 CLI 分区表恢复流程还原原厂边界；当前单根布局保留边界。

保留原厂防回滚检查，不恢复其他平板的 persist；存在非原厂镜像时保持 Bootloader 解锁。
恢复原厂固件会覆盖 Linux 启动链，相关清刷会清除数据。此恢复路线尚未单独真机验收，
不提供任意版本 ROM 混刷或自动恢复保证。

上游双系统与 KernelSU 组件作为适配参考保留，见 [组件说明](../device/android/ksu-boot-ubuntu/README.md)。
当前安装入口仅接受 `linux-single`；双系统参考代码不代表 Fedora 支持双系统或 A/B 自动回滚。
