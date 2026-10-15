# OEM 自适应模板持久化

厂商 fprintd 1.94.5 源码快照保持不变。`prepare.py` 将它复制到私有构建树，应用生成的
三文件补丁和 `oem-update.inc`。通过组件根目录的 `tools/build-fprintd-oem.sh`，使用平板
已安装的 libfprint ABI 仅构建守护进程。上游 PAM 模块与系统 libfprint 继续保留，候选守护进程私有安装。

TOD 驱动只在真实匹配成功后请求更新的不透明数据库。它克隆原 FpPrint，通过已录入指纹上的
私有 qdata 传递克隆。fprintd 在发出 `VerifyStatus` 前，于 `match_cb` 消费该标记：在共用的
录入/存储锁下比较磁盘指纹和原始版本，写入 root 私有临时文件，保留旧文件硬链接，并通过
rename 和 fsync 提交。失败时恢复内存中的原版、拒绝认证成功，并保留原文件或恢复链接。
取消、未匹配和不确定结果不发布自适应更新；发现逻辑忽略多字符恢复文件名。

历史记录中的原生目标编译与真实系统总线 GetDevices/Claim/Release 通过。
desktop-r3 启动记录具有真实 OEM 指纹，完成同指、重新加载和异指验收，用户也报告 GNOME 解锁。
位置敏感、卡顿及未覆盖的重试和冷启动范围仍以[用户指南](../../../../../../docs/FINGERPRINT.md)为准。
`prepare.py --patch-output PATH` 将生成补丁写入构建输出，不修改规范源码补丁。
