# liuqin 板级通用固件

[English](README.en.md) ｜ [项目首页](../../README.md)

`usr/lib/firmware/` 下的 197 个二进制文件构成锁定的通用固件组合，包括
SM8475 DSP（adsp/cdsp/slpi）、QCA6490 WLAN（ath11k WCN6855，含项目生成的
`board-2.bin`）、蓝牙、两种面板的 Novatek NT36532 触控、Adreno 730 GPU、
CS35L41/CS35L63 音频 DSP、音频拓扑、VPU 和无线监管数据库。

## 来源

- 使用 `tools/build-liuqin-firmware-prep.sh` 从原厂 Fastboot ROM
  `OS2.0.6.0.VMYCNXM`（liuqin_images_15.0_cn）离线提取。
  每个文件的 SHA256 锁定于 `firmware.manifest`，该清单 SHA256 为
  `012c413e0a5d5c3c14631fbdfe50da5c51e95ffc1ae431b8d93f776538b65ba2`。
- `ath11k/WCN6855/*/board-2.bin*` 由项目通过
  `tools/build-liuqin-wifi5g-hsp2-tuple.sh` 从厂商组合生成。
- 这些文件适用于同型号设备，不包含每台平板独有的扬声器保护校准、传感器
  注册表、WLAN MAC 或蓝牙地址。这些私有信息在安装时由
  `tools/provision-liuqin-from-persist.sh` 从本机 persist 读取。

## 许可证与声明

这些固件属于 Qualcomm、Xiaomi、Cirrus Logic 及其授权方。保留此组合的目的是
让小米平板 6 Pro 用户构建并运行其设备系统，不授予超出该用途的权利。
见仓库根目录的 [NOTICE](../../NOTICE)。权利人如有异议，请提交 Issue。

## 版本策略

锁定组合基于已验证的 OS2.0.6.0。采用更新原厂 ROM 的固件前必须做真机验证；
设备原厂 ROM 较新不意味着本组合必须随之替换，具体版本敏感性见安装文档。
本目录是唯一权威副本，发布材料由本目录生成，而不是反向覆盖此目录。
