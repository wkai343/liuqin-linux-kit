# 构建与持续集成

[English](CI.en.md) ｜ [项目首页](../README.md)

## 内核变更

内核仓库的 `Liuqin kernel` 工作流构建推送到 `liuqin-6.17` 的提交和 PR。
它检出集成仓库的 `main`，复用 `.github/actions/kernel`，不需要跨仓库写入令牌。
启用前需确保两个仓库位于同一个 GitHub 所有者名下。

集成仓库的 `Kernel build` 使用 `kernel/source.json`。更新其中的确切提交来
接受内核变更，配置与打包仍由集成仓库管理。开发构建的测试 SHA 单独记录，
不能直接作为正式镜像输入。唯一例外是开发构建恰好使用当前锁定的提交、配置哈希
和逐字节相同的配置片段；装配器会核验并记录这种情况。

内核任务使用 GitHub 托管的 Ubuntu runner 和 ccache，上传 Image、设备树、配置、
符号、匹配的模块输入与校验值作为 Actions 产物。编译不等于真机验收或自动发布。

## 系统镜像

`tools/build-liuqin-image.py` 是本地与 CI 的装配入口。它消费已经完成的正式内核
构建和准备好的输入，不重复构建全部第三方依赖。`--stage` 可从中间阶段继续；
装配时不要改动源码或输入。

`System image` 在 `main` 上手动触发，上传匹配的 boot、RAM 安装器、rootfs、
安装工具和校验值。它需要标记为 `liuqin-images` 的专用 Linux x86-64 runner，
足够容纳内核输出与两份桌面树的空间，以及[构建指南](BUILD.md)中的主机依赖。
Python 另需 pyelftools；镜像装配需 root、GNU tar、ACL/xattr、squashfs-tools、
cpio、ARM64 binfmt 和交叉工具链。

runner 必须允许用 sudo 非交互运行受信任的装配命令。不要在此特权 runner 上
构建外部 PR。配置完成后，可设置仓库变量 `LIUQIN_IMAGE_CI=enabled`，
使相关修改进入 `main` 时自动装配。

设置 `LIUQIN_IMAGE_INPUTS` 指向 runner 本地的 JSON，默认
`/opt/liuqin/inputs.json`。其内容为装配器列出的输入变量：Ubuntu 根树和清单、
固件池与准备目录、音频拓扑、WLAN 组合、原厂 DTB/DTBO、传感器归档、设置程序及
构建清单、BusyBox、mkbootimg。哈希保持锁定；本地路径与原始输入 JSON 不上传。
上游下载及预编译材料可以缓存在检出目录之外。

准备好主机后：

```sh
python3 tools/build-liuqin-kernel.py
sudo python3 tools/build-liuqin-image.py \
  --inputs /opt/liuqin/inputs.json --kernel-out "$PWD/out/kernel"
python3 tools/install-liuqin.py --bundle out/image/bundle --check
```

工作流不会自动创建公开 Release，也不会把安装包标成已经真机验收。
候选包必须经过实际安装后才能使用 `--device-tested`。Android 恢复需单独验收，
Ubuntu 启动成功不能证明恢复 Android 成功。

## 安装器检查

集成检查包含命令行安装器、实际容量布局、设备端安装脚本和存储保护策略，
并运行 `tests/gui-engine.py` 与 `tests/gui-worker.py`。GUI 渲染需要 PySide6，
本地运行 `python3 tests/gui-qml.py` 使用 offscreen/software 模式走完整个演示向导。
上述测试均不应连接或刷写真实平板。

## 发布与材料

安装版本放在集成仓库的带标签 Release 中，不放入源码树或 Git LFS。
Actions 产物是临时构建结果，不是正式支持的安装发行版。

GitHub 的单个 Release 资源大小限制见其
[官方说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。
导出阶段会把桌面归档拆成小于 2GiB 的分卷，不重建镜像。上传
`out/image/release-assets/` 中的全部文件。用户按附带说明合并分卷，安装器随后
核验完整归档。导出命令见[构建指南](BUILD.md#release-文件)。

板级通用固件包含在匹配的系统与启动镜像中。小型、稳定的通用文件可在
`device/firmware/` 中保留唯一来源及许可证信息，避免重复提交大体积材料。
上游 Ubuntu 镜像、未改动的上游组件和完整原厂 ROM 不在本仓库镜像分发。
每台设备的校准与地址来自用户自己的平板，不属于公开发行材料。
