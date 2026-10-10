# FPC1264 OEM 指纹适配

[English](README.en.md) ｜ [中文使用指南](../../../docs/FINGERPRINT.md)

本组件通过 Ubuntu 的“设置 → 系统 → 用户 → 指纹登录”与正常 Polkit 密码对话框
录入指纹。libfprint TOD 上报采集进度和取消，fprintd 保存模板并向 GDM 登录及
锁屏提供标准验证接口，不新增日常解锁应用，也不修改 GNOME 设置。

当前支持首个本地账号（UID 1000）和一枚指纹。此可选组件不自行改变默认内核锁定。

## 已知验证范围

上游记录截至 2026-10-07：在 b87 支持内核完成原生设置录入，模板在安装用户态
`4d6b22f` 与 `6.17.0-rc1-gfe81794b5e1b` 组合后保留；同指通过、异指拒绝、原生
锁屏和普通重启后的指纹登录通过，同一持久启动上随后验证 DP 输出。这是一台已知
256GB 开发机、一名账号和一枚指纹的结果，不代表其他设备、转接器或所有掉电情形。

fprintd 使用 `UMask=0077`，模板必须为 root 所有、0600。早期 0644 模板仅修正
权限，保留内容和凭据；不能放宽自适应写入器的所有者及权限检查。存储失败只记录
固定阶段和错误域/代码，避免把存储错误静默变成认证失败。

## 使用与授权

安装匹配的内核和组件后，在原生设置中选择账号和手指，经 Polkit 授权并按采集
提示完成，再用普通登录或锁屏验证。密码登录继续可用，指纹 PAM 专用入口不插入
普通密码登录栈。

每次录入和删除使用 `auth_self`，不保留授权。fprintd 核验真实 D-Bus 发送者，
另行授权其他用户名；原生提供者还绑定账号配置。公开包在首次设置后解析 UID 1000
账号，开发包保留显式用户名/UID 绑定。改变配置不应重建、改名或迁移已有身份。
多账号管理不在当前范围内。

原生录入使用 `/var/lib/liuqin-fingerprint/native-enrollment/` 下的 root 私有
随机凭据和 Gatekeeper handle，原生 UID 为 `0x60000000 | linux_uid`。
这不是 Linux 密码副本，密码变化不需要另行同步。录入会进行真实 Gatekeeper/RPMB
写入；临时 RAM 内核不意味着录入没有写入。

旧 `0x40000000` / `0x50000000` 密码派生身份和 fprintd 模板不会自动迁移或覆盖。
已有手指时拒绝再次录入。不要删除凭据或不确定状态标记来强行重试。取消初始凭据
创建会等小事务安全结束；取消采集会等待已有 TEE 操作及监听清理。

## 匹配内核与运行时

必须使用候选材料中指定的确切内核、FPC 设备树、模块和 OEM 固件。
仅有 `/dev/fpc1020` 不够。延迟加载器核验内核、模块 SHA256、TEE 节点、FPC 线路
及 2960000–3008000 微伏供电范围，不重新绑定 GENI。

原生部署使用 `deployment_mode: "installed"`，模块服务等待 GDM 和旋转服务，
不依赖 SSH、网络或临时诊断通道。GDM 就绪不要求用户已经登录。
开发模式保留诊断/网络检查。两种模式都不刷 boot 或修改分区表。

