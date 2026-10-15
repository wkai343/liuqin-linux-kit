# KWin 排查参考

默认系统使用 Fedora 官方 KWin，不编译或安装这里的补丁。
liuqin 的键盘连接问题由 `build/kernel/patches/0001-nanosic-keyboard-tablet-mode.patch`
和设备 udev 规则修复，官方 KWin 已通过接上/取下键盘及重新登录测试。

排查发现 KWin 6.7.5 的两个独立问题：备用鼠标判断在延迟删除期间可能覆盖真实开关，
以及混合合盖/平板设备的事件丢失开关类型。保留补丁和真实观察器回归检查供上游研究，
当前驱动提供独立平板开关，设备策略处理常驻指针接口，无需更换 KWin。

固定来源与校验值见 `source.json`。单独下载其中归档后运行：

```sh
python3 tests/kwin-tablet-mode.py --source-archive /path/to/kwin-6.7.5.tar.xz
```

检查需要 C++、pkg-config 和 Qt6Core 开发工具。`--unpatched` 可复现原有失败，预期返回非零。
原作者与 SPDX 声明保留，修改按上游文件许可分发。
