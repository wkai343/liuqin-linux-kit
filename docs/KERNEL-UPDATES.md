# 内核更新与恢复

[项目首页](../README.md) ｜ [构建指南](BUILD.md) ｜ [方案依据](KERNEL-STRATEGY.md)

项目通过版本化 RPM/DEB 交付配套 boot 与模块，允许保留旧内核。
本机已验证安装软件包、Fastboot 临时启动、固定 A 槽与回读；
**系统内自动写引导尚未真机验收，默认停用，也未提供在线内核仓库。**

## 配套要求

每组 Image、模块和内核元数据必须匹配，同一 kernel release 不能对应不同载荷。
元数据放在 `/usr/share/liuqin/kernels/<release>/`，记录 release、Image 和模块清单的 SHA256。
桌面与普通设备服务可独立更新；安装和升级仍检查根系统身份、运行内核及目标模块。

当前仅支持 userdata 单根 ext4/Btrfs 和 A 槽。新 boot 必须支持当前根文件系统，
更新器不切槽、不重分区、不改 Android 固件；B 槽不是已配置的 Linux 救援系统。

## 构建升级包

按 [构建指南](BUILD.md) 修改并提交内核源码或配置，更新 `build.json` 中的版本：

- 每次发布提高 `package_version`。
- 源码或配置改变时更换 `kernel_localversion`；release 只用小写字母、数字、点、加号和连字符，并包含 `liuqin`。
- 仅改变 `kernel_reclaim_mte` 时可保留 release，但仍提高包版本；范围见 [MTE 说明](BUILD.md#可选回收-mte-预留)。

首次安装执行 `devbox run build`。已有系统仅需生成升级包：

```sh
devbox run kernel-packages
```

该命令自动准备目标内核、模块和 boot，不重建桌面或安装环境，并按配方输出：

| 配方 | 输出目录 |
| --- | --- |
| Fedora | `out/build/from-source/fedora-kernel-rpms-<内容标识>/rpms/` |
| Ubuntu | `out/build/from-source/kernel-debs-<内容标识>/debs/` |

同批软件包一起使用：

| 软件包 | 内容 |
| --- | --- |
| `liuqin-kernel-image-<release>` | 预构建 boot 与版本化内核身份 |
| `liuqin-kernel-modules-<release>` | 同版模块与清单 |
| `liuqin-kernel-updates` | 指向推荐版本的元包 |
| `liuqin-boot-tools` | 计划、升级、确认与恢复工具 |

旧载荷包不会直接替换为空元包，避免删除仍在使用的模块。
内核包附带源码提交、配置和许可证；发布者还需提供对应源码与修改。

## 已安装系统更新

1. 把同批软件包复制到平板，Fedora 用 `sudo dnf install ./*.rpm`，Ubuntu 用 `sudo apt install ./*.deb`。
   默认只安装文件，不写 boot。
2. 经电脑 Fastboot 临时启动新 boot，验证桌面和硬件后，再固定 A 槽并回读。
   保留上一版 boot、模块和恢复材料，且备份到平板之外。
3. 运行配套内核后，可用 `sudo liuqin-boot-update plan <release>` 只读检查后续更新计划。

日常 `dnf upgrade` / `apt upgrade` 主要更新用户态。发行版通用内核不能直接替代设备适配内核，
`update-initramfs` 也不会更新平板的启动分区。

### 系统内自动更新（待真机验收）

`sudo liuqin-boot-update enable` 绑定当前设备和布局，启用后软件包事务钩子才会写 boot。
RPM 与 DEB 使用同一后端；默认构建不启用此入口。

更新器只接受真实 liuqin、内部 Qualcomm UFS、A 槽及对应单根挂载，拒绝在构建目录/chroot 写入。
写入前保存旧 boot 整分区、校验值及事务记录；新镜像补零覆盖整分区，回读后恢复只读保护。

| 命令 | 作用 |
| --- | --- |
| `liuqin-boot-update status` | 查看启用状态和最近事务 |
| `liuqin-boot-update check <release>` | 校验目标镜像与模块 |
| `liuqin-boot-update plan <release>` | 只读检查设备与更新计划 |
| `liuqin-boot-update disable` | 停用自动写入，保留文件和恢复记录 |
| `liuqin-boot-update apply <release>` | 对已启用更新的系统写入新引导 |
| `liuqin-boot-update confirm` | 核对实际启动内核和分区内容 |
| `liuqin-boot-update rollback` | 校验后恢复最近旧引导，随后需正常重启 |
| `liuqin-boot-update recover` | 恢复中断事务，避免将半写入引导作为备份 |

管理命令需要管理员权限。状态在 `/var/lib/liuqin/boot-updates/`，
设备绑定在 `/etc/liuqin/boot-updates.json`，这些私有数据不能进入通用 rootfs。
正在运行、当前目标和最近回退所需的内核/模块受移除保护；APT 不自动删除项目版本化内核包。

## 故障与恢复

写入或回读错误会尝试恢复旧引导并保留记录，但断电、强制杀进程或设备故障无法保证自动恢复。
有未完成事务时先检查 `status` 并执行 `recover`；无法进入系统时使用电脑 Fastboot 与完整备份。
不承诺自动 A/B 回滚。

若从独立 RAM 环境挂载 Fedora 并通过 chroot 更新 RPM，结束后还需按目标策略补齐所有更新文件的
SELinux 标签，包括模块与 `/etc/ld.so.cache`。清单须解析 `/usr/sbin` 等父目录别名，保留文件本身链接。
正常使用优先在已启动的 Fedora 中通过 DNF 更新。

离线回归覆盖编包、版本共存、更新/回读、中断恢复、备份损坏和移除保护，
见 `tests/kernel-updates.py`、`tests/persistent-root.sh`、`tests/install-root-single.py`。
这些测试和 Fastboot 真机记录均不代表系统内自动写 boot 已验收。
