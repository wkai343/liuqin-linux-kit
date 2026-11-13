# 内核更新与恢复

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [硬件支持](HARDWARE.md)

项目通过版本化 RPM 交付配套 boot 与模块，允许保留旧内核。
安装软件包、Fastboot 临时启动、A 槽固定与回读已验证。
系统内手动 `apply`、普通重启和 `confirm` 已在单台设备的多系统 ext4 默认根上验证；
RPM 事务钩子的自动写入、故障恢复及其他布局仍需分别验收。
**自动写引导默认停用，尚未提供在线内核仓库。**

## 配套要求

每组 Image、模块和内核元数据必须匹配，同一 kernel release 不能对应不同载荷。
目标 Image 由构建流程封装为 2 MiB 对齐入口，以兼容 liuqin 的 ABL。
元数据放在 `/usr/share/liuqin/kernels/<release>/`，记录 release、Image 和模块清单的 SHA256。
桌面与普通设备服务可独立更新；安装和升级仍检查根系统身份、运行内核及目标模块。

引导工具从 `0.1.7` 起支持 userdata 单根布局，以及多系统布局中**当前正在运行、已登记为默认项的 Linux**；根系统须为 ext4 或单设备 Btrfs，启动槽须为 A。
更新器核对系统 ID、根分区 PARTUUID、文件系统 UUID、挂载路径和 `linux_boot` 身份，
拒绝从临时启动的其他系统覆盖默认项。新 boot 必须支持当前根文件系统，
更新器不切槽、不重分区、不改 Android 固件；B 槽不是已配置的 Linux 救援系统。

## 构建升级包

按 [构建指南](BUILD.md) 修改并提交内核源码或配置，更新 `build.json` 中的版本：

- 每次发布提高 `package_version`。
- 源码或配置改变时更换 `kernel_localversion`；release 只用小写字母、数字、点、加号和连字符，并包含 `liuqin`。
- 仅改变引导打包或压缩时可保留 release，但仍提高包版本。

首次安装执行 `devbox run build`。已有系统仅需生成升级包：

```sh
devbox run build -- --target packages
```

该命令自动准备目标内核、模块和 boot，不重建桌面或安装环境，输出到
`out/build/from-source/fedora-kernel-rpms-<内容标识>/rpms/`。

同批软件包一起使用：

| 软件包 | 内容 |
| --- | --- |
| `liuqin-kernel-image-<release>` | 预构建 boot 与版本化内核身份 |
| `liuqin-kernel-modules-<release>` | 同版模块与清单 |
| `liuqin-kernel-updates` | 指向推荐版本的元包 |
| `liuqin-boot-tools` | 计划、升级、确认与恢复工具 |

设备支持包与启动镜像须一起升级。UFS 保护要求 `liuqin-storage-policy-ufs >= 1`，由 `liuqin-device-support` 提供；安装器和引导更新器会检查配套文件。`liuqin-boot-tools` 依赖 `python3-zstandard` 以读取 Zstd 镜像。

迁移脚本会停用已移除的 USB 服务，只清理与已发布载荷一致的文件；管理员修改过的文件和符号链接保留并提示检查。旧 boot 若依赖这些文件，回退时须恢复对应设备包。回退时使用与目标内核同批的模块和设备包。

完整音频模块、桌面 DRM 和 OTG 配置随目标系统交付，不能套用 Rec 的固定 USB 设备模式。内核包附来源、配置和许可证；发布者须提供对应源码与修改。

## 已安装系统更新

1. 把同批软件包复制到平板，执行 `sudo dnf install ./*.rpm`。
   默认只安装文件，不写 boot。
