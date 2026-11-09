# 小米平板 6 Pro 指纹使用

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [组件源码与协议](../device/fingerprint/oem/README.md)

## 当前支持

Fedora / Plasma 配方已集成 FPC1264 专用识别栈，可通过 KDE 设置录入指纹并用于锁屏验证。

新构建的发行包按 fprintd 已授权的目标账号录入。不同账号使用独立的 Linux 凭据、Gatekeeper handle 和模板目录。升级指纹组件后生效。

录入和删除仍由 fprintd/Polkit 授权，软件拒绝 root、重复 UID 的歧义账号和已有凭据绑定冲突。

当前源码构建的多指组件支持每账号录入多枚指纹，并在锁屏时识别任意已录手指。按已核对的厂商枚举接口，支持最多五枚，可选择左右手任意手指。需要整套匹配的驱动、客户端和助手，不能只改配置开放多指。

Fedora 45 / Linux 7.3.3 在 SELinux Enforcing 下已验证两枚录入、未录手指拒绝、分别解锁 Plasma，以及 fprintd 服务重启后的解锁。五枚录满、多账号切换及系统重启后的多指专项仍需补齐。

开机的 Plasma Login 仍使用密码；锁屏指纹验证不等于开机指纹登录已经支持。
多账号隔离、多指文件事务、凭据复用、删除失效、异常拒绝和真实 libfprint 接口已有离线回归；休眠恢复、长期可靠性及 Android 往返共存仍待验收。偶发识别失败的原因仍需定位。

## 使用

1. 打开“系统设置 → 用户”，选择当前账号，进入“指纹”。
2. 选择要录入的手指，使用系统授权对话框验证密码。
3. 轻触侧边电源键的指纹感应面；进度增加后完全抬起，调整接触位置再轻触，直到完成。
4. 按相同步骤添加其他手指，再锁屏验证每枚已录手指均能解锁，未录手指不能解锁；正常重启后重复检查。

尽量不要按下电源键。删除或更换手指使用设置页，不直接删除私有状态文件。删除后该引用立即不能用于认证；下次录入时清除数据库中的失效条目并回收槽位。
密码登录继续保留，传感器暂时不可用时可使用密码。

