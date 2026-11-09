# OEM fprintd 适配

厂商 fprintd 1.94.5 源码快照保持不变。`prepare.py` 复制到私有构建树后生成补丁，通过 `tools/build-fprintd-oem.sh` 编译；系统 PAM 模块与系统 libfprint 保留。

多指驱动的数据库属于独立账号，重复录入检查仅加载当前已授权账号的手指。其他驱动继续沿用全局重复检查；D-Bus 认领、其他用户名授权及 Polkit 规则保持原实现。

新版多指数据库由账号助手在报告匹配前持久提交，FpPrint 保存不可变引用。提交失败或引用失效时拒绝认证成功。当前 TOD 不再提供旧单指格式的验证入口。

`prepare.py --patch-output PATH` 生成对应构建补丁，规范参考为 `fprintd-oem-update.patch`。源文件哈希、库 ABI 和实际测试范围分别记录；用户操作见[指纹指南](../../../../../../docs/FINGERPRINT.md)。
