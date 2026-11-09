# Android ROM 安装与预览

[安装助手](INSTALLER-GUI.md) ｜ [备份与恢复](FLASHING.md)

当前使用原生 Rec v2 执行 **A 槽完整重装**，会清空 Android 用户数据。ROM 按机型、包结构、完整性、分区容量及回滚元数据检查，不按 ROM 名称或 ZIP 白名单放行，不要求固件与本机相同。

## 安装流程

1. 读取真实分区，勾选 `userdata`；没有该分区时先创建。仅安装 Android 不需要 Linux 材料或 `linux_boot`。
2. 选择 liuqin 的完整 ROM ZIP。导入器提取镜像、组装 super，生成实际写入清单。
3. 选择固定 Rec 保护策略及默认系统；核对备份、空间、分区操作和数据清除范围后确认。
4. 分块校验并写入目标，完整回读，完成数据初始化与系统登记；最后提交选定的默认引导。完成后停留在 Rec，由用户验证系统启动。

可同时勾选一个或多个 Linux rootfs，形成统一计划；所选材料全部通过检查才执行，中间不重启或切槽。联合流程需要 Rec 声明 `combined_install_version=1`。任一步失败即停止并保留子任务记录，不自动重放整个安装。

要保留 Android，不勾选 `userdata` 即可。重装或删除当前默认系统时，须明确指定有效的新默认系统。

## 写入范围

| 对象 | 处理 |
| --- | --- |
| super、A 槽启动资源与包内固件 | 按确认清单写入、补零并回读；包中没有的固件保持 |
| userdata、metadata | 按 ROM 要求初始化，清除 Android 用户数据及加密状态 |
| recovery_a | 固定 Rec 保护通过时保留/恢复本机镜像，否则按 ROM 处理 |
| boot_a | 按默认系统选择处理；联合安装在全部系统完成后提交 |
| Linux rootfs | 不作为 ROM 目标；用户明确添加的删除/格式化仍按总计划执行 |
| linux_boot | 保留已有系统镜像，更新 Android 引导、快照和登记，不格式化 |
| persist、B 槽物理启动分区及计划外目标 | 不写入 |

设备端再次核验材料、分区身份、备份与作用范围，不能通过修改电脑请求将 rootfs 或其他受保护分区加入 ROM 清单。已有多系统布局中，内容与已核验备份、当前分区和 ROM 完全一致的镜像可跳过重复写入，仍记录实际摘要。

**super、DTBO 和固件是共享资源。** 保留 Linux 文件不保证旧内核兼容新固件；保留 B 槽物理分区也不保证旧 Android 可回退。基础备份、覆盖前镜像及其复用规则见[备份说明](INSTALLER-GUI.md#备份复用)，个人文件需另备。

## 支持的材料

- 完整 A/B payload v2 ZIP，支持 `REPLACE`、`REPLACE_BZ`、`REPLACE_XZ`；当前 super 布局使用单 `qti_dynamic_partitions` 组及 Virtual A/B。
- Android boot/vendor_boot v3/v4；userdata 为 F2FS 或 ext4，metadata 为 ext4。镜像必须适合实际分区容量。
- 保留 ROM 原有 AVB 元数据，接受格式有效的 flags 0–3，不自动关闭或重签 AVB。较低回滚索引会提示；当前镜像索引不等于真实 TEE 防回滚计数。
- 不执行 ZIP 中的 `update-binary`、`updater-script` 或任意脚本。增量 payload、必须执行的未知 postinstall、多动态分区组等未实现格式会拒绝。

LineageOS 24 的完整重装和启动已验证。MikuUI Blooming 26101322 的固定样本信息在 [liuqin-miku-profile.json](../shared/liuqin-miku-profile.json)；该样本不含 ABL 等底层固件，安装后仍取决于实际固件底座。样本识别与内容摘要不代表发行者签名验证，也不授予同名其他包相同资格。

Android 首次启动负责加密初始化；[Rec 不解密原有 data](NATIVE-RECOVERY.md#android-data)。

## ROM 安装预览

进入可核验的原生 Rec 后，在「设置与诊断 → 预览 ROM 刷写计划」选择材料。预览显示系统/固件/Recovery 清单、缺项、清除范围及安装前后的 ABL、DTBO、AVB 条件。

| 输入 | 预览用途 |
| --- | --- |
| 完整 ROM 与本机固件指纹 | 检查材料并推演安装后的启动条件 |
| 部分镜像目录或不完整 TAR | 展示已有内容和缺项，不能代替完整安装材料 |
| 已收录 MIUI、HyperOS、MikuUI 基线 | 参考比较，不授予写入权限 |

每次分析重新采集状态；设备或材料变化使旧结果失效。不在 Rec 时不会自动重启或回退成模拟数据。预览只有独立 `preview_id`，正式安装仍需返回分区页准备并确认完整计划。

## 固定 Rec 保护与失败恢复

默认策略核验设备绑定的固定资格及安装后的 ABL、AVB、内嵌 DTBO 和底层固件。兼容时保留或从已核验备份恢复 `recovery_a`，跳过 ROM 的 Recovery；没有匹配的固定资格时按 ROM 安装。无法证明兼容时显示原因，用户可明确选择按 ROM 原样处理 Recovery。

保护不会自动刷入没有匹配固定资格的镜像，也不修改 AVB。规则见[固定 Rec](RECOVERY-FIRMWARE-PROFILES.md#固定启动条件)，事务和中断恢复见[安装后端](NATIVE-RECOVERY-INSTALLER.md)。系统半写入或已格式化的数据不能靠旧 boot 恢复；固件写入时断电可能导致无法启动，不提供自动完整回滚。

实现入口：`installer/liuqin_gui/native_ota.py`、`rom_preview.py`，以及 `shared/liuqin_ota.py`、`liuqin_rom_analysis.py`、`liuqin_recovery_preserve.py`。
