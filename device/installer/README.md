# 平板端安装脚本

当前脚本由电脑 GUI/worker 通过配套原生 Rec 调用，承担 Android/Linux 写入、分区编辑、根身份核验、本机校准导入和引导事务。只接受与当前设备、分区、材料和确认计划绑定的操作；普通安装不会给任意固件分区开放格式化权限。

`android-device.py` 同时维护 Android、Linux 和分区事务；`install-root.sh` 根据已绑定的 PARTUUID/范围安装 ext4/Btrfs rootfs。原始单根/首次布局脚本继续供底层兼容协议和离线回归使用，不是当前 GUI 的模式选择器。

Rec 的会话、原生 boot 写入、默认切换、中断恢复和引导存储管理位于 device/recovery/，共用规则位于 shared/。细节见[安装后端](../../docs/NATIVE-RECOVERY-INSTALLER.md)和[多系统设计](../../docs/MULTIBOOT-DESIGN.md)。

这些脚本操作实际存储，只能通过核验和确认流程执行。离线测试使用普通临时文件、合成 GPT 和重定向挂载，不访问真实平板。
