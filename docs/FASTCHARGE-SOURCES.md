# 充电原厂依据

[充电与快充](FASTCHARGE.md) ｜ [准备原厂资料](BUILD.md#准备原厂资料)

本文记录可复核的原厂输入、函数位置和实现边界。二进制结论来自锁定 ROM 的静态分析，
不代表充电功率验收；公开内核与量产 ROM 版本不同，运行时属性编号以量产模块为准。

## 输入与公开源码

ROM 为 `OS2.0.6.0.VMYCNXM`，镜像身份固定在
[liuqin-rom-images.json](../shared/liuqin-rom-images.json)，获取方式见[构建指南](BUILD.md#准备原厂资料)。
认证程序来自 `super.img` 的 `vendor_a`，充电模块来自 `vendor_boot.img`。
程序、模块和反汇编产物留在本地，不随仓库分发。

公开源码使用小米官方 [MiCode / liuqin-t-oss](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/tree/eb49e10028e27fa98856cb5e458c0f0ec5b4554e)，
固定提交 `eb49e10028e27fa98856cb5e458c0f0ec5b4554e`：

| 原厂位置 | 能直接支持的结论 |
| --- | --- |
| [`qti_battery_charger.c`：`usbpd_request_vdm_cmd()`、`request_vdm_cmd_store()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L3490) | UVDM 命令含义、参数字节序及 GLINK 转发；该入口不编排用户态调用顺序 |
| [`verify_digest_store()` / `verify_digest_show()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L2671) | 电芯挑战及摘要读写接口；主机摘要计算在用户态完成 |
| [`pd_verifed_store()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L3683) | 用户态结果写入固件门控；接口本身不执行摘要校验 |
| [`xm_batt_update_work()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L6450) | 周期读取 `XM_THERMAL_TEMP` 的原厂路径 |
| [`pd.h`：PPS RDO 定义](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/include/linux/usb/pd.h#L381) | PPS 请求电压、电流的位段与 20 mV / 50 mA 单位 |
| [`ucsi.c`：`ucsi_get_src_pdos()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi.c#L520) | 从固件读取 source PDO、在协商变化后更新缓存 |

公开 UCSI 未提供当前主线的 PD 能力设备注册路径；能力目录替换仍需引用 Linux 的相应实现。
这些主线 API 依据与 [NOTICE](../NOTICE) 中的代码来源、作者署名不能改称原厂实现。

## 认证程序

`/vendor/etc/init/hw/init.batterysecret.rc` 在开机完成及关机充电模式下启动
`/vendor/bin/batterysecret`，并配置 `request_vdm_cmd`、`pd_verifed` 和电芯认证节点权限。
该 ELF 的 SHA-256 为 `7a450cdd5c1b65f584f470bb31d4c7a3b4c9cfd6b8cab4473dda6a06e084a1e0`；
函数名来自其 `.gnu_debugdata`，下表地址为该文件的 ELF 虚拟地址，不能套用于其他 ROM。

| 原厂函数 / 位置 | 静态分析结论 |
| --- | --- |
| `main()` `0x23b4`、`verify_fg_digest()` `0x36e8` | Liuqin 分支先分别检查、认证主从电芯，再进入适配器处理 |
| `main()` `0x270c`–`0x279c` | 常驻循环使用 `select()` / `recvfrom()` 接收事件，再调用处理函数；不是按事件重新启动的 oneshot |
| `calc_fg_digest()` `0x2ea0` | 使用 32 字节挑战和 HMAC-SHA256；Liuqin 分支选用的电芯密钥与当前 `FG_KEYS[0]` 一致 |
| `verify_pd_digest()` `0x4fc0` | PD/PPS 路径中先调用 `usbpd_dr_swap()`，再调用 `usbpd_conmunication()`；处理结束时写 `pd_verifed` |
| `check_pd_auth()` `0x4cd8` | 读取处理标志与认证门控以判断完成状态；读取失败返回错误，不在该检查路径写零撤销门控 |
| `usbpd_dr_swap()` `0x459c` | 检查 `SNK_Ready`，向 Type-C `data_role` 写 `host`，再读取角色确认结果 |
| `usbpd_conmunication()` `0x4a1c`、数据表 `0x7710` | 有效命令顺序为 `1 → 2 → 3 → 4 → 5 → 7 → 6`；表尾另有零项，发送函数不把它作为有效 UVDM 命令写出 |
| `usbpd_send_vdm_cmd()` `0x3d44` | 命令 8 在入口直接返回；发送其他有效命令前检查 `adapter_svid=0x2717`，7/6 携带摘要校验结果 |
| `calc_usbpd_digest()` `0x38d0` | 对 16 字节挑战与 4 字节适配器 ID 拼接的 20 字节消息计算 HMAC-SHA256，比对应答与摘要的前 16 字节 |

现有 10 组适配器种子及密钥按相同索引与原厂表逐字节一致；文件偏移分别为
`0x6530`（10 × 16 字节）和 `0x65d0`（10 × 32 字节）。Liuqin 电芯密钥位于 `0x6510`。
当前 `FG_KEYS[1]` 是既有实现额外接受的候选，本次在该原厂 ELF 中未找到，不能将整个候选集称为原厂策略。

### 等待与失败处理的边界

原厂命令表每项包含命令号和应答轮询次数：

| 命令 | 次数 | 状态未匹配时的轮询间隔 |
| --- | --- | --- |
| 1、2、3、7、6 | 5 | 10 ms |
| 4、5 | 500 | 10 ms |

发送函数先读应答；未匹配才调用 `usleep(10000)`。因此 `5/500` 不是固定的 5/500 ms 延时，
轮询等待上限分别约为 50 ms / 5 s，另有 sysfs/GLINK 调用耗时及发送前的连接状态等待。
轮询耗尽返回 `-EAGAIN`，`usbpd_conmunication()` 对该返回值继续后续项；其他负错误会提前退出。
发送前等待非零 UVDM 状态：命令 1 最多读取 20 次，其余命令 4 次，间隔 20 ms。
应答阶段读到状态 0 则终止，不能当作普通应答超时继续。

`verify_pd_digest()` 调用角色切换前写 `verify_process=1`，结束时先清零该标志再写 `pd_verifed`。
其角色切换可执行四轮，遇到就绪等待的 `-EAGAIN` 会继续等待；认证处理后记住已尝试状态，
在非 PD/PPS 类型或 UVDM 断开且 USB offline 时清除。未发现跨连接的固定 120 秒冷却。

当前实现采用上述轮询次数、间隔、命令顺序和处理标志，并保留以下 Linux 适配边界：

| 项目 | 当前处理与原厂的差异 |
| --- | --- |
| 轮询上限 | 同时限制次数和约 50 ms / 5 s 的总耗时，避免慢 GLINK 读取将 500 次轮询放大成数分钟；单次阻塞仍由内核超时约束 |
| 错误分类 | 仅本地应答状态未匹配超时可继续下一命令；读取 `EAGAIN` / `EBUSY` 在当前预算内重读，GLINK 写错误或读取超时直接终止，不重发 |
| 认证缺失 | 命令 5 无有效摘要时，按失败值发送 7 → 6；不设置认证门控，也不走诊断回退 |
| 角色重试 | 仅暂时错误或角色尚未就绪时重试，最多 4 次、总计 12 秒；原厂就绪等待没有同样的总时限。每次先读角色，已完成切换便不再写入 |
| 连接保护 | 保留 PDO USB 数据能力检查及绑定 partner 的成功凭据；同一 partner 不重放整轮，新 partner 可重新尝试，不使用跨连接冷却 |
| 就绪恢复与事件调度 | 原厂常驻处理事件；本项目用 systemd path 保存待处理标记，工作进程退出后补处理。被动就绪等待对同一失败观察值最多三轮，间隔 2 秒；读取恢复或能力变化可以继续。标记、预算和失败观察缓存是 Linux 适配策略，不是原厂时序常量 |
| 成功复用 | 原厂检查 `verify_process=0` 与 `pd_verifed=1`；本项目还要求本次连接的成功凭据及非断开的 UVDM 状态，不强求最后状态必须是 6/7 |
| 完成后读取失败 | 原厂检查路径不因读取失败写零门控；本项目对绑定当前 partner 的成功凭据延后复核，明确断线或门控复位仍清除。诊断和通知放在事务完成之后，不能撤销成功结果；连接凭据及三态读取是本项目实现 |
| 电芯重试 | 保留原厂每个电芯最多 10 次、挑战后 1.4 秒和两电芯之间 1 秒等待；暂时传输失败也消耗一次尝试，重试间隔 0.2 秒为本项目策略 |

命令 7/6 的短轮询超时不会抹去已校验的本轮摘要，但写入失败仍必须终止。
权限错误、身份变化、断线和收到无效数据不能被当作成功；异常路径负责清除处理标志与未完成认证的门控。

## 固件服务恢复

公开原厂 [`battery_chg_state_cb()` / `battery_chg_subsys_up_work()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/power/supply/qti_battery_charger.c#L1934)
在 GLINK 上线后重新订阅电源通知；`battery_chg_notify_enable()` 失败时没有定时重试。
公开原厂 [`ucsi_qti_state_cb()` / `ucsi_setup()`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi_glink.c#L536)
在下线时注销并销毁 UCSI 对象，上线时重新创建；设置工作同样没有自动重试。
这与主线 PMIC GLINK 保留并复用 UCSI 对象的生命周期不同，不能直接复制状态回调中的同步清理。

主线 GLINK 在客户端自旋锁内调用服务状态回调，不能在该回调里等待请求结束或同步取消工作。
项目的 `0043` 以延迟工作处理有限重试，用服务代次隔离旧结果；UCSI 复用前重置初始化预算、
事件标志和通知状态。10 次尝试、1 秒间隔、代次检查以及工作停用顺序是本项目策略，
不是原厂二进制或公开源码中的常量。`0042` 所引用的上游修复只纠正注册所有权，并未自带定时重试。

## UCSI 读写应答

原厂公开驱动在解析返回码前检查整包长度，读应答为 64 字节，写应答为 16 字节。
[原厂 UCSI 读写应答处理](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/eb49e10028e27fa98856cb5e458c0f0ec5b4554e/drivers/usb/typec/ucsi/ucsi_glink.c#L154-L195)

上游 UCSI 2.x 的 GLINK 数据区为 528 字节，加上报文头和返回码后，完整读应答为 544 字节。
`0044` 在版本确定前接受 64 或 544 字节的完整读应答，版本确定后按对应格式校验。写应答固定为
16 字节。长度不符的应答会被丢弃，请求继续等待有效应答直至原有超时。
[上游 UCSI 2.x 缓冲区定义](https://lkml.iu.edu/hypermail/linux/kernel/2510.2/09182.html)

## 离线复核

按构建指南准备锁定 ROM，在仓库根目录进入 `devbox shell`。下面只读取镜像并向新的
`out/stock-charger-reference` 目录提取文件，使用现有 sparse/super 解析器，无需挂载或连接平板。
输出目录已存在时先改用新的目录，避免覆盖先前材料。

```sh
python3 - <<'PY'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, 'build/lib')
from android_images import vendor_partition

rom = Path('inputs/roms/liuqin/OS2.0.6.0.VMYCNXM/images/super.img')
lock = json.loads(Path('shared/liuqin-rom-images.json').read_text())
with rom.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == lock['images']['super.img']['sha256']
out = Path('out/stock-charger-reference')
out.mkdir(parents=True, exist_ok=False)
vendor_partition(rom, out / 'vendor.img')
PY
fsck.erofs --extract=out/stock-charger-reference/vendor --no-preserve out/stock-charger-reference/vendor.img
```

校验认证程序、读取命令表及比较现有常量，不输出密钥内容：

```sh
python3 - <<'PY'
import ast, hashlib, struct
from pathlib import Path

blob = Path('out/stock-charger-reference/vendor/bin/batterysecret').read_bytes()
assert hashlib.sha256(blob).hexdigest() == '7a450cdd5c1b65f584f470bb31d4c7a3b4c9cfd6b8cab4473dda6a06e084a1e0'
commands = [struct.unpack_from('<II', blob, 0x6710 + 8 * i) for i in range(8)]
assert commands == [(1, 5), (2, 5), (3, 5), (4, 500), (5, 500), (7, 5), (6, 5), (0, 0)]
print('command, poll_count:', commands)
tree = ast.parse(Path('device/board-overlay/usr/local/libexec/liuqin-mipps-auth').read_text())
constants = {}
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('SEEDS', 'KEYS', 'FG_KEYS'):
                constants[target.id] = ast.literal_eval(node.value)
for name, offset, size in (('SEEDS', 0x6530, 16), ('KEYS', 0x65d0, 32)):
    assert len(constants[name]) == 10
    assert all(bytes.fromhex(value) == blob[offset+i*size:offset+(i+1)*size]
               for i, value in enumerate(constants[name]))
    print(name, 'matches stock')
assert bytes.fromhex(constants['FG_KEYS'][0]) == blob[0x6510:0x6530]
print('Liuqin fuel-gauge key matches FG_KEYS[0]')
PY
```

用 ARM64 `objdump` 复核表的调用方式、命令 8 的跳过分支、应答轮询与角色切换。
例如下列范围覆盖 `usbpd_send_vdm_cmd()`、`usbpd_dr_swap()` 和 `usbpd_conmunication()`：

```sh
aarch64-unknown-linux-musl-objdump -d --start-address=0x3d44 --stop-address=0x4bd0 \
  out/stock-charger-reference/vendor/bin/batterysecret
```

命令表的第二列需结合 `usbpd_send_vdm_cmd()` 中的循环及 `usleep()` 参数解释；
仅凭字符串、枚举顺序或表中数字不能得出完整时序结论。
