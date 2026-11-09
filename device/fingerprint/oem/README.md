# FPC1264 OEM 指纹组件

[使用指南](../../../docs/FINGERPRINT.md) ｜ [Fedora 构建](../../../build/lib/fedora_fingerprint.py)

组件通过 libfprint TOD 和标准 fprintd 提供录入、Verify/Identify 及锁屏认证。Fedora 默认从源码构建独立 RPM，使用 SELinux 策略和 `level-v1` IRQ 接口；当前多指格式不兼容旧单指模板。

## 源码布局

| 位置 | 职责 |
| --- | --- |
| [oem/](src/fingerprint/oem/) | FPC、Keymaster/Gatekeeper 客户端、授权录入、账号凭据和多指数据库 |
| [libfprint-tod/](src/fingerprint/libfprint-tod/) | TOD 驱动、版本 2 FpPrint 引用及解码器 |
| [fprintd-oem/](src/fingerprint/fprintd-oem/) | fprintd 补丁准备、账号范围和存储集成 |
| [protocol/](src/fingerprint/protocol/) | 多指协议和认证令牌结构 |
| [deployment/](src/fingerprint/deployment/) | 模块/固件加载及服务集成 |
| [qsee-supplicant/](src/third_party/qsee-supplicant/) | QSEE 传输与 UFS RPMB 提供者 |
| [QCBOR/](src/third_party/QCBOR/) | CBOR 构建依赖 |

每账号使用独立随机凭据、Gatekeeper handle 和厂商数据库；fprintd 保存各手指的不可变引用。只有实际硬件识别 ID 对应本次获准引用，且更新持久化和运行时清理完成，才报告匹配。账号绑定、容量和事务细节见[多指实现](src/fingerprint/libfprint-tod/README.md)。

`/dev/fpc1020` 和 `fpc,fpc1020` 是设备 ABI；守护进程仅启用 `fpc1264_oem`，不加载另一个 SPI 驱动竞争传感器。内核版本、模块摘要、设备接线和供电由加载器核对。

## 构建

Fedora 默认流程从 [SOURCE_LOCK.json](SOURCE_LOCK.json) 获取源归档，由 `build/lib/fedora_fingerprint.py` 编译、装配和打包。完整镜像构建见[构建指南](../../../docs/BUILD.md)。

只编译组件时，在项目根目录运行：

```sh
sh build/scripts/build-liuqin-fingerprint-oem.sh clients
FPRINTD_SOURCE=/path/to/fprintd-v1.94.5 sh build/scripts/build-liuqin-fingerprint-oem.sh native
```

目标环境需锁定的 libfprint/TOD、Gio、OpenSSL、Polkit/systemd 和 Meson/Ninja 开发输入；交叉编译使用目标 CC、pkg-config 和 `MESON_CROSS_FILE`。构建不连接设备或创建凭据。

## 固件、数据与检查

固件来自原厂 ROM，摘要和来源声明在 [firmware/](firmware/)。默认 rootfs 装配时加入经过摘要校验的十个 TA 文件；独立 RPM 不携带固件本体。补齐缓存的方法见[使用指南](../../../docs/FINGERPRINT.md#固件来源)。

录入会写入设备私有状态和 Gatekeeper/RPMB；随机秘密、handle、模板、令牌及校准不能进入源码或通用镜像。修改 Linux 密码不要求同步指纹密码；保留密码登录，故障时不要删除身份文件来强行重试。

离线检查包括 `tests/fingerprint-gallery.py`、`fingerprint-gallery-client.py` 和 `fingerprint-abi.py`，分别覆盖文件事务、C 协议及真实 libfprint API。实际录入、异指拒绝、重启/休眠恢复和锁屏需通过设备操作检查。

## 许可证

项目原有脚本与集成遵循 MIT；文件级许可证优先：OEM C/PAM 为 BSD-2-Clause，
TOD/FpPrint 为 LGPL-2.1-or-later，fprintd 适配为 GPL-2.0-or-later，AOSP 定义为
Apache-2.0，并保留厂商 BSD 声明。OEM 固件和任何私有凭据、HAT、模板、原始图像及
校准都不属于公共源码输入。

原始英文许可说明保留在 [NOTICE](NOTICE)，不作为使用文档维护。
