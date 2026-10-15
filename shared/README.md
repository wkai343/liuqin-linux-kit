# 公共模块

构建端和安装端共用的纯 Python 接口：

- `liuqin_artifacts.py`：启动镜像和根归档的结构、角色与配套关系检查。
- `liuqin_filesystems.py`：ext4/Btrfs 能力标记与格式选择。
- `liuqin_layout.py`：容量、分区计划和 GPT 参数计算，不执行设备操作。
- `liuqin_identity.py`：项目显示名、包名和上游地址，保持设备标识独立。
- `liuqin-rom-images.json`：原厂镜像大小与版本数据。

此目录不依赖 Qt、Devbox 或电脑端安装会话。修改公共接口时同时验证构建端与安装端。