Plasma 默认配置启用锁屏的原生指纹入口：点亮认证界面后选择指纹图标，可看到采集提示，也可切回密码图标。指纹入口、采集提示、解锁及切回密码均已验证。Plasma 6.8 的密码模式可能不显示旧的“也可扫描指纹”提示，这是 [KDE 已确认的问题](https://bugs.kde.org/show_bug.cgi?id=527171)；没有该提示不代表传感器失效。配置位于 `/etc/xdg/kscreenlockerrc`，作为系统默认值，不覆盖用户的显式选择。

## 指纹数据

当前组件仅接受版本 2 的多指引用，不兼容或迁移旧单指模板；追加手指复用本账号的凭据和已有有效引用。

fprintd 按账号保存每枚手指的独立引用；厂商不透明数据库保存在该账号私有的 `/var/lib/liuqin-fingerprint/native-enrollment/<原生 UID>/gallery.json`，与随机凭据和 Gatekeeper handle 一起由 root 管理。匹配得到的厂商 ID 必须对应本次获准验证的已发布引用，才会报告成功。自适应数据库更新须先持久保存；取消、缺失清理标记或提交失败均不报告认证成功。

## 构建要求

仅安装通用 `fprintd` 不够，还需要 `liuqin-fingerprint`、匹配的模块、板级接线及原厂固件。
默认目标内核加入 FPC IRQ 实际电平修正，用户态要求 `irq_interface=level-v1`。
RPM 装配及内核配套要求见 [构建指南](BUILD.md)。

随机机器凭据、Gatekeeper handle、认证令牌、模板及校准留在平板本机，不能公开分发。
原生流程不保存 Linux 密码；修改登录密码不要求另行同步“指纹密码”。
已有旧身份和模板不会被自动删除或迁移，不通过删除标记或创建随机用户 ID 排障。

## 固件来源

默认 Fedora 构建从 `rom_images/NON-HLOS.bin` 提取十个指纹 trusted application 文件，
先核对完整原厂镜像及每个文件的固定 SHA256，再写入最终 rootfs 的
`/usr/local/lib/liuqin-fpc-oem/firmware/`，文件为 root 所有、0600。
首次启动直接使用这份已验证缓存，不依赖平板 `modem_a` 中固件的签名版本。
固件缺失或校验失败会停止构建；原厂程序不改写，也不包含个人指纹、凭据或校准。

独立 `liuqin-fingerprint` RPM 仍只携带程序、固定哈希与来源说明；固件由最终镜像装配阶段加入。
缓存缺失的旧镜像仍会尝试从本机 `modem_a` 只读提取。需要手动补齐时，在 Devbox 环境运行：

```sh
python3 build/scripts/extract-liuqin-fingerprint-firmware.py \
  --image /path/to/OS2.0.6.0.VMYCNXM/images/NON-HLOS.bin --out inputs/fingerprint-firmware
```

把输出的十个文件复制到平板本地目录，再在平板执行：

```sh
sudo python3 /usr/local/lib/liuqin-fpc-oem/provision_firmware.py --from-directory /path/to/fingerprint-firmware
sudo systemctl restart liuqin-fpc-stable-module.service fprintd.service
```

此操作只写 Linux 固件缓存，不刷 modem 分区，也不录入指纹。

## 排障与停用

先查看 `fprintd.service` 和 `liuqin-fpc-stable-module.service` 的状态及阶段日志，
确认当前内核与模块配套。日志只共享错误和阶段，避免包含指纹私态。

管理员如需停用验证、保留私有状态及密码登录：

```sh
sudo systemctl mask --now fprintd.service
```

恢复前先解决原有错误，再使用 `sudo systemctl unmask fprintd.service`。
日常录入通过系统设置或标准 fprintd 工具进行；新版 RPM 不安装旧终端助手和密码派生身份工具。

## 原厂时序依据与 IRQ 接口

这里的设备协议依据是原厂 ROM 中的 `lib64/hw/fingerprint.fpc.so` 实现，
不是为 FPC1264 SPI 传感器公开发布的完整时序手册。对应 HAL 样本 SHA256 为
`ce2ffe2b1af8587a49a57e52038d330d68d7d5fc564d7f276757cc13e9c9c455`。
公共源码包只保留协议实现和此摘要，不分发原厂 HAL 二进制。

| 原厂函数 | 样本地址 | 观察到的流程 |
| --- | --- | --- |
| `fpc_irq_wait` | `0x38230` | 先读 IRQ GPIO 当前电平；不满足条件时等待事件，再读取电平 |
| `fpc_tee_wait_finger_down_wo_fngr_int_qual` | `0x39980` | `10/3` 配置检测，等待真实 IRQ；采集命令完成资格判断 |
| `fpc_tee_wait_finger_lost` | `0x3918c` | `10/2` → IRQ → `10/11` → `10/1`，未完成则继续等待 |
| `fpc_tee_capture_image` | `0x39aa8` | 区分带等待和不带等待的采集；返回 WAIT_TIME 时继续采集流程 |

旧驱动的 `poll()` 返回并清除“曾发生过中断”的软件标记。这个标记与当前 GPIO
电平不同：脉冲已结束时仍可能被当成就绪，消费标记又可能漏掉仍然有效的电平。
正式适配改为读取 TLMM GPIO 的实际电平，并提供只读 `irq_level` 和
`irq_interface=level-v1`。测试中反复查询接触状态及额外 200 ms 延时已移除。

该修复已纳入正式内核及默认构建补丁，录入与重启后锁屏验证通过。
Fedora 组件要求运行内核提供 `level-v1`，同时核验模块清单与供电接线。

[libfprint 支持列表](https://fprint.freedesktop.org/supported-devices.html)和
[Linux GPIO 使用文档](https://docs.kernel.org/driver-api/gpio/consumer.html)可作框架参考；
这块平板的原厂 SPI/TEE 指令仍需以对应 ROM 的 HAL 与固件为准。
