# 小米平板 6 Pro 指纹使用

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [组件源码与协议](../device/fingerprint/oem/README.md)

## 当前支持

Fedora 44 / Plasma 已集成 FPC1264 专用识别栈，支持首个本地 UID 1000 账号的一枚指纹。
在 256GB 开发设备上，KDE 设置录入、已录手指通过、未录手指拒绝、锁屏解锁及正常重启后验证已通过。
默认 Linux 7.3 已复测录入、两种手指验证、睡眠唤醒及重启后的锁屏解锁，SELinux 保持 Enforcing；
范围与限制见 [7.3 验证记录](KERNEL-7.3.md)。

开机的 Plasma Login 仍使用密码；锁屏指纹验证不等于开机指纹登录已经支持。
休眠恢复、长期可靠性、多用户、多指及 Android 往返共存未完成验收。
上游 Ubuntu/GNOME 的 GDM 指纹结果见原项目记录，不作为 Fedora 登录功能声明。

## 使用

1. 打开“系统设置 → 用户”，选择当前账号，进入“指纹”。
2. 选择一枚手指，使用系统授权对话框验证密码。
3. 轻触侧边电源键的指纹感应面；进度增加后完全抬起，调整接触位置再轻触，直到完成。
4. 锁屏后验证已录手指能解锁，未录手指不能解锁；再正常重启并重复锁屏检查。

尽量不要按下电源键。更换手指时使用设置页的删除功能，不直接删除私有状态文件。
密码登录继续保留，传感器暂时不可用时可使用密码。

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
旧终端助手和密码派生身份工具只保留给旧接口兼容与开发排障，不是当前录入入口。

## 原厂时序依据与 IRQ 接口

这里的设备协议依据是原厂 ROM 中的 `lib64/hw/fingerprint.fpc.so` 实现，
不是为 FPC1264 SPI 传感器公开发布的完整时序手册。本机分析样本 SHA256 为
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