`fastboot boot` 仅提供当前内核。普通重启后可用需要持久安装匹配的内核和模块，
不能改版本字符串或跳过模块哈希。原生验证直接读取 fprintd 持久数据库，使用
Keymaster `0x205`，不调用旧 PAM 凭据助手、旧 18 小时预备输入密钥或录入协商序列。
完整协议边界见[英文详版](README.en.md#matching-kernel-and-runtime)。

## 公开构建与打包

发行镜像使用 `liuqin-fingerprint` Debian 包。从同一 v0.6.0 发行版取得
`fingerprint-build-inputs.tar.gz`。该公共归档包含核验程序、对应源码/依赖和固定
TA 哈希，不含 OEM TA 本体或用户状态，构建器锁定其 SHA256。

```sh
python3 tools/build-liuqin-fingerprint-deb.py --bundle /path/to/fingerprint-build-inputs.tar.gz --out out/fingerprint-debs --version 0.6.0
```

`BUILD_INPUTS.json` 记录用户态 `4d6b22f` 与原始输入身份。维护者可以从审计过的
内部 r4 归档生成公开材料；普通用户只需要公共归档：

```sh
python3 tools/make-fingerprint-build-inputs.py --bundle /path/to/audited-r4/fingerprint.tar.gz --output out/fingerprint-build-inputs.tar.gz
```

转换保留核验过的二进制与 FE 模块，叠加当前 Python 集成、标准 fprintd 服务、
Polkit 规则与 `UMask=0077`。构建时不需要现成用户，不创建凭据/模板或独立录入
应用，并锁定 libfprint/TOD ABI 和匹配内核包。首用户配置及本机提取仍需最终镜像验收。

公开 Debian 包不分发 OEM trusted application 本体。包中只有
[固定哈希](firmware/SHA256.json)和[原厂来源声明](firmware/NOTICE)。正常启动时
复用本机已验证缓存，或将本机 modem_a FAT 只读挂载并只提取锁定的十个文件。
缺失或不匹配会停止指纹启动，不重试修复、裸写分区或阻塞密码登录。
Debian/rootfs 安装时不提取固件，也不创建凭据。

## 内部源码构建与开发候选

[SOURCE_LOCK.json](SOURCE_LOCK.json) 锁定源码及 Ubuntu 输入，
[FPRINTD_SOURCE_HASHES.json](FPRINTD_SOURCE_HASHES.json) 校验外部 fprintd 源码。
在主项目根目录构建静态 ARM64 客户端及原生组件：

```sh
sh tools/build-liuqin-fingerprint-oem.sh clients
FPRINTD_SOURCE=/path/to/fprintd-v1.94.5 sh tools/build-liuqin-fingerprint-oem.sh native
```

原生组件需目标 libfprint/TOD 1.95.1+tod1、Gio、PAM/OpenSSL、Polkit/systemd、
Meson/Ninja 开发输入。OUT_DIR 默认 `out/fingerprint-oem`。交叉编译可设置 CC、
目标 pkg-config 路径和 MESON_CROSS_FILE。构建不连接设备，不创建凭据，不能代替真机验收。

复制示例配置到源码树外，使用实际内核/boot 标识、用户名和 UID。
原生设置入口必须显式配置：

```json
{"enrollment_interface": "fprintd", "deployment_mode": "installed"}
```

历史配置本身选择开发助手接口。使用匹配的原生 TOD、native_enrol.py、Polkit 规则、
FPC 模块和外部 fpcliu.mdt/分段。凭据、随机秘密、模板和校准不是通用构建输入。
完整打包命令见[英文详版](README.en.md#internal-source-builds-and-bound-development-bundles)。

安装器核验运行内核，安装标准 fprintd/Polkit 集成，移除未修改的旧应用启动器和
旧免密规则，保留密码登录及已有凭据/模板。`--replace-owned-candidate` 仅核验并
备份已有程序，不迁移生物识别状态。更换支持内核时用 `--defer-start` 保持 fprintd
屏蔽；启动匹配内核后再次运行安装器完成检查和激活。暴露接口不等于录入/解锁验收。

## 排查与限制

acceptance.py、user_credentials.py 和旧终端/PAM 入口只保留给历史开发流程，
不是原生设置入口。旧 `--sync-password` 只适用于旧密码派生方案，不能把旧创建、
录入或随机 UID 试探操作当作原生预检查。

检查 `journalctl -u fprintd.service` 的有界阶段/状态及模块服务状态。
不要收集或发布凭据输入、handle、认证 token、随机秘密、模板、原始采集或私有校准。
若要暂时停用验证，管理员可以停止并 mask fprintd，保留状态和密码登录，不必重置凭据。

位置敏感性、延迟、重试频率、挂起恢复、长期可靠性及双系统安全存储共存尚未确定。
已知设备窗口内未观察到 GENI 故障，不证明历史问题全部解决或反复冷启动稳定。
离线 API 和打包测试不能证明真实匹配、硬件授权、用户可见解锁或完整镜像。

## 许可证

项目原有脚本与集成遵循 MIT；文件级许可证优先：OEM C/PAM 为 BSD-2-Clause，
TOD/FpPrint 为 LGPL-2.1-or-later，fprintd 适配为 GPL-2.0-or-later，AOSP 定义为
Apache-2.0，并保留厂商 BSD 声明。OEM 固件和任何私有凭据、HAT、模板、原始图像及
校准都不属于公共源码输入。
