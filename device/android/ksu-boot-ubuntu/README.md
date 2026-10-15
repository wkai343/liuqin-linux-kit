# KernelSU 模块：从 Android 返回 Ubuntu

[安装步骤](../../../docs/INSTALL-TESTING.md)

`liuqin_boot_ubuntu` 是双系统切换的 Android 一侧。Ubuntu 一侧使用同一个
`/usr/local/sbin/liuqin-switch`，由 `liuqin-device-support` 包提供，并带有
“重启到 Android”桌面入口。

## 切换方式

两个系统都从 A 槽启动。切换会将另一个系统的启动镜像写入 `boot_a`，回读核验后
重启；不会改变活动槽。另一份启动镜像保存在 Ubuntu 根文件系统
`linux_root` 的 `native-root` 中：

```text
/var/lib/liuqin/switch/
  ubuntu/    boot.img  SHA256SUMS  meta.json
  android/   boot.img  SHA256SUMS  meta.json
  state.json switch.log
```

`boot_b` 是 Ubuntu 的 Bootloader 回退副本，切换工具和本模块不写它。
此 Bootloader 的切槽同时涉及 A/B 类型 GUID 与 UFS boot LUN，不能只改 GPT 属性。
Android 的 `super` 只容纳一套动态分区，不能据此假设 B 槽 Android 可启动。

## 模块内容

| 路径 | 用途 |
| --- | --- |
| module.prop | 模块元数据 |
| system/bin/liuqin-switch | 构建时从项目共用脚本打包的切换器 |
| system/bin/boot-ubuntu | 终端确认入口，再调用 to-ubuntu |
| service.sh | 开机停用 Android 系统更新程序 |
| webroot/index.html | KernelSU WebUI 状态和按钮 |
| META-INF/com/google/android/ | 拒绝 Recovery sideload 的占位入口 |

## Android 上的操作

1. 将 linux_root 以 `ro,noload` 挂到 `/mnt/liuqin-switch`，不回放日志，也不改写镜像存储。
2. 检查 Ubuntu 启动镜像的 SHA256、头部及是否能放入 boot_a。
3. 检查当前 boot_a 与存储中的 Android 镜像一致。若 Android 镜像已经重新打补丁或
   手工刷写，先从 Ubuntu 用 `liuqin-switch import-android <image>` 更新存储。
4. 整分区写入镜像及零填充，同步、恢复只读标记并回读。失败则恢复 Android 镜像，停止且不重启。
5. 卸载存储后调用 `svc power reboot`。

Ubuntu 头部是 v2、4096 字节页、1660 字节头并带 DTB；Android 是 v3/v4。
其他格式拒绝。root 终端可使用模块绝对路径，无需依赖 KernelSU 的 system 挂载：

```sh
su -c 'sh /data/adb/modules/liuqin_boot_ubuntu/system/bin/liuqin-switch status'
su -c 'sh /data/adb/modules/liuqin_boot_ubuntu/system/bin/boot-ubuntu'
```

`boot-ubuntu -n` 只显示计划。日志在 `/data/adb/ksu/log/liuqin-switch.log`；
WebUI 同样调用模块中的脚本。

## Android OTA

A/B 更新引擎会写非活动 B 槽并选择它，覆盖 Ubuntu 回退镜像。
因此 service.sh 等待 `sys.boot_completed` 后对主用户执行：

```text
pm disable-user --user 0 com.android.updater
```

决策日志保存在 `/data/adb/ksu/log/liuqin-ota-freeze.log`，目录不存在时回退到
模块目录，且限制日志大小。可用 `LIUQIN_OTA_PACKAGES` 指定包名。
`pm enable com.android.updater` 可恢复程序；卸载模块会移除此开机脚本。
更新 Android 需要明确安排原厂分区表恢复、更新和重新安装。

## 构建与验证

```sh
sh build/scripts/build-ksu-module.sh
```

输出为 `out/ksu-module/liuqin_boot_ubuntu-v0.3.zip`，由 KernelSU 管理器安装。
拒绝 Recovery sideload，以免解压到错误位置。

最新整机结果以[安装步骤](../../../docs/INSTALL-TESTING.md)为准。调试 Android 下的脚本返回验证，
不能推导普通 Android 的自动加载或 WebUI 已完成验收。`tests/switch-selftest.sh` 使用普通文件，
仅验证决策和校验逻辑。下列项目保留为原模块的专项核实清单：

- Android SELinux 与挂载命名空间下的 `ro,noload` 挂载，以及 KernelSU su 域中的块设备访问。
- 写入 boot_a、刷新页缓存后的回读，以及从 A 槽冷启动的实际行为。
- WebUI 使用 `ksu.exec(command, optionsJson, callbackName)`，回调为
  `window[callbackName](errno, stdout, stderr)`；管理器版本的桥接接口和重启前回调时序需核实。
  桥接接口差异应在 `webroot/index.html` 的 `exec()` 中处理，终端入口可用于诊断。
- 原厂 GKI 为 `5.10.209-android12-9`、KMI 为 `android12-5.10`。补丁工具是
  `build/scripts/patch-android-boot-ksu.py`，资源由 `build/data/kernelsu-assets.json` 锁定。
  必须核实相应镜像能够启动，以及管理器是否报告 root。
- `ro.boot.slot_suffix` 应反映实际槽位，切换器据此拒绝从 B 槽执行切换。
- 更新程序包名按 ROM 核实；包不存在时，service.sh 的日志会记录
  `is not installed for user 0`。
