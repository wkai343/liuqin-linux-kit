# Liuqin 睡眠后亮屏补偿

这是可选的普通用户服务，保留 Fedora 官方 PowerDevil、KWin 和 Mesa。
PowerDevil 在恢复时发出亮屏请求，而 KWin 的真实输出尚未重建时，
libkscreen 可能跳过该请求且不重试。此服务仅补偿该恢复窗口。

服务监听 logind 的 `PrepareForSleep`，要求亲自记录了睡眠前的活动本地
Wayland 会话，且内核成功挂起计数恰好增加一次。它动态解析本次
`pm_wakeup_irq` 的中断名称，仅接受 `pmic_pwrkey`、`nanosic-data` 和
`nanosic-wakeup`。这些是唤醒源分类，不是键盘按下事件的逐键证明；
不打开输入设备、不读取或记录键盘内容。定时器、网络、背折及未知来源不补偿。
内核在冻结进程时清除旧唤醒 IRQ，因此不能用未经过完整睡眠的旧值触发。

恢复后最多等待 4 秒。服务要求同一用户的同一活动 Plasma 会话、唯一
Wayland socket、UPower 与键盘驱动均未报合盖、真实 `DSI-1` 输出已连接且
在桌面配置中启用。键盘唤醒还要求已连接且未被背折/合盖策略屏蔽。
当前 KWin 的 DPMS 是全局操作，`kscreen-doctor` 已移除旧版的
`--dpms-excluded` 参数。因此还要求未连接任何外屏，随后通过
`kscreen-doctor --dpms on` 最多发送一次 DPMS On，最多再等
2 秒确认驱动的显示和背光状态。即使命令超时也不重发，避免覆盖后来的关屏意图。
正常熄屏、服务启动/重启、会话切换和下一次睡眠不会沿用旧的请求。

这是对当前桌面恢复时序的设备适配，不是 PowerDevil 上游修复。
只处理单内屏和已经识别的电源键/磁吸键盘唤醒源；连接外屏时不补偿。
USB/蓝牙输入及未知来源
仍由桌面原有逻辑处理。其他面板和多用户往返需分别验证。

## 构建、启用与移除

在装有 `rpmbuild` 的构建机运行：

```sh
python3 tests/resume-display.py
python3 build/scripts/build-resume-display.py --out out/packages/resume-display
```

将生成的 `liuqin-resume-display-*.noarch.rpm` 复制到平板后安装，
在实际桌面用户的终端执行用户服务命令：

```sh
sudo dnf install ./liuqin-resume-display-*.noarch.rpm
systemctl --user daemon-reload
/usr/libexec/liuqin-resume-display --check
systemctl --user enable --now liuqin-resume-display.service
journalctl --user -b -u liuqin-resume-display.service
```

`--check` 只读取设备与会话状态，不发送亮屏请求。
此包不覆盖或锁定 Fedora 包，系统升级后继续存在；上游修复后可停用并删除：

```sh
systemctl --user disable --now liuqin-resume-display.service
sudo dnf remove liuqin-resume-display
systemctl --user daemon-reload
```

验收使用菜单睡眠、等待数秒、只短按一次电源键或键盘 Shift；核对实际画面，
以及服务日志中的 `dpms-on-sent`/`complete` 或 `already-on`。
合盖应保持黑屏，开盖后由原有桌面策略恢复；服务不能仅因合盖/开盖而补发请求。
日志和实机记录放在忽略的 `out/` 目录。

来源：[PowerDevil 恢复流程](https://github.com/KDE/powerdevil/blob/v6.8.2/daemon/actions/bundled/dpms.cpp)、
[libkscreen DPMS 请求](https://github.com/KDE/libkscreen/blob/v6.8.2/src/libdpms/waylanddpmshelper.cpp)、
[KWin 输出生命周期变更](https://github.com/KDE/kwin/commit/41097b10a34aa2d5d6257a58244c22e1ac53bac3)。
