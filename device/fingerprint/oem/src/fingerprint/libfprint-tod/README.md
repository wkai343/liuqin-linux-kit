# FPC1264 OEM TOD 与最终验收

[用户指南](../../../../../../docs/FINGERPRINT.md)记录支持与验收范围。
已安装候选由 `CANDIDATE.json` 和 `SHA256.json` 标识，私有程序目录为
`/usr/local/lib/liuqin-fpc-oem`；主机开发路径、`/run` 暂存目录和历史里程碑不能标识新候选。

适配器使用平板现有的 libfprint 1.95.1+tod1 ABI 和显式 TOD 发现。
在对应 ABI 上用组件根目录的 `tools/build-tod.sh` 构建，不替换系统 libfprint。
版本 1 RAW `(uay)` FpPrint 只保存一个不透明 OEM 数据库。正常匹配每次会话初始化硬件认证，
加载数据库、要求一个模板，并识别和核对返回的 match-HAT 封装；没有主机软件生物识别匹配器。

## 录入与自适应更新

既有 fprintd enrol API 不提供派生输入 FD，因此该路径的 enrol 回调返回 `NOT_SUPPORTED`。
授权录入通过提供的 PAM/原生凭据提供者，在一个 FPC 会话中进行两轮新 challenge/HAT、
接受 END、核对单模板库存并导出不透明数据库。`fpc-oem-print` 序列化调用方元数据和
已接受数据库，并在进程内检查反序列化、兼容性和相等性。
`enrol_publish.py` 只向普通私有 fprintd 文件存储发布。发布失败时保留已接受导出，
不确定导出也保留且不自动重试录入。当前范围为每个用户一枚手指。

真实匹配后请求 `UPDATED_DATABASE`。清理完成的成功结果可以替换内存中的录入指纹，
并以 qdata 附加原始克隆。`src/fingerprint/fprintd-oem` 中的私有补丁在报告 VerifyStatus
成功前提交自适应数据：持共用锁核对原始磁盘版本、fsync 新文件、保留旧文件链接并原子提交。
存储错误拒绝认证成功；未匹配、取消和不确定结果均不保存。使用 `tools/build-fprintd-oem.sh`
构建该私有守护进程。

## 历史开发验收记录

以下是原 desktop-r3 开发流程的历史记录；当前原生设置流程以组件主说明为准。
已通过目标 ARM64 构建、真实 PAM 账号检查、首次原生凭据创建、同输入的既有凭据更新及内部 SID 比较、
一个 FPC 会话内接受两次签名，以及跨进程 root 内核 keyring 输入消费和新的签名授权。
受限制的私有守护进程在系统 D-Bus 上通过 GetDevices/Polkit Claim/Release。
该启动记录完成真实 OEM 指纹的同指匹配、守护进程重载匹配和异指拒绝，用户报告 GNOME 解锁。
接触位置敏感与卡顿仍存在；编译成功不证明可靠性或延迟。

`fprintd-fpc1264-oem.conf` 设置私有守护进程 ExecStart、TOD 发现、
`RuntimeDirectoryPreserve=yes`、固件路径例外和实测 FPC/TEE/UFS 节点，保留厂商隔离范围。
历史开发模式在桌面/SSH/旋转启动后延迟加载稳定模块；完整同指/重载/异指验收前保持系统守护进程屏蔽。
密码 PAM 不变，沿用 GDM 指纹集成。

历史“指纹录入”应用调用限定范围的特权 `acceptance.py`：复用已接受指纹或导出，否则进行一次
物理录入、同指验证、守护进程重载后的同指验证和异指拒绝，再激活桌面 fprintd。
每次 Verify 前由调用者抬指并确认阶段。仅在真实 PAM 和签名预检查后准备 18 小时有效、
当前启动内一次性的 root 内核 keyring 输入；使用后撤销且不写磁盘，无需后台收集进程。
过期或重启后需重新进行普通 PAM 输入。`--recover` 屏蔽指纹认证并保留数据。

旧密码派生流程在 Linux 密码变化后，通过显式 `user_credentials.py --sync-password`
认证当前密码，并以隐藏 tty 提示读取旧指纹密码。独立封闭 FD 更新既有 Gatekeeper handle；
失败时保留旧文件及不确定输出，不创建回退身份或重置模板。此流程不适用于新原生随机凭据方案。

该历史记录的支持内核来自临时 `fastboot boot`，普通重启返回已安装内核。
应使用已接受的支持镜像和完全匹配模块；候选记录内核身份与公开组件哈希。
电压保持 2960000–3008000 微伏。特定重试、后续冷启动、挂起恢复及长期可靠性需要各自证据，
不能据此认定历史 GENI 内存损坏已解决。当前持久安装方式见组件主说明。
