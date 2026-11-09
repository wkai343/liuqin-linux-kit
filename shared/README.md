# 构建与安装的共用模块

本目录维护不依赖 Qt 的格式、规划、校验及 Rec 会话规则，由构建端和安装端共同使用。

| 范围 | 主要模块 |
| --- | --- |
| 启动与 rootfs 契约 | liuqin_artifacts.py、liuqin_kernel.py、liuqin_boot_target.py、liuqin_filesystems.py |
| 分区、身份与系统登记 | liuqin_layout.py、liuqin_partitions.py、liuqin_partition_editor.py、liuqin_partition_names.py、liuqin_block_identity.py、liuqin_managed.py、liuqin_multiboot.py |
| Android ROM 导入与执行描述 | liuqin_android.py、liuqin_android_images.py、liuqin_payload.py、liuqin_super.py、liuqin_ota.py、liuqin_rom_admission.py |
| 固件、启动链和镜像角色 | liuqin_firmware.py、liuqin_board_identity.py、liuqin_native_recovery.py、liuqin_fixed_recovery.py、liuqin_universal_recovery.py |
| Rec 写入范围与固定保护 | liuqin_native_install.py、liuqin_recovery_preserve.py |
| ROM 分析与预览 | liuqin_rom_analysis.py、liuqin_rom_preview.py |
| 项目标识及来源锁 | liuqin_identity.py 和对应 JSON 清单 |

规划/解析模块不负责擅自扫描或刷写设备；设备侧会话与保护助手在 Rec 的明确调用和确认范围内工作。不能把本目录所有函数都当成纯静态数据检查，导入模块也不代表获得设备写入权限。

旧固定 ROM、OTA v1 和布局协议仍供兼容执行器及回归使用，当前普通安装使用完整 ROM v2。修改共用接口需同时验证构建、主机和设备侧使用者。镜像能力以其清单为准；布局和事务见[多系统设计](../docs/MULTIBOOT-DESIGN.md)。
