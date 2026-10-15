# FPC1264 OEM 指纹组件

此目录是可选 [OEM 组件](../../README.md) 的内部源码布局。除主仓库包装脚本外，
以下构建和打包命令均在组件根目录 `device/fingerprint/oem` 执行。
设备状态、用户操作和验收范围见[使用指南](../../../../../docs/FINGERPRINT.md)。
新候选的 `actual_biometric_acceptance: pending` 不继承早期 desktop-r3 的真机结论。

## 组件分工

| 路径 | 职责 |
| --- | --- |
| 外部内核补丁系列 | 匹配的 SPI 模块、IRQ 和传感器供电 |
| `src/fingerprint/oem` | OEM FPC/Keymaster/Gatekeeper 客户端、Linux PAM 凭据输入、授权录入和私有运行时 |
| `src/third_party/qsee-supplicant` | 监听传输与经过认证的 UFS RPMB 提供者 |
| `src/third_party/QCBOR` | Gatekeeper CBOR 构建依赖 |
| `src/fingerprint/libfprint-tod` | 单指纹 OEM TOD 适配器与 FpPrint 序列化 |
| `src/fingerprint/fprintd-oem` | 私有 fprintd 源码准备与持久化自适应更新 |
| `src/fingerprint/deployment` | 安装、延迟模块加载、系统 D-Bus 验证、验收和恢复 |
| `tools` | 组件构建与公开程序打包 |

`/dev/fpc1020` 和设备树 compatible `fpc,fpc1020` 是既有设备 ABI，并不选择历史软件匹配器。
守护进程显式设置 `FP_DRIVERS_ALLOWLIST=fpc1264_oem`；此组件不包含历史主机软件匹配原型。

## 数据库边界

TA 导入和导出不透明的原始 OEM 数据库。位于
`/var/lib/fprint/<user>/fpc1264_oem/liuqin-fpc1264-oem/<finger-hex>` 的文件是序列化 FpPrint，
其私有 `fpi-data` 为版本 1 的 `(uay)`，其中保存原始数据库。TOD 经 libfprint 反序列化，
提取字节数组，再通过私有暂存文件调用 `oem_runtime.py --match-single`。
不要把 fprintd 存储文件直接传给原始数据库客户端；录入获准后，`fpc-oem-print --serialize` 执行反向转换。

匹配要求既有单个模板、通过 FPC 3/1 设置的新操作 ID、真实采集和识别结果、返回的认证结果封装，
以及运行时清理完成。原始命令成功或状态为零不代表生物识别匹配。未匹配仍视为未匹配；
清理后的明确状态 4/12 请求框架重试，不认证用户。成功的自适应更新必须先由私有 fprintd
存储补丁提交，才报告成功。取消、未匹配或不确定采集不会发布新模板。

## 构建和打包

完整组件只需各构建一次。客户端可在交叉编译环境中构建：

```sh
OUT_DIR="$PWD/build/review/clients" bash tools/build-userspace.sh --with-rpmb-provider
```

以下命令使用目标现有的 libfprint/TOD/PAM ABI，或相同开发环境，只编译程序：

```sh
OUT_DIR="$PWD/build/review" bash tools/build-tod.sh
OUT_DIR="$PWD/build/review" bash tools/build-pam-input.sh
FPRINTD_SOURCE=/path/to/fprintd-v1.94.5 OUT_DIR="$PWD/build/review" bash tools/build-fprintd-oem.sh
```

打包时使用公开固件/程序暂存目录的副本，把 `clients/`、两个 TOD 产物、`pam-input`、
`fprintd` 和原始厂商源码归档放到同一个 `--build` 目录。
打包器核验输入暂存目录的公开文件哈希，更新程序及源码副本，生成新清单和本地归档；
不会安装候选或自动切换当前部署指针。

```sh
python3 tools/package_fingerprint.py --stage work/review-stage \
  --build build/review --output work/review-candidate.tar.gz \
  --kernel-module /path/to/matching/fpc1264_spi_diag.ko \
  --candidate-config /path/to/public-config.json \
  --firmware /path/to/matching-firmware --revision review-20261002
```

`prepare.py` 将生成的 fprintd 补丁放在隔离构建树旁，或 `--patch-output` 指定的位置，
不重写规范源码补丁。`tools/build-userspace.sh` 包含 `acceptance_input.py`；打包时从构建
结果更新 `pam-input` 和 `fpc-oem-print`，不复用旧助手。两个 QSEE 源码副本均从编译输入更新，
避免新二进制附带旧传输源码。固件、模板状态和设备秘密不属于 PR 输入。

## 真机验收与恢复

复用已接受的导出和已发布的指纹。完整验收依次验证同指、重启守护进程后重新加载并验证同指、
拒绝异指；只有整个过程通过才写入 `acceptance.json` 并启用正常 D-Bus 守护进程。
单次 `--verify` 只返回一个结果，不写完整验收记录，排查时应保存其输出。
正常 D-Bus 空闲退出不是守护进程失败。密码登录始终保留，`--recover` 停用指纹认证并保留数据。

原 desktop-r3 记录通过了同指、重新加载、异指测试，用户报告 GNOME 解锁成功。
接触位置敏感和解锁卡顿仍存在，没有量化成功率或延迟承诺。特定 4/12 重试、挂起恢复、
冷启动和长期运行仍需真机证据，编译或打包检查不能证明这些行为。
该历史记录使用临时 `fastboot boot` 内核，传感器电压为 2960000–3008000 微伏，
不执行 GENI unbind/bind 清理。当前部署范围以组件主说明和用户指南为准。
