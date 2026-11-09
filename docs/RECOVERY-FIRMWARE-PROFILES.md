# Recovery 固件匹配与固定启动

[原生 Rec](NATIVE-RECOVERY.md) ｜ [ROM 安装](FULL-ROM-FLASH.md) ｜ [安装后端](NATIVE-RECOVERY-INSTALLER.md)

ROM 名称或 Android 版本不能单独决定 Rec 能否启动。匹配使用实际 ABL、DTBO、AVB、底层固件和分区布局；识别只读进行，不重启、切槽或刷写。

## 固件采集

每个 A/B 槽采集 ABL 整分区摘要、DTBO 有效区域及容量、根与系统 vbmeta 的摘要和 AVB 元数据，以及 boot/vendor_boot/recovery 容量。

读取前核验 Rec 与本地镜像的构建 ID、内核、接口和载荷；只读打开经设备号核对的块节点，读取前后复核设备、分区及启动会话。记录绑定序列号、boot ID、构建和活动槽，保存在安装器状态目录的 `firmware-fingerprints/`。

稳定指纹只覆盖固件内容及所需布局。换设备、重启或存储保护变化后重新检查，不复用旧会话结果。

| 资料 | 用途 |
| --- | --- |
| [原生镜像匹配表](../shared/liuqin-native-recovery-profiles.json) | 精确固件与临时镜像的匹配规则 |
| [固定候选匹配表](../shared/liuqin-universal-recovery-profiles.json) | ABL、DTBO、AVB 的离线条件 |
| [固定封装策略](../build/recovery/universal/liuqin-v2-policy.json) | boot header、布局、内嵌 DTBO 和允许的固件摘要 |
| 镜像内嵌能力及运行哈希 | 判断已运行环境是否具有安装、切换或恢复能力 |
| `hardware-qualification.json`、`restore-compatibility.json` | 分发包中与设备、镜像及固件绑定的固定启动与恢复资格；校验逻辑见 [liuqin_fixed_recovery.py](../shared/liuqin_fixed_recovery.py) |

文件名、离线匹配和 GUI 更新均不授予固定刷入资格。分发包的资格文件必须与实际镜像匹配，不能将其他设备或其他构建的结果套用过来。

## 固定启动条件

固定封装使用 Android boot header v2，内嵌内核、DTB、DTBO、RAM 环境及 Recovery AVB 元数据。A 槽固定启动、按键冷启动及原 Recovery 回刷已验证；适用范围仍由设备和固件绑定检查限定。

| 策略中的固件 | 固定候选检查 |
| --- | --- |
| MIUI 14.0.3 的 ABL 样本 | 需已解锁，且根 vbmeta 的验证禁用位为 2；仅禁用 hashtree 的位 1 不满足条件 |
| HyperOS 2.0.6 的 ABL 样本 | 需已解锁；保留镜像验证时还需匹配 vbmeta_system |
| MikuUI Blooming 26101322 | 包不含 ABL，需读取设备保留的固件；不能只按 ROM 名称选择 |
| 其他 ABL / 固件组合 | 需新增可核对的策略和启动测试，未知组合不授予自动固定资格 |

以上对应策略中的精确摘要，不代表同名 ROM 的全部版本。安装器不自动关闭 AVB。内嵌 DTBO 减少对机内覆盖层的依赖，但不能据此删除 Android 仍需使用的 DTBO、vbmeta 或固件分区。

已运行的 Rec 按构建 ID 和载荷连接；推荐镜像变化时需另行临时启动。通用候选与可启动镜像分目录，临时启动成功也不能替代固定封装检查。

## ROM 安装时保护固定 Rec

安装前根据待写与保留分区推演新的启动链。只有绑定的固定镜像及安装后的 ABL、AVB、DTBO 和底层固件都满足条件，才保留/恢复 `recovery_a` 并跳过 ROM 内 Recovery。

无法证明兼容时显示原因；用户可选择按 ROM 原样处理 Recovery。没有任意替换内核、任意提取 ROM DTBO 的通用适配功能，也不修改 ROM 的 AVB flags。事务与失败恢复见[安装后端](NATIVE-RECOVERY-INSTALLER.md#rom-安装时保护固定-rec)。

## 构建与检查

固定构建入口是 [build-universal.py](../build/recovery/build-universal.py)，安装载荷使用 `--installer`；输入以该命令的 `--help` 为准。

```sh
python3 build/recovery/build-universal.py \
  --base-image /path/to/native-recovery.img \
  --dtbo /path/to/matching-dtbo.img \
  --key /path/to/private-rsa2048.pem \
  --out out/build/new-universal-candidate
```

构建只操作主机文件，私钥不随包发布。检查镜像布局、载荷、签名和完整性后，分别测试临时启动、固定启动、冷启动及回刷。自签名不代表 OEM 信任。

[verify-universal-offline.py](../build/recovery/verify-universal-offline.py) 检查 ABL 指令、AVB、DTBO 选择/合并与内核解压；模型不能代替真实 TEE、UEFI 退出及内核交接。回归入口：`tests/universal-recovery.py`、`tests/fixed-recovery.py`、`tests/fixed-recovery-preserve.py`。