2. 按[已有系统临时启动要求](BUILD.md#当前构建产物)绑定独立测试镜像，经电脑 Fastboot
   验证桌面和硬件后，正常重启回已安装的默认内核。更新器要求运行内核与 `boot_a` 匹配。
3. 执行 `sudo liuqin-boot-update check <release>` 与
   `sudo liuqin-boot-update plan <release>`，核对目标版本及写入范围。
   保留上一版 boot、模块和恢复材料；将完整旧 boot、多系统 `linux_boot` 内容、
   当前根绑定及需更换的设备配置备份到平板之外，并校验备份。
4. 需要写入默认内核时，执行 `sudo liuqin-boot-update enable`，再执行
   `sudo liuqin-boot-update apply <release>`。成功后普通重启，核对实际版本、配套硬件，
   并执行 `sudo liuqin-boot-update confirm`。若此前停用自动写入，再执行
   `sudo liuqin-boot-update disable` 恢复设置。

多系统更新使用软件包中**尚未绑定系统**的发布镜像。更新器为当前默认项生成新的绑定版本，
保留旧镜像，并同步 `linux_boot` 中的系统清单、根分区槽位、默认版本和内核更新记录。
已经绑定的临时启动镜像不能直接作为升级包输入。
`plan` 中的版本 ID 和镜像校验值用于预览；`apply` 会重新核对现场并生成本次事务的版本 ID。
重复安装后若绑定镜像与当前 `boot_a` 完全相同，会跳过刷写并保留原有回退版本。

日常 `dnf upgrade` 主要更新用户态。发行版通用内核不能直接替代设备适配内核，
`update-initramfs` 也不会更新平板的启动分区。

### 普通重启后的核验

确认实际运行的 `uname -r` 与目标 release 相同，模块检查通过，`status` 的事务对应本次更新，
再检查桌面和本次改动涉及的设备，最后执行 `confirm`。`confirm` 会核对运行内核和
`boot_a`；临时启动通过、RPM 安装成功或 `apply` 完成回读都不能替代普通重启后的确认。

包含 DP 音频和四路麦克风的内核还需要配套的版本化拓扑与 UCM，见
[音频配套要求](AUDIO.md#拓扑与桌面)。重启后核对 Speaker、Mic、DisplayPort PCM
及实际采集数据；未接外屏时的设备识别不能替代外屏实际出声测试。
键盘改动检查状态中断、输入及睡眠恢复，方法见
[键盘状态中断与睡眠](HARDWARE.md#键盘状态中断与睡眠)。

多系统更新会生成新的 `boot_generation`。更新后再次临时启动时，须重新读取当前
`/etc/liuqin-boot-target` 并生成测试副本；更新前保存的绑定镜像已经过期。
精确 release、启动目标、文件摘要和实测日志保存在忽略的 `out/` 目录，
文档只记录更新方法、验证范围和限制。

### 系统内更新与自动写入

`sudo liuqin-boot-update enable` 绑定当前设备和布局，启用后软件包事务钩子才会写 boot。
默认构建不启用此入口。

更新器只接受真实 liuqin、内部 Qualcomm UFS、A 槽及对应根挂载，拒绝在构建目录/chroot 写入。
写入前保存旧 boot 整分区、校验值及事务记录；新镜像补零覆盖整分区，回读后恢复只读保护。
多系统更新先保存新镜像和回退材料，验证 `boot_a` 完整回读后再提交根绑定与默认登记；
任一步失败时同时恢复旧 boot、根绑定和登记。恢复前若发现其他操作已修改登记，停止并保留记录，
不会覆盖后续系统切换。更新与 Recovery 共用操作锁；`linux_boot` 仅在写入期间解除只读，
只读检查采用不回放日志的挂载方式。卸载失败时保留挂载点和内容，不递归清理。

| 命令 | 作用 |
| --- | --- |
| `liuqin-boot-update status` | 查看启用状态和最近事务 |
| `liuqin-boot-update check <release>` | 校验目标镜像与模块 |
| `liuqin-boot-update plan <release>` | 只读检查设备与更新计划 |
| `liuqin-boot-update disable` | 停用自动写入，保留文件和恢复记录 |
| `liuqin-boot-update apply <release>` | 对已启用更新的系统写入新引导 |
| `liuqin-boot-update confirm` | 核对实际启动内核和分区内容 |
| `liuqin-boot-update rollback` | 校验后恢复最近旧引导及对应多系统登记，随后需正常重启 |
| `liuqin-boot-update recover` | 恢复中断事务，避免将半写入引导作为备份 |

管理命令需要管理员权限。状态在 `/var/lib/liuqin/boot-updates/`，
设备绑定在 `/etc/liuqin/boot-updates.json`，这些私有数据不能进入通用 rootfs。
正在运行、当前目标和最近回退所需的内核/模块受移除保护。
多系统布局还会读取引导存储中的当前与上一版内核记录，避免本地更新记录缺失后误删回退模块。

## 故障与恢复

写入或回读错误会尝试恢复旧引导并保留记录，但断电、强制杀进程或设备故障无法保证自动恢复。
有未完成事务时先检查 `status` 并执行 `recover`；无法进入系统时使用电脑 Fastboot 与完整备份。
不承诺自动 A/B 回滚。

在独立 RAM 环境中维护已安装的 Fedora 时，可在目标 chroot 内用
`LIUQIN_OFFLINE_SELINUX_RELABEL=1 rpm -Uvh ...`。内核 RPM 会用目标系统的 `setfiles` 递归补齐
版本化目录的 SELinux 标签，并跳过引导更新钩子。事务结束后仍需校验共用工具、RPM 数据库及
`/etc/ld.so.cache` 等更新文件的标签。解析 `/usr/sbin` 等父目录别名时保留文件本身的链接。
正常使用优先在已启动的 Fedora 中通过 DNF 更新。
